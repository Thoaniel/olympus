"""SQLite slik Olympus Lab's bruker det: én person, flere prosesser (olympus-web, TUI-en) på samme filer.

- WAL + busy_timeout: TUI-en og serveren kan lese/skrive samtidig uten «database is locked».
- foreign_keys: spillelister sletter sine egne rader (ON DELETE CASCADE).
- Filene er bare lesbare for deg (600) – planen, enhetene og spillelistene er personlige.
- Skjemaet har et versjonsnummer (PRAGMA user_version); `migrate` kjører bare stegene som mangler, i rekkefølge.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

Migration = str | Callable[[sqlite3.Connection], None]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    db = sqlite3.connect(path, check_same_thread=False, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA synchronous=NORMAL")  # trygt med WAL, og mye raskere for mange små skrivinger
    if new or (path.stat().st_mode & 0o077):
        os.chmod(path, 0o600)
    return db


def migrate(db: sqlite3.Connection, steps: Sequence[Migration]) -> int:
    """Kjør migreringer som ikke er kjørt ennå. Steg n (0-basert) gir user_version n+1."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    for i, step in enumerate(steps[version:], start=version):
        with db:  # hvert steg er én transaksjon
            if callable(step):
                step(db)
            else:
                db.executescript(step)
            db.execute(f"PRAGMA user_version = {i + 1}")
    return len(steps)
