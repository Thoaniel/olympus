"""Avspilling på PC-en: Zeus spiller sanger fra Olympus' eget bibliotek (medialib) på PC-ens lydutgang –
Bluetooth-høyttaleren når den er koblet til. ffmpeg leser filen direkte; ingenting går over nettet.

Én spiller for hele hjernen (appen, Lab's og Zeus styrer den samme): spill, pause/fortsett (SIGSTOP/SIGCONT på
ffmpeg), neste, forrige, stopp, start midt i en sang (-ss), og hvor langt den har kommet. Hver endring meldes til
lytterne (websocketen), så alle skjermer viser det samme.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from olympus.medialib import SAFE_INPUT

PREV_RESTART = 5.0  # sekunder inn i en sang der «forrige» starter sangen på nytt i stedet for å gå tilbake
HISTORY = 50  # sanger fra tidligere køer som «forrige» kan gå tilbake til


def _public(t: dict[str, Any]) -> dict[str, Any]:
    """Sporet slik klientene ser det: aldri filstien på disken."""
    return {k: v for k, v in t.items() if k != "path"}


@dataclass
class Player:
    tracks: list[dict[str, Any]] = field(default_factory=list)  # hele køen; index peker på sangen som spilles
    index: int = 0
    current: dict[str, Any] | None = None
    paused: bool = False
    history: list[dict[str, Any]] = field(default_factory=list)  # spilt fra tidligere køer (nyeste sist)
    proc: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None
    listeners: list[Callable[[dict[str, Any]], Awaitable[None]]] = field(default_factory=list)
    lib: Any = None
    # posisjon: offset (der sangen startet) + tid siden start − tid på pause
    _offset: float = 0.0
    _started: float = 0.0
    _paused_at: float = 0.0
    _paused_total: float = 0.0
    _jump: tuple[int, float] | None = None  # neste/forrige: (indeks, posisjon) i stedet for neste sang
    _notify_task: asyncio.Task | None = None
    _dirty: bool = False

    # --- styring ------------------------------------------------------------------------------------
    async def play(self, lib: Any, songs: list[dict[str, Any]], index: int = 0, position: float = 0.0) -> None:
        """Spill lista fra sang nr. index, position sekunder inn."""
        played = self.tracks[:self.index + 1] if self.current else self.tracks[:self.index]
        await self._halt()
        self.history = (self.history + played)[-HISTORY:]
        self.lib = lib
        self.tracks = [_public(s) for s in songs]
        self.index = max(0, min(index, len(self.tracks) - 1)) if self.tracks else 0
        self._jump = None
        self.task = asyncio.create_task(self._run(lib, max(0.0, position)))

    def _spawn_args(self, path: str, start: float) -> list[str]:
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        ss = ["-ss", f"{start:.2f}"] if start > 0 else []
        return [ffmpeg, "-nostdin", "-loglevel", "error", "-re", *ss, *SAFE_INPUT, "-i", path,
                "-ac", "2", "-ar", "48000", "-f", "pulse", "-buffer_duration", "300", "Olympus"]

    async def _spawn(self, path: str, start: float) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(*self._spawn_args(path, start), stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)

    async def _run(self, lib: Any, start: float) -> None:
        try:
            while 0 <= self.index < len(self.tracks):
                t = lib.track(self.tracks[self.index]["id"])
                if t is None:
                    self.index += 1
                    start = 0.0
                    continue
                self.current = self.tracks[self.index]
                self.paused, self._offset, self._paused_total = False, start, 0.0
                self._started = time.monotonic()
                self.proc = await self._spawn(t["path"], start)
                self.changed()
                try:
                    await self.proc.wait()
                finally:
                    # Lyttehistorikk (musikksmaken): hvor lenge sangen faktisk spilte (uten pauser), også når den ble
                    # hoppet over eller stoppet
                    with contextlib.suppress(Exception):
                        lib.record_play(t["id"], max(0.0, self.position() - self._offset), t.get("duration") or 0, "pc")
                if self._jump is not None:
                    self.index, start = self._jump
                    self._jump = None
                else:
                    self.index += 1
                    start = 0.0
        finally:
            self.current, self.proc, self.paused = None, None, False
            self.changed()

    def _kill(self) -> None:
        p = self.proc
        if p is not None and p.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                p.kill()  # virker også på en stoppet (pauset) prosess
                p.send_signal(signal.SIGCONT)

    async def next(self) -> None:
        if self.current is not None:
            self._jump = (self.index + 1, 0.0)
            self._kill()

    async def prev(self) -> None:
        """Mer enn 5 s inn: start sangen på nytt. Ellers forrige sang (også fra en tidligere kø)."""
        if self.current is None:
            return
        if self.position() > PREV_RESTART or (self.index == 0 and not self.history):
            self._jump = (self.index, 0.0)
        elif self.index > 0:
            self._jump = (self.index - 1, 0.0)
        else:
            self.tracks.insert(0, self.history.pop())
            self.index += 1  # sangen som spiller flyttet ett hakk ned
            self._jump = (0, 0.0)
        self._kill()

    async def pause(self) -> bool:
        p = self.proc
        if self.paused or p is None or p.returncode is not None:
            return False
        with contextlib.suppress(ProcessLookupError):
            p.send_signal(signal.SIGSTOP)
            self.paused, self._paused_at = True, time.monotonic()
            self.changed()
        return self.paused

    async def resume(self) -> bool:
        p = self.proc
        if not self.paused or p is None or p.returncode is not None:
            return False
        with contextlib.suppress(ProcessLookupError):
            p.send_signal(signal.SIGCONT)
        self._paused_total += time.monotonic() - self._paused_at
        self.paused = False
        self.changed()
        return True

    async def _halt(self) -> None:
        if self.task is not None and not self.task.done():
            self._jump = (-1, 0.0)  # ut av løkka
            self._kill()
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 3)
            except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self.task
        self.task, self._jump = None, None

    async def stop(self) -> None:
        played = self.tracks[:self.index + 1] if self.current else []
        await self._halt()
        self.history = (self.history + played)[-HISTORY:]
        self.tracks, self.index = [], 0
        self.changed()

    # --- tilstand -----------------------------------------------------------------------------------
    def position(self) -> float:
        if self.current is None:
            return 0.0
        now = self._paused_at if self.paused else time.monotonic()
        return max(0.0, self._offset + now - self._started - self._paused_total)

    def status(self) -> dict[str, Any]:
        cur = self.current
        if cur is not None and self.lib is not None:
            with contextlib.suppress(Exception):
                cur = {**cur, "like": self.lib.like(cur["id"])}
        dur = float((cur or {}).get("duration") or 0)
        return {"current": cur, "position": round(min(self.position(), dur) if dur else self.position(), 1),
                "duration": dur, "playing": cur is not None and not self.paused, "paused": cur is not None and self.paused,
                "queue": self.tracks[:200], "index": self.index if cur is not None else 0,
                "upcoming": self.tracks[self.index + 1:self.index + 21] if cur is not None else []}

    def changed(self) -> None:
        """Meld endringen til lytterne – samlet: mange endringer på rad (neste-neste-neste) blir én melding."""
        if not self.listeners:
            return
        self._dirty = True
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            if self._notify_task is None or self._notify_task.done():
                self._notify_task = loop.create_task(self._notify())

    async def _notify(self) -> None:
        while self._dirty:  # en endring mens meldingen sendes, gir én melding til etterpå
            self._dirty = False
            await asyncio.sleep(0.15)
            st = self.status()
            for fn in list(self.listeners):
                with contextlib.suppress(Exception):
                    await fn(st)

    def describe(self) -> str:
        if not self.current:
            return "Ingenting spilles på PC-en."
        c = self.current
        after = ", ".join(s["title"] for s in self.tracks[self.index + 1:self.index + 6])
        return (f"{'Pauset' if self.paused else 'Spiller'}: {c['title']} – {c.get('artist', '?')}"
                + (f"\nNeste: {after}" if after else ""))


PLAYER = Player()
