"""HTTP client for the ops local API over a Unix socket. Imports nothing from core/."""
from __future__ import annotations

import os
from pathlib import Path

import httpx

DEFAULT_SOCKET = Path(__file__).resolve().parent.parent / "state" / "ops.sock"


class OpsClient:
    def __init__(self, socket_path: str | os.PathLike | None = None):
        self.socket_path = str(socket_path or os.environ.get("OPS_SOCKET") or DEFAULT_SOCKET)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=self.socket_path),
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
