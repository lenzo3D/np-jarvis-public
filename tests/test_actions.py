from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from core import actions
from core.actions import ActionNotPending
from core.config import load_settings
from core.models import Course
from ops.approvals import Approvals, card
from ops.watcher import Services
from tests.fakes import Clock, FakeBS, FakeNotifier

NOW = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
def ws_file(env_file):
    s = load_settings(env_file)
    f = s.workspace_dir / "INV2101" / "T5_answers.docx"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"PK" * 100)
    return f


def svc(db, env_file, enabled=True):
    s = replace(load_settings(env_file), actions_enabled=enabled)
    bs = FakeBS()
    bs.course_list = [Course(1001, "INV2101", "Investments")]
    return Services(settings=s, db=db, sessions=None, bs=bs, notifier=FakeNotifier(), clock=Clock(NOW))


def params(f, outbox=None):
    outbox = outbox or f.parent.parent.parent / "state" / "outbox"
    return {"ou": 1001, "folder_id": 55, **actions.prepare_submission(f.parent.parent, outbox, f)}


def ws(f):
    return f.parent.parent


def test_propose_does_not_execute(db, ws_file):
    a = actions.propose(db, "brightspace_submit", params(ws_file), "Submit T5", NOW, ws(ws_file), outbox=ws(ws_file).parent / "state" / "outbox")
    assert a.status == "pending" and a.expires_at == NOW + timedelta(hours=24)


def test_unknown_kind_rejected(db, ws_file):
    with pytest.raises(ValueError):
        actions.propose(db, "delete_course", {}, "x", NOW, ws(ws_file))


def test_file_outside_workspace_rejected(db, ws_file, tmp_path):
    outside = tmp_path / "evil.docx"
    outside.write_bytes(b"x")
    with pytest.raises(ValueError):
        actions.prepare_submission(ws(ws_file), tmp_path / "outbox", outside)


def test_symlinked_file_rejected(db, ws_file, tmp_path):
    secret = tmp_path / "secrets.env"
    secret.write_text("POLITEMALL_PASS=hunter2")
    link = ws_file.parent / "answers.docx"
    link.symlink_to(secret)
    with pytest.raises(ValueError):
        actions.prepare_submission(ws(ws_file), tmp_path / "outbox", link)


def test_snapshot_must_live_in_outbox(db, ws_file, tmp_path):
    p = params(ws_file)
    p["snapshot"] = str(tmp_path / "secrets.env")
    with pytest.raises(ValueError):
        actions.propose(db, "brightspace_submit", p, "x", NOW, ws(ws_file), outbox=tmp_path / "state" / "outbox")


def test_double_approve_rejected(db, ws_file):
    a = actions.propose(db, "brightspace_submit", params(ws_file), "x", NOW, ws(ws_file), outbox=ws(ws_file).parent / "state" / "outbox")
    actions.approve(db, a.id, NOW)
    with pytest.raises(ActionNotPending):
        actions.approve(db, a.id, NOW)


def test_expired_cannot_approve(db, ws_file):
    a = actions.propose(db, "brightspace_submit", params(ws_file), "x", NOW, ws(ws_file), outbox=ws(ws_file).parent / "state" / "outbox")
    with pytest.raises(ActionNotPending):
        actions.approve(db, a.id, NOW + timedelta(hours=25))
    assert actions.get(db, a.id).status == "expired"


def test_expire_sweeps_old(db, ws_file):
    actions.propose(db, "brightspace_submit", params(ws_file), "x", NOW, ws(ws_file), outbox=ws(ws_file).parent / "state" / "outbox")
    assert actions.expire(db, NOW + timedelta(hours=25)) == 1


async def test_api_propose_resolves_course_and_sends_card(db, env_file, ws_file):
    s = svc(db, env_file)
    ap = Approvals(s)
    res = await ap.propose("brightspace_submit",
                           {"course": "investments", "folder_id": 55, "file_path": str(ws_file)}, "")
    assert res["status"] == "awaiting approval" and "#1" in res["message"]
    text, buttons = s.notifier.messages[0]
    assert "T5_answers.docx" in text and "INV2101" in text
    assert [d for _, d in buttons[0]] == ["act:approve:1", "act:cancel:1", "act:preview:1"]
    assert s.bs.submit_calls == []


async def test_approve_then_execute_calls_submit_once(db, env_file, ws_file):
    s = svc(db, env_file)
    ap = Approvals(s)
    await ap.propose("brightspace_submit", {"course": "INV2101", "folder_id": 55, "file_path": str(ws_file)}, "")
    reply = await ap.on_approve("1")
    again = await ap.on_approve("1")
    assert len(s.bs.submit_calls) == 1
    assert "done" in reply and "not pending" in again
    st = await ap.status(1)
    assert st["status"] == "done" and st["result"]["status"] == 200


async def test_cancel(db, env_file, ws_file):
    s = svc(db, env_file)
    ap = Approvals(s)
    await ap.propose("brightspace_submit", {"course": "INV2101", "folder_id": 55, "file_path": str(ws_file)}, "")
    await ap.on_cancel("1")
    await ap.on_approve("1")
    assert s.bs.submit_calls == [] and (await ap.status(1))["status"] == "cancelled"


async def test_actions_disabled_noop(db, env_file, ws_file):
    s = svc(db, env_file, enabled=False)
    ap = Approvals(s)
    await ap.propose("brightspace_submit", {"course": "INV2101", "folder_id": 55, "file_path": str(ws_file)}, "")
    assert "actions disabled" in s.notifier.messages[0][0]
    reply = await ap.on_approve("1")
    assert s.bs.submit_calls == [] and "disabled" in reply


async def test_failed_execute_marks_failed(db, env_file, ws_file):
    s = svc(db, env_file)

    async def boom(*a, **k):
        raise RuntimeError("dropbox closed")

    s.bs.submit = boom
    ap = Approvals(s)
    await ap.propose("brightspace_submit", {"course": "INV2101", "folder_id": 55, "file_path": str(ws_file)}, "")
    reply = await ap.on_approve("1")
    assert "failed" in reply and (await ap.status(1))["status"] == "failed"


def test_card_text(db, ws_file):
    a = actions.propose(db, "brightspace_submit", params(ws_file), "Tutorial 5 answers", NOW, ws(ws_file), outbox=ws(ws_file).parent / "state" / "outbox")
    text, buttons = card(a, {"course": "INV2101", "target": "Tutorial 5", "due": "Fri 30 Oct 23:59"})
    assert "Submit" in text and "T5_answers.docx" in text and "Tutorial 5" in text and "Fri 30 Oct 23:59" in text
    assert "KB" in text and a.params["sha256"][:12] in text


async def test_submit_uses_snapshot_even_if_file_swapped(db, env_file, ws_file, tmp_path):
    s = svc(db, env_file)
    ap = Approvals(s)
    original = ws_file.read_bytes()
    await ap.propose("brightspace_submit", {"course": "INV2101", "folder_id": 55, "file_path": str(ws_file)}, "")
    secret = tmp_path / "secrets.env"
    secret.write_text("POLITEMALL_PASS=hunter2")
    ws_file.unlink()
    ws_file.symlink_to(secret)
    await ap.on_approve("1")
    (_, _, sent_path, sent_bytes) = s.bs.submit_calls[0]
    assert sent_bytes == original and sent_path.name == "T5_answers.docx"
    assert not str(sent_path).startswith(str(s.settings.workspace_dir))


async def test_extra_params_from_claude_ignored(db, env_file, ws_file, tmp_path):
    s = svc(db, env_file)
    ap = Approvals(s)
    secret = tmp_path / "secrets.env"
    secret.write_text("x")
    await ap.propose("brightspace_submit", {"course": "INV2101", "folder_id": 55, "file_path": str(ws_file),
                                            "snapshot": str(secret), "sha256": "0" * 64}, "")
    await ap.on_approve("1")
    assert s.bs.submit_calls[0][3] == ws_file.read_bytes()


class FakeTeams:
    async def describe(self, conv_id):
        return "direct chat with Mr Tan"

    async def send(self, conv_id, text):
        return {"id": "m1"}


async def test_teams_card_names_recipient(db, env_file):
    s = svc(db, env_file)
    s.teams = FakeTeams()
    res = await Approvals(s).propose("teams_send_message", {"conv_id": "19:abc@thread.v2", "text": "Hi sir"}, "")
    assert res["status"] == "awaiting approval"
    assert "direct chat with Mr Tan" in s.notifier.messages[0][0]


async def test_teams_bad_conv_id_rejected(db, env_file):
    s = svc(db, env_file)
    s.teams = FakeTeams()
    res = await Approvals(s).propose("teams_send_message", {"conv_id": "../../me/sendMail", "text": "x"}, "")
    assert res["status"] == "rejected" and s.notifier.messages == []


async def test_discussion_card_names_forum_and_topic(db, env_file):
    s = svc(db, env_file)

    async def names(ou, forum, topic):
        return ("Week 7 Q&A", "Tutorial questions")

    s.bs.discussion_names = names
    await Approvals(s).propose("brightspace_discussion_post", {"course": "INV2101", "forum_id": 3, "topic_id": 4,
                                                               "subject": "Q3", "text": "Is (b) 5%?"}, "")
    text = s.notifier.messages[0][0]
    assert "Week 7 Q&A › Tutorial questions" in text and "Is (b) 5%?" in text
