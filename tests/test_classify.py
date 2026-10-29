from core.models import Item
from ops.classify import classify
from ops.diff import Change


def ch(kind, title="x", payload=None, change="new", fields=None):
    return Change(change, Item("brightspace", "id", "INV2101", kind, title, payload or {}), fields or {})


def test_new_assignment_red():
    assert classify(ch("assignment")) == "red"


def test_due_change_red():
    assert classify(ch("assignment", change="changed", fields={"due_at": ("a", "b")})) == "red"


def test_assignment_title_change_not_red():
    assert classify(ch("assignment", change="changed", fields={"title": ("a", "b")})) == "yellow"


def test_quiz_announcement_red():
    assert classify(ch("announcement", title="Quiz 2 moved to week 9")) == "red"


def test_keyword_in_body_red():
    assert classify(ch("announcement", title="Update", payload={"body_text": "Lesson cancelled today"})) == "red"


def test_plain_announcement_yellow():
    assert classify(ch("announcement", title="Welcome to week 3", payload={"body_text": "Hi all"})) == "yellow"


def test_content_yellow():
    assert classify(ch("content", title="Week 7 slides")) == "yellow"


def test_grade_red():
    assert classify(ch("grade")) == "red"


def test_teams_and_group_red():
    for k in ["teams_mention", "teams_dm", "group_action"]:
        assert classify(ch(k)) == "red"


def test_teams_post_yellow():
    assert classify(ch("teams_post")) == "yellow"


def test_unknown_white():
    assert classify(ch("calendar_event")) == "white"
