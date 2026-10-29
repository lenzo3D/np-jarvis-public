import io
from datetime import datetime, timezone

from core.config import load_settings
from core.library import Library
from core.models import Course, Item
from ops.scan import full_scan
from ops.watcher import Services
from tests.fakes import Clock, FakeBS, FakeNotifier

NOW = datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc)


def docx_bytes(text):
    import docx
    d = docx.Document()
    d.add_paragraph(text)
    b = io.BytesIO()
    d.save(b)
    return b.getvalue()


def topic(tid, title, url, typ="File", lm="2026-10-20T00:00:00+00:00"):
    return Item("brightspace", f"bs:content:{tid}", "26S2-73TMBI_013687", "content", title,
                {"ou": 1044622, "topic_id": tid, "url": url, "type": typ, "module": "Course Info › Plans",
                 "last_modified": lm})


def svc(db, env_file):
    s = Services(settings=load_settings(env_file), db=db, sessions=None, bs=FakeBS(), notifier=FakeNotifier(),
                 clock=Clock(NOW))
    s.bs.course_list = [Course(1044622, "26S2-73TMBI_013687", "TRANSAC MGT & BLOCKCHAIN INNOV(73TMBI_013687)")]
    s.bs.content = {1044622: [topic(1, "Teaching Plan", "/content/enforced/1044622/TP.docx"),
                              topic(2, "Lecture video", "/content/enforced/1044622/L1.mp4"),
                              topic(3, "Reading", "https://example.com", typ="Link")]}
    s.bs.news = {1044622: [Item("brightspace", "bs:announcement:1044622:7", "26S2-73TMBI_013687", "announcement",
                                "Quiz 2 date", {"body_text": "Quiz 2 is on 10 Nov", "posted_at": "2026-10-20"})]}
    s.bs.instructions = {1044622: {55: ("CA1 Report", "Submit a 1500-word report on DeFi.")}}
    s.fetched = []

    async def fetch(ou, tid):
        s.fetched.append(tid)
        return ("TP.docx", docx_bytes("Week 9: Quiz 2. Week 12: CA2 presentation."))

    s.bs.fetch_file = fetch
    return s


async def test_full_scan_indexes_documents_announcements_instructions(db, env_file):
    s = svc(db, env_file)
    stats = await full_scan(s, extract=False)
    lib = Library(db)
    assert stats["documents"] == 1 and stats["announcements"] == 1 and stats["assignments"] == 1
    assert s.fetched == [1]  # mp4 and link never downloaded
    assert lib.search("CA2 presentation")[0]["key"] == "bs:content:1"
    assert lib.get("bs:assignment:1044622:55")["text"].startswith("Submit a 1500-word")
    assert lib.get("bs:content:1")["module"] == "Course Info › Plans"


async def test_rescan_skips_unchanged_files(db, env_file):
    s = svc(db, env_file)
    await full_scan(s, extract=False)
    await full_scan(s, extract=False)
    assert s.fetched == [1]
    s.bs.content[1044622][0].payload["last_modified"] = "2026-10-26T00:00:00+00:00"
    await full_scan(s, extract=False)
    assert s.fetched == [1, 1]


async def test_one_bad_file_does_not_stop_scan(db, env_file):
    s = svc(db, env_file)

    async def boom(ou, tid):
        raise RuntimeError("download failed")

    s.bs.fetch_file = boom
    stats = await full_scan(s, extract=False)
    assert stats["errors"] == 1 and stats["announcements"] == 1


async def test_oversized_file_recorded_and_not_retried(db, env_file):
    from core.brightspace import FileTooLarge
    s = svc(db, env_file)

    async def too_big(ou, tid):
        s.fetched.append(tid)
        raise FileTooLarge("300 MB")

    s.bs.fetch_file = too_big
    await full_scan(s, extract=False)
    await full_scan(s, extract=False)
    assert s.fetched == [1] and "too large" in Library(db).get("bs:content:1")["text"]
