"""Register over bakgrunnsprosesser Olympus starter (llama-server).

Serverne startes i egen sesjon så Ctrl+C i TUI-en ikke dreper dem. Ulempen er at de
overlever hvis Olympus dør brått. Derfor registreres hver server med eier-PID, og
servere fra døde instanser ryddes bort neste gang Olympus starter.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

from olympus.config import olympus_home

MARKERS = ("llama-server",)


def _dir() -> Path:
    d = olympus_home() / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _cmdline(pid: int) -> str:
    try:
        import psutil  # alle plattformer (før: /proc/<pid>/cmdline)

        return " ".join(psutil.Process(pid).cmdline())
    except OSError:
        return ""


def register(name: str, pid: int) -> None:
    (_dir() / f"{name}.json").write_text(json.dumps({"pid": pid, "owner": os.getpid()}))


def unregister(name: str) -> None:
    (_dir() / f"{name}.json").unlink(missing_ok=True)


def reap_orphans() -> list[str]:
    """Stopp servere hvis eier-prosessen er borte. Returnerer navnene som ble stoppet."""
    stopped = []
    for f in _dir().glob("*.json"):
        try:
            info = json.loads(f.read_text())
            pid, owner = int(info["pid"]), int(info["owner"])
        except (OSError, ValueError, KeyError):
            f.unlink(missing_ok=True)
            continue
        if _alive(owner) and owner != os.getpid():
            continue  # en annen Olympus-instans kjører fortsatt
        if _alive(pid) and any(m in _cmdline(pid) for m in MARKERS):
            try:
                os.killpg(pid, signal.SIGTERM)  # serveren leder sin egen prosessgruppe
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            stopped.append(f.stem)
        f.unlink(missing_ok=True)
    return stopped
