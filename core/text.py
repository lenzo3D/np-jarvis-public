"""Pure text helpers shared by core and ops."""
from __future__ import annotations

import re
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


def html_to_text(html: str, limit: int = 300) -> str:
    p = _Text()
    p.feed(html or "")
    p.close()
    text = re.sub(r"\s+", " ", "".join(p.parts).replace("\xa0", " ")).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text
