"""Mediadelen av Olympus' API (/api/v1/media/…): bibliotek, strømming, cover, spillelister, bøker, film og
framdrift. Brukes av Zeus-appen (enhetsnøkkel) og Lab's på PC-en (cookie) – auth gjøres av mellomvaren i server.py.

Musikk og lydbøker strømmes som de er (telefonen spiller mp3/flac/ogg/opus/m4a selv), med Range så spoling virker.
Film spilles direkte når telefonen kan formatet; ellers omkoder ffmpeg til H.264/AAC i fragmentert MP4 – på
GPU-en (VAAPI) når det går – fra et valgfritt startpunkt (appen ber om nytt startpunkt når du spoler).
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import mimetypes
import shutil
from pathlib import Path
from typing import Any

from aiohttp import web

from olympus.medialib import SAFE_INPUT, Library

for _ext, _mime in ((".ogg", "audio/ogg"), (".oga", "audio/ogg"), (".opus", "audio/ogg"), (".flac", "audio/flac"),
                    (".m4a", "audio/mp4"), (".m4b", "audio/mp4"), (".mkv", "video/x-matroska"), (".epub", "application/epub+zip")):
    mimetypes.add_type(_mime, _ext)

SAFE_IMAGES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def add_media_routes(app: web.Application, lib: Library, on_change) -> None:
    r = app.router

    async def body(request: web.Request) -> dict:
        try:
            data = await request.json()
        except (ValueError, RecursionError):
            raise web.HTTPBadRequest(text="Ugyldig JSON.") from None
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="Forventet et JSON-objekt.")
        return data

    def ok(data: Any, status: int = 200) -> web.Response:
        return web.json_response(data, status=status)

    async def stats(_r):
        return ok(lib.stats())

    async def albums(_r):
        # ♥/✕ på albumet (album:id) følger med – én spørring, så lista kan vise det uten et kall per album
        likes = dict(lib.db.execute("SELECT item_id, value FROM user.likes WHERE item_id LIKE 'album:%'").fetchall())
        return ok([{**a, "like": int(likes.get(f"album:{a['id']}", 0))} for a in lib.albums()])

    async def album(request: web.Request):
        tracks = lib.album_tracks(request.match_info["id"])
        if not tracks:
            raise web.HTTPNotFound(text="Fant ikke albumet.")
        return ok(tracks)

    async def search(request: web.Request):
        try:
            limit = max(1, min(100, int(request.query.get("limit", "20"))))
        except (ValueError, OverflowError):
            raise web.HTTPBadRequest(text="limit må være et tall.") from None
        return ok(lib.search(request.query.get("q", "")[:200], limit))

    async def stream_track(request: web.Request):
        t = lib.track(request.match_info["id"])
        if t is None or not Path(t["path"]).is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(t["path"])  # Range/206 håndteres av aiohttp

    async def cover(request: web.Request):
        art = await asyncio.to_thread(lib.cover, request.match_info["id"])
        if art is None:
            raise web.HTTPNotFound()
        # Filtypen fra musikkfila stoles ikke på: bare vanlige bildetyper, og nettleseren får ikke gjette eller kjøre noe.
        mime = art[1] if art[1] in SAFE_IMAGES else "image/jpeg"
        return web.Response(body=art[0], content_type=mime, headers={
            "Cache-Control": "max-age=86400", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'"})

    # spillelister
    async def playlists(_r):
        return ok(lib.playlists())

    async def playlist(request: web.Request):
        if not any(p["id"] == request.match_info["id"] for p in lib.playlists()):
            raise web.HTTPNotFound(text="Fant ikke spillelista.")
        return ok(lib.playlist_tracks(request.match_info["id"]))

    def id_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            raise web.HTTPBadRequest(text="Forventet en liste med spor-id-er.")
        return [str(t) for t in value]

    def opt_name(value: Any) -> str | None:
        if value is not None and not isinstance(value, str):
            raise web.HTTPBadRequest(text="name må være tekst.")
        return value

    async def playlist_create(request: web.Request):
        d = await body(request)
        p = lib.playlist_create(opt_name(d.get("name")) or "", id_list(d.get("tracks", [])))
        await on_change("playlists")
        return ok(p, 201)

    async def playlist_update(request: web.Request):
        d = await body(request)
        pid = request.match_info["id"]
        try:
            if "tracks" in d:
                lib.playlist_set(pid, id_list(d["tracks"]), opt_name(d.get("name")))
            elif "add" in d:
                lib.playlist_add(pid, id_list(d["add"]))
            elif opt_name(d.get("name")):
                lib.playlist_set(pid, [t["id"] for t in lib.playlist_tracks(pid)], d["name"])
        except KeyError:
            raise web.HTTPNotFound() from None
        await on_change("playlists")
        return ok(lib.playlist_tracks(pid))

    async def playlist_delete(request: web.Request):
        if not lib.playlist_delete(request.match_info["id"]):
            raise web.HTTPNotFound()
        await on_change("playlists")
        return ok({"ok": True})

    # bøker
    async def books(_r):
        return ok(lib.books())

    async def book(request: web.Request):
        b = lib.book(request.match_info["id"])
        if b is None:
            raise web.HTTPNotFound()
        b.pop("path", None)
        return ok(b)

    async def book_part(request: web.Request):
        p = lib.part_path(request.match_info["id"], int(request.match_info["idx"]))
        if p is None or not p.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(p)

    async def book_text(request: web.Request):
        if lib.book(request.match_info["id"]) is None:
            raise web.HTTPNotFound(text="Fant ikke boka.")
        chapters = await asyncio.to_thread(lib.book_text, request.match_info["id"])
        return ok([{"title": t, "text": x} for t, x in chapters])

    # video
    async def videos(_r):
        return ok(lib.videos())

    async def video(request: web.Request):
        v = lib.video(request.match_info["id"])
        if v is None:
            raise web.HTTPNotFound()
        v.pop("path", None)
        return ok(v)

    async def poster(request: web.Request):
        p = await asyncio.to_thread(lib.poster, request.match_info["id"])
        if p is None:
            raise web.HTTPNotFound()
        return web.FileResponse(p, headers={"Cache-Control": "max-age=86400"})

    async def video_file(request: web.Request):
        v = lib.video(request.match_info["id"])
        if v is None or not Path(v["path"]).is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(v["path"])

    async def video_transcode(request: web.Request):
        """Omkod til H.264/AAC (fragmentert MP4) fra ?start=sekunder. GPU (VAAPI) først, ellers CPU."""
        v = lib.video(request.match_info["id"])
        if v is None or not Path(v["path"]).is_file():
            raise web.HTTPNotFound()
        try:
            start = max(0.0, float(request.query.get("start", "0")))
            height = min(1080, max(240, int(request.query.get("h", "720"))))
        except ValueError:
            raise web.HTTPBadRequest(text="start/h må være tall.") from None
        return await stream_transcode(request, v["path"], start, height)

    def _tv(request: web.Request) -> tuple[dict, str, int]:
        """Telefonen som mellomledd (casting borte): samme valg som hjemme (olympus.cast.plan) for TV-typen den fant."""
        from olympus.cast import Device, plan

        v = lib.video(request.match_info["id"])
        if v is None or not Path(v["path"]).is_file():
            raise web.HTTPNotFound()
        row = lib.db.execute("SELECT container FROM videos WHERE id = ?", (v["id"],)).fetchone()
        v = {**v, "container": row["container"] if row else ""}
        kind = request.query.get("kind", "chromecast")
        if kind not in ("chromecast", "dlna", "airplay"):
            raise web.HTTPBadRequest(text="kind må være chromecast, dlna eller airplay.")
        mode, height = plan(Device("phone", "", kind, "", model=request.query.get("model", "")[:80]), v)
        return v, mode, height

    async def tv_plan(request: web.Request):
        v, mode, height = await asyncio.to_thread(_tv, request)
        ext = Path(v["path"]).suffix.lower() if mode == "direct" else ".mp4"
        mime = {".mkv": "video/x-matroska", ".webm": "video/webm", ".mov": "video/quicktime"}.get(ext, "video/mp4")
        from olympus.cast import lang_tag, pick_subtitle

        sub = pick_subtitle(v.get("subtitles") or [], request.query.get("subtitle", "auto"))
        return ok({"mode": mode, "height": height, "ext": ext, "mime": mime, "title": v.get("title") or "",
                   "sub": {"idx": sub["idx"], "lang": lang_tag(sub.get("lang", ""))} if sub else None})

    async def tv_stream(request: web.Request):
        v, mode, height = await asyncio.to_thread(_tv, request)
        try:
            start = max(0.0, float(request.query.get("start", "0")))
        except ValueError:
            start = 0.0
        if mode == "direct":
            return web.FileResponse(v["path"])
        if mode == "remux":
            return await stream_remux(request, v["path"], start, v.get("vcodec") == "hevc")
        return await stream_transcode(request, v["path"], start, height or 1080)

    async def subtitle(request: web.Request):
        try:
            start = max(0.0, float(request.query.get("start", "0")))
        except ValueError:
            start = 0.0
        fmt = "srt" if request.query.get("fmt") == "srt" else "webvtt"
        out = await subtitle_bytes(lib, request.match_info["id"], int(request.match_info["idx"]), start, fmt)
        if out is None:
            raise web.HTTPNotFound()
        return web.Response(body=out, content_type="text/vtt" if fmt == "webvtt" else "application/x-subrip",
                            charset="utf-8", headers={"Access-Control-Allow-Origin": "*"})

    # musikksmak: lyttehistorikk og ♥/✕
    async def played(request: web.Request):
        d = await body(request)
        try:
            listened, duration = float(d.get("listened", 0)), float(d.get("duration", 0) or 0)
            if not (math.isfinite(listened) and math.isfinite(duration)):
                raise ValueError
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="listened må være sekunder.") from None
        if not lib.record_play(str(d.get("id", "")), listened, duration, str(d.get("source") or "telefon")):
            raise web.HTTPNotFound(text="Ukjent sang.")
        return ok({"ok": True})

    async def like_get(request: web.Request):
        return ok({"value": lib.like(request.match_info["id"])})

    # framdrift
    async def progress_put(request: web.Request):
        d = await body(request)
        try:
            pos, dur = float(d.get("position", 0)), float(d.get("duration", 0) or 0)
            if not (math.isfinite(pos) and math.isfinite(dur)) or dur < 0:
                raise ValueError
            iid = request.match_info["id"]
            if lib.track(iid) is None and lib.video(iid) is None and lib.book(iid) is None:
                raise web.HTTPNotFound(text="Ukjent id.")
            lib.set_progress(iid, pos, dur)
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="position må være et tall.") from None
        await on_change("progress")
        return ok({"ok": True})

    async def progress_get(request: web.Request):
        return ok(lib.progress(request.match_info["id"]) or {"position": 0})

    m = "/api/v1/media"
    r.add_get(f"{m}/stats", stats)
    r.add_get(f"{m}/albums", albums)
    r.add_get(f"{m}/album/{{id}}", album)
    r.add_get(f"{m}/search", search)
    r.add_get(f"{m}/track/{{id}}/stream", stream_track)
    r.add_get(f"{m}/track/{{id}}/cover", cover)
    r.add_get(f"{m}/playlists", playlists)
    r.add_post(f"{m}/playlists", playlist_create)
    r.add_get(f"{m}/playlist/{{id}}", playlist)
    r.add_patch(f"{m}/playlist/{{id}}", playlist_update)
    r.add_delete(f"{m}/playlist/{{id}}", playlist_delete)
    r.add_get(f"{m}/books", books)
    r.add_get(f"{m}/book/{{id}}", book)
    r.add_get(f"{m}/book/{{id}}/part/{{idx:\\d+}}", book_part)
    r.add_get(f"{m}/book/{{id}}/text", book_text)
    r.add_get(f"{m}/videos", videos)
    r.add_get(f"{m}/video/{{id}}", video)
    r.add_get(f"{m}/video/{{id}}/poster", poster)
    r.add_get(f"{m}/video/{{id}}/file", video_file)
    r.add_get(f"{m}/video/{{id}}/transcode", video_transcode)
    r.add_get(f"{m}/video/{{id}}/tvplan", tv_plan)
    r.add_get(f"{m}/video/{{id}}/tvstream", tv_stream)
    r.add_get(f"{m}/video/{{id}}/sub/{{idx:\\d+}}", subtitle)
    r.add_post(f"{m}/played", played)
    r.add_get(f"{m}/like/{{id}}", like_get)
    r.add_get(f"{m}/progress/{{id}}", progress_get)
    r.add_put(f"{m}/progress/{{id}}", progress_put)


def add_audio_routes(app: web.Application, lib: Library, on_change) -> None:
    """Lyden på PC-en (utgang, volum, Bluetooth) og avspilling der – appens lydfane."""
    from olympus import audio
    from olympus.mediaserver import PLAYER

    async def body(request: web.Request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="Ugyldig JSON.") from None
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="Forventet et JSON-objekt.")
        return data

    async def state(_r):
        return web.json_response({**await audio.status(), "pc": PLAYER.status()})

    async def volume(request: web.Request):
        d = await body(request)
        try:
            await audio.set_volume(int(d.get("percent", 50)), d.get("sink"))
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="percent må være et tall.") from None
        await on_change("audio")
        return web.json_response(await audio.status())

    async def mute(request: web.Request):
        d = await body(request)
        await audio.set_mute(bool(d.get("muted")), d.get("sink"))
        await on_change("audio")
        return web.json_response(await audio.status())

    async def output(request: web.Request):
        if not await audio.set_default(str((await body(request)).get("sink", ""))):
            raise web.HTTPNotFound(text="Ukjent utgang.")
        await on_change("audio")
        return web.json_response(await audio.status())

    async def bt(request: web.Request):
        d = await body(request)
        ok = await audio.bt_connect(str(d.get("mac", "")), bool(d.get("connect", True)))
        await on_change("audio")
        return web.json_response({"ok": ok, **await audio.status()})

    def finite(value: Any, name: str, hi: float) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or not 0 <= value < hi:
            raise web.HTTPBadRequest(text=f"{name} må være et tall fra 0.")
        return float(value)

    async def pc_play(request: web.Request):
        """Spill på PC-en: {"playlist"} | {"album"} | {"query"} | {"tracks": [id, …]}, valgfritt "index" (startsang i
        lista) og "position" (sekunder inn i den) – så telefonen kan gi fra seg avspillingen akkurat der den var."""
        d = await body(request)
        index = d.get("index", 0)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise web.HTTPBadRequest(text="index må være et heltall fra 0.")
        position = finite(d.get("position", 0), "position", 86400)
        if d.get("tracks") is not None:
            if not isinstance(d["tracks"], list) or not all(isinstance(i, str) for i in d["tracks"]) or len(d["tracks"]) > 2000:
                raise web.HTTPBadRequest(text="tracks må være en liste med spor-id-er.")
            songs = [t for t in (lib.track(i) for i in d["tracks"]) if t]
            if len(songs) != len(d["tracks"]) and index:  # ukjente id-er ville flyttet startsangen
                known = {t["id"] for t in songs}
                index = sum(1 for i in d["tracks"][:index] if i in known)
        elif d.get("playlist"):
            songs = lib.playlist_tracks(str(d["playlist"]))
        elif d.get("album"):
            songs = lib.album_tracks(str(d["album"]))
        else:
            songs = await asyncio.to_thread(lib.songs_for, str(d.get("query", "")))
        if not songs:
            raise web.HTTPNotFound(text="Fant ingen musikk.")
        if index >= len(songs):
            raise web.HTTPBadRequest(text=f"index er utenfor lista ({len(songs)} spor).")
        await PLAYER.play(lib, songs, index, position)
        await asyncio.sleep(0.2)
        await on_change("pc")  # eldre klienter; nye følger {"type": "player"} på websocketen
        return web.json_response(PLAYER.status())

    async def pc_control(request: web.Request):
        action = (await body(request)).get("action")
        fn = {"next": PLAYER.next, "prev": PLAYER.prev, "stop": PLAYER.stop, "pause": PLAYER.pause,
              "resume": PLAYER.resume}.get(action) if isinstance(action, str) else None
        if fn is None:
            raise web.HTTPBadRequest(text="action må være next, prev, stop, pause eller resume.")
        await fn()
        if action in ("next", "prev"):
            await asyncio.sleep(0.3)  # neste sang har startet
        await on_change("pc")
        return web.json_response(PLAYER.status())

    async def pc_state(_r: web.Request):
        return web.json_response(PLAYER.status())

    async def pc_handoff(_r: web.Request):
        """Ta med musikken: PC-en stopper, og telefonen får resten av køen og hvor langt sangen hadde kommet."""
        if PLAYER.current is None:
            return web.json_response({"error": "ingenting spilles på PC-en"}, status=409)
        st = PLAYER.status()
        await PLAYER.stop()
        return web.json_response({"tracks": st["queue"][st["index"]:], "index": 0, "position": st["position"]})

    r = app.router
    r.add_get("/api/v1/audio", state)
    r.add_get("/api/v1/pc", pc_state)
    r.add_post("/api/v1/audio/volume", volume)
    r.add_post("/api/v1/audio/mute", mute)
    r.add_post("/api/v1/audio/output", output)
    r.add_post("/api/v1/audio/bluetooth", bt)
    r.add_post("/api/v1/pc/play", pc_play)
    r.add_post("/api/v1/pc/control", pc_control)
    r.add_post("/api/v1/pc/handoff", pc_handoff)


async def stream_transcode(request: web.Request, path: str, start: float, height: int) -> web.StreamResponse:
    """Omkod til H.264/AAC (fragmentert MP4) fra start (sekunder), strømmet mens ffmpeg jobber. GPU (VAAPI) først,
    ellers CPU. Brukes av telefonen og av TV-casting (olympus.cast)."""
    resp = web.StreamResponse(headers={"Content-Type": "video/mp4", "Cache-Control": "no-store"})
    await resp.prepare(request)
    if request.method == "HEAD":
        return resp
    for args in (vaapi_args(height), cpu_args(height)):
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.2f}", *args[0], *SAFE_INPUT, "-i", path,
            "-map", "0:v:0", "-map", "0:a:0?", *args[1], "-c:a", "aac", "-b:a", "160k", "-ac", "2",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        assert proc.stdout is not None
        sent = 0
        try:
            while chunk := await proc.stdout.read(256 * 1024):
                await resp.write(chunk)
                sent += len(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            proc.kill()
            raise
        finally:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        if sent:
            break  # VAAPI virket (eller CPU); ellers prøv neste
    with contextlib.suppress(ConnectionResetError):
        await resp.write_eof()
    return resp


async def subtitle_bytes(lib, video_id: str, idx: int, start: float = 0.0, fmt: str = "webvtt") -> bytes | None:
    """Et undertekstspor som WebVTT (telefon, Chromecast) eller SRT (DLNA-TV-er), fra start (samme startpunkt som en
    omkodet strøm). None = finnes ikke."""
    s = lib.subtitle(video_id, idx)
    v = lib.video(video_id)
    if s is None or v is None:
        return None
    src = s["path"] or v["path"]
    args = ["ffmpeg", "-v", "error", "-nostdin", *SAFE_INPUT, "-i", src]
    if s["path"] is None:
        args += ["-map", f"0:{s['stream']}"]
    if start:
        args[4:4] = ["-ss", f"{start:.2f}"]  # før -i
    proc = await asyncio.create_subprocess_exec(*args, "-f", "srt" if fmt == "srt" else "webvtt", "pipe:1",
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return out


async def stream_remux(request: web.Request, path: str, start: float, hevc: bool) -> web.StreamResponse:
    """Pakk om uten å røre bildet (4K HEVC beholdes): MKV → fragmentert MP4, lyd (EAC3/DTS/TrueHD) → AAC. Nesten
    gratis for PC-en, full kvalitet – til TV-er som spiller HEVC/H.264 men ikke MKV eller lyden."""
    resp = web.StreamResponse(headers={"Content-Type": "video/mp4", "Cache-Control": "no-store"})
    await resp.prepare(request)
    if request.method == "HEAD":
        return resp
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.2f}", *SAFE_INPUT, "-i", path,
        "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", *(["-tag:v", "hvc1"] if hevc else []),
        "-c:a", "aac", "-b:a", "256k", "-ac", "2",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    assert proc.stdout is not None
    try:
        while chunk := await proc.stdout.read(256 * 1024):
            await resp.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        proc.kill()
        raise
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
    with contextlib.suppress(ConnectionResetError):
        await resp.write_eof()
    return resp


def vaapi_args(height: int) -> tuple[list[str], list[str]]:
    if not Path("/dev/dri/renderD128").exists():
        return [], []
    return (["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128", "-hwaccel_output_format", "vaapi"],
            ["-vf", f"scale_vaapi=w=-2:h='min({height},ih)':format=nv12", "-c:v", "h264_vaapi",
             *(["-b:v", "20M", "-maxrate", "30M", "-bufsize", "40M"] if height > 1080 else
               ["-b:v", "5M", "-maxrate", "8M", "-bufsize", "10M"])])


def cpu_args(height: int) -> tuple[list[str], list[str]]:
    return [], ["-vf", f"scale=-2:'min({height},ih)'", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p"]


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None
