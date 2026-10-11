"""Det du (og Zeus) kan gjøre med biblioteket utover å spille: undersøke en lenke før nedlasting, rette tittel/artist/år,
bytte cover og se hva et element er. Brukes av handlingene i olympus.actions – samme kode for appen, Lab's og Zeus.

Ingenting her laster ned media: `inspect` leser bare metadata (HEAD/første byte, yt-dlp --dump-single-json,
.torrent-fila, Archive-metadata), alltid gjennom olympus.netsafe.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from olympus import netsafe
from olympus.medialib import (
    AUDIO_EXT,
    EBOOK_EXT,
    EPISODE,
    SUB_EXT,
    VIDEO_EXT,
    poster_files,
)

KINDS = ("music", "film", "show", "audiobook", "ebook")
MUSIC_EXTRACTORS = {"soundcloud", "bandcamp", "jamendo", "mixcloud", "audiomack", "bandcampalbum", "soundcloudset",
                    "soundcloudplaylist", "youtubemusicsearchurl"}
YEAR_RE = re.compile(r"\b(18[89]\d|19\d\d|20\d\d)\b")
FIELD_MAX = 200


class MediaError(Exception):
    """Feil som skal tilbake til brukeren/Zeus (status brukes av HTTP)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# --- lenker -------------------------------------------------------------------------------------------
def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()



def is_torrent(url: str) -> bool:
    return url.startswith("magnet:") or urlparse(url).path.lower().endswith(".torrent")


def archive_identifier(url: str) -> str:
    """archive.org/details/<id> eller archive.org/download/<id>/<id>_archive.torrent → id ("" ellers)."""
    if host_of(url) not in ("archive.org", "www.archive.org"):
        return ""
    m = re.match(r"^/(details|download)/([A-Za-z0-9._-]{1,200})(/|$)", urlparse(url).path)
    return m.group(2) if m else ""


def kind_from_name(name: str) -> str:
    ext = Path(name.lower()).suffix
    if ext == ".m4b":
        return "audiobook"
    if ext in AUDIO_EXT:
        return "music"
    if ext in VIDEO_EXT:
        return "show" if EPISODE.match(Path(name).stem) else "film"
    if ext in EBOOK_EXT:
        return "ebook"
    return "unknown"


def kind_from_files(names: list[tuple[str, int]]) -> str:
    """Typen ut fra filene i en torrent: det som tar mest plass bestemmer."""
    weight: dict[str, int] = {}
    for n, size in names:
        k = kind_from_name(n)
        if k != "unknown":
            weight[k] = weight.get(k, 0) + max(1, size)
    if not weight:
        return "unknown"
    best = max(weight, key=lambda k: weight[k])
    if best == "film" and sum(1 for n, _ in names if Path(n).suffix.lower() in VIDEO_EXT) > 2:
        return "show"
    return best


def clean_title(name: str) -> tuple[str, int | None]:
    """«Metropolis.1927.1080p.mkv» → («Metropolis», 1927)."""
    stem = Path(name).stem if Path(name).suffix.lower() in AUDIO_EXT | VIDEO_EXT | EBOOK_EXT | {".torrent"} else name
    stem = re.sub(r"[._]+", " ", stem).strip()
    m = YEAR_RE.search(stem)
    year = int(m.group(1)) if m else None
    title = stem[:m.start()].strip(" -([") if m and m.start() > 0 else stem
    from olympus.medialib import strip_release

    return strip_release(title)[:FIELD_MAX] or "Uten navn", year


def bdecode(data: bytes) -> Any:
    """Minimal bencode-leser for .torrent-filer (med grenser, så en ondsinnet fil ikke kan henge serveren)."""
    pos = 0
    depth = 0

    def item() -> Any:
        nonlocal pos, depth
        if pos >= len(data):
            raise ValueError("avkortet")
        c = data[pos:pos + 1]
        if c == b"i":
            end = data.index(b"e", pos)
            v = int(data[pos + 1:end])
            pos = end + 1
            return v
        if c in (b"l", b"d"):
            depth += 1
            if depth > 50:
                raise ValueError("for dyp")
            pos += 1
            out: Any = [] if c == b"l" else {}
            while data[pos:pos + 1] != b"e":
                if c == b"l":
                    out.append(item())
                else:
                    k = item()
                    out[k] = item()
            pos += 1
            depth -= 1
            return out
        if c.isdigit():
            colon = data.index(b":", pos)
            n = int(data[pos:colon])
            if n < 0 or colon + 1 + n > len(data):
                raise ValueError("ugyldig lengde")
            v = data[colon + 1:colon + 1 + n]
            pos = colon + 1 + n
            return v
        raise ValueError("ugyldig bencode")

    return item()


def torrent_meta(data: bytes) -> tuple[str, int, list[tuple[str, int]]]:
    """(navn, total størrelse, [(fil, størrelse)]) fra en .torrent-fil."""
    try:
        meta = bdecode(data)
        info = meta[b"info"]
        name = info.get(b"name.utf-8", info.get(b"name", b"")).decode("utf-8", "replace")
        if b"files" in info:
            files = [("/".join(p.decode("utf-8", "replace") for p in f.get(b"path", [])), int(f.get(b"length", 0)))
                     for f in info[b"files"]]
        else:
            files = [(name, int(info.get(b"length", 0)))]
    except (ValueError, KeyError, TypeError, AttributeError, IndexError) as e:
        raise MediaError("Kunne ikke lese torrent-fila.", 422) from e
    return name, sum(s for _, s in files), files


def magnet_meta(url: str) -> tuple[str, int | None]:
    q = parse_qs(urlparse(url).query)
    name = unquote((q.get("dn") or [""])[0])
    xl = (q.get("xl") or [""])[0]
    return name, int(xl) if xl.isdigit() else None


def ytdlp_base(exe: str) -> list[str]:
    """Faste flagg for yt-dlp: ingen brukerconfig, ingen --exec. Alle uttrekkere, også den generiske."""
    return [exe, "--ignore-config", "--no-exec", "--no-warnings", "--socket-timeout", "15",
            "--js-runtimes", "node"]


def _ytdlp_info(url: str, timeout: float = 20.0) -> dict[str, Any]:
    exe = shutil.which("yt-dlp")
    if not exe:
        raise MediaError("yt-dlp er ikke installert.", 503)
    argv = [*ytdlp_base(exe), "--dump-single-json", "--flat-playlist", "--no-progress", "--", url]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise MediaError("Lenken svarte ikke i tide.", 504) from None
    if r.returncode != 0 or not r.stdout.strip():
        err = next((ln for ln in reversed(r.stderr.splitlines()) if "ERROR" in ln), "")
        raise MediaError(("Fant ingen media på lenken. " + err.replace("ERROR:", "").strip())[:300], 422)
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise MediaError("yt-dlp ga et uleselig svar.", 502) from None


def _ytdlp_kind(d: dict[str, Any]) -> str:
    ex = str(d.get("extractor_key") or d.get("extractor") or "").lower()
    if "librivox" in ex:
        return "audiobook"
    if any(ex.startswith(m) for m in MUSIC_EXTRACTORS) or d.get("vcodec") == "none" or d.get("track") or d.get("album"):
        return "music"
    if "Music" in (d.get("categories") or []):
        return "music"
    if (d.get("duration") or 0) >= 3600:
        return "film"
    return "unknown"


def _year(*vals: Any) -> int | None:
    for v in vals:
        if isinstance(v, int) and 0 < v < 3000:
            return v
        if isinstance(v, str) and v[:4].isdigit():
            return int(v[:4])
    return None


def _base(url: str, method: str) -> dict[str, Any]:
    return {"url": url, "kind": "unknown", "method": method, "title": "", "artist": "", "year": None, "size": None,
            "duration": None, "source": host_of(url) or ("magnet" if url.startswith("magnet:") else ""),
            "warnings": [], "thumbnail": None, "count": None}


def inspect(url: str) -> dict[str, Any]:
    """Hva en lenke er, uten å laste ned: tittel, type, størrelse, kilde og kilde. Kjøres i en tråd."""
    url = (url or "").strip()
    if url.startswith("magnet:"):
        if not re.search(r"xt=urn:bt[im]h:[A-Za-z0-9]+", url) or len(url) > 4000:
            raise MediaError("Ugyldig magnetlenke.")
        out = _base(url, "torrent")
        name, size = magnet_meta(url)
        out["title"], out["year"] = clean_title(name) if name else ("Uten navn", None)
        out["size"], out["kind"] = size, kind_from_name(name) if name else "unknown"
    else:
        try:
            url = netsafe.check_url(url)
        except netsafe.UnsafeURL as e:
            raise MediaError(f"Ugyldig lenke: {e}.") from None
        if (ident := archive_identifier(url)) and not is_torrent(url):
            out = _inspect_archive(url, ident)
        elif is_torrent(url):
            out = _base(url, "torrent")
            data, _ = _fetch(url, 10 * 1024 * 1024)
            name, size, files = torrent_meta(data)
            out["title"], out["year"] = clean_title(name)
            out["size"], out["kind"], out["count"] = size, kind_from_files(files), len(files)
        else:
            out = _inspect_http(url)
    if out["size"] and out["size"] > 50 * 1024**3:
        out["warnings"].append("Veldig stor (over 50 GB).")
    out["title"] = str(out["title"] or "")[:FIELD_MAX]
    out["artist"] = str(out["artist"] or "")[:FIELD_MAX]
    return out


def _fetch(url: str, limit: int) -> tuple[bytes, str]:
    try:
        return netsafe.fetch_bytes(url, max_bytes=limit, timeout=20, any_host=True)
    except netsafe.UnsafeURL as e:
        raise MediaError(f"Avvist: {e}.", 400) from None
    except Exception as e:  # noqa: BLE001 – httpx-feil, tidsavbrudd
        raise MediaError(f"Fikk ikke hentet lenken: {type(e).__name__}.", 502) from None


def _inspect_archive(url: str, ident: str) -> dict[str, Any]:
    from olympus.downloads import _pick_archive_files

    out = _base(url, "direct")
    data, _ = _fetch(f"https://archive.org/metadata/{ident}", 20 * 1024 * 1024)
    try:
        meta = json.loads(data)
        md = meta.get("metadata") or {}
    except (ValueError, AttributeError):
        raise MediaError("Archive.org svarte uleselig.", 502) from None
    if not md:
        raise MediaError("Fant ikke elementet på archive.org.", 404)
    mt = str(md.get("mediatype") or "")
    cols = md.get("collection") or []
    cols = [cols] if isinstance(cols, str) else cols
    kind = {"movies": "film", "texts": "ebook", "audio": "audiobook" if "librivoxaudio" in cols else "music"}.get(mt, "unknown")
    files = [f for f in meta.get("files", []) if f.get("source") in ("original", "derivative")]
    picked = set(_pick_archive_files(files, kind)) if kind != "unknown" else set()
    first = lambda v: (v[0] if isinstance(v, list) and v else v) or ""
    out.update(kind=kind, title=first(md.get("title")) or ident, artist=first(md.get("creator")),
               year=_year(first(md.get("year")), first(md.get("date"))),
               size=sum(int(f.get("size") or 0) for f in files if f.get("name") in picked) or None,
               duration=sum(float(f.get("length") or 0) for f in files if f.get("name") in picked
                            and str(f.get("length") or "").replace(".", "", 1).isdigit()) or None,
               thumbnail=f"https://archive.org/services/img/{ident}", count=len(picked) or None)
    return out


def _inspect_http(url: str) -> dict[str, Any]:
    """Direkte fil (HEAD / første byte) – ellers en side yt-dlp kjenner (YouTube, SoundCloud, Bandcamp …)."""
    ctype, length, final, name = "", None, url, ""
    for method, headers in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        try:
            with netsafe.open_url(url, method=method, headers=headers, max_bytes=1 << 16, timeout=15, any_host=True) as f:
                ctype, length, final = f.content_type, f.length, f.url
                cd = f.headers.get("content-disposition", "")
                m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
                name = unquote(m.group(1)) if m else unquote(urlparse(final).path.rsplit("/", 1)[-1])
            break
        except netsafe.UnsafeURL as e:
            raise MediaError(f"Avvist: {e}.", 400) from None
        except Exception:  # noqa: BLE001 – HEAD ikke støttet o.l.: prøv neste
            continue
    media_type = ctype.startswith(("audio/", "video/")) or ctype in ("application/epub+zip", "application/pdf")
    if media_type or (ctype in ("application/octet-stream", "binary/octet-stream", "") and kind_from_name(name) != "unknown"):
        out = _base(url, "direct")
        out["title"], out["year"] = clean_title(name or "fil")
        out["size"] = length
        out["kind"] = kind_from_name(name)
        if out["kind"] == "unknown":
            out["kind"] = ("music" if ctype.startswith("audio/") else "film" if ctype.startswith("video/") else "ebook")
        return out
    if ctype == "application/x-bittorrent":
        out = _base(url, "torrent")
        data, _ = _fetch(url, 10 * 1024 * 1024)
        tname, size, files = torrent_meta(data)
        out["title"], out["year"] = clean_title(tname)
        out["size"], out["kind"], out["count"] = size, kind_from_files(files), len(files)
        return out
    d = _ytdlp_info(url)
    out = _base(url, "ytdlp")
    if d.get("_type") == "playlist":
        entries = d.get("entries") or []
        out["count"] = int(d.get("playlist_count") or len(entries)) or None
        out["duration"] = sum(float(e.get("duration") or 0) for e in entries if isinstance(e, dict)) or None
        first_entry = next((e for e in entries if isinstance(e, dict)), {})
        out["kind"] = _ytdlp_kind({**first_entry, **{k: v for k, v in d.items() if v}})
        if (out["count"] or 0) > 300:
            out["warnings"].append("Bare de første 300 i lista hentes.")
    else:
        out["duration"] = float(d["duration"]) if isinstance(d.get("duration"), int | float) else None
        out["size"] = d.get("filesize") or d.get("filesize_approx")
        out["kind"] = _ytdlp_kind(d)
    out["title"] = d.get("track") or d.get("album") or d.get("title") or ""
    out["artist"] = d.get("artist") or d.get("creator") or d.get("album_artist") or d.get("uploader") or d.get("channel") or ""
    out["year"] = _year(d.get("release_year"), d.get("upload_date"))
    thumb = d.get("thumbnail") or next((t.get("url") for t in reversed(d.get("thumbnails") or []) if isinstance(t, dict)), None)
    out["thumbnail"] = thumb if isinstance(thumb, str) and thumb.startswith("https://") else None
    return out


def classify(url: str) -> str:
    """Hvordan en lenke skal hentes, uten nettverk der det går: torrent | archive | direct | ytdlp."""
    if is_torrent(url):
        return "torrent"
    parts = [p for p in urlparse(url).path.split("/") if p]
    if archive_identifier(url) and len(parts) >= 3 and parts[0] == "download" and kind_from_name(parts[-1]) != "unknown":
        return "direct"  # archive.org/download/<id>/<fil>: bare den fila, ikke alle filene i elementet
    if archive_identifier(url):
        return "archive"
    if kind_from_name(urlparse(url).path) != "unknown":
        return "direct"
    return ""


# --- elementer i biblioteket ---------------------------------------------------------------------------
def _home(p: Path) -> str:
    try:
        return "~/" + str(p.relative_to(Path.home()))
    except ValueError:
        return str(p)


def _common_folder(files: list[Path]) -> Path:
    import os

    if not files:
        return Path()
    if len(files) == 1:
        return files[0].parent
    return Path(os.path.commonpath([str(f) for f in files]))


def resolve_item(lib: Any, item_id: str) -> tuple[str, dict[str, Any], list[Path]]:
    """(type, rad, filer) for en id: album | track | video | book. MediaError 404 hvis ukjent."""
    item_id = str(item_id or "")
    if not item_id or len(item_id) > 100:
        raise MediaError("Ukjent id.", 404)
    db = lib.db
    rows = db.execute("SELECT * FROM tracks WHERE album_id = ? ORDER BY disc_no, track_no", (item_id,)).fetchall()
    if rows:
        return "album", dict(rows[0]), [Path(r["path"]) for r in rows]
    r = db.execute("SELECT * FROM tracks WHERE id = ?", (item_id,)).fetchone()
    if r:
        return "track", dict(r), [Path(r["path"])]
    r = db.execute("SELECT * FROM videos WHERE id = ?", (item_id,)).fetchone()
    if r:
        return "video", dict(r), [Path(r["path"])]
    r = db.execute("SELECT * FROM books WHERE id = ?", (item_id,)).fetchone()
    if r:
        parts = [Path(p[0]) for p in db.execute("SELECT DISTINCT path FROM book_parts WHERE book_id = ?", (item_id,))]
        return "book", dict(r), parts or [Path(r["path"])]
    raise MediaError("Fant ikke elementet i biblioteket.", 404)


def item_info(lib: Any, item_id: str) -> dict[str, Any]:
    kind, r, files = resolve_item(lib, item_id)
    size = sum(f.stat().st_size for f in files if f.is_file())
    out: dict[str, Any] = {"id": item_id, "type": kind, "title": "", "artist": "", "album": "", "year": None, "genre": "",
                           "files": len(files), "size": size, "folder": _home(_common_folder(files))}
    if kind == "album":
        rows = lib.db.execute("SELECT year, genre FROM tracks WHERE album_id = ?", (item_id,)).fetchall()
        years = [x["year"] for x in rows if x["year"]]
        genres = [x["genre"] for x in rows if x["genre"]]
        out.update(title=r["album"] or "", artist=r["album_artist"] or "", album=r["album"] or "",
                   year=min(years) if years else None, genre=max(set(genres), key=genres.count) if genres else "")
    elif kind == "track":
        out.update(title=r["title"] or "", artist=r["artist"] or "", album=r["album"] or "", year=r["year"],
                   genre=r["genre"] or "")
    elif kind == "video":
        out.update(title=r["title"] or "", artist="", album=r["show"] or "", year=r["year"])
    else:
        out.update(title=r["title"] or "", artist=r["author"] or "")
    y = out["year"]
    if isinstance(y, int) and y > 3000 and str(y)[:4].isdigit():  # «20200319» fra date-taggen → 2020
        out["year"] = int(str(y)[:4])
    return out


EDIT_FIELDS = ("title", "artist", "album", "album_artist", "year", "genre")


def validate_fields(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in EDIT_FIELDS:
        if k not in d or d[k] is None:
            continue
        v = d[k]
        if k == "year":
            if isinstance(v, str) and v.strip().isdigit():
                v = int(v.strip())
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 3000:
                raise MediaError("year må være et heltall fra 0 til 3000.")
            out[k] = v
            continue
        if not isinstance(v, str):
            raise MediaError(f"{k} må være tekst.")
        v = " ".join(v.split())
        if len(v) > FIELD_MAX:
            raise MediaError(f"{k} kan være maks {FIELD_MAX} tegn.")
        if any(ord(ch) < 32 for ch in v):
            raise MediaError(f"{k} har ugyldige tegn.")
        out[k] = v
    if not out:
        raise MediaError("Ingen felt å endre (title, artist, album, album_artist, year, genre).")
    return out


TAG_KEYS = {"title": "title", "artist": "artist", "album": "album", "album_artist": "albumartist", "year": "date",
            "genre": "genre"}


def write_tags(path: Path, fields: dict[str, Any]) -> bool:
    """Skriv taggene i fila med mutagen (mp3, flac, ogg, opus, m4a …). False hvis formatet ikke har tagger."""
    try:
        import mutagen

        f = mutagen.File(path, easy=True)
        if f is None:
            return False
        if f.tags is None:
            f.add_tags()
        for k, v in fields.items():
            key = TAG_KEYS[k]
            if v in ("", None):
                if key in f.tags:
                    del f.tags[key]
            else:
                f.tags[key] = [str(v)]
        f.save()
        return True
    except Exception:  # noqa: BLE001 – formatet støtter ikke tagger (wav, mkv) eller fila er rar
        return False


def edit_item(lib: Any, item_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Rett tittel/artist/album/år/sjanger. Tagger skrives i filene når formatet tåler det, ellers lagres det som en
    overstyring i lab.db. Biblioteket skannes på nytt; id-en kan endre seg (den bygger på taggene) – svaret har den nye."""
    fields = validate_fields(raw)
    kind, r, files = resolve_item(lib, item_id)
    if kind == "album":
        per_file = {k: v for k, v in {"album": fields.get("title", fields.get("album")),
                                      "album_artist": fields.get("artist", fields.get("album_artist")),
                                      "year": fields.get("year"), "genre": fields.get("genre")}.items() if v is not None}
        targets = files
    elif kind == "track":
        per_file, targets = fields, files
    elif kind == "video":
        per_file = {k: v for k, v in {"title": fields.get("title"), "year": fields.get("year"),
                                      "album": fields.get("album")}.items() if v is not None}
        targets = files
    else:  # bok: tittel og forfatter for hele boka (mappa/fila), ikke per kapittel
        per_file = {k: v for k, v in {"album": fields.get("title", fields.get("album")),
                                      "artist": fields.get("artist")}.items() if v is not None}
        targets = files
    if not per_file:
        raise MediaError("Ingen av feltene gjelder for denne typen.")
    before = _ids_by_path(lib, files)
    old_album = r.get("album_id") if isinstance(r, dict) else None
    for f in targets:
        if not lib.inside_library(f) or not f.is_file() or f.is_symlink():
            continue
        # Hardlenket fil (torrenten deler den fortsatt): ikke rør innholdet – det ville ødelagt delingen.
        if kind == "video" or f.stat().st_nlink > 1 or not write_tags(f, per_file):
            lib.set_override(f, per_file)
            lib.db.execute("UPDATE tracks SET mtime = -1 WHERE path = ?", (str(f),))
            lib.db.execute("UPDATE videos SET mtime = -1 WHERE path = ?", (str(f),))
    if kind == "book":
        lib.set_override(Path(r["path"]), per_file)  # boka er mappa (eller .m4b-/e-bokfila)
        lib.db.execute("UPDATE books SET mtime = -1 WHERE id = ?", (item_id,))
    lib.db.commit()
    lib.scan()
    _carry_over(lib, before, _ids_by_path(lib, files), old_album)
    return item_info(lib, _find_again(lib, kind, files, r))


def _ids_by_path(lib: Any, files: list[Path]) -> dict[str, tuple[str, str]]:
    """path → (id, album_id) for spor, filmer og bøker som ligger i disse filene."""
    out: dict[str, tuple[str, str]] = {}
    paths = [str(f) for f in files]
    for i in range(0, len(paths), 500):
        chunk = paths[i:i + 500]
        q = ",".join("?" * len(chunk))
        for row in lib.db.execute(f"SELECT path, id, album_id FROM tracks WHERE path IN ({q})", chunk):
            out[row[0]] = (row[1], row[2] or "")
        for row in lib.db.execute(f"SELECT path, id FROM videos WHERE path IN ({q})", chunk):
            out[row[0]] = (row[1], "")
    return out


def _carry_over(lib: Any, before: dict[str, tuple[str, str]], after: dict[str, tuple[str, str]], old_album: str | None) -> None:
    """Id-en bygger på taggene, så den kan endre seg ved redigering. Det du har knyttet til den gamle id-en – plass i
    spillelister, ♥/✕, lyttehistorikk og framdrift – flyttes til den nye, så ingenting forsvinner."""
    db = lib.db
    new_album = ""
    for path, (old, old_alb) in before.items():
        new, alb = after.get(path, ("", ""))
        new_album = new_album or alb
        if not new or new == old:
            continue
        db.execute("UPDATE user.playlist_items SET track_id = ? WHERE track_id = ?", (new, old))
        db.execute("UPDATE user.plays SET track_id = ? WHERE track_id = ?", (new, old))
        db.execute("UPDATE OR REPLACE user.likes SET item_id = ? WHERE item_id = ?", (new, old))
        db.execute("UPDATE OR REPLACE user.progress SET item_id = ? WHERE item_id = ?", (new, old))
    for old_alb in {old_album or "", *(alb for _, alb in before.values())} - {"", new_album}:
        if new_album:
            db.execute("UPDATE OR REPLACE user.likes SET item_id = ? WHERE item_id = ?", (f"album:{new_album}", f"album:{old_alb}"))
    db.commit()


def _find_again(lib: Any, kind: str, files: list[Path], r: dict[str, Any]) -> str:
    db = lib.db
    first = str(files[0])
    if kind == "album":
        row = db.execute("SELECT album_id FROM tracks WHERE path = ?", (first,)).fetchone()
    elif kind == "track":
        row = db.execute("SELECT id FROM tracks WHERE path = ?", (first,)).fetchone()
    elif kind == "video":
        row = db.execute("SELECT id FROM videos WHERE path = ?", (first,)).fetchone()
    else:
        row = db.execute("SELECT id FROM books WHERE path = ?", (r["path"],)).fetchone()
    if not row:
        raise MediaError("Elementet forsvant under skanningen.", 409)
    return row[0]


# --- cover ---------------------------------------------------------------------------------------------
IMAGE_MAX = 10 * 1024 * 1024


def sniff_image(data: bytes) -> str:
    """Bildetype fra de første bytene (jpeg | png | webp) – Content-Type fra klienten stoles ikke på."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise MediaError("Ikke et JPEG-, PNG- eller WebP-bilde.", 415)


def to_jpeg(data: bytes) -> bytes:
    """Sjekk bildet med Pillow og lagre som JPEG (maks 1500 px). Uten Pillow: bare JPEG godtas som det er."""
    fmt = sniff_image(data)
    if len(data) > IMAGE_MAX:
        raise MediaError("Bildet er større enn 10 MB.", 413)
    try:
        import io

        from PIL import Image
    except ImportError:
        if fmt != "jpeg":
            raise MediaError("Bare JPEG støttes her (Pillow mangler).", 415) from None
        return data
    try:
        Image.MAX_IMAGE_PIXELS = 50_000_000
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in ("JPEG", "PNG", "WEBP"):
                raise MediaError("Ukjent bildeformat.", 415)
            im = im.convert("RGB")
            im.thumbnail((1500, 1500))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90)
            return buf.getvalue()
    except MediaError:
        raise
    except Exception:  # noqa: BLE001 – ødelagt bilde, bombe o.l.
        raise MediaError("Bildet kunne ikke leses.", 415) from None


def cover_target(lib: Any, item_id: str) -> tuple[str, Path]:
    """Hvor coveret til et element ligger: album/sang → cover.jpg i albummappa, lydbok → cover.jpg i bokmappa,
    film/episode → plakaten biblioteket leser (Film-poster.jpg / poster.jpg i seriemappa)."""
    kind, r, files = resolve_item(lib, item_id)
    if kind in ("album", "track"):
        return kind, files[0].parent / "cover.jpg"
    if kind == "video":
        return kind, poster_files(files[0], r.get("show"))[0]
    p = Path(r["path"])
    if p.is_dir():
        return kind, p / "cover.jpg"
    if r.get("kind") == "audio":  # .m4b-fil
        return kind, p.with_name(p.stem + "-cover.jpg")
    raise MediaError("E-bøker har ikke eget cover her.", 400)


def set_cover(lib: Any, item_id: str, data: bytes) -> dict[str, Any]:
    jpeg = to_jpeg(data)
    kind, target = cover_target(lib, item_id)
    if not lib.inside_library(target.parent) or target.is_symlink():
        raise MediaError("Mappa ligger utenfor biblioteket.", 400)
    tmp = target.with_name(f".{target.name}.{int(time.time())}.tmp")
    tmp.write_bytes(jpeg)
    tmp.replace(target)
    clear_art_cache(lib, item_id, kind)
    return {"ok": True, "type": kind, "path": _home(target), "size": len(jpeg)}


def clear_art_cache(lib: Any, item_id: str, kind: str) -> None:
    if kind in ("album", "track"):
        album = item_id if kind == "album" else lib.db.execute("SELECT album_id FROM tracks WHERE id = ?", (item_id,)).fetchone()[0]
        lib.db.execute("UPDATE tracks SET cover = 1 WHERE album_id = ?", (album,))
        lib.db.execute("INSERT OR REPLACE INTO art_tried VALUES (?, ?, 1)", (f"album:{album}", time.time()))
        lib.db.commit()
    elif kind == "video":
        (lib.cache / "posters" / f"{item_id}.jpg").unlink(missing_ok=True)


def fetch_cover(lib: Any, item_id: str) -> bool:
    """Hent cover/plakat fra nettet på nytt for akkurat dette elementet (MusicBrainz/Cover Art Archive, Wikipedia)."""
    import httpx

    from olympus.medialib import musicbrainz_cover, wikipedia_image

    kind, r, files = resolve_item(lib, item_id)
    headers = {"User-Agent": "OlympusLab/0.4 (personlig mediabibliotek)"}
    with httpx.Client(timeout=20, headers=headers, follow_redirects=True) as c:
        try:
            if kind in ("album", "track"):
                img = musicbrainz_cover(c, r["album_artist"] or r["artist"] or "", r["album"] or "")
            elif kind == "video":
                q = f"{r['show']} TV series" if r["kind"] == "episode" else f"{r['title']} {r['year'] or ''} film"
                img = wikipedia_image(c, q)
            else:
                img = None
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            img = None
    if not img:
        return False
    try:
        set_cover(lib, item_id, img)
    except MediaError:
        return False
    return True


# --- opplasting ----------------------------------------------------------------------------------------
UPLOAD_EXT = {"music": AUDIO_EXT - {".m4b"}, "audiobook": AUDIO_EXT, "film": VIDEO_EXT | SUB_EXT,
              "show": VIDEO_EXT | SUB_EXT, "ebook": EBOOK_EXT}
UPLOAD_MAX = 8 * 1024**3


def safe_filename(name: str) -> str:
    from olympus.downloads import _safe

    name = Path(unquote(str(name)).replace("\\", "/")).name  # aldri mapper fra klienten (heller ikke %2F)
    return _safe(name)


def check_upload_name(kind: str, name: str) -> str:
    if kind not in UPLOAD_EXT:
        raise MediaError(f"kind må være en av {', '.join(UPLOAD_EXT)}.")
    safe = safe_filename(name)
    if Path(safe).suffix.lower() not in UPLOAD_EXT[kind]:
        allowed = " ".join(sorted(UPLOAD_EXT[kind]))
        raise MediaError(f"Filtypen er ikke lov for {kind} (lov: {allowed}).", 415)
    return safe


def real_media(path: Path, kind: str) -> bool:
    """Er fila det den sier den er? Lyd må ha et lydspor, video et videospor (ffprobe, uten nett), e-bøker riktig
    signatur. Stopper tekst eller programmer som er døpt om til .ogg/.mp4/.epub."""
    import json as _json
    import shutil as _shutil
    import subprocess as _sp

    from olympus.medialib import SAFE_INPUT

    suffix = path.suffix.lower()
    head = path.open("rb").read(8) if path.is_file() else b""
    if suffix == ".epub":
        return head.startswith(b"PK")
    if suffix == ".pdf":
        return head.startswith(b"%PDF")
    if suffix in (".srt", ".vtt", ".ass", ".ssa", ".txt"):
        return True
    exe = _shutil.which("ffprobe")
    if not exe:
        return True  # kan ikke sjekke: slipp gjennom (skanningen hopper uansett over filer uten spor)
    try:
        out = _sp.run([exe, "-v", "error", *SAFE_INPUT, "-show_streams", "-of", "json", "--", str(path)],
                      capture_output=True, text=True, timeout=30).stdout
        types = {st.get("codec_type") for st in _json.loads(out or "{}").get("streams", [])}
    except (OSError, ValueError, _sp.TimeoutExpired):
        return False
    return "video" in types if kind in ("film", "show") else "audio" in types
