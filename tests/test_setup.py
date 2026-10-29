import json
import stat

import httpx
import respx
from aiohttp.test_utils import TestClient, TestServer

from core.config import load_settings, update_env_file
from ui import setup
from ui.server import make_app
from ui.supervisor import OpsSupervisor, Prefs

H = {"Host": "127.0.0.1:8765", "X-JARVIS-UI": "1"}
TOKEN = "123456789:" + "A" * 30
API = f"https://api.telegram.org/bot{TOKEN}"


def test_update_env_file_keeps_comments_and_sets_mode(tmp_path):
    p = tmp_path / "secrets.env"
    p.write_text("# keep me\nOPS_BOT_TOKEN=old\n# COURSE_PREFIX=\nOTHER=1\n")
    update_env_file(p, {"OPS_BOT_TOKEN": "new", "USER_NAME": "Aisha"})
    assert p.read_text().splitlines() == ["# keep me", "OPS_BOT_TOKEN=new", "# COURSE_PREFIX=", "OTHER=1", "USER_NAME=Aisha"]
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_update_env_file_creates_and_rejects_newlines(tmp_path):
    p = tmp_path / "new" / "secrets.env"
    update_env_file(p, {"A": "1"})
    assert p.read_text() == "A=1\n"
    try:
        update_env_file(p, {"A": "x\nB=2"})
    except ValueError:
        pass
    else:
        raise AssertionError("newline accepted")


def test_status_needed_until_everything_is_set(tmp_path):
    p = tmp_path / "s.env"
    s = load_settings(p)
    assert setup.status(s)["needed"] is True and setup.status(s)["default_base"].startswith("https://")
    p.write_text(f"OPS_BOT_TOKEN={TOKEN}\nALLOWED_TELEGRAM_ID=5\nPOLITEMALL_USER=u\nPOLITEMALL_PASS=p\n")
    assert setup.status(load_settings(p))["needed"] is False


@respx.mock
async def test_check_token():
    assert (await setup.check_token("nonsense"))["ok"] is False
    respx.get(f"{API}/getMe").mock(return_value=httpx.Response(200, json={"ok": True, "result": {"username": "my_bot", "first_name": "Jarvis"}}))
    assert await setup.check_token(TOKEN) == {"ok": True, "username": "my_bot", "name": "Jarvis"}
    respx.get(f"{API}/getMe").mock(return_value=httpx.Response(401, json={"ok": False}))
    assert (await setup.check_token(TOKEN))["ok"] is False
    respx.get(f"{API}/getMe").mock(side_effect=httpx.ConnectError("down"))
    assert "internet" in (await setup.check_token(TOKEN))["error"]


@respx.mock
async def test_find_owner_uses_private_human_sender_only():
    route = respx.get(f"{API}/getUpdates")
    route.mock(return_value=httpx.Response(200, json={"ok": True, "result": []}))
    r = await setup.find_owner(TOKEN)
    assert r["ok"] is False and r["waiting"] is True
    route.mock(return_value=httpx.Response(200, json={"ok": True, "result": [
        {"message": {"chat": {"type": "private"}, "from": {"id": 77, "first_name": "Aisha", "is_bot": False}}},
        {"message": {"chat": {"type": "group"}, "from": {"id": 88, "first_name": "Rando", "is_bot": False}}},
    ]}))
    assert await setup.find_owner(TOKEN) == {"ok": True, "id": 77, "name": "Aisha"}
    route.mock(return_value=httpx.Response(409, json={"ok": False}))
    assert "already running" in (await setup.find_owner(TOKEN))["error"]


def test_save_validates_then_writes(tmp_path):
    p = tmp_path / "secrets.env"
    good = {"token": TOKEN, "owner_id": 77, "school_user": "s123", "school_pass": "pw", "school_base": "",
            "name": "Aisha", "address": ""}
    assert setup.save(p, {**good, "token": "bad"})["ok"] is False
    assert setup.save(p, {**good, "owner_id": 0})["ok"] is False
    assert setup.save(p, {**good, "school_pass": ""})["ok"] is False
    assert setup.save(p, {**good, "school_base": "http://insecure"})["ok"] is False
    assert setup.save(p, {**good, "school_user": "a\nB=1"})["ok"] is False and not p.exists()
    assert setup.save(p, good) == {"ok": True}
    s = load_settings(p)
    assert (s.ops_bot_token, s.allowed_telegram_id, s.politemall_user, s.user_name, s.address_as) == (TOKEN, 77, "s123", "Aisha", "Sir")
    assert s.politemall_base == "https://politemall.polite.edu.sg"


async def test_endpoints_and_restart_after_save(tmp_path, env_file):
    p = tmp_path / "fresh.env"
    s = load_settings(p)
    s.state_dir.mkdir(parents=True, exist_ok=True)
    restarts = []
    sup = OpsSupervisor(s, Prefs(s.state_dir / "ui.json"))
    c = TestClient(TestServer(make_app(s, sup, port=8765, on_restart=lambda: restarts.append(1), env_file=p)))
    await c.start_server()
    try:
        assert (await (await c.get("/api/setup", headers=H)).json())["needed"] is True
        post = lambda a, b: c.post(f"/api/setup/{a}", headers=H, data=json.dumps(b))  # noqa: E731
        assert (await post("nonsense", {})).status == 404
        r = await (await post("save", {"token": "bad"})).json()
        assert r["ok"] is False and not restarts
        ok = await (await post("save", {"token": TOKEN, "owner_id": 5, "school_user": "u", "school_pass": "p"})).json()
        assert ok["ok"] is True and p.exists()
        import asyncio
        await asyncio.sleep(1)
        assert restarts == [1]
        assert (await c.post("/api/setup/save", data="{}")).status == 403  # CSRF header still required
    finally:
        await c.close()
