from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.models import Item
from ops.diff import Change
from ops.format import alert_text, fmt_dt, html_to_text

SGT = ZoneInfo("Asia/Singapore")


def test_fmt_dt_converts_to_sgt():
    assert fmt_dt(datetime(2026, 10, 30, 15, 59, tzinfo=timezone.utc), SGT) == "Fri 30 Oct 23:59"


def test_html_to_text():
    assert html_to_text("<p>Hello&nbsp;<b>class</b></p><script>x</script>") == "Hello class"


def test_html_to_text_block_spacing():
    assert html_to_text("<p>one</p><p>two</p>") == "one two"


def test_html_to_text_truncates():
    out = html_to_text("a" * 1000)
    assert len(out) <= 301
    assert out.endswith("…")


def test_alert_text_assignment():
    item = Item("brightspace", "bs:a:1", "INV2101", "assignment", "Tutorial 6",
                {"due_at": "2026-10-30T15:59:00Z"})
    now = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)
    text = alert_text(Change("new", item), "red", SGT, now=now)
    assert "INV2101" in text and "Tutorial 6" in text
    assert "Fri 30 Oct 23:59" in text
    assert "(4 days)" in text


def test_alert_text_no_due_date():
    item = Item("brightspace", "bs:a:1", "INV2101", "assignment", "Reflection", {"due_at": None, "end_at": None})
    text = alert_text(Change("new", item), "red", SGT)
    assert "No due date set" in text


def test_alert_text_due_change_shows_old_and_new():
    item = Item("brightspace", "bs:a:1", "INV2101", "assignment", "T6", {"due_at": "2026-11-02T15:59:00Z"})
    c = Change("changed", item, {"due_at": ("2026-10-30T15:59:00Z", "2026-11-02T15:59:00Z")})
    text = alert_text(c, "red", SGT)
    assert "Fri 30 Oct 23:59" in text and "Mon 02 Nov 23:59" in text


def test_content_alert_names_the_course_and_where_it_sits():
    item = Item("brightspace", "bs:content:7", "26S2-731IV_013686", "content", "Topic 1 homework",
                {"module": "Week 1 \u203a Homework", "type": "File"})
    names = {"26S2-731IV_013686": "INVESTMENTS(731IV_013686)"}
    text = alert_text(Change("new", item), "yellow", SGT, course_names=names)
    assert text == "[Update] Investments \u00b7 New material\nTopic 1 homework\nLocation: Week 1 \u203a Homework\nType: File"


def test_alert_without_known_course_name_falls_back_to_code():
    item = Item("brightspace", "bs:content:7", "INV2101", "content", "Slides", {})
    assert alert_text(Change("new", item), "yellow", SGT).startswith("[Update] INV2101 \u00b7 New material\nSlides")


def test_long_post_is_shown_in_full_not_cut_at_300_chars():
    body = "Hi class. " + "Margin is borrowed money used to buy more shares. " * 20 + "THE END"
    item = Item("teams", "t:1", None, "teams_post", "Tutor in Class", {"body_text": body})
    text = alert_text(Change("new", item), "yellow", SGT)
    assert len(body) > 900 and text.endswith("THE END") and "\u2026" not in text


def test_very_long_post_is_clipped_on_a_word_boundary():
    item = Item("teams", "t:1", None, "teams_post", "Tutor", {"body_text": "word " * 1000})
    last = alert_text(Change("new", item), "yellow", SGT).splitlines()[-1]
    assert last.endswith("word\u2026") and len(last) <= 1501
