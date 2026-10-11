"""Nedlastinger for Zeus: torrents via Transmission (bare localhost) og vanlige lenker direkte – og så rett inn i
biblioteket med riktig mappe og navn, cover/plakat og beskjed når det er klart.

Flyt: `start()` → status «laster ned» → `poll()` (hvert 20. sekund fra serveren) ser at den er ferdig →
`import_item()` legger filene i ~/Filmer, ~/Serier, ~/Musikk, ~/Lydbøker eller ~/Bøker (hardlenke når det går,
så torrenten deler videre til ratio 1,0 uten dobbel diskplass), skanner biblioteket og henter cover. Nedlastingene
huskes i lab.db (tabellen downloads), så de overlever omstart.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import httpx

from olympus.medialib import AUDIO_EXT, EBOOK_EXT, SUB_EXT, VIDEO_EXT

STAGING = Path.home() / "Nedlastinger"
ACTIVE = ("laster ned", "lastet ned", "deler")
DIRECT_MAX = 200 * 1024**3  # en direkte lenke kan være en stor film, men ikke uendelig
YTDLP_TIMEOUT = 3 * 3600
AUDIO_PREF = [".flac", ".mp3", ".ogg", ".opus", ".m4a", ".m4b", ".wav"]


def _safe(name: str) -> str:
    """Filnavn uten skråstreker, kontrolltegn eller ledende punktum."""
    name = re.sub(r'[\\/\x00-\x1f<>:"|?*]+', " ", name).strip(" .")
    return re.sub(r"\s+", " ", name)[:150] or "Uten navn"


class Transmission:
    def __init__(self, conf: Path = Path.home() / ".olympus" / "transmission.toml") -> None:
        c = tomllib.loads(conf.read_text()) if conf.exists() else {}
        self.url = c.get("url", "http://127.0.0.1:9091/transmission/rpc")
        self.auth = (c["user"], c["password"]) if c.get("user") else None
        self.session = ""

    def call(self, method: str, **args: Any) -> dict[str, Any]:
        for _ in range(2):
            r = httpx.post(self.url, json={"method": method, "arguments": args}, auth=self.auth, timeout=20,
                           headers={"X-Transmission-Session-Id": self.session})
            if r.status_code == 409:  # CSRF-vern: hent ny økt-id og prøv igjen
                self.session = r.headers.get("X-Transmission-Session-Id", "")
                continue
            r.raise_for_status()
            data = r.json()
            if data.get("result") != "success":
                raise RuntimeError(f"Transmission: {data.get('result')}")
            return data.get("arguments", {})
        raise RuntimeError("Transmission svarte ikke")

    def add(self, torrent: str, folder: Path) -> str:
        import base64

        folder.mkdir(parents=True, exist_ok=True)
        if torrent.startswith(("http://", "https://")):
            # Hent .torrent-fila selv (Transmission henter ikke alle https-adresser) – maks 10 MB, aldri interne adresser.
            from olympus import netsafe

            data, _ = netsafe.fetch_bytes(torrent, max_bytes=10 * 1024 * 1024, timeout=30, any_host=True)
            args = {"metainfo": base64.b64encode(data).decode()}
        elif torrent.startswith("magnet:"):
            args = {"filename": torrent}
        else:
            raise ValueError("Ikke en torrent- eller magnetlenke.")
        a = self.call("torrent-add", **args, **{"download-dir": str(folder)})
        t = a.get("torrent-added") or a.get("torrent-duplicate") or {}
        return t.get("hashString", "")

    def info(self, hashes: list[str]) -> dict[str, dict[str, Any]]:
        if not hashes:
            return {}
        a = self.call("torrent-get", ids=hashes, fields=["hashString", "name", "percentDone", "status", "rateDownload",
                                                          "eta", "error", "errorString", "isFinished", "sizeWhenDone"])
        return {t["hashString"]: t for t in a.get("torrents", [])}

    def remove(self, h: str, delete_data: bool) -> None:
        self.call("torrent-remove", ids=[h], **{"delete-local-data": delete_data})


class Downloads:
    def __init__(self, lab: Any, lib: Any, dirs: dict[str, Path]) -> None:
        self.db = lab.db
        self.lib = lib
        self.dirs = dirs  # music, film, show, audiobook, ebook → mappe i biblioteket
        self.tr = Transmission()
        self.notify: Any = None  # async fn(tekst) – settes av serveren
        self.procs: dict[str, subprocess.Popen] = {}  # yt-dlp som kjører (id → prosess), så de kan avbrytes
        self.cancelled: set[str] = set()  # direkte nedlastinger som skal stoppe
        self.rates: dict[str, int] = {}  # byte/s fra Transmission (siste runde) – vises i appen og widgetene
        self.rates: dict[str, int] = {}  # byte/s fra Transmission (siste runde) – vises i appen og widgetene

    # --- start ------------------------------------------------------------------------------------
    def _new_id(self, *key: str) -> str:
        return f"{int(time.time())}-{abs(hash((*key, time.monotonic_ns()))) % 10**6:06}"

    def start(self, kind: str, title: str, torrent: str = "", direct: str = "", year: int | None = None,
              source: str = "", page: str = "", creator: str = "", identifier: str = "") -> dict[str, Any]:
        if kind not in self.dirs:
            raise ValueError(f"Ukjent type: {kind}")
        if not torrent and not direct:
            raise ValueError("Trenger en torrent-/magnetlenke eller en direkte lenke.")
        did = self._new_id(title, torrent, direct)
        folder = STAGING / ("torrent" if torrent else "direkte") / did
        h = self.tr.add(torrent, folder) if torrent else ""
        meta = {"year": year, "source": source, "page": page, "creator": creator, "identifier": identifier}
        self.db.execute("INSERT INTO downloads VALUES (?, ?, ?, ?, ?, ?, ?, 'laster ned', 0, ?, '', ?, ?)",
                        (did, kind, title, torrent or direct, "torrent" if torrent else "direkte", h, str(folder),
                         json.dumps(meta), time.time(), time.time()))
        self.db.commit()
        return self.get(did)  # type: ignore[return-value]

    def start_ytdlp(self, kind: str, url: str, title: str = "", year: int | None = None, creator: str = "",
                    source: str = "") -> dict[str, Any]:
        """Nedlasting med yt-dlp (YouTube, SoundCloud, Bandcamp …): legges rett i biblioteksmappa. Kjør så
        `fetch_ytdlp(id)` i en tråd."""
        if kind not in ("music", "audiobook", "film", "show"):
            raise ValueError("yt-dlp kan hente musikk, lydbøker, film og serier – ikke e-bøker.")
        did = self._new_id(url, title)
        meta = {"year": year, "source": source, "page": url, "creator": creator, "identifier": ""}
        self.db.execute("INSERT INTO downloads VALUES (?, ?, ?, ?, 'ytdlp', '', ?, 'laster ned', 0, ?, '', ?, ?)",
                        (did, kind, title or url[:150], url, str(STAGING / "ytdlp" / did), json.dumps(meta),
                         time.time(), time.time()))
        self.db.commit()
        return self.get(did)  # type: ignore[return-value]

    def add_local(self, kind: str, folder: Path, title: str, year: int | None = None, creator: str = "",
                  source: str = "opplasting") -> dict[str, Any]:
        """En fil som allerede ligger i en mappe under STAGING (opplastet fra appen, eller en fil Zeus fikk lov å
        importere): sorteres inn med samme navneregler som nedlastingene, med en gang."""
        if kind not in self.dirs:
            raise ValueError(f"Ukjent type: {kind}")
        did = self._new_id(title, str(folder))
        meta = {"year": year, "source": source, "page": "", "creator": creator, "identifier": ""}
        self.db.execute("INSERT INTO downloads VALUES (?, ?, ?, '', 'opplasting', '', ?, 'lastet ned', 1, ?, '', ?, ?)",
                        (did, kind, title, str(folder), json.dumps(meta), time.time(), time.time()))
        self.db.commit()
        try:
            dest = self.import_item(did)
        except (OSError, ValueError, zipfile.BadZipFile) as e:
            self._set(did, status="feilet", error=f"Import: {e}"[:300])
            shutil.rmtree(folder, ignore_errors=True)
            raise ValueError(str(e)) from e
        r = self._row(did)
        self._set(did, status="ferdig", meta=json.dumps({**json.loads(r["meta"] or "{}"), "path": str(dest)}))
        shutil.rmtree(folder, ignore_errors=True)  # biblioteket har sin kopi (hardlenke eller kopi)
        return self.get(did)  # type: ignore[return-value]

    def runner(self, did: str):
        """Funksjonen som skal kjøres i en tråd for denne nedlastingen (None for torrents – Transmission jobber selv)."""
        r = self._row(did)
        if r is None or r["torrent_hash"]:
            return None
        if r["method"] == "ytdlp":
            return self.fetch_ytdlp
        if r["method"].startswith("direkte"):
            return self.fetch_archive if json.loads(r["meta"] or "{}").get("identifier") else self.fetch_direct
        return None

    def fetch_ytdlp(self, did: str) -> None:
        """Kjør yt-dlp (fast argv, ingen skall, ingen config, ingen --exec, bare kjente nettsteder) med utmappe inne i
        biblioteket. Framdriften leses fra utskriften."""
        from olympus.mediaops import ytdlp_base

        r = self._row(did)
        if r is None:
            return
        exe = shutil.which("yt-dlp")
        if not exe:
            self._set(did, status="feilet", error="yt-dlp er ikke installert")
            return
        kind = r["kind"]
        meta = json.loads(r["meta"] or "{}")
        root = self.dirs[kind]
        root.mkdir(parents=True, exist_ok=True)
        esc = lambda s: _safe(s).replace("%", "%%")
        title, year = (r["title"] if r["title"] != r["url"][:150] else ""), meta.get("year")
        if kind == "music":
            layout = ("%(artist,album_artist,uploader|Ukjent)s/%(album,playlist_title|Singler)s/"
                      "%(track_number|)s%(track_number& |)s%(track,title)s.%(ext)s")
        elif kind == "audiobook":
            layout = (esc(title) if title else "%(album,playlist_title,title)s") + "/%(title)s.%(ext)s"
        elif kind == "film":
            name = esc(f"{title} ({year})" if title and year else title) if title else "%(title)s"
            layout = f"{name}/{name}.%(ext)s"
        else:
            layout = (esc(title) if title else "%(series,playlist_title,uploader|Serie)s") + "/%(title)s.%(ext)s"
        fmt = (["-f", "bestaudio/best", "-x", "--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg"]
               if kind in ("music", "audiobook") else
               ["-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mkv", "--embed-metadata"])
        argv = [*ytdlp_base(exe), "--no-overwrites", "--continue", "--newline", "--progress",
                "--progress-template", "download:OLYP %(progress._percent_str)s",
                "--print", "before_dl:OLYI %(playlist_index|1)s %(n_entries|1)s", *fmt, "--playlist-end", "300",
                "-o", str(root / layout), "--print", "after_move:OLYF %(filepath)s", "--", r["url"]]
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    cwd=root, text=True, errors="replace", start_new_session=True)
        except OSError as e:
            self._set(did, status="feilet", error=f"yt-dlp: {e}"[:300])
            return
        self.procs[did] = proc
        timer = threading.Timer(YTDLP_TIMEOUT, proc.kill)
        timer.start()
        files, errors, idx, n, last = [], [], 1, 1, 0.0
        dests: list[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("OLYI "):
                    parts = line.split()
                    idx = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else idx
                    n = max(1, int(parts[2])) if len(parts) > 2 and parts[2].isdigit() else n
                elif line.startswith("OLYP "):
                    m = re.search(r"(\d+(?:\.\d+)?)%", line)
                    if m and time.time() - last > 1:
                        frac = (min(idx, n) - 1 + float(m.group(1)) / 100) / n
                        self._set(did, progress=round(min(frac, 0.99), 3))
                        last = time.time()
                elif line.startswith("OLYF "):
                    files.append(line[5:].strip())
                elif "Destination: " in line:  # det yt-dlp skriver akkurat nå (brukes for å rydde ved avbrudd)
                    dests.append(line.split("Destination: ", 1)[1].strip())
                elif "ERROR" in line:
                    errors.append(line[:300])
            proc.wait()
        finally:
            timer.cancel()
            self.procs.pop(did, None)
        if did in self.cancelled or (self._row(did) or {"status": ""})["status"] == "avbrutt":
            self.cancelled.discard(did)
            _rm_partials(root, dests)
            return
        lib_root = root.resolve()
        inside = [f for f in files if lib_root in Path(f).resolve().parents]
        if not inside:
            self._set(did, status="feilet", error=(errors[-1] if errors else "Ingenting ble lastet ned.")[:300])
            return
        folder = Path(os.path.commonpath(inside)) if len(inside) > 1 else Path(inside[0]).parent
        if not title:  # fikk ingen tittel fra brukeren: bruk det yt-dlp fant
            self._set(did, title=(folder.name if len(inside) > 1 else Path(inside[0]).stem)[:150])
        self._set(did, status="lastet ned", progress=1.0, error=("; ".join(errors[-2:]))[:300],
                  meta=json.dumps({**meta, "path": str(folder), "files": len(inside), "paths": inside[:500]}))

    def fetch_direct(self, did: str) -> None:
        """Last ned en direkte lenke (kjøres i en tråd). Filnavn fra lenken/serveren, aldri utenfor mappa."""
        row = self._row(did)
        if row is None:
            return
        from urllib.parse import unquote

        from olympus import netsafe

        folder = Path(row["folder"])
        folder.mkdir(parents=True, exist_ok=True)
        try:
            # Gjennom netsafe: bare offentlige adresser (sjekket ved hver tilkobling og videresending), maks størrelse/tid.
            with netsafe.open_url(row["url"], max_bytes=DIRECT_MAX, timeout=24 * 3600, any_host=True) as r:
                total = r.length or 0
                cd = r.headers.get("content-disposition", "")
                m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd)
                raw = unquote(m.group(1)) if m else unquote(httpx.URL(r.url).path.rsplit("/", 1)[-1])
                name = _safe(Path(raw.replace("\\", "/")).name) or "fil"
                done = 0
                last = 0.0
                with open(folder / name, "wb") as f:
                    for chunk in r.iter_bytes(1 << 16):
                        if did in self.cancelled:
                            raise InterruptedError("avbrutt")
                        f.write(chunk)
                        done += len(chunk)
                        if total and time.time() - last > 1:
                            self._set(did, progress=min(done / total, 0.99))
                            last = time.time()
            self._set(did, status="lastet ned", progress=1.0)
        except InterruptedError:
            self.cancelled.discard(did)
            shutil.rmtree(folder, ignore_errors=True)
        except (httpx.HTTPError, OSError, netsafe.UnsafeURL) as e:
            self._set(did, status="feilet", error=f"{type(e).__name__}: {e}"[:300])

    def fetch_archive(self, did: str) -> None:
        """Reserve for Archive-torrents som ikke kommer i gang: hent bare filene som trengs, direkte over HTTP."""
        r = self._row(did)
        if r is None:
            return
        ident = json.loads(r["meta"] or "{}").get("identifier", "")
        folder = Path(r["folder"])
        folder.mkdir(parents=True, exist_ok=True)
        try:
            meta = httpx.get(f"https://archive.org/metadata/{ident}", timeout=30).json()
            files = [f for f in meta.get("files", []) if f.get("source") in ("original", "derivative")]
            names = _pick_archive_files(files, r["kind"])
            if not names:
                raise ValueError("fant ingen passende filer")
            total = sum(int(f.get("size") or 0) for f in files if f["name"] in names) or 1
            done = 0
            for name in names:
                target = folder / _safe(Path(name).name)
                with httpx.stream("GET", f"https://archive.org/download/{ident}/{name}", follow_redirects=True,
                                  timeout=60) as resp:
                    resp.raise_for_status()
                    with open(target, "wb") as out:
                        for chunk in resp.iter_bytes(1 << 16):
                            out.write(chunk)
                            done += len(chunk)
                self._set(did, progress=min(done / total, 0.99))
            self._set(did, status="lastet ned", progress=1.0, method="direkte", torrent_hash="")
        except (httpx.HTTPError, OSError, ValueError) as e:
            self._set(did, status="feilet", error=f"{type(e).__name__}: {e}"[:300])

    # --- status -----------------------------------------------------------------------------------
    def _row(self, did: str):
        return self.db.execute("SELECT * FROM downloads WHERE id = ?", (did,)).fetchone()

    def _set(self, did: str, **vals: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in vals)
        self.db.execute(f"UPDATE downloads SET {sets}, updated = ? WHERE id = ?", (*vals.values(), time.time(), did))
        self.db.commit()

    @staticmethod
    def _dict(r) -> dict[str, Any]:
        meta = json.loads(r["meta"] or "{}")
        if "path" in meta:
            meta["path"] = _home(meta["path"])
        meta.pop("paths", None)
        return {**meta, "id": r["id"], "kind": r["kind"], "title": r["title"], "method": r["method"],
                "status": r["status"], "progress": r["progress"], "error": r["error"], "added": r["added"],
                "updated": r["updated"]}

    def get(self, did: str) -> dict[str, Any] | None:
        r = self._row(did)
        return {**self._dict(r), "rate": self.rates.get(did, 0)} if r else None

    def list(self, limit: int = 30) -> list[dict[str, Any]]:
        return [{**self._dict(r), "rate": self.rates.get(r["id"], 0)}
                for r in self.db.execute("SELECT * FROM downloads ORDER BY added DESC LIMIT ?", (limit,))]

    def cancel(self, did: str) -> bool:
        r = self._row(did)
        if r is None:
            return False
        if r["status"] not in ACTIVE:
            return True  # allerede ferdig, feilet eller avbrutt: ingenting å stoppe
        if r["torrent_hash"]:
            try:
                self.tr.remove(r["torrent_hash"], delete_data=True)
            except (httpx.HTTPError, RuntimeError):
                pass
        self.cancelled.add(did)
        proc = self.procs.get(did)
        if proc is not None:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, 15)  # yt-dlp og ffmpeg-barna
        self._rm_staging(r["folder"])
        self._set(did, status="avbrutt")
        return True

    @staticmethod
    def _rm_staging(folder: str) -> None:
        """Slett bare mapper under nedlastingsmappa – aldri noe i biblioteket."""
        p = Path(folder or "/")
        if STAGING.resolve() in p.resolve().parents:
            shutil.rmtree(p, ignore_errors=True)

    def forget(self, did: str) -> bool:
        """Fjern fra lista (avbryter først hvis den pågår). Filer som er kommet inn i biblioteket, blir liggende."""
        r = self._row(did)
        if r is None:
            return False
        if r["status"] in ACTIVE:
            self.cancel(did)
        self.db.execute("DELETE FROM downloads WHERE id = ?", (did,))
        self.db.commit()
        return True

    def retry(self, did: str) -> dict[str, Any]:
        """Prøv en feilet eller avbrutt nedlasting på nytt. KeyError: ukjent. ValueError: pågår eller er ferdig.
        Kalleren starter `runner(id)` i en tråd etterpå."""
        r = self._row(did)
        if r is None:
            raise KeyError(did)
        if r["status"] in ACTIVE:
            raise ValueError("nedlastingen pågår allerede")
        if r["status"] == "ferdig":
            raise ValueError("nedlastingen er allerede ferdig")
        if r["method"] == "opplasting":
            raise ValueError("en opplasting kan ikke prøves på nytt – last opp fila igjen")
        self.cancelled.discard(did)
        self._rm_staging(r["folder"])
        h = ""
        if r["method"] == "torrent":
            h = self.tr.add(r["url"], Path(r["folder"]))
        self._set(did, status="laster ned", progress=0.0, error="", torrent_hash=h)
        return self.get(did)  # type: ignore[return-value]

    async def poll(self) -> list[str]:
        """Oppdater framdrift, importer det som er ferdig, rydd torrents som har delt ferdig. Returnerer titlene
        som ble klare."""
        import asyncio

        ready = []
        rows = self.db.execute("SELECT * FROM downloads WHERE status IN ('laster ned', 'lastet ned', 'deler')").fetchall()
        hashes = [r["torrent_hash"] for r in rows if r["torrent_hash"]]
        try:
            info = await asyncio.to_thread(self.tr.info, hashes)
        except (httpx.HTTPError, RuntimeError, OSError):
            info = {}
        for r in rows:
            t = info.get(r["torrent_hash"]) if r["torrent_hash"] else None
            meta = json.loads(r["meta"] or "{}")
            stalled = (t is not None and t.get("percentDone", 0) == 0 and not t.get("rateDownload")
                       and time.time() - r["added"] > 180)
            if r["status"] == "laster ned" and stalled and meta.get("identifier"):
                # Ingen deler torrenten (og webseeds kommer ikke i gang): hent direkte fra Archive i stedet.
                try:
                    await asyncio.to_thread(self.tr.remove, r["torrent_hash"], True)
                except (httpx.HTTPError, RuntimeError):
                    pass
                self._set(r["id"], torrent_hash="", method="direkte (reserve)")
                asyncio.get_running_loop().run_in_executor(None, self.fetch_archive, r["id"])
                continue
            if r["status"] == "laster ned" and t is not None:
                if t.get("error"):
                    self._set(r["id"], error=str(t.get("errorString", ""))[:300])
                self._set(r["id"], progress=float(t.get("percentDone", 0)))
                self.rates[r["id"]] = int(t.get("rateDownload") or 0)
                self.rates[r["id"]] = int(t.get("rateDownload") or 0)
                if t.get("percentDone", 0) >= 1:
                    self._set(r["id"], status="lastet ned")
            r = self._row(r["id"])
            if r["status"] == "lastet ned":
                try:
                    verdict = await asyncio.to_thread(self.virus_check, r)
                    if not verdict.clean:
                        await asyncio.to_thread(self._destroy, r)
                        self._set(r["id"], status="virus", error=verdict.summary()[:300])
                        if self.notify:
                            await self.notify(f"⚠ «{r['title']}» inneholdt virus/trojaner og er slettet: "
                                              f"{verdict.summary()[:200]}")
                        continue
                    dest = await asyncio.to_thread(self.import_item, r["id"])
                    self._apply_names(r, dest)
                    self._set(r["id"], status="deler" if r["torrent_hash"] else "ferdig", error="",
                              meta=json.dumps({**json.loads(r["meta"] or "{}"), "path": str(dest),
                                               "virus": verdict.summary()}))
                    if not r["torrent_hash"]:
                        self._rm_staging(r["folder"])  # direkte: biblioteket har kopien, mellomlageret skal ikke holde på plassen
                    ready.append(r["title"])
                except (OSError, ValueError, zipfile.BadZipFile) as e:
                    self._set(r["id"], status="feilet", error=f"Import: {e}"[:300])
            elif r["status"] == "deler" and t is not None and t.get("isFinished"):
                # Delt ferdig (ratio 1,0 eller tomgang): fjern torrenten og nedlastingsmappa – biblioteket har sin kopi.
                try:
                    await asyncio.to_thread(self.tr.remove, r["torrent_hash"], True)
                except (httpx.HTTPError, RuntimeError):
                    pass
                shutil.rmtree(r["folder"], ignore_errors=True)
                self._set(r["id"], status="ferdig")
        if ready:
            await asyncio.to_thread(self.lib.scan)
            await asyncio.to_thread(self.lib.fetch_art, 10)
            if self.notify:
                for title in ready:
                    await self.notify(f"↓ «{title}» er klar i biblioteket.")
        return ready

    def set_names(self, did: str, names: dict[str, str]) -> None:
        """Tittel/artist du skrev selv ved «legg til»: huskes til importen og legges da over filenes egne tagger."""
        r = self._row(did)
        names = {k: str(v)[:200] for k, v in names.items() if k in ("title", "artist") and v}
        if r is not None and names:
            self._set(did, meta=json.dumps({**json.loads(r["meta"] or "{}"), "named": names}))

    def _apply_names(self, r: Any, dest: Path) -> None:
        """Legg navnene du skrev over taggene som overstyringer i lab.db – filene røres ikke (torrenten deler dem
        videre via hardlenker, og en endret fil ville ødelagt delingen)."""
        named = json.loads(r["meta"] or "{}").get("named") or {}
        if not named or r["kind"] == "film" or not hasattr(self.lib, "set_override"):
            return
        title, artist = named.get("title"), named.get("artist")
        own = [Path(p) for p in json.loads(r["meta"] or "{}").get("paths") or []]  # yt-dlp: akkurat filene den skrev
        if r["method"] == "ytdlp" and not own:
            return  # mappa kan være felles med andre album – rør ingenting vi ikke vet er vårt
        if own:
            dest = own[0] if len(own) == 1 else dest
        if r["kind"] == "music":
            files = own or ([dest] if dest.is_file() else [p for p in dest.rglob("*") if p.suffix.lower() in AUDIO_EXT])
            fields = {k: v for k, v in {"album": title, "album_artist": artist}.items() if v}
        elif r["kind"] == "show":
            files = own or [p for p in ([dest] if dest.is_file() else dest.rglob("*")) if p.suffix.lower() in VIDEO_EXT]
            fields = {"album": title} if title else {}
        else:  # lydbok (mappa eller fila) og e-bok: boka er dest
            files, fields = [dest], {k: v for k, v in {"album": title, "artist": artist}.items() if v}
        root = self.dirs[r["kind"]].resolve()
        for f in files if fields else []:
            # Bare noe som ligger INNI denne typens bibliotekmappe – aldri selve mappa eller noe ved siden av den.
            if root not in f.resolve().parents:
                continue
            with contextlib.suppress(Exception):
                self.lib.set_override(f, fields)

    # --- viruskontroll ------------------------------------------------------------------------------
    def virus_check(self, r: Any) -> Any:
        """Sjekk det som er lastet ned før det slippes inn (se olympus.virus). Zip pakkes ut først, så innholdet sjekkes."""
        from olympus import virus

        meta = json.loads(r["meta"] or "{}")
        if r["method"] == "ytdlp":
            paths = [Path(p) for p in meta.get("paths") or []]
        else:
            src = Path(r["folder"])
            if src.is_dir():
                _unzip_all(src)
            paths = [src]
        return virus.scan(paths, AUDIO_EXT | VIDEO_EXT | SUB_EXT | EBOOK_EXT)

    def _destroy(self, r: Any) -> None:
        """Smittet nedlasting: slett alt den la fra seg (torrenten med data, mellomlageret, yt-dlp-filene)."""
        if r["torrent_hash"]:
            with contextlib.suppress(Exception):
                self.tr.remove(r["torrent_hash"], True)
        if r["method"] == "ytdlp":
            root = self.dirs[r["kind"]].resolve()
            for p in json.loads(r["meta"] or "{}").get("paths") or []:
                f = Path(p)
                if root in f.resolve().parents and f.is_file() and not f.is_symlink():
                    f.unlink(missing_ok=True)
        else:
            self._rm_staging(r["folder"])

    # --- import -----------------------------------------------------------------------------------
    def import_item(self, did: str) -> Path:
        r = self._row(did)
        if r is None:
            raise ValueError("ukjent nedlasting")
        src = Path(r["folder"])
        meta = json.loads(r["meta"] or "{}")
        title, kind, year = _safe(r["title"]), r["kind"], meta.get("year")
        if r["method"] == "ytdlp":  # yt-dlp la filene rett i biblioteket
            dest = Path(meta.get("path") or "")
            lib_root = self.dirs[kind].resolve()
            if not meta.get("path") or (lib_root not in dest.resolve().parents and dest.resolve() != lib_root):
                raise ValueError("fant ikke filene yt-dlp lastet ned")
            return dest
        _unzip_all(src)
        root = src.resolve()
        # Bare vanlige filer som faktisk ligger i nedlastingsmappa (ingen symlenker eller stier ut av den).
        files = [p for p in src.rglob("*") if p.is_file() and not p.is_symlink() and not p.name.startswith(".")
                 and root in p.resolve().parents]
        if kind == "film":
            videos = sorted((p for p in files if p.suffix.lower() in VIDEO_EXT), key=lambda p: p.stat().st_size, reverse=True)
            if not videos:
                raise ValueError("fant ingen videofil")
            name = f"{title} ({year})" if year else title
            dest = self.dirs["film"] / name
            main = videos[0]
            _place(main, dest / f"{name}{main.suffix.lower()}")
            for sub in (p for p in files if p.suffix.lower() in SUB_EXT):
                _place(sub, dest / f"{name}.{sub.stem.rsplit('.', 1)[-1] if '.' in sub.stem else 'ekstern'}{sub.suffix.lower()}")
        elif kind == "show":
            dest = self.dirs["show"] / title
            for p in files:
                if p.suffix.lower() in VIDEO_EXT | SUB_EXT:
                    _place(p, dest / _safe(p.name))
        elif kind in ("music", "audiobook"):
            audio = [p for p in files if p.suffix.lower() in AUDIO_EXT]
            if not audio:
                raise ValueError("fant ingen lydfiler")
            # Archive har ofte samme spor i flere formater: ta bare det beste formatet.
            best = next((ext for ext in AUDIO_PREF if any(p.suffix.lower() == ext for p in audio)), audio[0].suffix.lower())
            audio = [p for p in audio if p.suffix.lower() == best]
            creator = _safe(meta.get("creator") or "Ukjent")
            dest = (self.dirs["music"] / creator / title) if kind == "music" else (self.dirs["audiobook"] / title)
            for p in audio:
                _place(p, dest / _safe(p.name))
            cover = next((p for p in files if p.name.lower() in ("cover.jpg", "folder.jpg", "cover.png")), None)
            if cover:
                _place(cover, dest / ("cover" + cover.suffix.lower()))
        elif kind == "ebook":
            books = [p for p in files if p.suffix.lower() in EBOOK_EXT]
            if not books:
                raise ValueError("fant ingen e-bok")
            b = next((p for p in books if p.suffix.lower() == ".epub"), books[0])
            dest = self.dirs["ebook"] / f"{title}{b.suffix.lower()}"
            _place(b, dest)
        else:
            raise ValueError(f"ukjent type {kind}")
        lib_root = self.dirs["film" if kind == "film" else kind].resolve()
        if lib_root not in dest.resolve().parents and dest.resolve() != lib_root:
            raise ValueError("målet havnet utenfor biblioteket")
        return dest


def _home(p: str) -> str:
    try:
        return "~/" + str(Path(p).relative_to(Path.home()))
    except ValueError:
        return p


def _pick_archive_files(files: list[dict[str, Any]], kind: str) -> list[str]:
    """Hvilke filer i et Archive-element som trengs: beste lydformat, største video (+ undertekster), EPUB/PDF."""
    by_ext: dict[str, list[dict[str, Any]]] = {}
    for f in files:
        by_ext.setdefault(Path(f["name"]).suffix.lower(), []).append(f)
    if kind in ("music", "audiobook"):
        ext = next((e for e in AUDIO_PREF if e in by_ext), None)
        chosen = [f["name"] for f in by_ext.get(ext, [])] if ext else []
        chosen += [f["name"] for f in files if f["name"].lower() in ("cover.jpg", "folder.jpg")][:1]
        return chosen
    if kind in ("film", "show"):
        vids = sorted((f for f in files if Path(f["name"]).suffix.lower() in VIDEO_EXT), key=lambda f: int(f.get("size") or 0),
                      reverse=True)
        if kind == "film":
            vids = vids[:1]
        return [f["name"] for f in vids] + [f["name"] for f in files if Path(f["name"]).suffix.lower() in SUB_EXT]
    if kind == "ebook":
        for ext in (".epub", ".pdf", ".txt"):
            if ext in by_ext:
                return [by_ext[ext][0]["name"]]
    return []


def _rm_partials(root: Path, dests: list[str]) -> None:
    """Etter en avbrutt yt-dlp-nedlasting: fjern bare halvfilene til DENNE nedlastingen – filene yt-dlp selv meldte at
    den skrev («Destination: …») med endelsene .part/.ytdl/.part-FragN – og mapper som ble tomme. Aldri andre
    nedlastingers filer, aldri ferdige filer, aldri noe utenfor bibliotekmappa."""
    root = root.resolve()
    touched: set[Path] = set()
    for d in dict.fromkeys(dests):
        base = Path(d) if Path(d).is_absolute() else root / d
        with contextlib.suppress(OSError):
            base = base.resolve()
        if root not in base.parents:
            continue
        candidates = [base.with_name(base.name + ext) for ext in (".part", ".ytdl")]
        candidates += list(base.parent.glob(glob_escape(base.name) + ".part-Frag*")) if base.parent.is_dir() else []
        for f in candidates:
            with contextlib.suppress(OSError):
                if f.is_file() and not f.is_symlink():
                    f.unlink()
                    touched.add(f.parent)
    for d in sorted(touched, key=lambda p: len(p.parts), reverse=True):
        while d != root and root in d.parents:
            try:
                d.rmdir()
            except OSError:
                break
            d = d.parent


def glob_escape(name: str) -> str:
    import glob

    return glob.escape(name)


def _place(src: Path, dest: Path) -> None:
    """Hardlenke (samme disk: ingen ekstra plass, torrenten kan dele videre), ellers kopi. Overskriver aldri."""
    if src.is_symlink() or ".." in dest.parts:
        raise ValueError(f"ugyldig sti: {dest.name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


def _unzip_all(folder: Path) -> None:
    """Pakk ut zip-filer trygt (ingen stier ut av mappa, ingen symlenker, maks 20 GB)."""
    for z in list(folder.rglob("*.zip")):
        target = z.with_suffix("")
        with zipfile.ZipFile(z) as zf:
            total = 0
            for info in zf.infolist():
                total += info.file_size
                if total > 20 * 1024**3:
                    raise ValueError("arkivet er for stort")
                name = Path(info.filename)
                if name.is_absolute() or ".." in name.parts or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    continue
                out = (target / name).resolve()
                if target.resolve() not in out.parents and out != target.resolve():
                    continue
                if info.is_dir():
                    out.mkdir(parents=True, exist_ok=True)
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        z.unlink()


def open_downloads(c: Any, lab: Any, lib: Any) -> Downloads:
    from olympus.config import media_dir

    return Downloads(lab, lib, {k: media_dir(c, k) for k in ("music", "film", "show", "audiobook", "ebook")})
