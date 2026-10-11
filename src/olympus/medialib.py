"""Olympus' eget mediebibliotek: musikk, spillelister, bøker (lyd og e-bok) og film/serier – uten Navidrome,
Jellyfin eller Audiobookshelf.

Alt ligger i ~/.olympus/media.db. `scan()` går gjennom mappene (standard ~/Musikk, ~/Lydbøker, ~/Bøker, ~/Filmer,
~/Serier) og leser bare filer som er nye eller endret. Musikk: tagger og cover via mutagen. Lydbøker: én mappe
eller én .m4b = én bok, kapitler fra filene eller fra ffprobe. Film: navnet («Tittel (1927)», «Serie S01E02») og
ffprobe gir tittel, år, varighet og kodeker; et bilde fra filmen blir plakat. Hvor langt du er kommet (bøker,
film) lagres i `progress`, så PC og telefon er enige.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import time
import zipfile
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

AUDIO_EXT = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav", ".wma", ".alac", ".aiff", ".m4b"}
VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".wmv", ".mpg", ".mpeg"}
EBOOK_EXT = {".epub", ".pdf", ".txt"}
SUB_EXT = {".srt", ".vtt", ".ass", ".ssa"}
EPISODE = re.compile(r"(?i)^(?P<show>.*?)[ ._-]*s(?P<s>\d{1,2})[ ._-]*e(?P<e>\d{1,3})")
YEAR = re.compile(r"^(?P<title>.*?)[ ._(\[]*(?P<year>(19|20)\d\d)[)\] ._]*")
# Formater telefonen spiller direkte; alt annet omkodes.
DIRECT_VIDEO = {"h264", "hevc", "vp9", "av1"}
DIRECT_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}
# ffmpeg/ffprobe får bare lese lokale filer i vanlige lyd-/video-/undertekstformater. Spillelisteformater (HLS,
# concat …) kunne ellers få en ondsinnet «videofil» til å lese andre filer på maskinen eller hente fra nettet.
SAFE_INPUT = ["-protocol_whitelist", "file,pipe", "-format_whitelist",
              "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mpegts,mp3,flac,ogg,wav,aac,asf,mpeg,mpegvideo,m4v,flv,"
              "aiff,ape,wv,dts,ac3,eac3,srt,ass,webvtt"]


# Indeksen over filene: kan alltid bygges på nytt (slett media.db og skann). Dine data ligger i lab.db.
MEDIA_SCHEMA = [
    """
    CREATE TABLE tracks (id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, title TEXT, artist TEXT, album TEXT,
        album_artist TEXT, album_id TEXT, track_no INTEGER, disc_no INTEGER, year INTEGER, duration REAL, mtime REAL,
        cover INTEGER DEFAULT 0, genre TEXT DEFAULT '');
    CREATE INDEX tracks_album ON tracks(album_id);
    CREATE INDEX tracks_artist ON tracks(artist COLLATE NOCASE);
    CREATE TABLE books (id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, kind TEXT, title TEXT, author TEXT,
        duration REAL, mtime REAL);
    CREATE TABLE book_parts (book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE, idx INTEGER, path TEXT,
        title TEXT, start REAL, duration REAL, PRIMARY KEY (book_id, idx));
    CREATE TABLE videos (id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, kind TEXT, title TEXT, year INTEGER, show TEXT,
        season INTEGER, episode INTEGER, duration REAL, vcodec TEXT, acodec TEXT, container TEXT, mtime REAL);
    CREATE INDEX videos_show ON videos(kind, show, season, episode);
    CREATE TABLE subtitles (video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE, idx INTEGER, lang TEXT,
        title TEXT, path TEXT, stream INTEGER, PRIMARY KEY (video_id, idx));
    CREATE TABLE art_tried (key TEXT PRIMARY KEY, ts REAL, ok INTEGER);
    """,
]


def _legacy_user_data(db_path: Path) -> dict[str, Any] | None:
    """Fra den første utgaven av media.db (spillelister og framdrift lå der, id-er fra filbanen): les ut dine data
    og flytt den gamle fila til side, så indeksen bygges på nytt med stabile id-er."""
    if not db_path.exists():
        return None
    old = sqlite3.connect(db_path)
    try:
        if old.execute("PRAGMA user_version").fetchone()[0] != 0:
            return None
        names = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "tracks" not in names:
            return None
        data: dict[str, Any] = {"paths": {}}
        for table in ("tracks", "videos", "books"):
            if table in names:
                data["paths"].update(dict(old.execute(f"SELECT id, path FROM {table}").fetchall()))
        data["playlists"] = old.execute("SELECT id, name, created, updated FROM playlists").fetchall() if "playlists" in names else []
        data["items"] = old.execute("SELECT playlist_id, pos, track_id FROM playlist_items").fetchall() if "playlist_items" in names else []
        data["progress"] = old.execute("SELECT item_id, position, duration, updated FROM progress").fetchall() if "progress" in names else []
    finally:
        old.close()
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db_path) + suffix)
        if f.exists():
            f.rename(Path(str(db_path) + ".v0" + suffix))
    return data


def _restore_legacy(lib: Library, data: dict[str, Any]) -> None:
    by_path: dict[str, str] = {}
    for table in ("tracks", "videos", "books"):
        by_path.update({r["path"]: r["id"] for r in lib.db.execute(f"SELECT id, path FROM {table}")})
    new_id = lambda old: by_path.get(data["paths"].get(old, ""), old)
    with lib.db:
        for pid, name, created, updated in data["playlists"]:
            lib.db.execute("INSERT OR IGNORE INTO user.playlists VALUES (?, ?, ?, ?)", (pid, name, created, updated))
        for pid, pos, tid in data["items"]:
            lib.db.execute("INSERT OR IGNORE INTO user.playlist_items VALUES (?, ?, ?)", (pid, pos, new_id(tid)))
        for item, pos, dur, upd in data["progress"]:
            lib.db.execute("INSERT OR REPLACE INTO user.progress VALUES (?, ?, ?, ?)", (new_id(item), pos, dur, upd))


def _id(*parts: str) -> str:
    return hashlib.sha1("\0".join(parts).encode()).hexdigest()[:16]


def _clean(name: str) -> str:
    return re.sub(r"[._]+", " ", name).strip(" -")


# Utgivelsesinfo i film-/seriefilnavn: alt fra første slike merke og ut er ikke en del av navnet.
RELEASE = re.compile(
    r"(?i)(?:^|[ ._\-\[(])(?:s\d{1,2}(?:e\d{1,3})?|season[ ._]?\d+|\d{3,4}p|4k|uhd|hdr(?:10)?|dv|"
    r"web[ ._-]?(?:dl|rip)?|blu[ ._-]?ray|bdrip|brrip|hdtv|dvdrip|amzn|nf|dsnp|hmax|atvp|hulu|"
    r"x26[45]|h[ ._]?26[45]|hevc|avc|\d{1,2}bit|ddp?(?:[ ._]?\d(?:[ ._]\d)?)?|aac(?:[ ._]?\d(?:[ ._]\d)?)?|ac3|"
    r"dts(?:-?hd)?|truehd|atmos|remux|repack|proper|complete|multi)(?=$|[ ._\-\])])")


def strip_release(name: str) -> str:
    """«Big Buck Bunny S01 2160p AMZN» → «Big Buck Bunny»;
    «Sintel 2160p 10bit WEB-DL DDP5 1 HEVC-Grp» → «Sintel». Tomt → navnet som det var."""
    name = name[:300]  # navn er korte; en lang streng skal aldri gi lang søketid
    m = RELEASE.search(name)
    out = name[:m.start()].strip(" .-_([") if m else name
    return out or name


def ffprobe(path: Path) -> dict[str, Any]:
    try:
        out = subprocess.run(["ffprobe", "-v", "quiet", *SAFE_INPUT, "-print_format", "json", "-show_format",
                              "-show_streams", "-show_chapters", str(path)], capture_output=True, text=True, timeout=60).stdout
        return json.loads(out or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


class Library:
    def __init__(self, db_path: Path, roots: dict[str, list[Path]], cache: Path, user_db: Path | None = None) -> None:
        """`db_path`: indeksen (kan bygges på nytt fra filene). `user_db`: dine data (lab.db) – spillelister og
        framdrift – koblet inn som skjemaet «user», så de aldri forsvinner med indeksen."""
        from olympus.db import connect, migrate

        self.roots = roots
        self.cache = cache
        user_db = user_db or db_path.with_name("lab.db")
        legacy = _legacy_user_data(db_path)
        self.db = connect(db_path)
        migrate(self.db, MEDIA_SCHEMA)
        from olympus.lab import Lab

        Lab(user_db)  # sørg for at dine tabeller finnes (migrerer lab.db)
        self.db.execute("ATTACH DATABASE ? AS user", (str(user_db),))
        if legacy:
            self.scan()
            _restore_legacy(self, legacy)

    # --- skanning ---------------------------------------------------------------------------------
    def _files(self, kind: str, exts: set[str]) -> Iterator[Path]:
        for root in self.roots.get(kind, []):
            if root.is_dir():
                yield from (p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in exts
                            and not p.name.startswith("."))

    def scan(self) -> dict[str, int]:
        """Les inn nye og endrede filer, fjern det som er borte. Trygg å kjøre ofte."""
        return {"musikk": self._scan_music(), "bøker": self._scan_books(), "video": self._scan_videos()}

    def _stable(self, table: str, path: Path, *key: str) -> str:
        """Id ut fra innholdet, ikke filbanen: flytter eller omdøper du en fil, beholder spillelister og framdrift
        koblingen. To ulike filer med samme innhold (to utgaver av samme sang) får hver sin."""
        sid = _id(*key)
        row = self.db.execute(f"SELECT path FROM {table} WHERE id = ?", (sid,)).fetchone()
        if row and row[0] != str(path) and Path(row[0]).exists():
            return _id(sid, str(path))
        return sid

    _COLS: dict[str, tuple[str, ...]] = {
        "tracks": ("id", "path", "title", "artist", "album", "album_artist", "album_id", "track_no", "disc_no", "year",
                   "duration", "mtime", "cover", "genre"),
        "books": ("id", "path", "kind", "title", "author", "duration", "mtime"),
        "videos": ("id", "path", "kind", "title", "year", "show", "season", "episode", "duration", "vcodec", "acodec",
                   "container", "mtime"),
    }

    def _upsert(self, table: str, values: tuple) -> None:
        """Sett inn eller oppdater på id (ikke «INSERT OR REPLACE», som ville slettet raden og dermed delene/
        undertekstene som henger på den). En fil som har flyttet (samme id, ny bane) får ny bane."""
        cols = self._COLS[table]
        sets = ", ".join(f"{c} = excluded.{c}" for c in cols[1:])
        self.db.execute(f"DELETE FROM {table} WHERE path = ? AND id != ?", (values[1], values[0]))
        self.db.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                        f"ON CONFLICT(id) DO UPDATE SET {sets}", values)

    def override(self, path: Path) -> dict[str, Any]:
        """Det brukeren har rettet på for denne fila/mappa (felt: title, artist, album, album_artist, year, genre)."""
        try:
            r = self.db.execute("SELECT data FROM user.media_overrides WHERE path = ?", (str(path),)).fetchone()
        except sqlite3.OperationalError:
            return {}
        if not r:
            return {}
        try:
            d = json.loads(r[0])
        except ValueError:
            return {}
        return {k: v for k, v in d.items() if v not in (None, "")} if isinstance(d, dict) else {}

    def set_override(self, path: Path, fields: dict[str, Any]) -> None:
        old = self.override(path)
        data = {**old, **fields}
        self.db.execute("INSERT OR REPLACE INTO user.media_overrides VALUES (?, ?, ?)",
                        (str(path), json.dumps(data, ensure_ascii=False), time.time()))
        self.db.commit()

    def _known(self, table: str) -> dict[str, float]:
        return {r["path"]: r["mtime"] for r in self.db.execute(f"SELECT path, mtime FROM {table}")}

    def _scan_music(self) -> int:
        known, seen = self._known("tracks"), set()
        for f in self._files("music", AUDIO_EXT - {".m4b"}):
            seen.add(str(f))
            mtime = f.stat().st_mtime
            if known.get(str(f)) == mtime:
                continue
            t = {**read_tags(f), **self.override(f)}
            album = t.get("album") or f.parent.name
            # Uten tagger: mappene sier Artist/Album (slik Zeus og nedlastingene legger dem).
            root = next((r for r in self.roots.get("music", []) if r in f.parents), None)
            folder_artist = f.parent.parent.name if root is not None and root not in (f.parent, f.parent.parent) else ""
            album_artist = t.get("album_artist") or t.get("artist") or folder_artist or "Ukjent"
            tid = self._stable("tracks", f, "t", album_artist.lower(), album.lower(), str(t.get("disc") or 0),
                               str(t.get("track") or 0), (t.get("title") or f.stem).lower())
            self._upsert(
                "tracks",
                (tid, str(f), t.get("title") or _clean(f.stem), t.get("artist") or album_artist, album,
                 album_artist, _id(album_artist.lower(), album.lower()), t.get("track"), t.get("disc"), t.get("year"),
                 t.get("duration"), mtime, int(bool(t.get("cover")) or self._folder_cover(f) is not None),
                 t.get("genre", "")))
        gone = set(known) - seen
        self.db.executemany("DELETE FROM tracks WHERE path = ?", [(g,) for g in gone])
        self.db.commit()
        return self.db.execute("SELECT count(*) FROM tracks").fetchone()[0]

    def _scan_books(self) -> int:
        known, seen = self._known("books"), set()
        audio_dirs: dict[Path, list[Path]] = {}
        for f in self._files("audiobooks", AUDIO_EXT):
            if f.suffix.lower() == ".m4b":
                audio_dirs[f] = [f]
            else:
                audio_dirs.setdefault(f.parent, []).append(f)
        for path, parts in audio_dirs.items():
            seen.add(str(path))
            mtime = max(p.stat().st_mtime for p in parts)
            if known.get(str(path)) == mtime:
                continue
            tags = {**read_tags(parts[0]), **self.override(path)}
            title0 = tags.get("album") or (path.stem if path.is_file() else path.name)
            bid = self._stable("books", path, "lydbok", _clean(title0).lower(), (tags.get("artist") or "").lower())
            self.db.execute("DELETE FROM book_parts WHERE book_id = ?", (bid,))
            start, rows = 0.0, []
            if len(parts) == 1:
                info = ffprobe(parts[0])
                chapters = info.get("chapters") or []
                for i, ch in enumerate(chapters):
                    s, e = float(ch.get("start_time", 0)), float(ch.get("end_time", 0))
                    rows.append((bid, i, str(parts[0]), ch.get("tags", {}).get("title") or f"Kapittel {i + 1}", s, e - s))
                total = float(info.get("format", {}).get("duration") or 0)
                if not rows:
                    rows.append((bid, 0, str(parts[0]), tags.get("title") or _clean(parts[0].stem), 0.0, total))
                start = total
            else:
                for i, p in enumerate(sorted(parts, key=lambda x: _natural(x.name))):
                    d = read_tags(p).get("duration") or 0.0
                    rows.append((bid, i, str(p), read_tags(p).get("title") or _clean(p.stem), start, d))
                    start += d
            title = tags.get("album") or (path.stem if path.is_file() else path.name)
            self._upsert("books", (bid, str(path), "audio", _clean(title),
                                   tags.get("artist") or tags.get("album_artist") or "", start, mtime))
            self.db.execute("DELETE FROM book_parts WHERE book_id = ?", (bid,))
            self.db.executemany("INSERT INTO book_parts VALUES (?, ?, ?, ?, ?, ?)", rows)
        for f in self._files("ebooks", EBOOK_EXT):
            seen.add(str(f))
            mtime = f.stat().st_mtime
            if known.get(str(f)) == mtime:
                continue
            title, author = _clean(f.stem), ""
            if f.suffix.lower() == ".epub":
                title, author = epub_meta(f) or (title, author)
            o = self.override(f)
            title, author = o.get("album") or o.get("title") or title, o.get("artist") or author
            self._upsert("books", (self._stable("books", f, "ebok", title.lower(), author.lower()), str(f), "ebook", title,
                                   author, 0, mtime))
        for g in set(known) - seen:
            bid = self.db.execute("SELECT id FROM books WHERE path = ?", (g,)).fetchone()
            if bid:
                self.db.execute("DELETE FROM book_parts WHERE book_id = ?", (bid[0],))
            self.db.execute("DELETE FROM books WHERE path = ?", (g,))
        self.db.commit()
        return self.db.execute("SELECT count(*) FROM books").fetchone()[0]

    def _scan_videos(self) -> int:
        known, seen = self._known("videos"), set()
        for kind, root_key in (("film", "films"), ("episode", "shows")):
            for f in self._files(root_key, VIDEO_EXT):
                seen.add(str(f))
                mtime = f.stat().st_mtime
                if known.get(str(f)) == mtime:
                    continue
                info = ffprobe(f)
                streams = info.get("streams", [])
                v = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), {})
                a = next((s for s in streams if s.get("codec_type") == "audio"), {})
                name = _clean(f.stem)
                show = season = episode = year = None
                title = name
                m = EPISODE.match(f.stem)
                if kind == "episode" and m:
                    show = _clean(m.group("show")) or strip_release(_clean(f.parent.name))
                    season, episode = int(m.group("s")), int(m.group("e"))
                    rest = strip_release(_clean(f.stem[m.end():]))
                    title = rest or f"Episode {episode}"
                elif kind == "episode":
                    show = strip_release(_clean(f.parent.name))
                y = YEAR.match(f.stem if kind == "film" else f.parent.name)
                if kind == "film" and y and y.group("title"):
                    title, year = strip_release(_clean(y.group("title"))), int(y.group("year"))
                elif kind == "film":
                    title = strip_release(name)
                o = self.override(f)
                title, year = o.get("title") or title, o.get("year", year)
                if kind == "episode" and o.get("album"):
                    show = strip_release(o["album"])
                vid = (self._stable("videos", f, "ep", (show or "").lower(), str(season), str(episode)) if kind == "episode" and m
                       else self._stable("videos", f, "film", title.lower(), str(year or "")))
                self._upsert("videos", (vid, str(f), kind, title, year, show, season, episode,
                                 float(info.get("format", {}).get("duration") or 0), v.get("codec_name", ""),
                                 a.get("codec_name", ""), info.get("format", {}).get("format_name", ""), mtime))
                self.db.execute("DELETE FROM subtitles WHERE video_id = ?", (vid,))
                subs = []
                for i, s in enumerate(x for x in streams if x.get("codec_type") == "subtitle"):
                    if s.get("codec_name") in ("subrip", "ass", "ssa", "webvtt", "mov_text"):
                        subs.append((vid, len(subs), s.get("tags", {}).get("language", ""), s.get("tags", {}).get("title", ""),
                                     None, s.get("index", i)))
                for sf in sorted(f.parent.glob(f.stem + "*")):
                    if sf.suffix.lower() in SUB_EXT:
                        lang = sf.stem[len(f.stem):].strip(". _-") or "ekstern"
                        subs.append((vid, len(subs), lang, sf.name, str(sf), None))
                self.db.executemany("INSERT INTO subtitles VALUES (?, ?, ?, ?, ?, ?)", subs)
        for g in set(known) - seen:
            row = self.db.execute("SELECT id FROM videos WHERE path = ?", (g,)).fetchone()
            if row:
                self.db.execute("DELETE FROM subtitles WHERE video_id = ?", (row[0],))
            self.db.execute("DELETE FROM videos WHERE path = ?", (g,))
        self.db.commit()
        return self.db.execute("SELECT count(*) FROM videos").fetchone()[0]

    # --- musikk -----------------------------------------------------------------------------------
    @staticmethod
    def _track(r: sqlite3.Row) -> dict[str, Any]:
        return {"id": r["id"], "title": r["title"], "artist": r["artist"], "album": r["album"],
                "album_id": r["album_id"], "track_no": r["track_no"], "year": r["year"],
                "duration": r["duration"], "cover": bool(r["cover"])}

    def track(self, track_id: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        return {**self._track(r), "path": r["path"]} if r else None

    def albums(self) -> list[dict[str, Any]]:
        rows = self.db.execute("""SELECT album_id, album, album_artist, MIN(year) AS year, COUNT(*) AS n,
            SUM(duration) AS dur, MIN(id) AS cover_track, MAX(cover) AS cover FROM tracks GROUP BY album_id
            ORDER BY lower(album_artist), year, lower(album)""").fetchall()
        return [{"id": r["album_id"], "title": r["album"], "artist": r["album_artist"], "year": r["year"],
                 "tracks": r["n"], "duration": r["dur"], "cover_track": r["cover_track"] if r["cover"] else None}
                for r in rows]

    def album_tracks(self, album_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM tracks WHERE album_id = ? ORDER BY disc_no, track_no, title",
                               (album_id,)).fetchall()
        return [self._track(r) for r in rows]

    def search(self, query: str, limit: int = 20) -> dict[str, list[dict[str, Any]]]:
        q = f"%{query.strip().lower()}%"
        tracks = self.db.execute("""SELECT * FROM tracks WHERE lower(title) LIKE ? OR lower(artist) LIKE ?
            OR lower(album) LIKE ? OR lower(genre) LIKE ? ORDER BY lower(artist), album, track_no LIMIT ?""",
                                 (q, q, q, q, limit)).fetchall()
        albums = [a for a in self.albums() if query.lower() in f"{a['title']} {a['artist']}".lower()][:limit]
        books = self.db.execute("SELECT * FROM books WHERE lower(title) LIKE ? OR lower(author) LIKE ? LIMIT ?",
                                (q, q, limit)).fetchall()
        videos = self.db.execute("SELECT * FROM videos WHERE lower(title) LIKE ? OR lower(coalesce(show,'')) LIKE ? "
                                 "LIMIT ?", (q, q, limit)).fetchall()
        return {"tracks": [self._track(r) for r in tracks], "albums": albums,
                "books": [self._book(r) for r in books], "videos": [self._video(r) for r in videos]}

    def songs_for(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        """Det et ønske gjelder: en spilleliste, et album, en artist som passer navnet – ellers sangtreff."""
        q = query.strip().lower()
        if any(w in q for w in self.TASTE_WORDS):
            return self.songs_for_taste(min(limit, 40))
        for p in self.playlists():
            if q == p["name"].lower():
                return self.playlist_tracks(p["id"])
        for a in self.albums():
            if q and q in a["title"].lower():
                return self.album_tracks(a["id"])
        rows = self.db.execute("SELECT * FROM tracks WHERE lower(artist) = ? OR lower(album_artist) = ? "
                               "ORDER BY year, album, disc_no, track_no LIMIT ?", (q, q, limit)).fetchall()
        return [self._track(r) for r in rows] or self.search(query, limit)["tracks"]

    # --- musikksmak ------------------------------------------------------------------------------------
    TASTE_WORDS = ("noe jeg liker", "jeg liker", "favoritt", "min smak", "mine favoritter", "smaken min", "mix", "miks")

    def record_play(self, track_id: str, listened: float, duration: float = 0, source: str = "") -> bool:
        """En avspilling: hoppet over hvis du hørte under 30 s (eller under halve sangen hvis den er kortere)."""
        if self.track(track_id) is None or not (0 <= listened < 86400):
            return False
        duration = duration if 0 < duration < 86400 else (self.track(track_id) or {}).get("duration") or 0
        skipped = listened < min(30.0, 0.5 * duration) if duration else listened < 30
        self.db.execute("INSERT INTO user.plays (track_id, ts, listened, duration, skipped, source) VALUES (?, ?, ?, ?, ?, ?)",
                        (track_id, time.time(), float(listened), float(duration), int(skipped), source[:20]))
        self.db.commit()
        return True

    def set_like(self, item_id: str, value: int) -> None:
        """♥ (1), ✕ (-1) eller nøytral (0) på en sang, et album (album:id) eller en artist (artist:navn)."""
        if value == 0:
            self.db.execute("DELETE FROM user.likes WHERE item_id = ?", (item_id,))
        else:
            self.db.execute("INSERT OR REPLACE INTO user.likes VALUES (?, ?, ?)", (item_id, 1 if value > 0 else -1, time.time()))
        self.db.commit()

    def like(self, item_id: str) -> int:
        r = self.db.execute("SELECT value FROM user.likes WHERE item_id = ?", (item_id,)).fetchone()
        return int(r[0]) if r else 0

    def _affinity(self, days: int = 180) -> dict[str, dict[str, float]]:
        """Poeng per sang, artist, album og sjanger: fullførte avspillinger teller, hopp trekker, ♥/✕ veier tungt.
        Nyere lytting veier mer (halvert etter 60 dager)."""
        now = time.time()
        score: dict[str, dict[str, float]] = {"track": {}, "artist": {}, "album": {}, "genre": {}}
        rows = self.db.execute(
            "SELECT p.track_id, p.ts, p.listened, p.duration, p.skipped, t.artist, t.album_artist, t.album_id, t.genre "
            "FROM user.plays p JOIN tracks t ON t.id = p.track_id WHERE p.ts > ?", (now - days * 86400,)).fetchall()
        for r in rows:
            w = 0.5 ** ((now - r["ts"]) / (60 * 86400))
            frac = min(1.0, r["listened"] / r["duration"]) if r["duration"] else (0.3 if r["skipped"] else 1.0)
            v = w * (-0.6 if r["skipped"] else 0.4 + 0.6 * frac)
            artist = (r["album_artist"] or r["artist"] or "").strip()
            for kind, key in (("track", r["track_id"]), ("artist", artist), ("album", r["album_id"]), ("genre", r["genre"] or "")):
                if key:
                    score[kind][key] = score[kind].get(key, 0.0) + v
        for item, value in self.db.execute("SELECT item_id, value FROM user.likes"):
            kind, _, key = item.partition(":") if ":" in item else ("track", "", item)
            if kind in score:
                score[kind][key] = score[kind].get(key, 0.0) + 5.0 * value
                if kind == "track" and (t := self.track(key)):
                    a = (t.get("artist") or "").strip()
                    score["artist"][a] = score["artist"].get(a, 0.0) + 1.5 * value
                    score["album"][t["album_id"]] = score["album"].get(t["album_id"], 0.0) + 1.0 * value
        return score

    def taste(self) -> dict[str, Any]:
        """Smaksprofilen: det Zeus trenger for å forstå hva du liker."""
        s = self._affinity()
        top = lambda kind, n: [k for k, v in sorted(s[kind].items(), key=lambda kv: -kv[1]) if v > 0][:n]
        album_name = {r["album_id"]: f"{r['album']} – {r['album_artist']}" for r in
                      self.db.execute("SELECT DISTINCT album_id, album, album_artist FROM tracks")}
        plays = self.db.execute("SELECT count(*), sum(skipped), sum(listened) FROM user.plays").fetchone()
        hours: dict[str, dict[str, int]] = {}
        for r in self.db.execute("SELECT p.ts, t.artist, t.genre FROM user.plays p JOIN tracks t ON t.id = p.track_id "
                                 "WHERE p.skipped = 0 AND p.ts > ?", (time.time() - 180 * 86400,)):
            h = time.localtime(r["ts"]).tm_hour
            part = "morgen" if 5 <= h < 11 else "dag" if h < 17 else "kveld" if h < 23 else "natt"
            key = r["genre"] or r["artist"] or "?"
            hours.setdefault(part, {})[key] = hours.setdefault(part, {}).get(key, 0) + 1
        recent = [f"{r['title']} – {r['artist']}" for r in self.db.execute(
            "SELECT t.title, t.artist, max(p.ts) AS last FROM user.plays p JOIN tracks t ON t.id = p.track_id "
            "WHERE p.skipped = 0 GROUP BY p.track_id ORDER BY last DESC LIMIT 8")]
        skipped = [f"{r['title']} – {r['artist']}" for r in self.db.execute(
            "SELECT t.title, t.artist, sum(p.skipped) AS s, count(*) AS n FROM user.plays p JOIN tracks t ON t.id = p.track_id "
            "GROUP BY p.track_id HAVING s >= 2 AND s * 2 >= n ORDER BY s DESC LIMIT 6")]
        liked = [f"{t['title']} – {t['artist']}" for i, in self.db.execute("SELECT item_id FROM user.likes WHERE value > 0 "
                 "AND item_id NOT LIKE '%:%' ORDER BY ts DESC LIMIT 10") if (t := self.track(i))]
        disliked = [f"{t['title']} – {t['artist']}" for i, in self.db.execute("SELECT item_id FROM user.likes WHERE value < 0 "
                    "AND item_id NOT LIKE '%:%' ORDER BY ts DESC LIMIT 10") if (t := self.track(i))]
        return {"plays": int(plays[0] or 0), "skipped": int(plays[1] or 0), "hours": round((plays[2] or 0) / 3600, 1),
                "artists": top("artist", 10), "genres": top("genre", 8),
                "albums": [album_name.get(a, a) for a in top("album", 8)], "recent": recent, "liked": liked,
                "disliked": disliked, "often_skipped": skipped,
                "by_time": {k: sorted(v, key=lambda x: -v[x])[:3] for k, v in hours.items()},
                "library": {"tracks": self.stats()["tracks"], "artists": self.db.execute(
                    "SELECT count(DISTINCT coalesce(nullif(album_artist, ''), artist)) FROM tracks").fetchone()[0]}}

    def songs_for_taste(self, limit: int = 40, seed: str = "") -> list[dict[str, Any]]:
        """En miks etter smaken din: mest det du liker, litt nytt fra artister og sjangre du liker, aldri ✕."""
        import random

        s = self._affinity()
        rng = random.Random(seed or time.strftime("%Y%m%d%H"))
        disliked = {i for i, in self.db.execute("SELECT item_id FROM user.likes WHERE value < 0")}
        played = {i for i, in self.db.execute("SELECT DISTINCT track_id FROM user.plays")}
        scored = []
        for r in self.db.execute("SELECT * FROM tracks"):
            if r["id"] in disliked or f"album:{r['album_id']}" in disliked:
                continue
            artist = (r["album_artist"] or r["artist"] or "").strip()
            if f"artist:{artist}" in disliked:
                continue
            v = (s["track"].get(r["id"], 0) * 2 + s["artist"].get(artist, 0) + s["album"].get(r["album_id"], 0) * 0.5
                 + s["genre"].get(r["genre"] or "", 0) * 0.5)
            if r["id"] not in played and v > 0:
                v += 0.8  # litt oppdagelse: uspilte spor fra det du liker
            scored.append((v + rng.random() * 0.9, r))
        scored.sort(key=lambda x: -x[0])
        picked, per_artist = [], {}
        for v, r in scored:
            a = (r["album_artist"] or r["artist"] or "").lower()
            if per_artist.get(a, 0) >= max(3, limit // 5):  # ikke bare én artist
                continue
            per_artist[a] = per_artist.get(a, 0) + 1
            picked.append(self._track(r))
            if len(picked) >= limit:
                break
        rng.shuffle(picked)
        return picked

    def cover(self, track_id: str) -> tuple[bytes, str] | None:
        t = self.track(track_id)
        if t is None:
            return None
        folder = self._folder_cover(Path(t["path"]))
        if folder is not None:  # mappebildet først: det er det du (eller Olympus) har valgt for albumet
            return folder.read_bytes(), "image/jpeg" if folder.suffix.lower() != ".png" else "image/png"
        return read_cover(Path(t["path"]))

    @staticmethod
    def _folder_cover(f: Path) -> Path | None:
        for name in ("cover.jpg", "folder.jpg", "cover.png", "front.jpg"):
            if (f.parent / name).is_file():
                return f.parent / name
        return None

    # --- spillelister -----------------------------------------------------------------------------
    def playlists(self) -> list[dict[str, Any]]:
        rows = self.db.execute("""SELECT p.id, p.name, p.updated, COUNT(i.track_id) AS n FROM user.playlists p
            LEFT JOIN user.playlist_items i ON i.playlist_id = p.id GROUP BY p.id ORDER BY lower(p.name)""").fetchall()
        return [{"id": r["id"], "name": r["name"], "tracks": r["n"], "updated": r["updated"]} for r in rows]

    def playlist_tracks(self, playlist_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("""SELECT t.* FROM user.playlist_items i JOIN tracks t ON t.id = i.track_id
            WHERE i.playlist_id = ? ORDER BY i.pos""", (playlist_id,)).fetchall()
        return [self._track(r) for r in rows]

    def playlist_create(self, name: str, track_ids: list[str] | None = None) -> dict[str, Any]:
        name = name.strip()[:80] or "Ny spilleliste"
        pid, now = _id(name, str(time.time())), time.time()
        self.db.execute("INSERT INTO user.playlists VALUES (?, ?, ?, ?)", (pid, name, now, now))
        self.db.commit()
        if track_ids:
            self.playlist_set(pid, track_ids)
        return next(p for p in self.playlists() if p["id"] == pid)

    def playlist_set(self, playlist_id: str, track_ids: list[str], name: str | None = None) -> None:
        if not self.db.execute("SELECT 1 FROM user.playlists WHERE id = ?", (playlist_id,)).fetchone():
            raise KeyError(playlist_id)
        valid = {r[0] for r in self.db.execute("SELECT id FROM tracks")}
        self.db.execute("DELETE FROM user.playlist_items WHERE playlist_id = ?", (playlist_id,))
        self.db.executemany("INSERT INTO user.playlist_items VALUES (?, ?, ?)",
                            [(playlist_id, i, t) for i, t in enumerate(x for x in track_ids if x in valid)])
        self.db.execute("UPDATE user.playlists SET updated = ?, name = coalesce(?, name) WHERE id = ?",
                        (time.time(), name.strip()[:80] if name else None, playlist_id))
        self.db.commit()

    def playlist_add(self, playlist_id: str, track_ids: list[str]) -> None:
        current = [t["id"] for t in self.playlist_tracks(playlist_id)]
        self.playlist_set(playlist_id, current + [t for t in track_ids if t not in current])

    def playlist_delete(self, playlist_id: str) -> bool:
        self.db.execute("DELETE FROM user.playlist_items WHERE playlist_id = ?", (playlist_id,))
        cur = self.db.execute("DELETE FROM user.playlists WHERE id = ?", (playlist_id,))
        self.db.commit()
        return cur.rowcount > 0

    # --- bøker ------------------------------------------------------------------------------------
    def _book(self, r: sqlite3.Row) -> dict[str, Any]:
        p = self.progress(r["id"])
        return {"id": r["id"], "kind": r["kind"], "title": r["title"], "author": r["author"],
                "duration": r["duration"], "position": p["position"] if p else 0,
                "format": Path(r["path"]).suffix.lower().lstrip(".") if r["kind"] == "ebook" else "audio"}

    def books(self) -> list[dict[str, Any]]:
        return [self._book(r) for r in self.db.execute("SELECT * FROM books ORDER BY lower(author), lower(title)")]

    def book(self, book_id: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if r is None:
            return None
        parts = [{"idx": p["idx"], "title": p["title"], "start": p["start"], "duration": p["duration"]}
                 for p in self.db.execute("SELECT * FROM book_parts WHERE book_id = ? ORDER BY idx", (book_id,))]
        return {**self._book(r), "parts": parts, "path": r["path"]}

    def part_path(self, book_id: str, idx: int) -> Path | None:
        r = self.db.execute("SELECT path FROM book_parts WHERE book_id = ? AND idx = ?", (book_id, idx)).fetchone()
        return Path(r[0]) if r else None

    def book_text(self, book_id: str) -> list[tuple[str, str]]:
        """E-bok som tekst, kapittel for kapittel: [(tittel, tekst)]. Til lesing i appen og for Zeus."""
        b = self.book(book_id)
        if b is None or b["kind"] != "ebook":
            return []
        return ebook_text(Path(b["path"]))

    # --- sletting -------------------------------------------------------------------------------------
    def files_for(self, item_id: str) -> tuple[str, list[Path]]:
        """Hva en id i biblioteket er, og filene den består av: album, sang, film/episode eller bok."""
        q = lambda sql: self.db.execute(sql, (item_id,)).fetchall()
        if rows := q("SELECT path, album, album_artist FROM tracks WHERE album_id = ?"):
            return f"albumet «{rows[0]['album']}» – {rows[0]['album_artist']}", [Path(r["path"]) for r in rows]
        if rows := q("SELECT path, title, artist FROM tracks WHERE id = ?"):
            return f"sangen «{rows[0]['title']}» – {rows[0]['artist']}", [Path(rows[0]["path"])]
        if rows := q("SELECT path, title, kind FROM videos WHERE id = ?"):
            return f"{'episoden' if rows[0]['kind'] == 'episode' else 'filmen'} «{rows[0]['title']}»", [Path(rows[0]["path"])]
        if rows := q("SELECT path, title FROM books WHERE id = ?"):
            parts = [Path(r["path"]) for r in q("SELECT path FROM book_parts WHERE book_id = ?")]
            return f"boka «{rows[0]['title']}»", parts or [Path(rows[0]["path"])]
        return "", []

    def inside_library(self, path: Path) -> bool:
        p = path.resolve()
        return any(p.is_relative_to(r.resolve()) for roots in self.roots.values() for r in roots)

    def trash_files(self, paths: list[Path]) -> None:
        """Små restfiler (cover o.l.) til papirkurven, med samme regler som trash: aldri utenfor biblioteket."""
        import shutil
        import subprocess

        for p in paths:
            if p.is_symlink() or not self.inside_library(p):
                continue
            if shutil.which("gio") and subprocess.run(["gio", "trash", "--", str(p)], capture_output=True).returncode == 0:
                continue
            dest = Path.home() / ".local/share/Trash/files"
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), dest / f"{int(time.time())}-{p.name}")

    def trash(self, paths: list[Path]) -> list[Path]:
        """Flytt filene til papirkurven (freedesktop, kan hentes tilbake) – aldri utenfor bibliotekmappene."""
        import shutil
        import subprocess

        done = []
        for p in paths:
            if not p.exists() or p.is_symlink() or not self.inside_library(p):
                continue
            if shutil.which("gio") and subprocess.run(["gio", "trash", "--", str(p)], capture_output=True).returncode == 0:
                done.append(p)
                continue
            dest = Path.home() / ".local/share/Trash/files"
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), dest / f"{int(time.time())}-{p.name}")
            done.append(p)
        # Mapper uten mediefiler igjen: rester (cover, .nfo, .cue, spillelister) følger med i papirkurven,
        # så albumet ikke blir liggende som en tom mappe med bare et cover.
        media = AUDIO_EXT | VIDEO_EXT | EBOOK_EXT
        leftovers = {".jpg", ".jpeg", ".png", ".webp", ".nfo", ".cue", ".log", ".m3u", ".m3u8", ".sfv", ".srt", ".vtt", ".ass",
                     ".json"}
        for d in {p.parent for p in done}:
            if not d.is_dir() or not self.inside_library(d) or any(r.resolve() == d.resolve() for rs in self.roots.values() for r in rs):
                continue
            rest = [f for f in d.iterdir() if f.is_file()]
            if rest and not any(f.suffix.lower() in media for f in rest) and all(f.suffix.lower() in leftovers for f in rest):
                self.trash_files(rest)
        # Tomme album-/artistmapper ryddes bort (bare tomme, og bare inne i biblioteket)
        for p in done:
            for d in p.parents:
                if not self.inside_library(d) or any(r.resolve() == d.resolve() for rs in self.roots.values() for r in rs):
                    break
                try:
                    d.rmdir()
                except OSError:
                    break
        return done

    # --- video ------------------------------------------------------------------------------------
    def _video(self, r: sqlite3.Row) -> dict[str, Any]:
        p = self.progress(r["id"])
        return {"id": r["id"], "kind": r["kind"], "title": r["title"], "year": r["year"], "show": r["show"],
                "season": r["season"], "episode": r["episode"], "duration": r["duration"],
                "position": p["position"] if p else 0, "direct": self.direct_play(r)}

    @staticmethod
    def direct_play(r: sqlite3.Row | dict) -> bool:
        container = (r["container"] or "").split(",")
        return (r["vcodec"] in DIRECT_VIDEO and r["acodec"] in DIRECT_AUDIO | {""}
                and any(c in ("mov", "mp4", "m4a", "matroska", "webm") for c in container))

    def videos(self) -> dict[str, Any]:
        films = [self._video(r) for r in self.db.execute("SELECT * FROM videos WHERE kind = 'film' ORDER BY lower(title)")]
        shows: dict[str, list[dict[str, Any]]] = {}
        for r in self.db.execute("SELECT * FROM videos WHERE kind = 'episode' ORDER BY show, season, episode"):
            shows.setdefault(r["show"] or "Ukjent", []).append(self._video(r))
        return {"films": films, "shows": [{"name": k, "episodes": v} for k, v in shows.items()]}

    def video(self, video_id: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        if r is None:
            return None
        subs = [{"idx": s["idx"], "lang": s["lang"], "title": s["title"]}
                for s in self.db.execute("SELECT * FROM subtitles WHERE video_id = ? ORDER BY idx", (video_id,))]
        from olympus.tmdb import read_meta

        info = read_meta(Path(r["path"]), r["show"])
        return {**self._video(r), "path": r["path"], "vcodec": r["vcodec"], "acodec": r["acodec"], "subtitles": subs,
                **({"info": {k: v for k, v in info.items() if k != "poster"}} if info else {})}

    def subtitle(self, video_id: str, idx: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM subtitles WHERE video_id = ? AND idx = ?", (video_id, idx)).fetchone()

    def poster(self, video_id: str) -> Path | None:
        """Et bilde fra ca. 10 % ut i filmen, laget én gang og lagret i cachen."""
        v = self.video(video_id)
        if v is None:
            return None
        for cand in poster_files(Path(v["path"]), v.get("show")):
            if cand.is_file():
                return cand
        out = self.cache / "posters" / f"{video_id}.jpg"
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            at = max(1.0, (v["duration"] or 60) * 0.1)
            subprocess.run(["ffmpeg", "-v", "quiet", "-ss", f"{at:.0f}", *SAFE_INPUT, "-i", v["path"], "-frames:v", "1",
                            "-vf", "scale=360:-2", "-y", str(out)], timeout=60, check=False)
        return out if out.exists() else None

    # --- cover og plakater fra nettet -----------------------------------------------------------
    def fetch_art(self, limit: int = 20) -> dict[str, int]:
        """Hent cover til album uten bilde (MusicBrainz → Cover Art Archive) og plakater til filmer/serier
        (Wikipedia). Hvert album/hver film prøves én gang (art_tried). Returnerer hvor mange som ble funnet."""
        import httpx

        got = {"album": 0, "film": 0}
        headers = {"User-Agent": "OlympusLab/0.2 (personlig mediabibliotek; https://github.com/)"}
        # Funnet: aldri på nytt. Ikke funnet / nettfeil: prøv igjen etter en uke.
        tried = {r[0] for r in self.db.execute("SELECT key FROM art_tried WHERE ok = 1 OR ts > ?", (time.time() - 7 * 86400,))}
        with httpx.Client(timeout=20, headers=headers, follow_redirects=True) as c:
            for a in self.albums():
                if limit <= 0:
                    break
                key = f"album:{a['id']}"
                if a["cover_track"] or key in tried or a["artist"] in ("", "Ukjent"):
                    continue
                limit -= 1
                ok = False
                try:
                    folder = Path(self.db.execute("SELECT path FROM tracks WHERE album_id = ? LIMIT 1", (a["id"],)).fetchone()[0]).parent
                    img = musicbrainz_cover(c, a["artist"], a["title"])
                    if img and not (folder / "cover.jpg").exists():
                        (folder / "cover.jpg").write_bytes(img)
                        self.db.execute("UPDATE tracks SET cover = 1 WHERE album_id = ?", (a["id"],))
                        ok = True
                        got["album"] += 1
                except (httpx.HTTPError, OSError, TypeError, ValueError, KeyError):
                    pass
                self.db.execute("INSERT OR REPLACE INTO art_tried VALUES (?, ?, ?)", (key, time.time(), int(ok)))
                self.db.commit()
                time.sleep(1.1)  # MusicBrainz: høyst ett kall i sekundet
            if self.tmdb_key:
                limit = self._fetch_tmdb(c, tried, limit, got)
            for r in self.db.execute("SELECT * FROM videos ORDER BY mtime DESC").fetchall():
                if limit <= 0:
                    break
                f = Path(r["path"])
                target = poster_files(f, r["show"])[0]
                key = f"poster:{target}"
                if target.exists() or key in tried:
                    continue
                limit -= 1
                ok = False
                try:
                    query = f"{r['show']} TV series" if r["kind"] == "episode" else f"{r['title']} {r['year'] or ''} film"
                    img = wikipedia_image(c, query)
                    time.sleep(1.0)
                    if img:
                        target.write_bytes(img)
                        ok = True
                        got["film"] += 1
                except (httpx.HTTPError, OSError, ValueError, KeyError):
                    pass
                self.db.execute("INSERT OR REPLACE INTO art_tried VALUES (?, ?, ?)", (key, time.time(), int(ok)))
                self.db.commit()
                tried.add(key)
        return got

    tmdb_key: str = ""  # settes av serveren fra auth.json (olympus auth login tmdb)

    def _fetch_tmdb(self, c: Any, tried: set[str], limit: int, got: dict[str, int]) -> int:
        """Filminfo + plakat fra TMDb til filmer/serier uten tmdb.json (én gang per serie). Returnerer gjenstående grense."""
        import httpx

        from olympus.tmdb import lookup, meta_file

        seen: set[str] = set()
        for r in self.db.execute("SELECT * FROM videos ORDER BY mtime DESC").fetchall():
            if limit <= 0:
                break
            f = Path(r["path"])
            mf = meta_file(f, r["show"])
            key = f"tmdb:{mf}"
            if mf.exists() or key in tried or str(mf) in seen:
                continue
            seen.add(str(mf))
            limit -= 1
            ok = False
            try:
                info = lookup(c, self.tmdb_key, r["kind"], r["show"] if r["kind"] == "episode" else r["title"], r["year"])
                if info:
                    mf.write_text(json.dumps(info, ensure_ascii=False, indent=1))
                    target = poster_files(f, r["show"])[0]
                    if info.get("poster") and not target.exists():
                        img = c.get(info["poster"])
                        if img.status_code == 200 and img.headers.get("content-type", "").startswith("image/"):
                            target.write_bytes(img.content)
                            got["film"] += 1
                    ok = True
            except (httpx.HTTPError, OSError, ValueError, KeyError):
                pass
            self.db.execute("INSERT OR REPLACE INTO art_tried VALUES (?, ?, ?)", (key, time.time(), int(ok)))
            self.db.commit()
            time.sleep(0.3)
        return limit

    # --- framdrift --------------------------------------------------------------------------------
    def progress(self, item_id: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM user.progress WHERE item_id = ?", (item_id,)).fetchone()
        return {"position": r["position"], "duration": r["duration"], "updated": r["updated"]} if r else None

    def set_progress(self, item_id: str, position: float, duration: float = 0) -> None:
        self.db.execute("INSERT OR REPLACE INTO user.progress VALUES (?, ?, ?, ?)",
                        (item_id, max(0.0, float(position)), float(duration or 0), time.time()))
        self.db.commit()

    def stats(self) -> dict[str, int]:
        q = lambda sql: self.db.execute(sql).fetchone()[0]
        return {"tracks": q("SELECT count(*) FROM tracks"), "albums": q("SELECT count(DISTINCT album_id) FROM tracks"),
                "books": q("SELECT count(*) FROM books"), "films": q("SELECT count(*) FROM videos WHERE kind='film'"),
                "episodes": q("SELECT count(*) FROM videos WHERE kind='episode'"),
                "playlists": q("SELECT count(*) FROM user.playlists")}


# --- hjelpere -------------------------------------------------------------------------------------
def _natural(s: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _num(v: Any) -> int | None:
    m = re.match(r"\d+", str(v or ""))
    return int(m.group()) if m else None


def read_tags(path: Path) -> dict[str, Any]:
    try:
        import mutagen

        f = mutagen.File(path, easy=True)
    except Exception:  # noqa: BLE001 – ødelagte filer skal ikke stoppe skanningen
        return {}
    if f is None:
        return {}
    tags = f.tags or {}

    def first(key: str) -> str:
        v = tags.get(key)
        return str(v[0]) if isinstance(v, list) and v else (str(v) if v else "")

    return {"title": first("title"), "artist": first("artist"), "album": first("album"),
            "album_artist": first("albumartist"), "track": _num(first("tracknumber")), "disc": _num(first("discnumber")),
            "year": _num(first("date")), "genre": first("genre"), "duration": getattr(f.info, "length", None),
            "cover": read_cover(path) is not None}


def read_cover(path: Path) -> tuple[bytes, str] | None:
    try:
        import mutagen

        f = mutagen.File(path)
    except Exception:  # noqa: BLE001
        return None
    if f is None:
        return None
    pics = getattr(f, "pictures", None)  # FLAC/OGG
    if pics:
        return pics[0].data, pics[0].mime or "image/jpeg"
    tags = f.tags or {}
    for key in list(tags.keys()) if hasattr(tags, "keys") else []:
        if str(key).startswith("APIC"):  # ID3 (mp3)
            return tags[key].data, tags[key].mime or "image/jpeg"
    covr = tags.get("covr") if hasattr(tags, "get") else None  # MP4
    if covr:
        return bytes(covr[0]), "image/png" if getattr(covr[0], "imageformat", 13) == 14 else "image/jpeg"
    if "metadata_block_picture" in tags:  # OGG/Opus
        import base64

        from mutagen.flac import Picture

        pic = Picture(base64.b64decode(tags["metadata_block_picture"][0]))
        return pic.data, pic.mime or "image/jpeg"
    return None


BLOCKS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote", "section", "article",
          "dd", "dt", "pre", "hr", "table", "ul", "ol"}


class _Text(HTMLParser):
    """HTML → avsnitt. Blokker (p, div, li, h1 …) gir avsnittsskift; linjeskift og innrykk fra kildekoden blir til
    mellomrom (før havnet de midt i setninger, og ord fra to blokker kunne klistres sammen)."""

    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self.title = ""
        self.page_title = ""
        self._skip = 0
        self._in_h = False
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in BLOCKS:
            self.out.append("\n\n")
        if tag in ("h1", "h2", "h3"):
            self._in_h = True
        if tag == "li":
            self.out.append("• ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in BLOCKS:
            self.out.append("\n\n")
        if tag in ("h1", "h2", "h3"):
            self._in_h = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.page_title += data
            return
        if self._skip:
            return
        self.out.append(re.sub(r"\s+", " ", data))
        if self._in_h and not self.title:
            self.title = data.strip()

    def paragraphs(self) -> list[str]:
        return [p.strip() for p in re.split(r"\n\s*\n", "".join(self.out)) if p.strip()]


def _split_flat(par: str) -> list[str]:
    """Et «avsnitt» som egentlig er en hel side (PDF konvertert til EPUB): del ved punktlister og ved overskrifter i
    store bokstaver («MEDICAL EMERGENCIES Medical problems …»)."""
    if len(par) < 60:
        return [par]
    par = re.sub(r"\s+([•▪●])\s*", r"\n\1 ", par)
    # OVERSKRIFT (minst to ord i store bokstaver) etterfulgt av en vanlig setning
    par = re.sub(r"(?<=[.!?:)\]»\"]) +((?:[A-ZÆØÅ][A-ZÆØÅ'\-]* +){1,8}[A-ZÆØÅ][A-ZÆØÅ'\-]+)(?= +[A-ZÆØÅ][a-zæøå])",
                 r"\n\1\n", par)
    out: list[str] = []
    for piece in (x.strip() for x in par.split("\n")):
        # støy fra skanningen foran teksten («• ^: ^ A shelter …»): to eller flere tegnklumper uten bokstaver
        piece = re.sub(r"^(?:[^\w\s]{1,3}\s+){2,}", "", piece)
        if not piece:
            continue
        # fortsatt en vegg av tekst: avsnitt på ~500 tegn, delt ved setningsslutt
        while len(piece) > 900:
            cut = max(piece.rfind(". ", 0, 550), piece.rfind("? ", 0, 550), piece.rfind("! ", 0, 550))
            if cut < 200:
                cut = piece.find(". ", 550)
                if cut < 0:
                    break
            out.append(piece[:cut + 1].strip())
            piece = piece[cut + 1:].strip()
        out.append(piece)
    return out


def _toc(z: zipfile.ZipFile, opf: str, base: str) -> dict[str, str]:
    """Kapitteltitler fra innholdsfortegnelsen (EPUB 3 nav eller EPUB 2 toc.ncx): {fil: tittel}."""
    out: dict[str, str] = {}
    nav = re.search(r'<item[^>]*properties="[^"]*nav[^"]*"[^>]*href="([^"]+)"', opf) or \
        re.search(r'<item[^>]*href="([^"]+)"[^>]*properties="[^"]*nav[^"]*"', opf)
    try:
        if nav:
            html = z.read(base + nav.group(1)).decode(errors="replace")
            toc = re.search(r'<nav[^>]*toc[^>]*>(.*?)</nav>', html, re.S)
            for href, label in re.findall(r'<a[^>]*href="([^"#]+)[^"]*"[^>]*>(.*?)</a>', toc.group(1) if toc else html, re.S):
                label = re.sub(r"<[^>]+>|\s+", " ", label).strip()
                if label:
                    out.setdefault(href.rsplit("/", 1)[-1], label)
        if not out:
            ncx = re.search(r'<item[^>]*href="([^"]+\.ncx)"', opf)
            if ncx:
                x = z.read(base + ncx.group(1)).decode(errors="replace")
                for label, src in re.findall(r"<navPoint.*?<text>(.*?)</text>.*?<content src=\"([^\"#]+)", x, re.S):
                    if label.strip():
                        out.setdefault(src.rsplit("/", 1)[-1], label.strip())
    except (KeyError, AttributeError):
        pass
    return out


def _strip_running_header(pages: list[list[str]]) -> list[list[str]]:
    """Topp- og bunntekst som går igjen på (nesten) hver side («THE BOOK TITLE Reprinted with permission …»,
    «Page 2 of 233»): fjernes."""
    import os

    pages = [[x for x in p if not re.fullmatch(r"(?:Page|Side)\s+\d+(?:\s+(?:of|av)\s+\d+)?|\d{1,4}", x)] for p in pages]
    firsts = [p[0] for p in pages if p]
    if len(firsts) < 6:
        return pages
    heads = [f[:40] for f in firsts if len(f) > 40]
    if not heads:
        return pages
    common = max(set(heads), key=heads.count)
    group = [f for f in firsts if f.startswith(common)]
    if len(group) < 0.5 * len(firsts):
        return pages
    head = os.path.commonprefix(group)
    head = head[:head.rfind(" ") + 1] if " " in head else head  # helt ord
    if len(head) < 20:
        return pages
    return [[(x[len(head):].strip() if i == 0 and x.startswith(head) else x) for i, x in enumerate(p)] for p in pages]


# «CHAPTER 5 - SHELTERS A shelter can …» (ikke «CHAPTER 5 - SHELTERS 38» – det er innholdsfortegnelsen)
CHAPTER = re.compile(r"((?:CHAPTER|KAPITTEL)\s+[\dIVXLC]+\s*[-–:.]?\s*(?:[A-ZÆØÅ][A-ZÆØÅ0-9,'&\-]*\s+)*?[A-ZÆØÅ][A-ZÆØÅ0-9,'&\-]*)"
                     r"(?=\s+(?:[A-ZÆØÅ]?[a-zæøå\"“«(]|[^\w\s]|\d+\.\s)|\s*$)")


def epub_meta(path: Path) -> tuple[str, str] | None:
    try:
        with zipfile.ZipFile(path) as z:
            container = z.read("META-INF/container.xml").decode(errors="replace")
            opf_path = re.search(r'full-path="([^"]+)"', container).group(1)  # type: ignore[union-attr]
            opf = z.read(opf_path).decode(errors="replace")
    except (OSError, KeyError, AttributeError, zipfile.BadZipFile):
        return None
    t = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", opf, re.S)
    a = re.search(r"<dc:creator[^>]*>(.*?)</dc:creator>", opf, re.S)
    return (t.group(1).strip() if t else path.stem, a.group(1).strip() if a else "")


def ebook_text(path: Path) -> list[tuple[str, str]]:
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return [(path.stem, path.read_text(errors="replace"))]
    if suffix == ".pdf":
        try:
            out = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=120).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        pages = out.split("\f")
        return [(f"Side {i + 1}–{min(i + 10, len(pages))}", "\n".join(pages[i:i + 10])) for i in range(0, len(pages), 10)]
    try:
        with zipfile.ZipFile(path) as z:
            container = z.read("META-INF/container.xml").decode(errors="replace")
            opf_path = re.search(r'full-path="([^"]+)"', container).group(1)  # type: ignore[union-attr]
            opf = z.read(opf_path).decode(errors="replace")
            base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
            manifest = dict(re.findall(r'<item[^>]*id="([^"]+)"[^>]*href="([^"]+)"', opf))
            manifest.update({i: h for h, i in re.findall(r'<item[^>]*href="([^"]+)"[^>]*id="([^"]+)"', opf)})
            spine = re.findall(r'<itemref[^>]*idref="([^"]+)"', opf)
            toc = _toc(z, opf, base)
            parts: list[tuple[str, str, list[str]]] = []  # (fil, tittel, avsnitt)
            for idref in spine:
                href = manifest.get(idref)
                if not href:
                    continue
                try:
                    html = z.read(base + href.split("#")[0]).decode(errors="replace")
                except KeyError:
                    continue
                p = _Text()
                p.feed(html)
                pars = [x for par in p.paragraphs() for x in _split_flat(par)]
                fname = href.split("#")[0].rsplit("/", 1)[-1]
                parts.append((fname, toc.get(fname) or p.title, pars))
            cleaned = _strip_running_header([pars for _, _, pars in parts])
            parts = [(f, t, pars) for (f, t, _), pars in zip(parts, cleaned)]
            # Kapitler: start ved hver oppføring i innholdsfortegnelsen; uten den ved «CHAPTER 3 – …»-overskrifter
            # i teksten; ellers er hver fil et kapittel. Sider uten egen overskrift slås sammen med kapitlet før.
            chapters: list[tuple[str, list[str]]] = []
            page_book = not toc and len(parts) > 30 and sum(1 for _, t, _ in parts if not t) > 0.7 * len(parts)
            for n, (fname, title, pars) in enumerate(parts):
                if page_book:
                    for par in pars:
                        m = CHAPTER.search(par)
                        if m and not re.match(r"\s+\d{1,4}(?:\s|$)(?!\S*\.)", par[m.end():m.end() + 8]):
                            before, title, after = par[:m.start()].strip(), m.group(1).strip(), par[m.end():].strip()
                            if before:
                                (chapters[-1][1] if chapters else chapters.append(("Start", [])) or chapters[-1][1]).append(before)
                            chapters.append((title.title() if title.isupper() else title, [after] if after else []))
                        elif not chapters:
                            chapters.append(("Start", [par]))
                        else:
                            chapters[-1][1].append(par)
                    continue
                if not pars:
                    continue
                if toc and fname not in toc and chapters:
                    chapters[-1][1].extend(pars)
                else:
                    chapters.append((title or f"Del {len(chapters) + 1}", pars))
            return [(t, "\n\n".join(pars)) for t, pars in chapters if pars]
    except (OSError, KeyError, AttributeError, zipfile.BadZipFile):
        return []


def poster_files(video: Path, show: str | None) -> list[Path]:
    """Hvor plakaten ligger/lagres: «Film (1927)-poster.jpg» ved filmen, «poster.jpg» i seriemappen."""
    if show:
        return [video.parent / "poster.jpg", video.parent.parent / "poster.jpg"]
    return [video.with_name(video.stem + "-poster.jpg"), video.parent / "poster.jpg"]


def musicbrainz_cover(c: Any, artist: str, album: str) -> bytes | None:
    q = f'releasegroup:"{album}" AND artist:"{artist}"'
    r = c.get("https://musicbrainz.org/ws/2/release-group/", params={"query": q, "fmt": "json", "limit": 3})
    r.raise_for_status()
    for rg in r.json().get("release-groups", []):
        if int(rg.get("score", 0)) < 85:
            continue
        img = c.get(f"https://coverartarchive.org/release-group/{rg['id']}/front-500")
        if img.status_code == 200 and img.headers.get("content-type", "").startswith("image/"):
            return img.content
    return None


def wikipedia_image(c: Any, query: str) -> bytes | None:
    """Hovedbildet (plakaten) i Wikipedia-artikkelen som passer best: engelsk først (der ligger plakatene), så norsk."""
    for lang in ("en", "no"):
        r = c.get(f"https://{lang}.wikipedia.org/w/api.php", params={
            "action": "query", "generator": "search", "gsrsearch": query, "gsrlimit": 1, "prop": "pageimages",
            "piprop": "original", "pilicense": "any", "format": "json"})
        if r.status_code == 429:
            raise ValueError("Wikipedia ber oss vente")  # prøves igjen senere
        r.raise_for_status()
        pages = (r.json().get("query") or {}).get("pages") or {}
        for page in pages.values():
            src = (page.get("original") or {}).get("source")
            if src and src.split("?", 1)[0].lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                img = c.get(src)
                if img.status_code == 200 and img.headers.get("content-type", "").startswith("image/"):
                    return img.content
    return None


def open_library(c: Any) -> Library:
    home = Path.home()

    from olympus.config import media_dir

    def dirs(kind: str) -> list[Path]:
        v = c.get(f"media_server.{kind}_dir", "")
        return [Path(p).expanduser() for p in v] if isinstance(v, list) else [media_dir(c, kind)]

    roots = {"music": dirs("music"), "audiobooks": dirs("audiobook"), "ebooks": dirs("ebook"), "films": dirs("film"),
             "shows": dirs("show")}
    return Library(c.home / "media.db", roots, home / ".cache" / "olympus", c.home / "lab.db")
