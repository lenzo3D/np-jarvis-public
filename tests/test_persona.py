from ops.claude_runner import load_prompt


def test_chat_agent_persona(env_file):
    from core.config import load_settings
    from ops import persona
    s = load_settings(env_file)
    rules = persona.rules(s)
    assert "Sir" in rules and "J.A.R.V.I.S." in rules and "British" in rules
    import dataclasses
    s = dataclasses.replace(s, address_as="Boss", user_name="Aisha")
    assert "Boss" in persona.rules(s) and "Sir" not in persona.rules(s)
    assert persona.fields(s)["user"] == "Aisha"


def test_briefing_and_summary_persona(env_file):
    from core.config import load_settings
    from ops import persona
    f = persona.fields(load_settings(env_file))
    b = load_prompt("briefing", **f)
    assert "Good morning, Sir" in b and "{{" not in b
    g = load_prompt("group_summary", group="G", kind="class", **f)
    assert "Sir" in g and "{{address}}" not in g


async def test_mfa_prompt_addresses_sir():
    from types import SimpleNamespace
    from ops.bot import Notifier
    sent = []

    class Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append(text)
            return SimpleNamespace(message_id=1)

    await Notifier(Bot(), 1).mfa_prompt("brightspace", "47")
    assert "Sir" in sent[0] and "47" in sent[0]
    sent.clear()
    await Notifier(Bot(), 1, "Boss").mfa_prompt("brightspace", "47")
    assert sent[0].startswith("Boss,")


async def test_agent_acknowledgement_and_reset(db, env_file):
    from core.config import load_settings
    from ops.chat_agent import ChatAgent
    from ops.watcher import Services
    from tests.fakes import Clock, FakeBS, FakeNotifier

    s = Services(settings=load_settings(env_file), db=db, sessions=None, bs=FakeBS(), notifier=FakeNotifier(),
                 clock=Clock())

    async def runner(prompt, session_id):
        yield "result", "Done, Sir."

    a = ChatAgent(s, runner=runner)
    await a.handle("hi")
    await a.wait()
    assert "Sir" in s.notifier.messages[0][0]
    assert "Sir" in a.reset()


def test_semester_start_reminder_in_briefing_facts():
    import asyncio
    from datetime import datetime, timezone
    from core.config import load_settings
    from core.db import DB
    from ops.briefing import build_briefing_input
    from ops.watcher import Services
    from tests.fakes import Clock, FakeBS, FakeNotifier
    import tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp())
    env = tmp / "s.env"
    env.write_text("ALLOWED_TELEGRAM_ID=1\nOPS_BOT_TOKEN=t\nPOLITEMALL_BASE=https://x\nPOLITEMALL_USER=u\n"
                   f"POLITEMALL_PASS=p\nSTATE_DIR={tmp}/state\nWORKSPACE_DIR={tmp}/ws\n")
    db = DB(tmp / "t.db"); db.init()
    s = Services(settings=load_settings(env), db=db, sessions=None, bs=FakeBS(), notifier=FakeNotifier(),
                 clock=Clock())
    text = asyncio.run(build_briefing_input(s, datetime(2026, 10, 27, 23, 0, tzinfo=timezone.utc)))
    assert "SEMESTER_START" in text
    env.write_text(env.read_text() + "SEMESTER_START=2026-10-19\n")
    s.settings = load_settings(env)
    text = asyncio.run(build_briefing_input(s, datetime(2026, 10, 27, 23, 0, tzinfo=timezone.utc)))
    assert "SEMESTER_START" not in text
