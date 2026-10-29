from datetime import datetime, timezone

from core.library import Library

T0 = datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc)


def seed(lib):
    lib.upsert("bs:content:1", "26S2-73TMBI_013687", "document", "Teaching Plan.pdf", "Course Info",
               "https://x/1", "Week 9: Quiz 2 (15%). Week 12: CA2 presentation. Blockchain consensus.", T0, "v1")
    lib.upsert("bs:content:2", "26S2-731IV_013686", "document", "Week 7 slides.pptx", "Week 7",
               "https://x/2", "Bond pricing and yield to maturity. Duration and convexity.", T0, "v1")
    lib.upsert("bs:announcement:3", "26S2-731IV_013686", "announcement", "Quiz 2 moved", "", "",
               "Dear all, quiz 2 for Investments is moved to Week 10.", T0, "v1")


def test_search_ranks_and_snippets(db):
    lib = Library(db)
    seed(lib)
    hits = lib.search("quiz 2")
    assert {h["key"] for h in hits} == {"bs:content:1", "bs:announcement:3"}
    assert all("[" in h["snippet"] for h in hits)


def test_search_course_filter(db):
    lib = Library(db)
    seed(lib)
    assert [h["key"] for h in lib.search("quiz", course="26S2-731IV_013686")] == ["bs:announcement:3"]


def test_search_tolerates_punctuation(db):
    lib = Library(db)
    seed(lib)
    assert lib.search('"yield (to) maturity?') and lib.search("") == []


def test_upsert_replaces_text_and_versions(db):
    lib = Library(db)
    seed(lib)
    assert lib.needs_update("bs:content:2", "v1") is False
    assert lib.needs_update("bs:content:2", "v2") is True
    assert lib.needs_update("bs:content:99", "v1") is True
    lib.upsert("bs:content:2", "26S2-731IV_013686", "document", "Week 7 slides.pptx", "Week 7", "", "Options", T0, "v2")
    assert lib.search("convexity") == [] and lib.search("options")[0]["key"] == "bs:content:2"
    assert lib.count() == 3


def test_get_full_text(db):
    lib = Library(db)
    seed(lib)
    m = lib.get("bs:content:1")
    assert m["title"] == "Teaching Plan.pdf" and "CA2 presentation" in m["text"]
    assert lib.get("nope") is None


def test_list_by_kind(db):
    lib = Library(db)
    seed(lib)
    assert [m["key"] for m in lib.list(kind="announcement")] == ["bs:announcement:3"]
