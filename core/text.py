"""Pure text helpers shared by core and ops."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote
from html.parser import HTMLParser


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in ("p", "div", "li", "tr"):
            self.parts.append(" ")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
    return text[:limit].rstrip() + "…" if len(text) > limit else text


def plain_to_text(text: str, limit: int = 300) -> str:
    """Text that is already plain (no markup): whitespace tidied, nothing decoded. "&amp;lt;" stays as written."""
    return _clip(text or "", limit)


def rich_to_text(field: dict | None, limit: int = 300) -> str:
    """A Brightspace rich-text field {"Text": plain, "Html": markup}. The Html is decoded once; the Text is already
    decoded by the server, so it is only tidied (decoding it again would turn "&amp;lt;" into "<")."""
    field = field or {}
    if field.get("Html"):
        return html_to_text(field["Html"], limit)
    return plain_to_text(field.get("Text") or "", limit)


def html_to_text(html: str, limit: int = 300) -> str:
    p = _Text()
    p.feed(html or "")
    p.close()
    text = re.sub(r"\s+", " ", "".join(p.parts).replace("\xa0", " ")).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


_NAME_BAD = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff\"\\/:*?<>|]")


def safe_file_name(name: str, limit: int = 120) -> str:
    """A file name that is safe to show, to put in a header and to use as a path part: last path component only,
    no control or bidi characters (CR/LF included), no quotes or separators. Unicode letters are kept."""
    name = unicodedata.normalize("NFC", str(name or "")).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"\s+", " ", _NAME_BAD.sub("_", name)).strip(" .")
    if len(name) > limit:  # keep the extension when shortening
        stem, dot, ext = name.rpartition(".")
        name = (stem[: limit - len(ext) - 1] + "." + ext) if dot and 0 < len(ext) <= 10 else name[:limit]
    return name or "file"


def ascii_file_name(name: str) -> str:
    """The plain-ASCII `filename=` value that is sent for a (cleaned) name; non-ASCII characters become "_"."""
    return safe_file_name(name).encode("ascii", "replace").decode().replace("?", "_")


def shown_file_name(name: str) -> str:
    """What an approval card shows: exactly the `filename=` that will be sent, with the real name in brackets when
    that differs (the server may also get it as filename*)."""
    real, sent = safe_file_name(name), ascii_file_name(name)
    return real if real == sent else f"{sent} [{real}]"


def disposition_filename(name: str) -> str:
    """The filename part of a Content-Disposition header: a quoted ASCII fallback, plus RFC 5987 filename* when the
    name has non-ASCII characters."""
    name = safe_file_name(name)
    ascii_name = ascii_file_name(name)
    out = f'filename="{ascii_name}"'
    if ascii_name != name:
        out += f"; filename*=UTF-8''{quote(name, safe='')}"
    return out
