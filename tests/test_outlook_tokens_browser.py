"""Runs the real token-reading JavaScript in Chromium against a token cache encrypted the way msal-browser 4.x does it
(AES-GCM, key derived with HKDF-SHA256 from the base key in the msal.cache.encryption cookie)."""
import glob
import json

import pytest

from core.outlook import _TOKEN_JS

playwright_async = pytest.importorskip("playwright.async_api")

CLIENT = "9199bf20-a13f-4107-85dc-02114787ef48"
HOME = "17008ba6-22c9-403f-ba91-2645210d25c1.cba9e115-3016-4462-a1ab-a5000000000a"
SEED = """async ({context, withCookie}) => {
  const b64u = (a) => btoa(String.fromCharCode(...a)).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
  const raw = crypto.getRandomValues(new Uint8Array(32));
  const base = await crypto.subtle.importKey('raw', raw, 'HKDF', false, ['deriveKey']);
  const encrypt = async (text, ctx) => {
    const nonce = crypto.getRandomValues(new Uint8Array(16));
    const key = await crypto.subtle.deriveKey({name: 'HKDF', salt: nonce, hash: 'SHA-256', info: new TextEncoder().encode(ctx)}, base, {name: 'AES-GCM', length: 256}, false, ['encrypt']);
    const data = new Uint8Array(await crypto.subtle.encrypt({name: 'AES-GCM', iv: new Uint8Array(12)}, key, new TextEncoder().encode(text)));
    return {id: 'cookie-id-1', nonce: b64u(nonce), data: b64u(data), lastUpdatedAt: String(Date.now())};
  };
  if (withCookie) document.cookie = 'msal.cache.encryption=' + encodeURIComponent(JSON.stringify({id: 'cookie-id-1', key: b64u(raw)})) + '; path=/';
  localStorage.setItem('msal.3.token.keys.' + context.client, JSON.stringify({accessToken: []}));
  const entries = {
    ['msal.3|' + context.home + '|login.windows.net|accesstoken|' + context.client + '|realm|https://outlook.office.com/.default']: {secret: 'TOKEN-OUTLOOK', target: 'https://outlook.office.com/.default', expiresOn: '4102444800'},
    ['msal.3|' + context.home + '|login.windows.net|accesstoken|' + context.client + '|realm|https://graph.microsoft.com/User.Read']: {secret: 'TOKEN-GRAPH', target: 'User.Read', expiresOn: '4102444800'},
  };
  for (const [k, v] of Object.entries(entries)) localStorage.setItem(k, JSON.stringify(await encrypt(JSON.stringify(v), context.ctx === 'clientId' ? context.client : context.ctx)));
  // an old-style plain entry and an unrelated key must also be handled
  localStorage.setItem('msal.2|x|accesstoken|plain', JSON.stringify({secret: 'TOKEN-PLAIN', target: 'plain', expiresOn: '1'}));
  localStorage.setItem('olk-isauthed', 'true');
}"""


async def run(context_kind="clientId", with_cookie=True):
    async with playwright_async.async_playwright() as pw:
        browser = None
        for exe in [None, *glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome")]:  # CI / sandbox fallbacks
            try:
                browser = await pw.chromium.launch(**({"executable_path": exe} if exe else {}))
                break
            except Exception:  # noqa: BLE001
                continue
        if browser is None:
            pytest.skip("no Chromium available")
        page = await browser.new_page()
        await page.route("https://outlook.office.com/**", lambda r: r.fulfill(body="<html><body>x</body></html>", content_type="text/html"))
        await page.goto("https://outlook.office.com/mail/")
        await page.evaluate(SEED, {"context": {"client": CLIENT, "home": HOME, "ctx": context_kind}, "withCookie": with_cookie})
        result = await page.evaluate(_TOKEN_JS)
        await browser.close()
        return result


async def test_encrypted_msal_cache_is_decrypted_in_the_page():
    out = await run()
    assert sorted(t["secret"] for t in out["tokens"]) == ["TOKEN-GRAPH", "TOKEN-OUTLOOK", "TOKEN-PLAIN"]
    d = out["diag"]
    assert (d["entries"], d["plain"], d["encrypted"], d["decrypted"], d["has_cookie_key"]) == (3, 1, 2, 2, True)
    assert d["worked_with"] == {"clientId": 2}
    assert "secret" not in json.dumps(d).lower() or "TOKEN" not in json.dumps(d)  # diagnostics never carry token values


async def test_other_known_contexts_are_tried_too():
    out = await run(context_kind="")
    assert out["diag"]["decrypted"] == 2 and out["diag"]["worked_with"] == {"empty": 2}


async def test_without_the_key_cookie_the_entries_are_reported_but_not_opened():
    out = await run(with_cookie=False)
    assert [t["secret"] for t in out["tokens"]] == ["TOKEN-PLAIN"]
    d = out["diag"]
    assert (d["encrypted"], d["decrypted"], d["has_cookie_key"]) == (2, 0, False)


async def test_account_id_context_is_found_too():
    out = await run(context_kind=HOME)
    assert out["diag"]["decrypted"] == 2 and out["diag"]["worked_with"] == {"home": 2}
