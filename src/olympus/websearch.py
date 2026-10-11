"""Websøk og sidelesing for Zeus. Søk: DuckDuckGo (HTML-utgaven, ingen nøkkel). Lesing: via netsafe (bare offentlige
adresser, videresendinger sjekkes, størrelsesgrense), HTML → ren tekst i biter.

Treff fra siste søk kan leses med nummer uten godkjenning (Zeus valgte ikke lenken selv); en lenke Zeus skriver selv
går gjennom enforce_egress (godkjenning med hele lenken) som alle andre utgående lenker.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from olympus import netsafe

CHUNK = 8000
LAST: list[dict[str, str]] = []  # treffene fra siste søk (én bruker, én hjerne)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def search(query: str, limit: int = 8) -> list[dict[str, str]]:
    with netsafe.client(timeout=15) as c:
        r = c.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": "no-no"})
        r.raise_for_status()
        page = r.text[:2_000_000]
    out: list[dict[str, str]] = []
    for a in re.finditer(r'class="result__a"\s+href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
        url = html.unescape(a.group(1))
        if "duckduckgo.com/l/" in url:  # omdirigeringslenke: den ekte lenken ligger i uddg
            url = unquote(parse_qs(urlsplit(url).query).get("uddg", [""])[0])
        if not url.startswith(("http://", "https://")) or "duckduckgo.com/y.js" in url:
            continue  # annonser
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</(?:a|td|div)>', page[a.end():a.end() + 4000], re.S)
        out.append({"title": _clean(a.group(2))[:150], "url": url[:500], "snippet": _clean(snip.group(1))[:300] if snip else ""})
        if len(out) >= limit:
            break
    LAST[:] = out
    return out


class _Page(HTMLParser):
    """Synlig tekst: attributter (der sider gjemmer JSON) tas aldri med; skript, stil, meny o.l. hoppes over.
    Tekst inne i <main>/<article> samles også for seg (hovedinnholdet) – uten regex over hele siden (ReDoS)."""
    SKIP = {"script", "style", "noscript", "svg", "nav", "header", "footer", "form", "aside", "iframe", "template"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "blockquote", "pre", "td"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.main: list[str] = []
        self.title = ""
        self._skip = 0
        self._main = 0
        self._t = False

    def _emit(self, x: str) -> None:
        self.out.append(x)
        if self._main:
            self.main.append(x)

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._t = True
        if tag in ("main", "article"):
            self._main += 1
        if tag in self.BLOCK:
            self._emit("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._t = False
        if tag in ("main", "article"):
            self._main = max(0, self._main - 1)
        if tag in self.BLOCK:
            self._emit("\n\n")

    def handle_data(self, data: str) -> None:
        if self._t:
            self.title = (self.title + data)[:500]
        elif not self._skip:
            self._emit(re.sub(r"\s+", " ", data))


def page_text(raw: str) -> tuple[str, str]:
    """(tittel, lesbar tekst): hovedinnholdet (<main>/<article> når siden har det), avsnitt beholdes."""
    p = _Page()
    p.feed(raw)
    body = "".join(p.main) if len("".join(p.main).strip()) > 200 else "".join(p.out)
    text = "\n\n".join(x.strip() for x in re.split(r"\n\s*\n", body) if len(x.strip()) > 1)
    return _clean(p.title)[:200], text


def read(url: str, part: int = 1) -> dict[str, Any]:
    with netsafe.open_url(url, max_bytes=3 * 1024 * 1024, timeout=20) as f:
        ctype = f.content_type
        if not ctype.startswith(("text/", "application/xhtml")):
            raise netsafe.UnsafeURL(f"ikke en nettside ({ctype or 'ukjent type'})")
        raw = f.read().decode(errors="replace")
        final = f.url
    title, text = page_text(raw)
    parts = max(1, -(-len(text) // CHUNK))
    part = min(max(1, part), parts)
    return {"url": final, "title": title, "part": part, "parts": parts, "text": text[(part - 1) * CHUNK:part * CHUNK]}
