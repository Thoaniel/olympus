"""Biblioteket: lokal fulltekstindeks (SQLite FTS5) over Arch Wiki og egne dokumenter.

Hver side deles opp per seksjon, så søk treffer riktig avsnitt. Atlas bruker det via
verktøyene library_search / library_read, og brukeren direkte med /bibliotek.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from olympus.config import Config

TEXT_EXT = {".md", ".txt", ".rst", ".org"}


@dataclass
class Hit:
    id: int
    source: str
    title: str
    section: str
    snippet: str
    path: str


class _WikiHTML(HTMLParser):
    """Trekker ut tittel og seksjoner (h2/h3) som ren tekst fra en Arch Wiki-side."""

    SKIP = {"script", "style", "nav", "head"}
    BLOCK = {"p", "div", "li", "pre", "tr", "br", "dd", "dt", "table", "ul", "ol", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.sections: list[tuple[str, list[str]]] = [("", [])]
        self._skip = 0
        self._in_title = False
        self._heading: str | None = None
        self._head_buf: list[str] = []
        self._in_pre = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("h1", "h2", "h3", "h4"):
            self._heading = tag
            self._head_buf = []
        elif tag == "pre":
            self._in_pre = True
            self.sections[-1][1].append("\n```\n")
        elif tag in self.BLOCK:
            self.sections[-1][1].append("\n")
        elif tag == "code" and not self._in_pre:
            self.sections[-1][1].append("`")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == self._heading:
            text = " ".join("".join(self._head_buf).split())
            if tag == "h1" and not self.title:
                self.title = text
            elif tag in ("h2", "h3") and text:
                self.sections.append((text, []))
            self._heading = None
        elif tag == "pre":
            self._in_pre = False
            self.sections[-1][1].append("\n```\n")
        elif tag == "code" and not self._in_pre:
            self.sections[-1][1].append("`")

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title = self.title or data.split(" - ")[0].strip()
            return
        if self._heading:
            self._head_buf.append(data)
            return
        self.sections[-1][1].append(data)


NOISE = re.compile(r"^(Jump to content|From ArchWiki|Related articles|\(Discuss in Talk:[^)]*\))\s*", re.M)


def _clean(parts: list[str]) -> str:
    text = NOISE.sub("", "".join(parts))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def parse_wiki_page(path: Path) -> tuple[str, list[tuple[str, str]]]:
    p = _WikiHTML()
    p.feed(path.read_text(encoding="utf-8", errors="replace"))
    title = p.title or path.stem.replace("_", " ")
    sections = [(name, _clean(parts)) for name, parts in p.sections]
    return title, [(n, t) for n, t in sections if len(t) > 40]


def parse_text_doc(path: Path) -> tuple[str, list[tuple[str, str]]]:
    """Markdown/tekst: del på overskrifter (#, ##, ###). Hugo-frontmatter gir tittelen."""
    text = path.read_text(encoding="utf-8", errors="replace")
    title = path.stem
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            m = re.search(r"^title:\s*[\"']?(.+?)[\"']?\s*$", text[3:end], re.M)
            if m:
                title = m.group(1)
            text = text[end + 4:]
        if title == "_index":
            title = path.parent.name.replace("-", " ").title()
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            if len(m.group(1)) == 1 and title == path.stem:
                title = m.group(2).strip()
                continue
            sections.append((m.group(2).strip(), []))
        else:
            sections[-1][1].append(line)
    return title, [(n, "\n".join(b).strip()) for n, b in sections if "\n".join(b).strip()]


CHANGE_NOTE = re.compile(r"^(Overskrift endret|Tilføyd ved|Endret ved|Opphevet ved|Kapitteloverskrift endret)")
WIKI_SKIP = {"referanser", "litteratur", "eksterne lenker", "se også", "sjå òg", "kjelder", "kilder", "fotnoter",
             "noter", "references", "external links", "see also", "further reading", "notes", "bibliography"}


class Library:
    """Kilder (navn → type, mappe/fil). Typer: wiki-html (Arch Wiki), markdown, lovdata, zim."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.root = Path(config.get("library.dir", "~/.olympus/library")).expanduser()
        self.db_path = self.root / "library.db"

    # --- kilder ----------------------------------------------------------
    def catalog(self) -> dict[str, tuple[str, Path]]:
        out: dict[str, tuple[str, Path]] = {}
        wiki = self.root / "arch-wiki/usr/share/doc/arch-wiki/html"
        for lang in self.config.get("library.wiki_languages", ["en"]):
            if (wiki / lang).is_dir():
                out["arch-wiki" if lang == "en" else f"arch-wiki-{lang}"] = ("wiki-html", wiki / lang)
        hypr = self.root / "hyprland-wiki-src/content"
        if hypr.is_dir():
            out["hyprland-wiki"] = ("markdown", hypr)
        for name, sub in (("lovdata-lover", "gjeldende-lover"), ("lovdata-forskrifter", "gjeldende-sentrale-forskrifter")):
            if (self.root / "lovdata" / sub).is_dir():
                out[name] = ("lovdata", self.root / "lovdata" / sub)
        for z in sorted((self.root / "wikipedia").glob("wikipedia_*.zim")):
            lang = z.name.split("_")[1]
            out[f"wikipedia-{lang}"] = ("zim", z)
        docs = self.root / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        out["docs"] = ("markdown", docs)
        for extra in self.config.get("library.extra_dirs", []):
            p = Path(extra).expanduser()
            if p.is_dir():
                name = f"tldr-{p.name}" if "tldr" in p.parts else p.name  # tldr/pages/linux → tldr-linux
                out[name] = ("markdown", p)
        return out

    def sources(self) -> dict[str, Path]:
        return {name: path for name, (_kind, path) in self.catalog().items()}

    def read_dirs(self) -> list[Path]:
        return [p if p.is_dir() else p.parent for p in self.sources().values()]

    # --- indeks ----------------------------------------------------------
    def _db(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5("
            "source UNINDEXED, title, section, body, path UNINDEXED, tokenize='porter unicode61')"
        )
        db.execute("CREATE TABLE IF NOT EXISTS indexed (path TEXT PRIMARY KEY, mtime REAL)")
        db.execute("CREATE TABLE IF NOT EXISTS counts (source TEXT PRIMARY KEY, n INTEGER)")
        return db

    def build(self, progress=None, only: set[str] | None = None) -> dict[str, int]:
        """Indekser nye/endrede filer. Returnerer antall dokumenter per kilde."""
        db = self._db()
        known = dict(db.execute("SELECT path, mtime FROM indexed"))
        counts: dict[str, int] = {}
        seen: set[str] = set()
        for source, (kind, path) in self.catalog().items():
            if only and source not in only:
                seen.update(k for k in known if k.startswith(str(path)))
                continue
            if kind == "zim":
                counts[source] = self._build_zim(db, source, path, known, progress)
                seen.add(str(path))
                continue
            suffixes = {".html"} if kind == "wiki-html" else {".xml", ".html"} if kind == "lovdata" else TEXT_EXT
            files = [p for p in path.rglob("*") if p.is_file() and p.suffix in suffixes]
            counts[source] = 0
            for i, f in enumerate(files):
                key = str(f)
                seen.add(key)
                mtime = f.stat().st_mtime
                if known.get(key) == mtime:
                    counts[source] += 1
                    continue
                title, sections = (parse_text_doc if kind == "markdown" else parse_wiki_page)(f)
                if kind == "lovdata":
                    # Kapitteloverskrifter som bare har et endringsnotat, gir bare støy i søk.
                    sections = [(n, b) for n, b in sections if not (len(b) < 200 and CHANGE_NOTE.match(b))]
                if title.lower().startswith(("category:", "kategori:")) or not sections:
                    # Husk at den er sett, så den ikke tolkes på nytt ved hver oppstart.
                    db.execute("INSERT OR REPLACE INTO indexed VALUES (?, ?)", (key, mtime))
                    continue
                if key in known:  # bare endrede filer har gamle rader (DELETE i fulltekstindeksen er et fullt søk)
                    db.execute("DELETE FROM docs WHERE path = ?", (key,))
                db.executemany(
                    "INSERT INTO docs (source, title, section, body, path) VALUES (?, ?, ?, ?, ?)",
                    [(source, title, name, body, key) for name, body in sections],
                )
                db.execute("INSERT OR REPLACE INTO indexed VALUES (?, ?)", (key, mtime))
                counts[source] += 1
                if progress and i % 500 == 0:
                    progress(source, i, len(files))
            db.commit()
        for gone in set(known) - seen:
            db.execute("DELETE FROM docs WHERE path = ? OR path LIKE ?", (gone, gone + "#%"))
            db.execute("DELETE FROM indexed WHERE path = ?", (gone,))
        db.commit()
        db.close()
        return counts

    def _build_zim(self, db: sqlite3.Connection, source: str, path: Path, known: dict, progress) -> int:
        """Wikipedia fra Kiwix-ZIM: én rad per seksjon, uten referanse- og lenkelister."""
        from libzim.reader import Archive

        mtime = path.stat().st_mtime
        if known.get(str(path)) == mtime:
            row = db.execute("SELECT n FROM counts WHERE source = ?", (source,)).fetchone()
            if row is None:  # telling i fulltekstindeksen er et fullt søk: gjør det én gang og husk svaret
                row = db.execute("SELECT count(DISTINCT path) FROM docs WHERE source = ?", (source,)).fetchone()
                db.execute("INSERT OR REPLACE INTO counts VALUES (?, ?)", (source, row[0]))
                db.commit()
            return int(row[0])
        db.execute("DELETE FROM docs WHERE source = ?", (source,))
        archive = Archive(str(path))
        total = archive.all_entry_count
        batch: list[tuple] = []
        n = 0
        for i in range(total):
            entry = archive._get_entry_by_id(i)
            if entry.is_redirect:
                continue
            item = entry.get_item()
            if not item.mimetype.startswith("text/html"):
                continue
            p = _WikiHTML()
            try:
                p.feed(bytes(item.content).decode("utf-8", errors="replace"))
            except Exception:
                continue
            title = entry.title or p.title
            kept = 0
            for name, parts in p.sections:
                if name.strip().lower() in WIKI_SKIP:
                    continue
                body = _clean(parts)
                if len(body) < 60:
                    continue
                batch.append((source, title, name, body[:4000], f"{path}#{entry.path}"))
                kept += 1
                if kept >= 15:
                    break
            n += 1
            if len(batch) >= 5000:
                db.executemany("INSERT INTO docs (source, title, section, body, path) VALUES (?, ?, ?, ?, ?)", batch)
                db.commit()
                batch.clear()
            if progress and i % 20000 == 0:
                progress(source, i, total)
        if batch:
            db.executemany("INSERT INTO docs (source, title, section, body, path) VALUES (?, ?, ?, ?, ?)", batch)
        db.execute("INSERT OR REPLACE INTO indexed VALUES (?, ?)", (str(path), mtime))
        db.execute("INSERT OR REPLACE INTO counts VALUES (?, ?)", (source, n))
        db.commit()
        return n
    def ready(self) -> bool:
        if not self.db_path.exists():
            return False
        db = self._db()
        n = db.execute("SELECT count(*) FROM indexed").fetchone()[0]
        db.close()
        return n > 0

    # --- søk -------------------------------------------------------------
    @staticmethod
    def _fts_query(query: str) -> str:
        words = re.findall(r"[\w\-+.]+", query.lower())
        stop = {"the", "a", "an", "how", "to", "i", "is", "og", "i", "å", "er", "hvordan", "hva", "jeg", "min", "mitt"}
        terms = [w for w in words if w not in stop and len(w) > 1] or words
        return " OR ".join(f'"{t}"' for t in terms[:12])

    def search(self, query: str, limit: int = 8, source: str | None = None) -> list[Hit]:
        if not self.ready():
            return []
        db = self._db()
        sql = (
            "SELECT rowid, source, title, section, snippet(docs, 3, '[', ']', ' … ', 24), path "
            "FROM docs WHERE docs MATCH ?"
        )
        args: list = [self._fts_query(query)]
        if source:
            sql += " AND source = ?"
            args.append(source)
        # Tittel teller mest, så seksjon, så brødtekst.
        sql += " ORDER BY bm25(docs, 0, 10.0, 4.0, 1.0) LIMIT ?"
        args.append(limit)
        try:
            rows = db.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            rows = []
        db.close()
        return [Hit(r[0], r[1], r[2], r[3], " ".join(r[4].split()), r[5]) for r in rows]

    def read(self, ref: str | int, section: str | None = None, limit: int = 8000,
             source: str | None = None) -> tuple[str, str] | None:
        """Hent en seksjon (id fra søk) eller en hel artikkel (tittel). Returnerer (tittel, tekst)."""
        db = self._db()
        try:
            if isinstance(ref, int) or str(ref).isdigit():
                row = db.execute("SELECT title, section, body, path FROM docs WHERE rowid = ?", (int(ref),)).fetchone()
                if not row:
                    return None
                title, sec, body, _ = row
                return title, f"## {sec}\n\n{body}" if sec else body
            where, args = "title = ? COLLATE NOCASE", [str(ref)]
            if source:
                where += " AND source = ?"
                args.append(source)
            rows = db.execute(f"SELECT title, section, body FROM docs WHERE {where} ORDER BY rowid", args).fetchall()
            if not rows:
                rows = db.execute(
                    "SELECT title, section, body FROM docs WHERE docs MATCH ? ORDER BY bm25(docs, 0, 10.0, 1.0, 0.5), rowid LIMIT 40",
                    (f'title:"{ref}"',),
                ).fetchall()
                if rows:
                    first = rows[0][0]
                    rows = [r for r in rows if r[0] == first]
            if not rows:
                return None
            if section:
                rows = [r for r in rows if section.lower() in (r[1] or "").lower()] or rows
            text = "\n\n".join(f"## {s}\n\n{b}" if s else b for _, s, b in rows)
            if len(text) > limit:
                text = text[:limit] + "\n\n… (avkortet; be om en bestemt seksjon med section=…)"
            return rows[0][0], text
        finally:
            db.close()
