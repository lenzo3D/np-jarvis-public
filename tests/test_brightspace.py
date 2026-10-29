import json
from pathlib import Path

import httpx
import pytest
import respx

from core.auth import AuthRequired
from core.brightspace import Brightspace

FIX = Path(__file__).parent / "fixtures" / "bs_synthetic"
BASE = "https://politemall.example"


def j(name):
    return json.loads((FIX / f"{name}.json").read_text())


class FakeSession:
    async def cookies(self, site):
        return httpx.Cookies({"d2lSessionVal": "x"})


@pytest.fixture
def mock():
    with respx.mock(base_url=BASE, assert_all_called=False) as m:
        m.get("/d2l/api/versions/").respond(json=j("versions"))
        yield m


@pytest.fixture
def bs():
    return Brightspace(FakeSession(), BASE)


async def test_courses_current_semester_only(mock, bs):
    mock.get(url__regex=r".*/lp/1.47/enrollments/myenrollments/.*").respond(json=j("myenrollments"))
    courses = await bs.courses()
    assert [c.code for c in courses] == ["26S2-731IV_013686", "26S2-731CMBK_013682"]


async def test_courses_prefix_override(mock):
    mock.get(url__regex=r".*/lp/1.47/enrollments/myenrollments/.*").respond(json=j("myenrollments"))
    b = Brightspace(FakeSession(), BASE, course_prefix="26S1")
    assert [c.code for c in await b.courses()] == ["26S1-731CFIN_013674"]


async def test_courses_all_when_prefix_star(mock):
    mock.get(url__regex=r".*/lp/1.47/enrollments/myenrollments/.*").respond(json=j("myenrollments"))
    b = Brightspace(FakeSession(), BASE, course_prefix="*")
    assert len(await b.courses()) == 5  # inactive and duplicate dropped


def test_latest_semester_tag():
    from core.brightspace import latest_semester
    assert latest_semester(["25S2-A", "26S1-B", "26S2-C", "26EG-D", "OLD1001"]) == "26S2"
    assert latest_semester(["OLD1001"]) is None


async def test_assignment_null_due_date(mock, bs):
    mock.get("/d2l/api/le/1.79/1001/dropbox/folders/").respond(json=j("dropbox_folders"))
    items = {a.folder_id: a for a in await bs.assignments(1001, "INV2101")}
    assert items[56].due_at is None and items[56].end_at is not None
    assert items[57].due_at is None and items[57].end_at is None


async def test_assignment_items_payload_iso_utc(mock, bs):
    mock.get("/d2l/api/le/1.79/1001/dropbox/folders/").respond(json=j("dropbox_folders"))
    items = await bs.assignment_items(1001, "INV2101")
    t5 = next(i for i in items if i.title == "Tutorial 5")
    assert t5.stable_id == "bs:assignment:1001:55"
    assert t5.payload["due_at"] == "2026-10-30T15:59:00+00:00"
    assert t5.payload["ou"] == 1001 and t5.payload["folder_id"] == 55


async def test_announcement_body_is_plain_text(mock, bs):
    mock.get("/d2l/api/le/1.79/1001/news/").respond(json=j("news"))
    items = await bs.announcements(1001, "INV2101")
    assert len(items) == 1  # unpublished draft skipped
    assert items[0].payload["body_text"] == "Dear all, Quiz 2 is now in week 9."


async def test_content_items_flattened(mock, bs):
    mock.get("/d2l/api/le/1.79/1001/content/toc").respond(json=j("content_toc"))
    items = await bs.content_items(1001, "INV2101")
    assert {i.stable_id for i in items} == {"bs:content:701", "bs:content:702", "bs:content:703"}
    t7 = next(i for i in items if i.stable_id == "bs:content:702")
    assert t7.payload["module"] == "Week 7 › Tutorial" and t7.payload["type"] == "File"


async def test_grades(mock, bs):
    mock.get("/d2l/api/le/1.79/1001/grades/values/myGradeValues/").respond(json=j("grades"))
    g = (await bs.grades(1001, "INV2101"))[0]
    assert g.stable_id == "bs:grade:1001:7"
    assert g.payload["value"] == 8.0 and g.payload["out_of"] == 10.0
    assert g.payload["feedback_text"] == "Well done"


async def test_submitted(mock, bs):
    route = mock.get("/d2l/api/le/1.79/1001/dropbox/folders/55/submissions/mysubmissions/")
    route.respond(json=j("mysubmissions_yes"))
    assert await bs.submitted(1001, 55) is True
    route.respond(json=j("mysubmissions_no"))
    assert await bs.submitted(1001, 55) is False


async def test_calendar_sends_course_ids(mock, bs):
    mock.get(url__regex=r".*/lp/1.47/enrollments/myenrollments/.*").respond(json=j("myenrollments"))
    route = mock.get(url__regex=r".*/le/1.79/calendar/events/myEvents/.*").respond(json=j("calendar"))
    events = await bs.calendar_range_days(7)
    assert events[0]["Title"] == "INV2101 Lecture"
    assert route.calls.last.request.url.params["orgUnitIdsCSV"] == "1001,1002"


async def test_401_raises_auth_required(mock, bs):
    mock.get("/d2l/api/le/1.79/1001/news/").respond(401)
    with pytest.raises(AuthRequired):
        await bs.announcements(1001, "INV2101")


async def test_redirect_to_login_raises_auth_required(mock, bs):
    mock.get("/d2l/api/le/1.79/1001/news/").respond(302, headers={"Location": f"{BASE}/d2l/login"})
    with pytest.raises(AuthRequired):
        await bs.announcements(1001, "INV2101")


async def test_fetch_file(mock, bs):
    mock.get("/d2l/api/le/1.79/1001/content/topics/702/file").respond(
        200, content=b"PK..", headers={"Content-Disposition": 'attachment; filename="Tutorial 7.docx"'})
    assert await bs.fetch_file(1001, 702) == ("Tutorial 7.docx", b"PK..")


async def test_fetch_file_redirect_limit(mock, bs):
    mock.get("/d2l/api/le/1.79/1001/content/topics/702/file").respond(302, headers={"Location": f"{BASE}/loop"})
    mock.get("/loop").respond(302, headers={"Location": f"{BASE}/loop"})
    with pytest.raises(httpx.HTTPError):
        await bs.fetch_file(1001, 702)


async def test_submit_sends_xsrf_and_multipart(mock, bs, tmp_path):
    f = tmp_path / "T5.docx"
    f.write_bytes(b"PK..")
    mock.get("/d2l/lp/auth/xsrf-tokens").respond(json={"referrerToken": "XSRF123"})
    route = mock.post("/d2l/api/le/1.79/1001/dropbox/folders/55/submissions/mysubmissions/").respond(200)
    await bs.submit(1001, 55, f, "done")
    req = route.calls.last.request
    assert req.headers["X-Csrf-Token"] == "XSRF123"
    assert req.headers["content-type"].startswith("multipart/mixed")
    assert b"T5.docx" in req.content and b"PK.." in req.content


async def test_403_is_not_auth_required(mock, bs):
    mock.get("/d2l/api/le/1.79/1001/grades/values/myGradeValues/").respond(403)
    mock.get("/d2l/api/lp/1.47/users/whoami").respond(json={"Identifier": "1"})
    with pytest.raises(httpx.HTTPStatusError):
        await bs.grades(1001, "INV2101")


async def test_403_with_dead_session_is_auth_required(mock, bs):
    mock.get("/d2l/api/le/1.79/1001/news/").respond(403, text='{ Errors: [ {Message: "Forbidden"} ] }')
    mock.get("/d2l/api/lp/1.47/users/whoami").respond(403)
    with pytest.raises(AuthRequired):
        await bs.announcements(1001, "INV2101")


async def test_403_with_live_session_is_tool_error(mock, bs):
    mock.get("/d2l/api/le/1.79/1001/news/").respond(403)
    mock.get("/d2l/api/lp/1.47/users/whoami").respond(json={"Identifier": "1"})
    with pytest.raises(httpx.HTTPStatusError):
        await bs.announcements(1001, "INV2101")


async def test_fetch_file_aborts_past_size_cap(mock, bs):
    from core.brightspace import FileTooLarge
    mock.get("/d2l/api/le/1.79/1001/content/topics/702/file").respond(200, content=b"x" * 2048)
    with pytest.raises(FileTooLarge):
        await bs.fetch_file(1001, 702, max_bytes=1024)
