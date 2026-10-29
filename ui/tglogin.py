"""One-time Telegram login for the group reader, done from the panel (phone, then the code Telegram sends you).

The phone number and code are used once and never stored or logged; only Telegram's own session file is saved.
"""
from __future__ import annotations

import re
import time

from ops import telethon_main

PHONE_RE = re.compile(r"^\+?[0-9]{7,15}$")
EXPIRE = 600.0

FRIENDLY = {
    "PhoneNumberInvalidError": "Telegram does not accept that phone number. Use the international format, e.g. +65 9123 4567.",
    "PhoneCodeInvalidError": "That code is not right. Check the latest code in your Telegram app and try again.",
    "PhoneCodeExpiredError": "That code expired. Start again to get a new one.",
    "PasswordHashInvalidError": "That two-step verification password is not right.",
    "FloodWaitError": "Telegram asks you to wait before trying again. Try again in a few minutes.",
    "ApiIdInvalidError": "Telegram rejected TELETHON_API_ID / TELETHON_API_HASH. Check them at my.telegram.org.",
}


class TelegramLogin:
    def __init__(self, settings, client_factory=telethon_main.client_for):
        self.settings = settings
        self._factory = client_factory
        self._client = None
        self._phone = self._hash = ""
        self.stage = "idle"  # idle | code_sent | password_needed | done
        self.error = ""
        self._started = 0.0

    def status(self) -> dict:
        if self.stage in ("code_sent", "password_needed") and time.time() - self._started > EXPIRE:
            self.stage = "idle"
        return {"stage": self.stage, "error": self.error}

    def _lock(self, on: bool) -> None:
        path = telethon_main.login_lock_path(self.settings)
        if on:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(time.time()))
        else:
            path.unlink(missing_ok=True)

    async def _close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._lock(False)

    def _fail(self, e: Exception) -> dict:
        self.error = FRIENDLY.get(type(e).__name__, f"Telegram said: {type(e).__name__}")
        return self.status()

    async def start(self, phone: str) -> dict:
        phone = re.sub(r"[\s\-()]", "", phone or "")
        self.error = ""
        if not PHONE_RE.match(phone):
            self.error = FRIENDLY["PhoneNumberInvalidError"]
            return self.status()
        if not (self.settings.telethon_api_id and self.settings.telethon_api_hash):
            self.error = "Set TELETHON_API_ID and TELETHON_API_HASH in secrets.env first (from my.telegram.org)."
            return self.status()
        await self._close()
        try:
            self._lock(True)
            self._client = self._factory(self.settings)
            await self._client.connect()
            if await self._client.is_user_authorized():
                self.stage = "done"
                await self._close()
                return self.status()
            sent = await self._client.send_code_request(phone)
            self._phone, self._hash, self._started, self.stage = phone, sent.phone_code_hash, time.time(), "code_sent"
        except Exception as e:  # noqa: BLE001
            await self._close()
            self.stage = "idle"
            return self._fail(e)
        return self.status()

    async def code(self, code: str) -> dict:
        self.error = ""
        if self.stage != "code_sent" or self._client is None:
            self.error = "Ask for a code first."
            return self.status()
        try:
            await self._client.sign_in(phone=self._phone, code=re.sub(r"\D", "", code or ""), phone_code_hash=self._hash)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "SessionPasswordNeededError":
                self.stage = "password_needed"
                return self.status()
            if type(e).__name__ in ("PhoneCodeExpiredError",):
                await self._close()
                self.stage = "idle"
            return self._fail(e)
        return await self._finish()

    async def password(self, password: str) -> dict:
        self.error = ""
        if self.stage != "password_needed" or self._client is None:
            self.error = "Enter the code first."
            return self.status()
        try:
            await self._client.sign_in(password=password or "")
        except Exception as e:  # noqa: BLE001
            return self._fail(e)
        return await self._finish()

    async def _finish(self) -> dict:
        self.stage = "done"
        self._phone = self._hash = ""
        await self._close()
        return self.status()

    async def cancel(self) -> dict:
        await self._close()
        self.stage, self.error = "idle", ""
        return self.status()
