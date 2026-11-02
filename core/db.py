"""SQLite storage (WAL). Schema follows spec section 5."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from core.models import Item

SCHEMA = """
create table if not exists items (
  source text not null, stable_id text not null, course_code text, kind text not null,
  title text not null, payload_json text not null, content_hash text not null,
  first_seen text not null, last_seen text not null,
  primary key (source, stable_id));
create table if not exists due_dates (
  item_key text primary key, due_at text, submitted integer not null default 0,
  reminders_sent_json text not null default '[]');
create table if not exists group_messages (
  chat_id integer not null, msg_id integer not null, sender text, text text, ts text not null,
  primary key (chat_id, msg_id));
create virtual table if not exists group_fts using fts5(text, content='group_messages');
create trigger if not exists group_messages_ai after insert on group_messages begin
  insert into group_fts(rowid, text) values (new.rowid, new.text);
end;
create table if not exists pending_actions (
  id integer primary key autoincrement, kind text not null, params_json text not null,
  summary text not null, status text not null, created_at text not null, expires_at text not null,
  decided_at text, result_json text);
create table if not exists settings (key text primary key, value text);
create table if not exists materials (
  key text primary key, course_code text, kind text not null, title text not null, module text,
  source_ref text, text text not null, version text, updated_at text not null);
create virtual table if not exists materials_fts using fts5(key unindexed, title, text);
create table if not exists deadlines (
  key text primary key, title text not null, course_code text, kind text not null,
  start_utc text not null, end_utc text, all_day integer not null default 0, source text not null,
  source_ref text, confidence text not null, quote text, status text not null default 'active',
  gcal_event_id text, gcal_hash text, updated_at text not null, as_event integer not null default 0);
create table if not exists claude_usage (
  id integer primary key autoincrement, ts text not null, job text not null, kind text not null, model text,
  ok integer not null, seconds real not null, in_tokens integer, out_tokens integer, cache_tokens integer,
  estimated integer not null default 0, turns integer, cost_usd real);
create index if not exists claude_usage_ts on claude_usage(ts);
create table if not exists emails (
  id text primary key, received text not null, sender_name text, sender_addr text, subject text, preview text,
  body text, is_read integer not null default 0, has_attachments integer not null default 0, importance text,
  web_link text, conv_id text, category text not null default 'normal', first_seen text not null);
create index if not exists emails_received on emails(received);
create virtual table if not exists emails_fts using fts5(id unindexed, subject, sender, body);
create table if not exists group_replies (
  id integer primary key autoincrement, ts text not null, chat_id integer not null, in_msg_id integer, text text not null);
create index if not exists group_replies_chat_ts on group_replies(chat_id, ts);
create table if not exists deadline_reminders (
  key text primary key, moment text not null, sent_json text not null default '[]');
create table if not exists group_tasks (
  id integer primary key autoincrement, chat_id integer not null, task text not null, owner_name text,
  owner_is_me integer not null default 0, volunteered integer not null default 0, kind text not null default 'task',
  due_utc text, all_day integer not null default 0, source_msg_id integer, source_quote text,
  status text not null default 'open', created_at text not null, updated_at text not null, done_at text);
create index if not exists group_tasks_chat on group_tasks(chat_id, status);
create table if not exists group_task_seen (
  chat_id integer not null, msg_id integer not null, primary key (chat_id, msg_id));
create table if not exists group_task_suggestions (
  id integer primary key autoincrement, task_id integer not null, chat_id integer not null, new_status text not null,
  claimed_by text, msg_id integer, quote text, state text not null default 'pending', created_at text not null,
  kind text not null default 'close', new_due text, new_all_day integer not null default 0);
create index if not exists group_task_suggestions_task on group_task_suggestions(task_id, state);
create table if not exists group_decisions (
  id integer primary key autoincrement, chat_id integer not null, text text not null, source_msg_id integer,
  source_quote text, created_at text not null);
create index if not exists group_decisions_chat on group_decisions(chat_id, created_at);
create table if not exists source_health (
  source text primary key, last_ok text, consecutive_failures integer not null default 0);
"""


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row

    def init(self) -> None:
        self.conn.execute("pragma journal_mode=wal")
        self.conn.executescript(SCHEMA)
        for migration in ("alter table materials add column extracted_version text",
                          "alter table due_dates add column user_confirmed integer not null default 0",
                          "alter table due_dates add column snooze_until text",
                          "alter table due_dates add column confirm_note_sent integer not null default 0",
                          "alter table due_dates add column missed_sent integer not null default 0",
                          "alter table group_messages add column sender_id integer",
                          "alter table group_tasks add column assigned_by text",
                          "alter table group_tasks add column confirmed integer not null default 0",
                          "alter table group_tasks add column date_note text",
                          "alter table group_task_suggestions add column kind text not null default 'close'",
                          "alter table group_task_suggestions add column new_due text",
                          "alter table group_task_suggestions add column new_all_day integer not null default 0",
                          "alter table deadlines add column as_event integer not null default 0"):  # old rows stay due moments
            try:
                self.conn.execute(migration)
            except sqlite3.OperationalError:
                continue  # already applied
            if "confirm_note_sent" in migration:  # old submissions must not get a "Submission confirmed" note now
                self.conn.execute("update due_dates set confirm_note_sent=1 where submitted=1")
        self.conn.execute("delete from settings where key like 'project_tasks_wm:%'")  # retired watermark

    def get_item(self, source: str, stable_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "select * from items where source=? and stable_id=?", (source, stable_id)
        ).fetchone()

    def has_source(self, source: str) -> bool:
        return self.conn.execute("select 1 from items where source=? limit 1", (source,)).fetchone() is not None

    def upsert_item(self, item: Item, content_hash: str, now: datetime) -> None:
        self.conn.execute(
            """insert into items values (?,?,?,?,?,?,?,?,?)
               on conflict(source, stable_id) do update set course_code=excluded.course_code,
               kind=excluded.kind, title=excluded.title, payload_json=excluded.payload_json,
               content_hash=excluded.content_hash, last_seen=excluded.last_seen""",
            (item.source, item.stable_id, item.course_code, item.kind, item.title,
             json.dumps(item.payload, sort_keys=True, default=str), content_hash,
             now.isoformat(), now.isoformat()),
        )

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("select value from settings where key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "insert into settings values (?,?) on conflict(key) do update set value=excluded.value", (key, value)
        )

    def get_json_setting(self, key: str, default):
        raw = self.get_setting(key)
        return json.loads(raw) if raw else default

    def set_json_setting(self, key: str, value) -> None:
        self.set_setting(key, json.dumps(value, default=str))

    def record_usage(self, now: datetime, job: str, kind: str, model: str | None, ok: bool, seconds: float,
                     in_tokens: int | None = None, out_tokens: int | None = None, cache_tokens: int | None = None,
                     estimated: bool = False, turns: int | None = None, cost_usd: float | None = None) -> None:
        self.conn.execute(
            "insert into claude_usage(ts, job, kind, model, ok, seconds, in_tokens, out_tokens, cache_tokens, "
            "estimated, turns, cost_usd) values (?,?,?,?,?,?,?,?,?,?,?,?)",
            (now.isoformat(), job, kind, model, int(ok), round(seconds, 2), in_tokens, out_tokens, cache_tokens,
             int(estimated), turns, cost_usd))

    def prune_usage(self, before: datetime) -> None:
        self.conn.execute("delete from claude_usage where ts < ?", (before.isoformat(),))

    def record_health(self, source: str, ok: bool, now: datetime) -> int:
        if ok:
            self.conn.execute(
                """insert into source_health values (?,?,0) on conflict(source)
                   do update set last_ok=excluded.last_ok, consecutive_failures=0""",
                (source, now.isoformat()),
            )
            return 0
        self.conn.execute(
            """insert into source_health(source, consecutive_failures) values (?,1) on conflict(source)
               do update set consecutive_failures=consecutive_failures+1""",
            (source,),
        )
        return self.conn.execute(
            "select consecutive_failures from source_health where source=?", (source,)
        ).fetchone()[0]
