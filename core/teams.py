"""MS Teams client, built on the tokens the Teams web client already holds (persistent Playwright profile).

- Chats and meetings (1:1, group, class chats): Teams' own chat service. The web client's
  api.spaces.skype.com token is exchanged at authsvc for a skype token, then
  {chatService}/v1/users/ME/conversations[/id/messages] is read (and written for sends).
  The web client's Graph token has no Chat.Read scope, so Graph cannot read chats.
- Class teams/channels ("space"/"topic" threads) also come from the chat service; Graph is used only for
  the user's profile and for sending to "<teamId>/<channelId>" channels.
- UI mode: last-resort chat-list preview scraping when no usable tokens are found.

Conversation ids: chat-service ids look like "19:...@thread.v2"; Graph channels use "<teamId>/<channelId>".
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Literal

import httpx

from core.auth import TEAMS_URL, AuthRequired
from core.models import Item
from core.text import html_to_text

log = logging.getLogger(__name__)
GRAPH = "https://graph.microsoft.com/v1.0"
AUTHZ_URL = "https://teams.microsoft.com/api/authsvc/v1.0/authz"
READ_THREAD_TYPES = {"chat", "meeting", "space", "topic"}  # chats, meeting chats, class teams, channels
MESSAGE_TYPES = {"RichText/Html", "Text", "RichText", "RichText/Media_Card"}
UI_SELECTORS = {
    "chat_item": '[data-tid="chat-list-item"], [role="treeitem"][data-item-type="chat"]',
    "chat_title": '[data-tid="chat-list-item-title"], [id^="title-chat-list-item"]',
    "chat_preview": '[data-tid="chat-list-item-preview"], [id^="preview-chat-list-item"]',
}
_TOKEN_JS = """(aud) => {
  const out = [];
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (!k || !k.toLowerCase().includes('accesstoken')) continue;
    try {
      const v = JSON.parse(localStorage.getItem(k));
      if (v && v.secret && (v.target || '').toLowerCase().includes(aud))
        out.push({secret: v.secret, expiresOn: v.expiresOn || v.extendedExpiresOn});
    } catch (e) {}
  }
  return out;
}"""
_ID_PART = r"[A-Za-z0-9:._@\-]+"
_CONV_RE = re.compile(rf"^{_ID_PART}(/{_ID_PART})?$")


def valid_conv_id(conv_id: str) -> bool:
    return bool(conv_id) and ".." not in conv_id and bool(_CONV_RE.match(conv_id))


def ui_key(title: str, preview: str) -> str:
    """Stable across processes (unlike hash())."""
    return "ui:" + hashlib.sha256(f"{title}\x00{preview}".encode()).hexdigest()[:16]


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _oid(from_url: str | None) -> str | None:
    m = re.search(r"8:orgid:([0-9a-fA-F-]+|[\w-]+)$", from_url or "")
    return m.group(1) if m else None


class Teams:
    def __init__(self, session, token_provider: Callable[[str], Awaitable[str | None]] | None = None):
        self.session = session
        self._token_provider = token_provider or self._token_from_browser
        self._tokens: dict[str, str] = {}
        self._skype: tuple[str, str, float] | None = None  # (token, chat service base, expires epoch)
        self._me: dict | None = None
        self._mode: str | None = None
        self._names: dict[str, str] = {}
        self._last_sender: dict[str, str] = {}

    # ---------- tokens ----------
    async def _token_from_browser(self, aud: str) -> str | None:
        ctx = await self.session.context("teams")
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        if "teams." not in page.url:
            await page.goto(TEAMS_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(8000)
        tokens = await page.evaluate(_TOKEN_JS, aud)
        now = time.time()
        valid = [t for t in tokens if not t.get("expiresOn") or float(t["expiresOn"]) > now + 120]
        if not valid:  # tokens expired: reload so the web app refreshes them silently
            await page.goto(TEAMS_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(8000)
            tokens = await page.evaluate(_TOKEN_JS, aud)
            valid = [t for t in tokens if not t.get("expiresOn") or float(t["expiresOn"]) > now + 120]
        valid.sort(key=lambda t: float(t.get("expiresOn") or 0), reverse=True)
        return valid[0]["secret"] if valid else None

    async def _token(self, aud: str) -> str:
        if aud not in self._tokens:
            tok = await self._token_provider(aud)
            if not tok:
                raise AuthRequired(f"teams: no {aud} token in the Teams web session")
            self._tokens[aud] = tok
        return self._tokens[aud]

    async def _skype_token(self) -> tuple[str, str]:
        if self._skype and self._skype[2] > time.time() + 300:
            return self._skype[0], self._skype[1]
        bearer = await self._token("api.spaces.skype.com")
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(AUTHZ_URL, headers={"Authorization": f"Bearer {bearer}"})
        if r.status_code in (401, 403):
            self._tokens.pop("api.spaces.skype.com", None)
            raise AuthRequired("teams: chat service token rejected")
        r.raise_for_status()
        d = r.json()
        tok = d["tokens"]["skypeToken"]
        base = d["regionGtms"]["chatService"].rstrip("/")
        self._skype = (tok, base, time.time() + float(d["tokens"].get("expiresIn", 3600)))
        return tok, base

    def _forget_tokens(self) -> None:
        self._tokens.clear()
        self._skype = None

    # ---------- http ----------
    async def _graph(self, method: str, path: str, **kw) -> dict:
        token = await self._token("graph.microsoft.com")
        async with httpx.AsyncClient(base_url=GRAPH, timeout=30, headers={"Authorization": f"Bearer {token}"}) as c:
            r = await c.request(method, path, **kw)
        if r.status_code == 401:
            self._forget_tokens()
            raise AuthRequired("teams: Graph token expired")
        r.raise_for_status()
        return r.json() if r.content else {}

    async def _chat(self, method: str, path: str, retry: bool = True, **kw) -> dict:
        tok, base = await self._skype_token()
        async with httpx.AsyncClient(timeout=30, headers={"Authentication": f"skypetoken={tok}"}) as c:
            r = await c.request(method, f"{base}/v1/users/ME{path}", **kw)
        if r.status_code == 401:
            self._forget_tokens()
            if retry:
                return await self._chat(method, path, retry=False, **kw)
            raise AuthRequired("teams: chat service session expired")
        r.raise_for_status()
        return r.json() if r.content else {}

    # ---------- mode ----------
    async def mode(self) -> Literal["graph", "ui"]:
        if self._mode:
            return self._mode  # type: ignore[return-value]
        try:
            await self.me()
            self._mode = "graph"
        except AuthRequired:
            raise
        except Exception:  # noqa: BLE001
            log.warning("Teams tokens unusable; using UI mode", exc_info=True)
            self._mode = "ui"
        return self._mode  # type: ignore[return-value]

    async def me(self) -> dict:
        if not self._me:
            self._me = await self._graph("GET", "/me")
        return self._me

    # ---------- read ----------
    @staticmethod
    def _conv_name(cv: dict) -> str:
        tp = cv.get("threadProperties") or {}
        if tp.get("topic") or tp.get("spaceThreadTopic"):
            return tp.get("topic") or tp["spaceThreadTopic"]
        if tp.get("productThreadType") == "OneToOneChat" or cv.get("id", "").endswith("@unq.gbl.spaces"):
            return "Direct message"
        return "Meeting chat" if tp.get("threadType") == "meeting" else "Group chat"

    async def _conversations(self) -> list[dict]:
        data = await self._chat("GET", "/conversations", params={"view": "msnp24Equivalent", "pageSize": 100})
        convs = data.get("conversations", [])
        for cv in convs:
            self._names[cv["id"]] = self._conv_name(cv)
        return convs

    def _chat_item(self, cv: dict, m: dict, me_id: str) -> Item:
        name = self._names.get(cv["id"]) or self._conv_name(cv)
        sender = m.get("imdisplayname") or "?"
        mentions = []
        try:
            mentions = json.loads((m.get("properties") or {}).get("mentions") or "[]")
        except ValueError:
            pass
        mentioned = any(str(x.get("mri", "")).endswith(me_id) for x in mentions)
        tp = cv.get("threadProperties") or {}
        one_to_one = tp.get("productThreadType") == "OneToOneChat" or cv["id"].endswith("@unq.gbl.spaces")
        kind = "teams_mention" if mentioned else "teams_dm" if one_to_one else "teams_post"
        if one_to_one:
            self._last_sender[cv["id"]] = sender
        return Item("teams", f"teams:{cv['id']}:{m['id']}", None, kind, f"{sender} in {name}"[:120], {
            "conv_id": cv["id"], "chat_name": name, "sender": sender, "ts": m.get("composetime"),
            "body_text": html_to_text(m.get("content") or "", limit=1500),
        })

    async def _recent_chats(self, since: datetime, me_id: str) -> list[Item]:
        items: list[Item] = []
        for cv in await self._conversations():
            tp = cv.get("threadProperties") or {}
            if tp.get("threadType") not in READ_THREAD_TYPES:
                continue  # notification/mention/call-log streams are skipped
            last = _parse((cv.get("lastMessage") or {}).get("composetime"))
            if not last or last < since:
                continue
            data = await self._chat("GET", f"/conversations/{cv['id']}/messages",
                                    params={"view": "msnp24Equivalent", "pageSize": 20})
            for m in data.get("messages", []):
                ts = _parse(m.get("composetime"))
                if (not ts or ts < since or m.get("messagetype") not in MESSAGE_TYPES
                        or _oid(m.get("from")) == me_id or not (m.get("content") or "").strip()):
                    continue
                items.append(self._chat_item(cv, m, me_id))
        return items

    async def recent(self, since: datetime) -> list[Item]:
        if await self.mode() == "ui":
            return await self._recent_ui()
        me_id = (await self.me())["id"]
        return await self._recent_chats(since, me_id)

    async def read(self, conv_id: str, limit: int = 30) -> list[dict]:
        if not valid_conv_id(conv_id):
            raise ValueError("invalid Teams conversation id")
        if await self.mode() == "ui":
            raise NotImplementedError("Reading a specific Teams conversation needs token access")
        if "/" in conv_id:
            team, ch = conv_id.split("/", 1)
            msgs = (await self._graph("GET", f"/teams/{team}/channels/{ch}/messages",
                                      params={"$top": min(limit, 50)}))["value"]
            return [{"sender": ((m.get("from") or {}).get("user") or {}).get("displayName"),
                     "text": html_to_text((m.get("body") or {}).get("content") or "", limit=3000),
                     "ts": m.get("createdDateTime")} for m in msgs]
        data = await self._chat("GET", f"/conversations/{conv_id}/messages",
                                params={"view": "msnp24Equivalent", "pageSize": min(limit, 50)})
        return [{"sender": m.get("imdisplayname"), "text": html_to_text(m.get("content") or "", limit=3000),
                 "ts": m.get("composetime")}
                for m in data.get("messages", []) if m.get("messagetype") in MESSAGE_TYPES]

    async def _recent_ui(self) -> list[Item]:
        ctx = await self.session.context("teams")
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(TEAMS_URL + "?#/conversations", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(8000)
        rows = page.locator(UI_SELECTORS["chat_item"])
        items = []
        for i in range(min(await rows.count(), 30)):
            row = rows.nth(i)
            title = (await row.locator(UI_SELECTORS["chat_title"]).first.inner_text()).strip()
            preview = (await row.locator(UI_SELECTORS["chat_preview"]).first.inner_text()).strip()
            items.append(Item("teams", f"teams:{ui_key(title, preview)}", None, "teams_post", title[:120],
                              {"conv_id": None, "chat_name": title, "sender": title, "body_text": preview[:500]}))
        return items

    async def describe(self, conv_id: str) -> str:
        """Human name of a chat or channel, shown on approval cards."""
        if not valid_conv_id(conv_id):
            raise ValueError("invalid Teams conversation id")
        if "/" in conv_id:
            team, ch = conv_id.split("/", 1)
            t = await self._graph("GET", f"/teams/{team}")
            c = await self._graph("GET", f"/teams/{team}/channels/{ch}")
            return f"channel {t.get('displayName', team)} › {c.get('displayName', ch)}"
        if conv_id not in self._names:
            await self._conversations()
        name = self._names.get(conv_id)
        if name is None:
            return f"unknown conversation {conv_id}"
        if name == "Direct message":
            if conv_id not in self._last_sender:
                me_id = (await self.me())["id"]
                data = await self._chat("GET", f"/conversations/{conv_id}/messages",
                                        params={"view": "msnp24Equivalent", "pageSize": 20})
                for m in data.get("messages", []):
                    if m.get("messagetype") in MESSAGE_TYPES and _oid(m.get("from")) != me_id:
                        self._last_sender[conv_id] = m.get("imdisplayname") or "?"
                        break
            return f"direct chat with {self._last_sender.get(conv_id, 'unknown')}"
        return f"group chat “{name}”" if name not in ("Group chat", "Meeting chat") else name.lower()

    # ---------- write (only via core.actions.execute) ----------
    async def send(self, conv_id: str, text: str) -> dict:
        if not valid_conv_id(conv_id):
            raise ValueError("invalid Teams conversation id")
        if await self.mode() == "ui":
            raise NotImplementedError("Sending Teams messages needs token access")
        if "/" in conv_id:
            team, ch = conv_id.split("/", 1)
            return await self._graph("POST", f"/teams/{team}/channels/{ch}/messages", json={"body": {"content": text}})
        me = await self.me()
        body = {"content": text, "messagetype": "Text", "contenttype": "text",
                "clientmessageid": str(int(time.time() * 1000)), "imdisplayname": me.get("displayName", "")}
        return await self._chat("POST", f"/conversations/{conv_id}/messages", json=body)
