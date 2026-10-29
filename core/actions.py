"""Pending outward actions. propose() never executes; approve() is only called by the Ops bot."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

from core import safefs
from core.db import DB
from core.models import PendingAction

KINDS = {"brightspace_submit", "brightspace_discussion_post", "teams_send_message"}
TTL = timedelta(hours=24)


class ActionNotPending(Exception):
    pass


def _row_to_action(r) -> PendingAction:
    return PendingAction(r["id"], r["kind"], json.loads(r["params_json"]), r["summary"], r["status"],
                         datetime.fromisoformat(r["created_at"]), datetime.fromisoformat(r["expires_at"]),
                         json.loads(r["result_json"]) if r["result_json"] else None)


def get(db: DB, action_id: int) -> PendingAction:
    r = db.conn.execute("select * from pending_actions where id=?", (action_id,)).fetchone()
    if not r:
        raise ActionNotPending(f"action #{action_id} not found")
    return _row_to_action(r)


def list_pending(db: DB) -> list[PendingAction]:
    return [_row_to_action(r) for r in
            db.conn.execute("select * from pending_actions where status='pending' order by id")]


def prepare_submission(workspace: Path, outbox: Path, file_path) -> dict:
    """Copy the workspace file (never following links) into the ops-only outbox. Execution and preview use
    only this snapshot, so the file the user approved is exactly the file that gets submitted."""
    try:
        data = safefs.read_file(Path(workspace), file_path)
    except (safefs.UnsafePath, OSError) as e:
        raise ValueError(f"file_path must be a regular file inside the workspace ({e})") from e
    name = Path(file_path).name
    safefs._check_part(name)
    digest = hashlib.sha256(data).hexdigest()
    snap_dir = Path(outbox) / digest
    snap_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    snap = snap_dir / name
    snap.write_bytes(data)
    return {"file_name": name, "sha256": digest, "size": len(data), "snapshot": str(snap)}


def _snapshot_ok(params: dict, outbox: Path | None) -> Path:
    if outbox is None:
        raise ValueError("no outbox configured")
    snap = Path(params.get("snapshot", ""))
    if not snap.resolve().is_relative_to(Path(outbox).resolve()) or not snap.is_file() or snap.is_symlink():
        raise ValueError("snapshot must be a file in the ops outbox")
    if hashlib.sha256(snap.read_bytes()).hexdigest() != params.get("sha256"):
        raise ValueError("snapshot hash mismatch")
    return snap


def _validate(kind: str, params: dict, workspace: Path, outbox: Path | None = None) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown action kind: {kind}")
    if kind == "brightspace_submit":
        _snapshot_ok(params, outbox)
        for k in ("ou", "folder_id"):
            if not isinstance(params.get(k), int):
                raise ValueError(f"{k} must be an integer")
    elif kind == "brightspace_discussion_post":
        for k in ("ou", "forum_id", "topic_id"):
            if not isinstance(params.get(k), int):
                raise ValueError(f"{k} must be an integer")
        if not str(params.get("text", "")).strip():
            raise ValueError("text is required")
    elif kind == "teams_send_message":
        from core.teams import valid_conv_id

        if not valid_conv_id(str(params.get("conv_id", ""))) or not str(params.get("text", "")).strip():
            raise ValueError("a valid conv_id and text are required")


def propose(db: DB, kind: str, params: dict, summary: str, now: datetime, workspace: Path,
            outbox: Path | None = None) -> PendingAction:
    _validate(kind, params, workspace, outbox)
    if outbox is not None:
        params = {**params, "outbox": str(outbox)}
    cur = db.conn.execute(
        "insert into pending_actions(kind, params_json, summary, status, created_at, expires_at) values (?,?,?,?,?,?)",
        (kind, json.dumps(params), summary, "pending", now.isoformat(), (now + TTL).isoformat()),
    )
    return get(db, cur.lastrowid)


def approve(db: DB, action_id: int, now: datetime) -> PendingAction:
    a = get(db, action_id)
    if a.status == "pending" and now >= a.expires_at:
        db.conn.execute("update pending_actions set status='expired' where id=? and status='pending'", (action_id,))
        raise ActionNotPending(f"action #{action_id} expired")
    cur = db.conn.execute(
        "update pending_actions set status='approved', decided_at=? where id=? and status='pending'",
        (now.isoformat(), action_id),
    )
    if cur.rowcount != 1:
        raise ActionNotPending(f"action #{action_id} is {get(db, action_id).status}, not pending")
    return get(db, action_id)


def cancel(db: DB, action_id: int, now: datetime | None = None) -> bool:
    cur = db.conn.execute(
        "update pending_actions set status='cancelled', decided_at=? where id=? and status='pending'",
        ((now or datetime.now()).isoformat(), action_id),
    )
    return cur.rowcount == 1


def expire(db: DB, now: datetime) -> int:
    return db.conn.execute(
        "update pending_actions set status='expired' where status='pending' and expires_at<=?", (now.isoformat(),)
    ).rowcount


async def execute(services, action: PendingAction) -> dict:
    if action.status != "approved":
        raise ActionNotPending(f"action #{action.id} is {action.status}, not approved")
    p = action.params
    try:
        if action.kind == "brightspace_submit":
            snap = _snapshot_ok(p, Path(p["outbox"]))
            result = await services.bs.submit(p["ou"], p["folder_id"], snap, p.get("comment", ""))
        elif action.kind == "brightspace_discussion_post":
            result = await services.bs.post_discussion(p["ou"], p["forum_id"], p["topic_id"],
                                                       p.get("subject", ""), p["text"])
        elif action.kind == "teams_send_message":
            if not services.teams:
                raise RuntimeError("Teams not configured")
            result = await services.teams.send(p["conv_id"], p["text"])
        else:
            raise ValueError(action.kind)
    except Exception as e:  # noqa: BLE001
        result = {"error": f"{type(e).__name__}: {e}"[:500]}
        services.db.conn.execute("update pending_actions set status='failed', result_json=? where id=?",
                                 (json.dumps(result, default=str), action.id))
        return result
    services.db.conn.execute("update pending_actions set status='done', result_json=? where id=?",
                             (json.dumps(result, default=str), action.id))
    return result
