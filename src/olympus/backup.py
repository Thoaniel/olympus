"""Nattlig backup av det som er ditt i Olympus: planen, spillelister, framdrift og enheter (lab.db), Zeus' minne,
samtalene og egne innstillinger. Ikke mediefilene (de er dine egne filer) og ikke indeksen (media.db kan
bygges på nytt). 14 dager tilbake; resultatet lagres i lab.db, så dashbordet og Zeus kan si fra om noe feilet.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

KEEP_DAYS = 14


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def run_backup(home: Path, dest_root: Path, sessions_dir: Path | None = None, keep: int = KEEP_DAYS) -> dict[str, Any]:
    started = time.time()
    dest = dest_root / date.today().isoformat()
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, mode=0o700)
    try:
        src = sqlite3.connect(home / "lab.db")
        out = sqlite3.connect(tmp / "lab.db")
        with out:
            src.backup(out)  # konsistent kopi selv mens serveren skriver
        src.close()
        out.close()
        (tmp / "lab.db").chmod(0o600)
        for name in ("memory", "agents"):
            if (home / name).is_dir():
                shutil.copytree(home / name, tmp / name)
        if sessions_dir and sessions_dir.is_dir():
            shutil.copytree(sessions_dir, tmp / "sessions")
        for name in ("config.toml", "ui.json"):
            if (home / name).is_file():
                shutil.copy2(home / name, tmp / name)
        info = {"date": date.today().isoformat(), "created": datetime.now().isoformat(timespec="seconds"),
                "seconds": round(time.time() - started, 1)}
        (tmp / "backup.json").write_text(json.dumps(info))
        shutil.rmtree(dest, ignore_errors=True)
        tmp.rename(dest)
        info["bytes"] = _dir_size(dest)
        info["ok"] = True
    except Exception as e:  # noqa: BLE001 – backup skal aldri ta ned serveren; feilen rapporteres
        shutil.rmtree(tmp, ignore_errors=True)
        info = {"date": date.today().isoformat(), "ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    # Behold de siste `keep` dagene.
    days = sorted((p for p in dest_root.iterdir() if p.is_dir() and len(p.name) == 10), key=lambda p: p.name)
    for old in days[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
    info["kept"] = len([p for p in dest_root.iterdir() if p.is_dir() and len(p.name) == 10])
    return info


def due(last: dict[str, Any] | None, now: datetime | None = None, hour: int = 3) -> bool:
    """Tid for backup: etter kl. 03 og ikke allerede (vellykket) tatt i dag."""
    now = now or datetime.now()
    if now.hour < hour:
        return False
    return not last or last.get("date") != now.date().isoformat() or not last.get("ok")


def list_backups(dest_root: Path) -> list[dict[str, Any]]:
    if not dest_root.is_dir():
        return []
    out = []
    for p in sorted((p for p in dest_root.iterdir() if p.is_dir() and len(p.name) == 10), reverse=True):
        try:
            info = json.loads((p / "backup.json").read_text())
        except (OSError, ValueError):
            info = {"date": p.name}
        out.append({**info, "bytes": _dir_size(p)})
    return out
