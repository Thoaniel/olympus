"""Lekser: brukerens korrigeringer av feil svar.

/feil <hva som er riktig> lagrer en lekse for agenten som svarte. Når et lignende
spørsmål kommer senere, legges relevante lekser inn i agentens systemprompt, så
rettelsen virker med en gang, for både lokale modeller og skymodeller.

Hver lekse lagres også som treningseksempel (JSONL) for senere LoRA-finjustering
av de lokale modellene.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from olympus.config import Config


@dataclass
class Lesson:
    id: int
    agent: str
    question: str
    wrong: str
    correction: str
    created: str


class Lessons:
    def __init__(self, config: Config) -> None:
        self.home = config.home
        self.db_path = config.home / "lessons.db"
        self.train_dir = config.home / "training"

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path)
        db.execute(
            "CREATE TABLE IF NOT EXISTS lessons (id INTEGER PRIMARY KEY, agent TEXT, question TEXT, "
            "wrong TEXT, correction TEXT, created TEXT)"
        )
        db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS lessons_fts USING fts5(question, correction, "
            "content='lessons', content_rowid='id', tokenize='porter unicode61')"
        )
        return db

    def add(self, agent: str, question: str, wrong: str, correction: str, model: str = "") -> int:
        db = self._db()
        now = datetime.now().isoformat(timespec="seconds")
        cur = db.execute(
            "INSERT INTO lessons (agent, question, wrong, correction, created) VALUES (?, ?, ?, ?, ?)",
            (agent, question, wrong, correction, now),
        )
        lid = int(cur.lastrowid or 0)
        db.execute("INSERT INTO lessons_fts (rowid, question, correction) VALUES (?, ?, ?)", (lid, question, correction))
        db.commit()
        db.close()
        # Treningseksempel (for senere finjustering): feil svar + rettelse.
        self.train_dir.mkdir(parents=True, exist_ok=True)
        with (self.train_dir / f"{agent}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": lid, "created": now, "agent": agent, "model": model,
                "question": question, "rejected": wrong, "correction": correction,
            }, ensure_ascii=False) + "\n")
        return int(lid)

    def all(self, agent: str | None = None) -> list[Lesson]:
        if not self.db_path.exists():
            return []
        db = self._db()
        sql = "SELECT id, agent, question, wrong, correction, created FROM lessons"
        rows = db.execute(sql + (" WHERE agent = ?" if agent else "") + " ORDER BY id DESC", (agent,) if agent else ()).fetchall()
        db.close()
        return [Lesson(*r) for r in rows]

    def remove(self, lid: int) -> bool:
        db = self._db()
        row = db.execute("SELECT question, correction FROM lessons WHERE id = ?", (lid,)).fetchone()
        if row:
            db.execute("INSERT INTO lessons_fts (lessons_fts, rowid, question, correction) VALUES ('delete', ?, ?, ?)", (lid, *row))
            db.execute("DELETE FROM lessons WHERE id = ?", (lid,))
            db.commit()
        db.close()
        return row is not None

    def relevant(self, agent: str, question: str, k: int = 3) -> list[Lesson]:
        """Lekser for agenten som ligner spørsmålet (fulltekstsøk)."""
        if not self.db_path.exists():
            return []
        words = [w for w in re.findall(r"[\w\-+.]+", question.lower()) if len(w) > 2][:16]
        if not words:
            return []
        db = self._db()
        try:
            rows = db.execute(
                "SELECT l.id, l.agent, l.question, l.wrong, l.correction, l.created FROM lessons_fts "
                "JOIN lessons l ON l.id = lessons_fts.rowid WHERE lessons_fts MATCH ? AND l.agent = ? "
                "ORDER BY bm25(lessons_fts) LIMIT ?",
                (" OR ".join(f'"{w}"' for w in words), agent, k),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        db.close()
        return [Lesson(*r) for r in rows]

    def set_chosen(self, lid: int, agent: str, chosen: str, teacher: str) -> None:
        """Legg lærerens riktige svar til treningseksempelet (preferansepar: rejected/chosen)."""
        path = self.train_dir / f"{agent}.jsonl"
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        out = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                out.append(line)
                continue
            if row.get("id") == lid:
                row["chosen"] = chosen
                row["teacher"] = teacher
            out.append(json.dumps(row, ensure_ascii=False))
        path.write_text("\n".join(out) + "\n", encoding="utf-8")

    def stats(self) -> dict[str, tuple[int, int]]:
        """(eksempler, med lærersvar) per agent."""
        out: dict[str, tuple[int, int]] = {}
        if not self.train_dir.exists():
            return out
        for f in self.train_dir.glob("*.jsonl"):
            rows = [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
            out[f.stem] = (len(rows), sum(1 for r in rows if r.get("chosen")))
        return out

    @staticmethod
    def as_prompt(lessons: list[Lesson]) -> str:
        if not lessons:
            return ""
        parts = ["## Rettelser fra brukeren (tidligere feil – ikke gjenta dem)"]
        for les in lessons:
            q = " ".join(les.question.split())[:200]
            parts.append(f"- Spørsmål: «{q}»\n  Rettelse: {les.correction.strip()}")
        return "\n".join(parts)
