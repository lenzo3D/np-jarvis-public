import os

from ops.main import build_services


async def test_build_services_wires_hooks(env_file, monkeypatch):
    monkeypatch.setenv("SECRETS_FILE", str(env_file))
    s = build_services()
    assert {"status", "sweep", "login"} <= set(s.extra["hooks"])
    text = await s.extra["hooks"]["status"]()
    assert "JARVIS status" in text and "pending approvals: 0" in text


async def test_build_services_wires_approvals(env_file, monkeypatch):
    monkeypatch.setenv("SECRETS_FILE", str(env_file))
    s = build_services()
    router = s.extra["router"]
    assert {"act:approve", "act:cancel", "act:preview", "login:retry", "mute", "doit", "dl"} <= set(router._routes)
    assert s.extra["propose"] and s.extra["action_status"]
    assert await s.extra["hooks"]["pending"]() == "No pending approvals."


async def test_build_services_wires_deadline_hooks(env_file, monkeypatch):
    monkeypatch.setenv("SECRETS_FILE", str(env_file))
    s = build_services()
    assert {"deadlines", "scan", "ask"} <= set(s.extra["hooks"])
    assert "No deadlines" in await s.extra["hooks"]["deadlines"]()


async def test_chat_hooks_wired(env_file, monkeypatch):
    import asyncio
    monkeypatch.setenv("SECRETS_FILE", str(env_file))
    s = build_services()
    gate = asyncio.Event()

    async def runner(prompt, session_id):
        await gate.wait()
        yield "result", "ok"

    s.extra["chat"].runner = runner
    s.extra["chat"].ctx.notifier = __import__("tests.fakes", fromlist=["FakeNotifier"]).FakeNotifier()
    assert await s.extra["hooks"]["ask"]("q1") is None
    await asyncio.sleep(0)
    assert "still working" in await s.extra["hooks"]["ask"]("q2")
    gate.set()
    await s.extra["chat"].wait()
    assert "fresh" in await s.extra["hooks"]["new"]()
