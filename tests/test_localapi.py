"""The local API on both transports, and the client choosing from what the server wrote."""
import asyncio
import dataclasses
import json
import os
import socket
import sys
import tempfile
from datetime import datetime, timezone

from pathlib import Path

import httpx
import pytest

from core import localapi
from core.config import load_settings
from core.models import Course
from ops import api
from ops.watcher import Services
from school_mcp.client import OpsClient, resolve_endpoint
from tests.fakes import Clock, FakeBS, FakeNotifier
from ui.supervisor import ops_alive

NOW = datetime(2026, 10, 26, 2, 0, tzinfo=timezone.utc)
HAS_UNIX = hasattr(socket, "AF_UNIX") and sys.platform != "win32"


@pytest.fixture
def services(db, env_file):
    settings = load_settings(env_file)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    bs = FakeBS()
    bs.course_list = [Course(1001, "INV2101", "Investments")]
    return Services(settings=settings, db=db, sessions=None, bs=bs, notifier=FakeNotifier(), clock=Clock(NOW))


def test_endpoint_file_roundtrip_and_bad_files(tmp_path):
    p = tmp_path / "s" / "ops.api.json"
    localapi.write_endpoint(p, {"transport": "tcp", "host": "127.0.0.1", "port": 1, "token": "t"})
    assert localapi.read_endpoint(p)["port"] == 1
    if sys.platform != "win32":
        assert p.stat().st_mode & 0o777 == 0o600
    p.write_text("{not json", encoding="utf-8")
    assert localapi.read_endpoint(p) is None
    p.write_text(json.dumps({"transport": "carrier-pigeon"}), encoding="utf-8")
    assert localapi.read_endpoint(p) is None
    assert localapi.read_endpoint(tmp_path / "missing") is None


def test_token_is_random():
    assert localapi.new_token() != localapi.new_token() and len(localapi.new_token()) >= 32


async def test_tcp_requires_the_token_and_client_follows_the_endpoint_file(services):
    runner = await api.start(services, transport="tcp")
    try:
        ep_path = localapi.endpoint_path(services.settings)
        ep = localapi.read_endpoint(ep_path)
        assert ep["transport"] == "tcp" and ep["host"] == "127.0.0.1" and ep["port"] > 0 and ep["token"]
        base = f"http://127.0.0.1:{ep['port']}"
        async with httpx.AsyncClient(trust_env=False) as c:
            assert (await c.get(f"{base}/courses")).status_code == 401
            assert (await c.get(f"{base}/courses", headers={"Authorization": "Bearer wrong"})).status_code == 401
            assert (await c.get(f"{base}/health", headers={"Authorization": f"Bearer {ep['token']}"})).status_code == 200
        out = await OpsClient(endpoint_file=ep_path).get("/courses/find", q="investments")
        assert out == [{"ou": 1001, "code": "INV2101", "name": "Investments"}]
        # the checks are blocking calls, as in the panel; Ops (this server) must keep running meanwhile, so use a thread
        assert await asyncio.to_thread(localapi.endpoint_alive, ep)
        assert await asyncio.to_thread(ops_alive, services.settings)
        assert not await asyncio.to_thread(localapi.endpoint_alive, {**ep, "token": "nope"})
    finally:
        await runner.cleanup()
    assert not localapi.endpoint_alive(ep)  # nothing listening any more
    api.stop_cleanup(services)
    assert not ep_path.exists() and not ops_alive(services.settings)


async def test_a_restarted_ops_with_a_new_port_and_token_is_picked_up(services):
    client = OpsClient(endpoint_file=localapi.endpoint_path(services.settings))
    for _ in range(2):
        runner = await api.start(services, transport="tcp")
        try:
            assert (await client.get("/courses"))[0]["code"] == "INV2101"
        finally:
            await runner.cleanup()


@pytest.mark.skipif(not HAS_UNIX, reason="Unix sockets")
async def test_unix_transport_still_works_and_is_written_to_the_endpoint_file(services):
    short = tempfile.mkdtemp(prefix="jv")  # unix socket paths are limited to ~100 characters
    services.settings = dataclasses.replace(services.settings, ops_socket=Path(short) / "ops.sock")
    runner = await api.start(services, transport="unix")
    try:
        ep = localapi.read_endpoint(localapi.endpoint_path(services.settings))
        assert ep == {"transport": "unix", "path": str(services.settings.ops_socket)}
        assert (await OpsClient(endpoint_file=localapi.endpoint_path(services.settings)).get("/courses"))[0]["ou"] == 1001
        assert (await OpsClient(services.settings.ops_socket).get("/courses"))[0]["ou"] == 1001  # explicit socket path
        assert await asyncio.to_thread(ops_alive, services.settings)
    finally:
        await runner.cleanup()


def test_client_precedence(tmp_path):
    ep = tmp_path / "ep.json"
    ep.write_text(json.dumps({"transport": "tcp", "host": "127.0.0.1", "port": 9, "token": "t"}), encoding="utf-8")
    assert resolve_endpoint(socket_path="/s.sock", environ={"OPS_ENDPOINT": str(ep)})["transport"] == "unix"
    assert resolve_endpoint(environ={"OPS_ENDPOINT": str(ep), "OPS_SOCKET": "/s.sock"})["transport"] == "tcp"
    assert resolve_endpoint(environ={"OPS_SOCKET": "/s.sock"}) == {"transport": "unix", "path": "/s.sock"}
    with pytest.raises(RuntimeError, match="not running"):
        resolve_endpoint(environ={"OPS_ENDPOINT": str(tmp_path / "nope.json")})
    assert resolve_endpoint(environ={})["transport"] in ("unix", "tcp")  # default location


def test_client_and_server_agree_on_the_file_format(tmp_path):
    p = tmp_path / "e.json"
    localapi.write_endpoint(p, {"transport": "tcp", "host": "127.0.0.1", "port": 5, "token": "abc"})
    assert resolve_endpoint(endpoint_file=p, environ={}) == localapi.read_endpoint(p)


def test_chat_agent_hands_windows_the_endpoint_file(env_file):
    from ops.chat_agent import _mcp_config_json

    s = load_settings(env_file)
    win = json.loads(_mcp_config_json(s, windows=True))["mcpServers"]["school"]["env"]
    mac = json.loads(_mcp_config_json(s, windows=False))["mcpServers"]["school"]["env"]
    assert win["OPS_ENDPOINT"] == str(localapi.endpoint_path(s)) and win["PYTHONUTF8"] == "1"
    assert "OPS_ENDPOINT" not in mac and mac["OPS_SOCKET"] == str(s.ops_socket)


def test_replace_retries_while_a_client_holds_the_file(tmp_path, monkeypatch):
    real, calls = os.replace, []

    def flaky(a, b):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("in use")
        return real(a, b)

    monkeypatch.setattr(os, "replace", flaky)
    localapi.write_endpoint(tmp_path / "e.json", {"transport": "unix", "path": "/s"})
    assert len(calls) == 3 and localapi.read_endpoint(tmp_path / "e.json")["path"] == "/s"
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError("always")))
    with pytest.raises(PermissionError):
        localapi._replace_with_retry(tmp_path / "a", tmp_path / "b", seconds=0.1, sleep=lambda s: None)


def test_clear_endpoint_never_raises(tmp_path, monkeypatch):
    p = tmp_path / "e.json"
    p.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(type(p), "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(PermissionError()))
    localapi.clear_endpoint(p)


def test_transport_setting_is_validated_and_drives_the_mcp_env(tmp_path, env_file):
    with env_file.open("a", encoding="utf-8") as fh:
        fh.write("OPS_API_TRANSPORT=tcp\n")
    s = load_settings(env_file)
    from ops.chat_agent import _mcp_config_json
    env = json.loads(_mcp_config_json(s, windows=False))["mcpServers"]["school"]["env"]
    assert s.ops_api_transport == "tcp" and env["OPS_ENDPOINT"] == str(localapi.endpoint_path(s))
    with env_file.open("a", encoding="utf-8") as fh:
        fh.write("OPS_API_TRANSPORT=pipe\n")
    with pytest.raises(ValueError, match="tcp or unix"):
        load_settings(env_file)
