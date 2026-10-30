"""Outlook (school email) reader, built on the tokens the signed-in Outlook web app already holds.

Same idea as core/teams.py: a persistent Playwright profile logged in to outlook.office.com keeps access tokens in
the page's storage. We read those and call the mailbox over HTTPS. Read-only: nothing here can send, move or delete mail.

Which API accepts which token depends on how the school's Microsoft 365 is set up, so we try the known ones in order
(Microsoft Graph, then Outlook's own REST API) and remember what works. probe() reports, without any message
content, exactly what was tried and what came back; it is what the panel's "Check Outlook access" button runs.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx

from core.auth import OUTLOOK_URL, AuthRequired, is_login_page, outlook_signed_in
from core.mail import Mail
from core.text import html_to_text

log = logging.getLogger(__name__)

ENDPOINTS = [
    {"name": "graph", "url": "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages", "style": "graph"},
    {"name": "outlook-rest", "url": "https://outlook.office.com/api/v2.0/me/mailfolders/inbox/messages", "style": "rest"},
    {"name": "outlook365-rest", "url": "https://outlook.office365.com/api/v2.0/me/mailfolders/inbox/messages", "style": "rest"},
]
SELECT = {
    "graph": "id,subject,bodyPreview,receivedDateTime,from,isRead,hasAttachments,webLink,importance,conversationId,"
             "inferenceClassification,body",
    "rest": "Id,Subject,BodyPreview,ReceivedDateTime,From,IsRead,HasAttachments,WebLink,Importance,ConversationId,"
            "InferenceClassification,Body",
}
TOKEN_REUSE_MARGIN = 120
_TOKEN_JS = """async () => {
  // Newer Microsoft sign-in code (msal-browser 4.x) keeps its token cache in localStorage ENCRYPTED: each entry is
  // {id, nonce, data}, AES-GCM with a key derived (HKDF-SHA256, salt = nonce) from the base key stored in the
  // "msal.cache.encryption" cookie. The page can read both, so we decrypt here, in the page, exactly as the app does.
  const out = [];
  const diag = {entries: 0, plain: 0, encrypted: 0, decrypted: 0, worked_with: {}, has_cookie_key: false, errors: []};
  const b64 = (s) => { s = String(s).replace(/-/g, '+').replace(/_/g, '/'); while (s.length % 4) s += '=';
    const bin = atob(s); const a = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) a[i] = bin.charCodeAt(i); return a; };
  let enc = null;
  const c = document.cookie.split('; ').find((x) => x.startsWith('msal.cache.encryption='));
  if (c) { try { enc = JSON.parse(decodeURIComponent(c.slice('msal.cache.encryption='.length))); } catch (e) { diag.errors.push('cookie-parse'); } }
  let base = null;
  if (enc && enc.key) {
    try { base = await crypto.subtle.importKey('raw', b64(enc.key), 'HKDF', false, ['deriveKey']); diag.has_cookie_key = true; }
    catch (e) { diag.errors.push('key-import'); }
  }
  const clientIds = [];
  for (let i = 0; i < localStorage.length; i++) { const m = /^msal\.\d+\.token\.keys\.(.+)$/.exec(localStorage.key(i) || ''); if (m) clientIds.push(m[1]); }
  const decrypt = async (entry, contexts) => {
    for (const [label, ctx] of contexts) {
      try {
        const key = await crypto.subtle.deriveKey({name: 'HKDF', salt: b64(entry.nonce), hash: 'SHA-256', info: new TextEncoder().encode(ctx)},
          base, {name: 'AES-GCM', length: 256}, false, ['decrypt']);
        const pt = await crypto.subtle.decrypt({name: 'AES-GCM', iv: new Uint8Array(12)}, key, b64(entry.data));
        return {text: new TextDecoder().decode(pt), label};
      } catch (e) { /* wrong context: try the next */ }
    }
    return null;
  };
  for (const store of [localStorage, sessionStorage]) {
    for (let i = 0; i < store.length; i++) {
      const k = store.key(i);
      if (!k || !k.toLowerCase().includes('accesstoken')) continue;
      diag.entries++;
      let v; try { v = JSON.parse(store.getItem(k)); } catch (e) { continue; }
      if (v && v.secret) { diag.plain++; out.push({secret: v.secret, target: v.target || '', expiresOn: v.expiresOn || v.extendedExpiresOn || ''}); continue; }
      if (!(v && v.nonce && v.data)) continue;
      diag.encrypted++;
      if (!base) continue;
      // Which string the app mixes into the key is not documented, so try the plausible ones and report which worked.
      const rest = k.replace(/^msal\.\d+\|/, '');
      const home = (/([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\.[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})/i.exec(k) || [])[1];
      const guids = k.match(/[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/gi) || [];
      const contexts = [...clientIds.map((id) => ['clientId', id]), ['empty', ''], ['key', k], ['rest', rest], ['cookieId', (enc && enc.id) || ''],
                        ...(home ? [['home', home]] : []), ...guids.map((g) => ['guid', g]),
                        ...k.split('|').map((x) => ['pipe', x]), ...rest.split('-').map((x) => ['dash', x]), ...rest.split('.').map((x) => ['dot', x])];      const r = await decrypt(v, contexts);
      if (!r) continue;
      diag.decrypted++; diag.worked_with[r.label] = (diag.worked_with[r.label] || 0) + 1;
      let e = null;
      try { e = JSON.parse(r.text); } catch (err) { if (/^ey/.test(r.text)) e = {secret: r.text}; }
      if (e && e.secret) out.push({secret: e.secret, target: e.target || '', expiresOn: e.expiresOn || e.extendedExpiresOn || ''});
    }
  }
  return {tokens: out, diag};
}"""
_STORAGE_JS = """() => {
  const names = (s) => { const out = []; for (let i = 0; i < s.length; i++) { const k = s.key(i) || ''; if (/token|msal|auth|login|account/i.test(k)) out.push(k.slice(0, 70)); } return out.slice(0, 25); };
  return {origin: location.origin, local_keys: localStorage.length, session_keys: sessionStorage.length,
          local_matches: names(localStorage), session_matches: names(sessionStorage), cookies: document.cookie.split(';').map(c => c.split('=')[0].trim()).filter(Boolean).slice(0, 30)};
}"""
_OWA_SERVICE_JS = """async () => {
  const m = document.cookie.match(/X-OWA-CANARY=([^;]+)/);
  try {
    const r = await fetch('/owa/service.svc?action=GetFolder&app=Mail', {method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/json; charset=utf-8', 'Action': 'GetFolder', 'X-OWA-CANARY': m ? m[1] : ''},
      body: JSON.stringify({__type: 'GetFolderJsonRequest:#Exchange', Header: {__type: 'JsonRequestHeaders:#Exchange',
        RequestServerVersion: 'Exchange2013'}, Body: {__type: 'GetFolderRequest:#Exchange',
        FolderShape: {__type: 'FolderResponseShape:#Exchange', BaseShape: 'IdOnly'},
        FolderIds: [{__type: 'DistinguishedFolderId:#Exchange', Id: 'inbox'}]}})});
    return {status: r.status, canary: !!m};
  } catch (e) { return {status: 0, canary: !!m, error: String(e).slice(0, 80)}; }
}"""


class MailUnavailable(Exception):
    """The mailbox could not be read with any known method. The message says why (never includes mail content)."""


def jwt_claims(token: str) -> dict:
    """The readable part of an access token (audience, scopes, expiry). Never the token itself."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}
    return {"aud": data.get("aud"), "scp": data.get("scp") or "", "exp": data.get("exp"), "appid": data.get("appid") or data.get("azp")}


def _lower(d):
    return {k.lower(): _lower(v) if isinstance(v, dict) else v for k, v in d.items()} if isinstance(d, dict) else d


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def mail_from_json(raw: dict) -> Mail:
    """One message from Microsoft Graph (camelCase) or Outlook REST v2.0 (PascalCase)."""
    d = _lower(raw)
    addr = ((d.get("from") or {}).get("emailaddress") or {})
    body = d.get("body") or {}
    content = body.get("content") or ""
    text = html_to_text(content, limit=20000) if str(body.get("contenttype", "")).lower() == "html" else content[:20000]
    return Mail(id=str(d.get("id") or ""), received=_parse_dt(d.get("receiveddatetime")),
                sender_name=(addr.get("name") or addr.get("address") or "").strip(), sender_addr=(addr.get("address") or "").strip(),
                subject=(d.get("subject") or "(no subject)").strip(), preview=(d.get("bodypreview") or "").strip()[:300],
                body=text, is_read=bool(d.get("isread")), has_attachments=bool(d.get("hasattachments")),
                importance=str(d.get("importance") or "normal").lower(), web_link=d.get("weblink") or "",
                conv_id=str(d.get("conversationid") or ""), inbox_tab=str(d.get("inferenceclassification") or "").lower())


def _best_first(ep: dict, tokens: list[dict]) -> list[dict]:
    """Tokens issued for this endpoint's host first; the rest after (they usually just get a 401)."""
    host = ep["url"].split("/")[2]
    return sorted(tokens, key=lambda t: host not in str(t["claims"].get("aud") or ""))


class Outlook:
    def __init__(self, session, token_provider: Callable[[], Awaitable[list[dict]]] | None = None,
                 strategy: str | None = None, timeout: float = 30.0):
        self.session = session
        self._provider = token_provider or self._browser_tokens
        self.strategy = strategy  # name of the endpoint that worked last time
        self.timeout = timeout
        self._cache: tuple[list[dict], float] | None = None
        self.token_diag: dict = {}  # how many token entries the page held and how many we could open (no secrets)

    # ---------- tokens ----------
    async def _browser_tokens(self) -> list[dict]:
        ctx = await self.session.context("outlook")
        page = await ctx.new_page()
        try:
            await page.goto(OUTLOOK_URL, wait_until="domcontentloaded", timeout=60000)
            if not await outlook_signed_in(page, 25):
                raise AuthRequired("outlook: signed out")
            await page.wait_for_timeout(4000)  # let the web app refresh its tokens
            result = await page.evaluate(_TOKEN_JS)
            self.token_diag = result.get("diag") or {}
            return result.get("tokens") or []
        finally:
            await page.close()

    async def _tokens(self, fresh: bool = False) -> list[dict]:
        now = time.time()
        if self._cache and not fresh and self._cache[1] > now:
            return self._cache[0]
        raw = await self._provider()
        tokens = []
        for t in raw:
            claims = jwt_claims(t.get("secret", ""))
            exp = float(claims.get("exp") or t.get("expiresOn") or 0)
            if exp and exp < now + TOKEN_REUSE_MARGIN:
                continue
            tokens.append({"secret": t["secret"], "claims": claims, "exp": exp})
        tokens.sort(key=lambda t: t["exp"], reverse=True)
        self._cache = (tokens, min((t["exp"] for t in tokens), default=now) - TOKEN_REUSE_MARGIN if tokens else now + 30)
        return tokens

    # ---------- http ----------
    async def _get(self, endpoint: dict, token: str, params: dict) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            return await c.get(endpoint["url"], params=params, headers={
                "Authorization": f"Bearer {token}", "Accept": "application/json",
                "Prefer": 'outlook.body-content-type="text"'})

    # ---------- diagnostics ----------
    async def probe(self, progress=None, step_timeout: float = 150.0) -> dict:
        """What can this login read? No message content is returned, only statuses and token audiences/scopes.
        `progress(text)` is called before each step, and every step has a time limit, so a stall is visible and the
        report is always returned (with the stalled step named in report["errors"])."""
        say = progress or (lambda _m: None)
        report = {"tokens": [], "attempts": [], "strategy": None, "owa_service": None, "storage": None, "token_cache": None,
                  "errors": []}
        say("Step 1 of 3: reading the sign-in tokens that Outlook keeps in the browser (up to 2 minutes)...")
        try:
            tokens = await asyncio.wait_for(self._tokens(fresh=True), step_timeout)
        except asyncio.TimeoutError:
            tokens = []
            report["errors"].append("step 1 (reading tokens) did not finish in time")
        except AuthRequired:
            raise
        except Exception as e:  # noqa: BLE001 - report it instead of losing the rest of the diagnosis
            tokens = []
            report["errors"].append(f"step 1 (reading tokens) failed: {type(e).__name__}")
        report["token_cache"] = self.token_diag or None
        say(f"Step 2 of 3: trying to read the inbox with {len(tokens)} token(s)...")
        for t in tokens:
            c = t["claims"]
            report["tokens"].append({"aud": c.get("aud"), "scopes": sorted(str(c.get("scp") or "").split()),
                                     "minutes_left": max(0, int((t["exp"] - time.time()) / 60))})
        for ep in ENDPOINTS:
            for t in _best_first(ep, tokens):
                try:
                    r = await self._get(ep, t["secret"], {"$top": 1})
                except httpx.HTTPError as e:
                    report["attempts"].append({"endpoint": ep["name"], "aud": t["claims"].get("aud"), "status": 0, "ok": False,
                                               "error": type(e).__name__})
                    continue
                err = ""
                if r.status_code != 200:
                    try:
                        err = str((r.json().get("error") or {}).get("code") or "")[:60]
                    except ValueError:
                        err = ""
                report["attempts"].append({"endpoint": ep["name"], "aud": t["claims"].get("aud"), "status": r.status_code,
                                           "ok": r.status_code == 200, "error": err})
                if r.status_code == 200 and report["strategy"] is None:
                    report["strategy"] = ep["name"]
        say("Step 3 of 3: checking Outlook's own web service (up to 2 minutes)...")
        try:
            report["owa_service"], report["storage"] = await asyncio.wait_for(self._probe_page(), step_timeout)
        except asyncio.TimeoutError:
            report["errors"].append("step 3 (Outlook web service) did not finish in time")
        if report["strategy"]:
            self.strategy = report["strategy"]
        return report

    async def _probe_page(self) -> tuple[dict | None, dict | None]:
        """Diagnostics only. Opens Outlook once and reports (a) whether its own service endpoint answers this login
        (not used to read mail yet) and (b) the NAMES, never values, of what the page keeps in storage and cookies, which
        tells us where its tokens live."""
        try:
            ctx = await self.session.context("outlook")
        except Exception:  # noqa: BLE001 - e.g. no browser in tests
            return None, None
        page = await ctx.new_page()
        try:
            await page.goto(OUTLOOK_URL, wait_until="domcontentloaded", timeout=60000)
            signed_in = await outlook_signed_in(page, 25)
            await page.wait_for_timeout(3000)
            storage = await page.evaluate(_STORAGE_JS)
            storage["signed_in"], storage["url_host"] = signed_in, page.url.split("/")[2] if "//" in page.url else ""
            return await page.evaluate(_OWA_SERVICE_JS), storage
        except Exception as e:  # noqa: BLE001
            return {"status": 0, "error": type(e).__name__}, None
        finally:
            await page.close()

    # ---------- reading ----------
    async def recent(self, since: datetime | None = None, limit: int = 40) -> list[Mail]:
        """Newest inbox messages (bodies as plain text). Raises MailUnavailable when no method works."""
        order = sorted(ENDPOINTS, key=lambda e: e["name"] != self.strategy)
        last = "no tokens found in the Outlook web session"
        for attempt in (0, 1):
            tokens = await self._tokens(fresh=attempt == 1)
            if not tokens:
                d = self.token_diag
                last = ("Outlook keeps its tokens encrypted and none could be opened" if d.get("encrypted") and not d.get("decrypted")
                        else "no access tokens found in the Outlook web session")
                continue
            for ep in order:
                for t in _best_first(ep, tokens):
                    params = self._params(ep, since, limit)
                    try:
                        r = await self._get(ep, t["secret"], params)
                    except httpx.HTTPError as e:
                        last = f"{ep['name']}: {type(e).__name__}"
                        continue
                    if r.status_code == 200:
                        self.strategy = ep["name"]
                        return [m for m in (mail_from_json(x) for x in r.json().get("value", [])) if m.id]
                    last = f"{ep['name']}: HTTP {r.status_code}"
        raise MailUnavailable(f"Could not read the mailbox ({last}). Run Check Outlook access in Logins & Tools.")

    @staticmethod
    def _params(ep: dict, since: datetime | None, limit: int) -> dict:
        graph = ep["style"] == "graph"
        field = "receivedDateTime" if graph else "ReceivedDateTime"
        params = {"$top": max(1, min(limit, 100)), "$select": SELECT[ep["style"]], "$orderby": f"{field} desc"}
        if since:
            params["$filter"] = f"{field} ge {since.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
        return params
