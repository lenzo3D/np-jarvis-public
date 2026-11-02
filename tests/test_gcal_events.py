from zoneinfo import ZoneInfo

from core.deadlines import Deadline
from ops.gcal_events import event_body, event_hash, short_course

SGT = ZoneInfo("Asia/Singapore")


def dl(**kw):
    base = dict(key="bs:assignment:1:5", title="Tutorial 5", course_code="26S2-731IV_013686", kind="assignment",
                start_utc="2026-10-30T15:29:00+00:00", end_utc="2026-10-30T15:59:00+00:00", all_day=False,
                source="brightspace", source_ref="https://nplms.polite.edu.sg/d2l/le/1044225/dropbox/55",
                confidence="confirmed", quote="")
    base.update(kw)
    return Deadline(**base)


def test_short_course():
    assert short_course("INVESTMENTS(731IV_013686)", "26S2-731IV_013686") == "Investments"
    assert short_course("TRANSAC MGT & BLOCKCHAIN INNOV(73TMBI_013687)", "x") == "Transac Mgt & Blockchain Innov"
    assert short_course("", "26S2-73BKALY_012872") == "73BKALY"


def test_confirmed_timed_deadline():
    b = event_body(dl(), "INVESTMENTS(731IV_013686)", SGT)
    assert b["summary"] == "\U0001F4CC Investments: Tutorial 5"
    assert b["start"] == {"dateTime": "2026-10-30T23:29:00+08:00", "timeZone": "Asia/Singapore"}
    assert b["end"] == {"dateTime": "2026-10-30T23:59:00+08:00", "timeZone": "Asia/Singapore"}
    assert b["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 1440},
                                                                 {"method": "popup", "minutes": 180}]}
    assert b["extendedProperties"]["private"] == {"jarvis": "1", "jarvis_key": "bs:assignment:1:5"}
    assert "nplms.polite.edu.sg" in b["description"] and "Added by JARVIS" in b["description"]


def test_tentative_all_day_test():
    d = dl(key="x:1", title="Quiz 2", kind="quiz", start_utc="2026-11-10", end_utc=None, all_day=True,
           source="announcement", source_ref="", confidence="tentative", quote="Quiz 2 will be held in Week 9")
    b = event_body(d, "TMBI", SGT)
    assert b["summary"] == "❓ Tmbi: Quiz 2"
    assert b["start"] == {"date": "2026-11-10"} and b["end"] == {"date": "2026-11-11"}
    assert "Quiz 2 will be held in Week 9" in b["description"] and "tentative" in b["description"].lower()
    assert b["reminders"]["overrides"] == [{"method": "popup", "minutes": 1440}]


def test_meeting_uses_start_and_end():
    d = dl(key="x:2", title="Project meeting", kind="meeting", start_utc="2026-10-29T06:00:00+00:00",
           end_utc="2026-10-29T07:30:00+00:00", confidence="tentative", source="group")
    b = event_body(d, "Investments", SGT)
    assert b["start"]["dateTime"] == "2026-10-29T14:00:00+08:00" and b["end"]["dateTime"] == "2026-10-29T15:30:00+08:00"
    assert b["reminders"]["overrides"] == [{"method": "popup", "minutes": 30}]


def test_meeting_without_end_is_one_hour():
    d = dl(key="x:3", kind="meeting", start_utc="2026-10-29T06:00:00+00:00", end_utc=None, source="group",
           confidence="tentative")
    assert event_body(d, "X", SGT)["end"]["dateTime"] == "2026-10-29T15:00:00+08:00"


def test_hash_stable_and_sensitive():
    a = event_body(dl(), "Investments", SGT)
    assert event_hash(a) == event_hash(event_body(dl(), "Investments", SGT))
    assert event_hash(a) != event_hash(event_body(dl(title="Tutorial 6"), "Investments", SGT))


def test_timed_other_event_is_an_event_with_a_default_hour_and_reminders():
    from core.deadlines import is_due
    from ops.gcal_events import event_body
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Singapore")
    d = Deadline(key="k1", title="Industry briefing", course_code=None, kind="other", start_utc="2026-11-10T06:00:00+00:00",
                 end_utc=None, all_day=False, source="brightspace", source_ref="", confidence="confirmed", quote="",
                 as_event=True)
    b = event_body(d, "", tz)
    assert b["start"]["dateTime"].startswith("2026-11-10T14:00") and b["end"]["dateTime"].startswith("2026-11-10T15:00")
    assert [o["minutes"] for o in b["reminders"]["overrides"]] == [1440, 60]
    assert b["description"].startswith("Event")
    due = Deadline(**{**d.__dict__, "key": "k2", "title": "Reflection due", "as_event": False})
    bd = event_body(due, "", tz)
    assert bd["end"]["dateTime"].startswith("2026-11-10T14:00") and bd["start"]["dateTime"].startswith("2026-11-10T13:30")
    assert [o["minutes"] for o in bd["reminders"]["overrides"]] == [1440, 180]
    assert is_due("other") and not is_due("other", True)


def test_tentative_events_never_carry_web_addresses():
    from ops.gcal_events import event_body
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Singapore")
    kw = dict(course_code=None, kind="quiz", start_utc="2026-11-10T06:00:00+00:00", end_utc=None, all_day=False,
              source="teams", source_ref="", quote="see https://evil.example/x?a=1 or www.evil.example now")
    t = event_body(Deadline(key="a", title="Quiz at http://evil.example/q", confidence="tentative", **kw), "", tz)
    assert "evil" not in t["summary"] and "evil" not in t["description"]
    c = event_body(Deadline(key="b", title="Quiz at http://ok.example/q", confidence="confirmed", **kw), "", tz)
    assert "ok.example" in c["summary"]
