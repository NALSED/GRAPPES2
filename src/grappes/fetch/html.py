"""Extraction de texte et de liens d'une page HTML, sans exécuter quoi que ce soit."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin

log = logging.getLogger(__name__)

_SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "object", "canvas"}
_BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "table", "section", "article", "header",
          "footer", "nav", "main", "aside", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "form"}  # fmt: skip
MAX_TEXT = 200_000  # octets de texte gardés avant nettoyage


@dataclass(slots=True)
class Extracted:
    title: str = ""
    text: str = ""
    description: str = ""
    lang: str = ""
    site_name: str = ""
    canonical: str = ""
    noindex: bool = False
    has_password_field: bool = False
    links: list[tuple[str, str]] = field(default_factory=list)  # (href absolu, ancre)


class _Parser(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.out = Extracted()
        self._skip = 0
        self._in_title = False
        self._title: list[str] = []
        self._text: list[str] = []
        self._size = 0
        self._anchor: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag, attrs):
        a = {k.casefold(): (v or "") for k, v in attrs}
        if tag == "html" and a.get("lang"):
            self.out.lang = a["lang"]
        if tag in _SKIP:
            self._skip += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").casefold()
            content = a.get("content", "")
            if name == "robots" and "noindex" in content.casefold():
                self.out.noindex = True
            elif name in ("description", "og:description") and not self.out.description:
                self.out.description = content
            elif (
                name == "og:site_name"
                and not self.out.site_name
                or name == "application-name"
                and not self.out.site_name
            ):
                self.out.site_name = content
        elif tag == "link" and a.get("rel", "").casefold() == "canonical" and a.get("href"):
            self.out.canonical = urljoin(self.base, a["href"])
        elif tag == "input" and a.get("type", "").casefold() == "password":
            self.out.has_password_field = True
        elif tag == "a" and a.get("href"):
            self._anchor = (urljoin(self.base, a["href"]), [])
        if tag in _BLOCK:
            self._text.append(" ")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._anchor:
            href, parts = self._anchor
            self.out.links.append((href, " ".join("".join(parts).split())))
            self._anchor = None
        if tag in _BLOCK:
            self._text.append(" ")

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self._title.append(data)
        if self._anchor:
            self._anchor[1].append(data)
        if self._size < MAX_TEXT:
            self._text.append(data)
            self._size += len(data)


_CHARSET = re.compile(rb"""charset\s*=\s*["']?([\w.:-]+)""", re.IGNORECASE)


def decode_body(body: bytes, content_type: str = "") -> str:
    """Décodage tolérant : charset de l'en-tête, sinon de la page, sinon UTF-8 ; jamais d'exception."""
    m = re.search(r"charset=([\w.:-]+)", content_type or "", re.IGNORECASE)
    m2 = _CHARSET.search(body[:4096])
    for name in (
        m.group(1) if m else None,
        m2.group(1).decode("ascii", "ignore") if m2 else None,
        "utf-8",
    ):
        if not name:
            continue
        try:
            return body.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


def extract(body: bytes, content_type: str, base_url: str) -> Extracted:
    parser = _Parser(base_url)
    try:
        parser.feed(decode_body(body, content_type))
        parser.close()
    except Exception as exc:  # noqa: BLE001 - HTML malformé : on garde ce qui a été lu
        log.debug("HTML mal formé (%s) : extraction partielle", type(exc).__name__)
    out = parser.out
    out.title = " ".join("".join(parser._title).split())
    out.text = " ".join("".join(parser._text).split())
    return out
