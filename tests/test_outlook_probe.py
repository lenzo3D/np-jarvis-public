import asyncio

from core.outlook import Outlook


class NoBrowser:
    async def context(self, name):
        raise RuntimeError("no browser in tests")


async def test_probe_reports_a_stalled_step_instead_of_hanging():
    async def hangs():
        await asyncio.sleep(60)

    steps = []
    o = Outlook(NoBrowser(), token_provider=hangs)
    report = await asyncio.wait_for(o.probe(progress=steps.append, step_timeout=0.1), 5)
    assert report["strategy"] is None and report["tokens"] == []
    assert any("step 1" in e and "in time" in e for e in report["errors"])
    assert [s.split(":")[0] for s in steps] == ["Step 1 of 3", "Step 2 of 3", "Step 3 of 3"]


async def test_probe_reports_a_failing_step_and_keeps_going():
    async def boom():
        raise ValueError("x")

    report = await Outlook(NoBrowser(), token_provider=boom).probe(step_timeout=1)
    assert report["errors"] == ["step 1 (reading tokens) failed: ValueError"] and report["attempts"] == []


def test_saved_strategy_is_not_mistaken_for_the_token_provider():
    o = Outlook(NoBrowser(), strategy="outlook-rest")
    assert o.strategy == "outlook-rest" and callable(o._provider)
    try:
        Outlook(NoBrowser(), "outlook-rest")  # positional use is no longer possible
    except TypeError:
        pass
    else:
        raise AssertionError("positional strategy accepted")


def test_ops_builds_the_reader_with_the_saved_strategy():
    import pathlib
    src = pathlib.Path("ops/main.py").read_text()
    assert 'Outlook(sessions, strategy=db.get_setting("outlook_strategy"))' in src
