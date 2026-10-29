import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import tg_group
from core.models import GroupMessage

T0 = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent


def gm(i, text, mins=0, chat=-100):
    return GroupMessage(chat, i, "Ben", text, T0 + timedelta(minutes=mins))


def test_store_idempotent(db):
    tg_group.store_message(db, gm(1, "hello"))
    tg_group.store_message(db, gm(1, "hello"))
    assert db.conn.execute("select count(*) from group_messages").fetchone()[0] == 1


def test_search_fts(db):
    tg_group.store_message(db, gm(1, "Investments tutorial due Friday"))
    tg_group.store_message(db, gm(2, "lunch?"))
    tg_group.store_message(db, gm(3, "who did the TUTORIAL q3"))
    assert sorted(m.msg_id for m in tg_group.search(db, "tutorial")) == [1, 3]


def test_search_handles_fts_syntax_chars(db):
    tg_group.store_message(db, gm(1, "submit by 23:59 (Fri)"))
    assert [m.msg_id for m in tg_group.search(db, 'by 23:59 "(Fri)')] == [1]


def test_messages_since(db):
    tg_group.store_message(db, gm(1, "old", mins=-120))
    tg_group.store_message(db, gm(2, "new", mins=5))
    tg_group.store_message(db, gm(3, "other chat", mins=5, chat=-200))
    assert [m.msg_id for m in tg_group.messages_since(db, -100, T0)] == [2]
    assert [m.msg_id for m in tg_group.messages_since(db, None, T0)] == [2, 3]
    assert tg_group.count_since(db, T0) == 2


def test_no_send_calls_in_module():
    banned = {"send_message", "send_file", "reply", "forward_messages", "edit_message", "delete_messages"}
    for rel in ["core/tg_group.py", "ops/telethon_main.py"]:
        tree = ast.parse((ROOT / rel).read_text())
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not attrs & banned, f"{rel} uses {attrs & banned}"
