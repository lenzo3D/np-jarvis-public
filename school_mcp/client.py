"""HTTP client for the ops local API. Imports nothing from core/.

How to reach Ops is decided on every request from what Ops wrote when it started (the "endpoint file", see
core/localapi.py), so a restarted Ops with a new port or token is picked up without restarting this process:

1. OPS_ENDPOINT (path to the endpoint file), if set;
2. OPS_SOCKET (a Unix socket path), if set: how the server deployment is wired;
3. state/ops.api.json next to the repo, if it exists;
4. state/ops.sock.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

STATE = Path(__file__).resolve().parent.parent / "state"
DEFAULT_SOCKET = STATE / "ops.sock"
DEFAULT_ENDPOINT = STATE / "ops.api.json"


def _read_endpoint(path: str | os.PathLike) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("transport") in ("unix", "tcp") else None


def resolve_endpoint(socket_path=None, endpoint_file=None, environ=None) -> dict:
    """Pick the transport. Returns {"transport": "unix", "path": ...} or {"transport": "tcp", "host", "port", "token"}."""
    environ = os.environ if environ is None else environ
    if socket_path:
        return {"transport": "unix", "path": str(socket_path)}
    endpoint_file = endpoint_file or environ.get("OPS_ENDPOINT")
    if endpoint_file:
        ep = _read_endpoint(endpoint_file)
        if ep is None:
            raise RuntimeError(f"JARVIS Ops is not running (no usable endpoint file at {endpoint_file}).")
        return ep
    if environ.get("OPS_SOCKET"):
        return {"transport": "unix", "path": environ["OPS_SOCKET"]}
    ep = _read_endpoint(DEFAULT_ENDPOINT)
    return ep or {"transport": "unix", "path": str(DEFAULT_SOCKET)}


class OpsClient:
    def __init__(self, socket_path: str | os.PathLike | None = None, endpoint_file: str | os.PathLike | None = None):
        self.socket_path = str(socket_path) if socket_path else None
        self.endpoint_file = endpoint_file

    def _client(self) -> httpx.AsyncClient:
        ep = resolve_endpoint(self.socket_path, self.endpoint_file)
        if ep["transport"] == "tcp":
            return httpx.AsyncClient(base_url=f"http://{ep.get('host', '127.0.0.1')}:{int(ep['port'])}",
                                     headers={"Authorization": f"Bearer {ep['token']}"}, timeout=120,
                                     trust_env=False)  # never send localhost traffic through a proxy
        return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(ep["path"])),
                                 base_url="http://ops", timeout=120)

    @staticmethod
    def _result(r: httpx.Response):
        try:
            data = r.json()
        except ValueError:
            data = {"error": r.text}
        if r.status_code >= 400 and not isinstance(data, dict):
            data = {"error": data}
        return data

    async def get(self, path: str, **params):
        async with self._client() as c:
            return self._result(await c.get(path, params={k: v for k, v in params.items() if v is not None}))

    async def post(self, path: str, json: dict):
        async with self._client() as c:
            return self._result(await c.post(path, json=json))
