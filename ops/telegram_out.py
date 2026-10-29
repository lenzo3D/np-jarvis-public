"""Telegram output limits. Telegram counts message length in UTF-16 code units."""
from __future__ import annotations

TEXT_LIMIT = 4096
FILE_LIMIT = 50 * 1024 * 1024


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _hard_split(text: str, limit: int) -> list[str]:
    out, cur, n = [], [], 0
    for ch in text:
        w = 2 if ord(ch) > 0xFFFF else 1
        if n + w > limit:
            out.append("".join(cur))
            cur, n = [], 0
        cur.append(ch)
        n += w
    if cur:
        out.append("".join(cur))
    return out


def chunk(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Split on paragraphs, then lines, then hard cuts. Joining parts with the separator restores the text."""
    if utf16_len(text) <= limit:
        return [text]
    for sep in ("\n\n", "\n"):
        pieces = text.split(sep)
        if len(pieces) > 1:
            out: list[str] = []
            cur = ""
            for p in pieces:
                cand = p if not cur else cur + sep + p
                if utf16_len(cand) <= limit:
                    cur = cand
                    continue
                if cur:
                    out.append(cur)
                cur = p
            out.append(cur)
            if all(utf16_len(o) <= limit for o in out):
                return out
            return [x for o in out for x in chunk(o, limit)]
    return _hard_split(text, limit)
