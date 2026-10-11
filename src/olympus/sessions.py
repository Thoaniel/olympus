"""Lagrede økter: hver samtale som én JSON-fil i ~/.olympus/sessions/.

Filen har både modellhistorikken (det Orchestrator sender videre) og et visningsutdrag (hvem sa hva, med
hvilken modell), så en gammel økt kan vises igjen nøyaktig slik den så ut, og fortsettes der den slapp.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SessionInfo:
    id: str
    title: str
    updated: float
    messages: int = 0


def title_from(text: str, limit: int = 40) -> str:
    """Tittel fra første melding: første linje, kuttet ved et ord."""
    line = " ".join(text.strip().split())
    if len(line) <= limit:
        return line or "Ny økt"
    cut = line[:limit].rsplit(" ", 1)[0]
    return (cut or line[:limit]) + " …"


def when(ts: float, now: float | None = None) -> str:
    """Kort, menneskelig tidspunkt: «nå», «14:32», «i går», «12. okt»."""
    now = now or time.time()
    t, n = time.localtime(ts), time.localtime(now)
    if now - ts < 60:
        return "nå"
    if t.tm_year == n.tm_year and t.tm_yday == n.tm_yday:
        return time.strftime("%H:%M", t)
    if t.tm_year == n.tm_year and n.tm_yday - t.tm_yday == 1:
        return "i går"
    months = ["jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des"]
    return f"{t.tm_mday}. {months[t.tm_mon - 1]}"


class SessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def new_id(self) -> str:
        return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]

    def _path(self, sid: str) -> Path:
        return self.root / f"{sid}.json"

    def save(self, sid: str, title: str, messages: list[dict[str, Any]], transcript: list[dict[str, Any]]) -> None:
        # Samtaler kan inneholde private ting: bare eieren får lese mappen og filene.
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        path = self._path(sid)
        created = time.time()
        if path.exists():
            try:
                created = json.loads(path.read_text(encoding="utf-8")).get("created", created)
            except (OSError, ValueError):
                pass
        data = {"id": sid, "title": title, "created": created, "updated": time.time(),
                "messages": messages, "transcript": transcript}
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, default=str))
        os.chmod(tmp, 0o600)  # også om .tmp fantes fra før med andre rettigheter
        tmp.replace(path)

    def load(self, sid: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._path(sid).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def delete(self, sid: str) -> None:
        self._path(sid).unlink(missing_ok=True)

    def list(self, limit: int = 200) -> list[SessionInfo]:
        """Nyeste først. Leser bare tittel og tidspunkt (filene er små)."""
        out = []
        for p in self.root.glob("*.json") if self.root.exists() else ():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append(SessionInfo(d.get("id", p.stem), d.get("title", p.stem), float(d.get("updated", 0)),
                                   len(d.get("transcript") or [])))
        out.sort(key=lambda s: s.updated, reverse=True)
        return out[:limit]


class SessionIndex:
    """Fulltekstsøk (SQLite FTS5) i alle lagrede økter, som Hermes' session_search: finner det som ble sagt
    i tidligere samtaler når det ikke lenger er i konteksten. Bygges/oppdateres fra JSON-filene ved behov."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.db_path = store.root / "index.db"

    def _db(self):
        import sqlite3

        self.store.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.db_path)
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS turns USING fts5(sid UNINDEXED, title, who UNINDEXED, text, "
                   "tokenize='unicode61 remove_diacritics 2')")
        db.execute("CREATE TABLE IF NOT EXISTS seen (sid TEXT PRIMARY KEY, updated REAL)")
        os.chmod(self.db_path, 0o600)
        return db

    def refresh(self) -> None:
        db = self._db()
        seen = dict(db.execute("SELECT sid, updated FROM seen"))
        current = {s.id: s for s in self.store.list(10_000)}
        for sid in set(seen) - set(current):
            db.execute("DELETE FROM turns WHERE sid = ?", (sid,))
            db.execute("DELETE FROM seen WHERE sid = ?", (sid,))
        for sid, info in current.items():
            if seen.get(sid) == info.updated:
                continue
            data = self.store.load(sid) or {}
            db.execute("DELETE FROM turns WHERE sid = ?", (sid,))
            db.executemany("INSERT INTO turns VALUES (?, ?, ?, ?)", [
                (sid, info.title, e.get("agent", "bruker") if e.get("kind") == "agent" else "bruker", e.get("text", ""))
                for e in data.get("transcript", []) if e.get("text")])
            db.execute("INSERT OR REPLACE INTO seen VALUES (?, ?)", (sid, info.updated))
        db.commit()
        db.close()

    def search(self, query: str, limit: int = 8, exclude: str | None = None) -> list[dict]:
        self.refresh()
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 1]
        if not words:
            return []
        db = self._db()
        rows = db.execute(
            "SELECT sid, title, who, snippet(turns, 3, '[', ']', ' … ', 24) FROM turns WHERE turns MATCH ? "
            "AND sid != ? ORDER BY rank LIMIT ?",
            (" OR ".join(f'"{w}"' for w in words), exclude or "", limit)).fetchall()
        db.close()
        return [{"sid": r[0], "title": r[1], "who": r[2], "snippet": r[3]} for r in rows]
