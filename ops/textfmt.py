"""Clean plain-text output for Telegram: no Markdown symbols, no emoji, tidy bullets.

Applied to everything JARVIS sends, so replies stay professional even when the model slips into Markdown.
"""
from __future__ import annotations

import re

_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿⌀-⏿️‍⃣]")
_FENCE = re.compile(r"^```[\w-]*[ \t]*(?:\n|$)", re.M)
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*$", re.M)
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", re.S)
_ITALIC = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BULLET = re.compile(r"^([ \t]*)[*+-][ \t]+", re.M)
_RULE = re.compile(r"^[ \t]*([-*_])([ \t]*\1){2,}[ \t]*(?:\n|$)", re.M)


def _strip_emoji(text: str) -> str:
    out = []
    for line in text.split("\n"):
        clean = _EMOJI.sub("", line)
        if clean != line:
            indent = line[: len(line) - len(line.lstrip())]
            clean = (indent + re.sub(r" {2,}", " ", clean.lstrip())).rstrip()
        out.append(clean)
    return "\n".join(out)


def plain(text: str | None) -> str:
    if not text:
        return text or ""
    text = _FENCE.sub("", text)
    text = _RULE.sub("", text)
    text = _HEADING.sub(r"\1", text)
    text = _BOLD.sub(r"\2", text)
    text = _ITALIC.sub(r"\1", text)
    text = _LINK.sub(lambda m: m.group(2) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = _BULLET.sub(lambda m: m.group(1) + "• ", text)
    text = _strip_emoji(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip("\n")
