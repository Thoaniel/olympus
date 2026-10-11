"""Ripping av egne plater fra PC-ens plateleser (også USB): lyd-CD → FLAC med navn fra MusicBrainz; DVD → hovedfilmen
som MKV uten tap (bilde, alle lyd- og undertekstspor kopieres). Biblioteket skannes når den er ferdig.

Verktøy: cd-info/cd-paranoia (libcdio, libcdio-paranoia) og ffmpeg (dvdvideo). Kopibeskyttede DVD-er krever
libdvdcss (ikke installert som standard).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

HOME = Path.home()


def drives() -> list[str]:
    return sorted(str(p) for p in Path("/dev").glob("sr[0-9]*"))


def _run(args: list[str], timeout: float = 30) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def disc_info(dev: str) -> dict[str, Any]:
    """{"kind": audio | dvd | data | empty, "label": …, "tracks": [(nr, lsn)], "leadout": lsn}"""
    out = _run(["cd-info", "--no-header", "--no-device-info", dev])
    if not out or "no disc" in out.lower() or "cannot" in out.lower():
        return {"kind": "empty"}
    m = re.search(r"Disc mode is listed as:\s*(.+)", out)
    mode = m.group(1).strip() if m else ""
    tracks = [(int(n), int(lsn)) for n, lsn in re.findall(r"^\s*(\d+):\s+\d+:\d+:\d+\s+(\d+)\s+audio", out, re.M)]
    leadout = re.search(r"^\s*170:\s+\d+:\d+:\d+\s+(\d+)\s+leadout", out, re.M)
    label = _run(["lsblk", "-no", "LABEL", dev]).strip()
    if tracks:
        return {"kind": "audio", "label": label, "tracks": tracks, "leadout": int(leadout.group(1)) if leadout else 0}
    if "DVD" in mode.upper() or _run(["iso-info", "-l", "-i", dev]).find("VIDEO_TS") >= 0:
        return {"kind": "dvd", "label": label}
    return {"kind": "data", "label": label}


def musicbrainz_discid(tracks: list[tuple[int, int]], leadout: int) -> str:
    """MusicBrainz plate-ID fra innholdsfortegnelsen (sektorer + 150 for lead-in)."""
    first, last = tracks[0][0], tracks[-1][0]
    offsets = {n: lsn + 150 for n, lsn in tracks}
    s = f"{first:02X}{last:02X}{leadout + 150:08X}" + "".join(f"{offsets.get(i, 0):08X}" for i in range(1, 100))
    return base64.b64encode(hashlib.sha1(s.encode()).digest()).decode().replace("+", ".").replace("/", "_").replace("=", "-")


def musicbrainz_release(discid: str) -> dict[str, Any] | None:
    import httpx

    try:
        r = httpx.get(f"https://musicbrainz.org/ws/2/discid/{discid}", params={"inc": "artist-credits recordings", "fmt": "json"},
                      headers={"User-Agent": "OlympusLab/0.2 (personlig mediabibliotek)"}, timeout=20)
        if r.status_code != 200:
            return None
        rel = (r.json().get("releases") or [None])[0]
    except (httpx.HTTPError, ValueError):
        return None
    if not rel:
        return None
    artist = "".join(a.get("name", "") + a.get("joinphrase", "") for a in rel.get("artist-credit", [])) or "Ukjent"
    titles = [t.get("title", "") for m in rel.get("media", []) for t in m.get("tracks", [])]
    return {"artist": artist, "album": rel.get("title") or "Ukjent album", "year": (rel.get("date") or "")[:4], "titles": titles}


def safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name).strip(" .")[:120] or "Ukjent"


class Ripper:
    """Én ripping om gangen; status leses av appen/Lab's/Zeus."""

    def __init__(self) -> None:
        self.job: dict[str, Any] = {}
        self.dirs: dict[str, Path] = {}  # settes av serveren (config.media_dir)
        self.task: asyncio.Task | None = None

    def status(self) -> dict[str, Any]:
        return {"drives": [{"dev": d, **{k: v for k, v in disc_info(d).items() if k in ("kind", "label")}} for d in drives()],
                "job": self.job or None, "dvdcss": Path("/usr/lib/libdvdcss.so.2").exists()}

    def start(self, dev: str, title: str = "", on_done: Any = None) -> dict[str, Any]:
        if self.task and not self.task.done():
            raise RuntimeError("Ripper allerede en plate.")
        if dev not in drives():
            raise ValueError(f"Fant ingen plateleser {dev}.")
        info = disc_info(dev)
        if info["kind"] not in ("audio", "dvd"):
            raise ValueError("Ingen lyd-CD eller DVD i spilleren." if info["kind"] == "empty" else "Platen er verken lyd-CD eller DVD.")
        self.job = {"dev": dev, "kind": info["kind"], "status": "starter", "progress": 0.0, "title": title or info.get("label") or "",
                    "started": time.time(), "error": ""}
        coro = self._cd(dev, info) if info["kind"] == "audio" else self._dvd(dev, title or info.get("label") or "")
        self.task = asyncio.create_task(self._wrap(coro, on_done))
        return self.job

    async def _wrap(self, coro: Any, on_done: Any) -> None:
        try:
            await coro
            self.job.update(status="ferdig", progress=1.0)
        except Exception as e:  # noqa: BLE001 – vises i status
            self.job.update(status="feilet", error=str(e)[:300])
        if on_done:
            await on_done()

    async def _cd(self, dev: str, info: dict[str, Any]) -> None:
        rel = await asyncio.to_thread(musicbrainz_release, musicbrainz_discid(info["tracks"], info["leadout"]))
        artist, album = (rel["artist"], rel["album"]) if rel else ("Ukjent artist", info.get("label") or time.strftime("CD %Y-%m-%d"))
        folder = self.dirs.get("music", HOME / "Music") / safe(artist) / safe(album)
        folder.mkdir(parents=True, exist_ok=True)
        self.job.update(title=f"{artist} – {album}", status="ripper")
        n = len(info["tracks"])
        for i, (nr, _) in enumerate(info["tracks"]):
            title = (rel["titles"][i] if rel and i < len(rel["titles"]) else f"Spor {nr}")
            wav = folder / f".spor{nr:02d}.wav"
            p = await asyncio.create_subprocess_exec("cd-paranoia", "-q", "-d", dev, str(nr), str(wav),
                                                     stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            if await p.wait() != 0 or not wav.exists():
                raise RuntimeError(f"Klarte ikke å lese spor {nr}.")
            out = folder / f"{nr:02d} {safe(title)}.flac"
            meta = ["-metadata", f"title={title}", "-metadata", f"artist={artist}", "-metadata", f"album={album}",
                    "-metadata", f"track={nr}/{n}", *(["-metadata", f"date={rel['year']}"] if rel and rel["year"] else [])]
            p = await asyncio.create_subprocess_exec("ffmpeg", "-v", "error", "-y", "-i", str(wav), "-c:a", "flac", *meta, str(out),
                                                     stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await p.wait()
            wav.unlink(missing_ok=True)
            self.job["progress"] = (i + 1) / n

    async def _dvd(self, dev: str, name: str) -> None:
        """Lengste tittel (hovedfilmen) → MKV, alt kopiert uten omkoding."""
        best, best_dur = 1, 0.0
        for t in range(1, 41):
            out = await asyncio.to_thread(_run, ["ffprobe", "-v", "error", "-f", "dvdvideo", "-title", str(t), "-i", dev,
                                                 "-show_entries", "format=duration", "-of", "csv=p=0"], 60)
            try:
                d = float(out.strip())
            except ValueError:
                if t > 1:
                    break
                continue
            if d > best_dur:
                best, best_dur = t, d
        if best_dur <= 0:
            raise RuntimeError("Fant ingen titler (kopibeskyttet? Da trengs libdvdcss: sudo pacman -S libdvdcss).")
        name = safe(name.replace("_", " ").title() if name.isupper() else name) or time.strftime("DVD %Y-%m-%d")
        folder = self.dirs.get("film", HOME / "Videos" / "Movies") / name
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / f"{name}.mkv"
        self.job.update(title=name, status="ripper")
        p = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-y", "-progress", "pipe:1", "-f", "dvdvideo", "-title", str(best), "-i", dev,
            "-map", "0:v", "-map", "0:a?", "-map", "0:s?", "-c", "copy", str(out),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        assert p.stdout is not None
        async for line in p.stdout:
            m = re.match(rb"out_time_us=(\d+)", line)
            if m and best_dur:
                self.job["progress"] = min(0.99, int(m.group(1)) / 1e6 / best_dur)
        if await p.wait() != 0:
            err = (await p.stderr.read()).decode(errors="replace")[-300:] if p.stderr else ""
            raise RuntimeError(f"ffmpeg feilet: {err}")


RIPPER = Ripper()


def have_tools() -> bool:
    return all(shutil.which(t) for t in ("cd-info", "cd-paranoia", "ffmpeg"))
