"""Alt Olympus kan gjøre, på ett sted: én motor, to måter å styre den på.

Hver handling her er én evne på serveren (legg til media, rett tagger, avbryt en nedlasting, lås skjermen …), med
  - navn («media.add»), norsk etikett og beskrivelse,
  - parametre som JSON-skjema,
  - writes (endrer noe), ask (Zeus må få ja først), danger (rødt kort / appen bekrefter selv),
    cloud_sensitive (svaret er personlig: spør før det sendes til en skymodell),
  - HTTP-ruten (appen og Lab's på PC-en) og Zeus-verktøyet (+ handlingsverdien) den nås med,
  - selve implementasjonen: `async run(ctx, args)`, felles for appen, PC-en og Zeus.

Fra lista lages: Zeus' verktøy for media, downloads, pc og model (olympus.tools henter dem herfra), de nye
HTTP-rutene (olympus.web.actions_api), GET /api/v1/actions og oversikten over evner i Zeus' systemprompt.
Handlinger der både ruten og Zeus-verktøyet fantes fra før (plan, playlist, audio, memory, media_search), står her
med `run=None`: de peker på den eksisterende ruten og verktøyet (med sine finjusterte godkjenningskort), så
paritetstesten ser at begge veier finnes.

Godkjenning: kaller Zeus en handling med ask=True, får brukeren et godkjenningskort (rødt når danger). Trykker
brukeren selv i appen eller Lab's, er trykket godkjenningen (appen bekrefter de farlige selv).
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from olympus.mediaops import KINDS, MediaError

ActionError = MediaError  # (melding, status) – blir {"error": …} med status over HTTP, og en feil til Zeus


@dataclass
class Ctx:
    """Det en handling trenger: planen, biblioteket, nedlastingene, huben (modell, økter, kringkasting) og minnet."""

    lab: Any = None
    lib: Any = None
    dl: Any = None
    hub: Any = None
    memory: Any = None
    zeus: bool = False  # kallet kommer fra Zeus (og er godkjent der det trengs)
    who: str = "du"
    found: dict[str, Any] = field(default_factory=dict)  # treff fra /find og media_find (id → Result)

    async def send(self, msg: dict[str, Any]) -> None:
        if self.hub is not None:
            await self.hub.send(msg)

    async def changed(self, what: str, **extra: Any) -> None:
        await self.send({"type": "media_changed", "what": what, **extra})

    def need(self, *names: str) -> None:
        for n in names:
            if getattr(self, n) is None:
                raise ActionError("Dette er bare tilgjengelig i Olympus-serveren.", 503)


@dataclass(frozen=True)
class Action:
    name: str
    label: str
    description: str
    method: str
    path: str
    params: dict[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    run: Callable[[Ctx, dict[str, Any]], Awaitable[Any]] | None = None
    writes: bool = False
    ask: bool | None = None  # None = som writes
    danger: bool = False
    danger_if: dict[str, tuple[str, ...]] = field(default_factory=dict)  # {"action": ("sleep",)}
    cloud_sensitive: bool = False
    egress: bool = False  # serveren kontakter en lenke/vert fra argumentene: Zeus må alltid få ja, med hele lenken på kortet
    tool: str = ""
    tool_actions: tuple[str, ...] = ()
    status: int = 200  # HTTP-status ved suksess
    say: Callable[[Any], str] | None = None  # svaret som tekst til Zeus
    preview: Callable[[Ctx, dict[str, Any]], str] | None = None  # godkjenningskortet
    check: Callable[[Ctx, dict[str, Any]], str] | None = None  # "" = ok, ellers avvis før kortet
    custom_http: bool = False  # ruten er skrevet for hånd i web/actions_api.py (multipart, bilde-bytes, ?forget)
    app_only: str = ""  # ikke et Zeus-verktøy, med begrunnelse

    @property
    def needs_approval(self) -> bool:
        return self.writes if self.ask is None else self.ask

    def is_danger(self, args: dict[str, Any]) -> bool:
        return self.danger or any(str(args.get(k)) in v for k, v in self.danger_if.items())

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "label": self.label, "description": self.description,
                "params": schema(self.params, self.required), "writes": self.writes, "danger": self.danger,
                "danger_if": {k: list(v) for k, v in self.danger_if.items()},
                "route": {"method": self.method, "path": self.path}}


def schema(props: dict[str, Any], required: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(required)}


S = lambda desc="", **kw: {"type": "string", **({"description": desc} if desc else {}), **kw}  # noqa: E731
I = lambda desc="", **kw: {"type": "integer", **({"description": desc} if desc else {}), **kw}  # noqa: E731
KIND = {"type": "string", "enum": list(KINDS), "description": "music | film | show | audiobook | ebook"}
ID = S("id fra biblioteket (album, sang, film/episode eller bok)", maxLength=100)


# --- validering -------------------------------------------------------------------------------------------
def clean_args(action: Action, raw: dict[str, Any]) -> dict[str, Any]:
    """Bare kjente felt, riktige typer, innenfor grensene. Tall fra spørrestrengen godtas som tekst."""
    out: dict[str, Any] = {}
    for k, sch in action.params.items():
        if k not in raw or raw[k] is None:
            continue
        v, t = raw[k], sch.get("type")
        if t == "integer":
            if isinstance(v, str) and v.strip().lstrip("-").isdigit():
                v = int(v)
            if isinstance(v, bool) or not isinstance(v, int):
                raise ActionError(f"{k} må være et heltall.")
            if ("minimum" in sch and v < sch["minimum"]) or ("maximum" in sch and v > sch["maximum"]):
                raise ActionError(f"{k} er utenfor gyldig område.")
        elif t == "number":
            if isinstance(v, str):
                try:
                    v = float(v)
                except ValueError:
                    raise ActionError(f"{k} må være et tall.") from None
            if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
                raise ActionError(f"{k} må være et tall.")
        elif t == "boolean":
            if isinstance(v, str) and v.strip().lower() in ("1", "true", "0", "false"):  # fra spørrestrengen
                v = v.strip().lower() in ("1", "true")
            if not isinstance(v, bool):
                raise ActionError(f"{k} må være true/false.")
        elif t == "array":
            if not isinstance(v, list) or len(v) > 2000 or not all(isinstance(x, str) and len(x) <= 100 for x in v):
                raise ActionError(f"{k} må være en liste med id-er.")
        else:
            if not isinstance(v, str):
                raise ActionError(f"{k} må være tekst.")
            if len(v) > sch.get("maxLength", 4000):
                raise ActionError(f"{k} er for lang.")
        if "enum" in sch and v not in sch["enum"]:
            raise ActionError(f"{k} må være en av: {', '.join(map(str, sch['enum']))}.")
        out[k] = v
    missing = [r for r in action.required if r not in out or out[r] == ""]
    if missing:
        raise ActionError(f"Mangler {', '.join(missing)}.")
    return out


# --- media ------------------------------------------------------------------------------------------------
async def _inspect(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    from olympus import mediaops

    return await asyncio.to_thread(mediaops.inspect, a["url"])


def _say_inspect(d: dict[str, Any]) -> str:
    size = f"{d['size'] / 1e9:.1f} GB" if (d.get("size") or 0) > 1e9 else f"{(d.get('size') or 0) / 1e6:.0f} MB" if d.get("size") else "ukjent størrelse"
    lines = [f"{d['title'] or '(uten tittel)'}" + (f" – {d['artist']}" if d.get("artist") else "") + (f" ({d['year']})" if d.get("year") else ""),
             f"type: {d['kind']} · hentes med {d['method']} · {size} · fra {d['source']}"]
    if d.get("count"):
        lines.append(f"{d['count']} elementer")
    lines += [f"⚠ {w}" for w in d.get("warnings") or []]
    return "\n".join(lines)


def _add_preview(ctx: Ctx, a: dict[str, Any]) -> str:
    from olympus import mediaops

    if a.get("id"):
        res = ctx.found.get(str(a["id"]))
        if res is None:
            return f"ukjent treff {a['id']}"
        return f"{res.title} ({res.year or '?'}) · {res.source} → {res.kind}\nLenke: {res.torrent or res.direct or res.page}"
    url = str(a.get("url", ""))
    return f"{a.get('title') or '(tittel fra kilden)'} → {a.get('kind', '?')}\n  {url}"


def _add_check(ctx: Ctx, a: dict[str, Any]) -> str:
    if not a.get("id") and not (a.get("url") and a.get("kind")):
        return "Gi url og kind (eller id fra media_find)."
    return ""


async def _add(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    """Start en nedlasting: torrent (Transmission), Archive (bare filene som trengs), direkte fil eller yt-dlp."""
    from olympus import mediaops, netsafe

    ctx.need("dl")
    dl = ctx.dl
    if a.get("id"):
        res = ctx.found.get(str(a["id"]))
        if res is None:
            raise ActionError("Ukjent treff – søk på nytt.", 404)
        row = await _guarded(dl.start, res.kind, res.title, res.torrent, "" if res.torrent else res.direct,
                                      res.year, res.source, res.page, res.creator, res.extra.get("identifier", ""))
    else:
        url, kind = str(a.get("url") or "").strip(), a.get("kind")
        if not url or kind not in KINDS:
            raise ActionError("Trenger url og kind (music, film, show, audiobook, ebook).")
        title, artist, year = a.get("title") or "", a.get("artist") or "", a.get("year")
        if url.startswith("magnet:"):
            if not re.search(r"xt=urn:bt[im]h:[A-Za-z0-9]+", url) or len(url) > 4000:
                raise ActionError("Ugyldig magnetlenke.")
            method = "torrent"
            if not title:
                name, _ = mediaops.magnet_meta(url)
                title, year = (mediaops.clean_title(name)[0], year or mediaops.clean_title(name)[1]) if name else ("Uten navn", year)
        else:
            try:
                url = netsafe.check_url(url)
            except netsafe.UnsafeURL as e:
                raise ActionError(f"Avvist: {e}.") from None
            method = mediaops.classify(url) or a.get("method") or ""
            if method not in ("torrent", "archive", "direct", "ytdlp"):
                method = await asyncio.to_thread(_probe, url)
        ident = mediaops.archive_identifier(url)
        source = mediaops.host_of(url) or "magnet"
        if method == "ytdlp":
            row = await _guarded(dl.start_ytdlp, kind, url, title, year, artist, source)
        else:
            if not title:
                title, y = mediaops.clean_title(Path(urlparse(url).path).name or ident or "Uten navn")
                year = year or y
            if method == "archive" or (method == "direct" and ident):
                row = await _guarded(dl.start, kind, title, "", url, year, "Internet Archive", url, artist, ident)
            elif method == "torrent":
                row = await _guarded(dl.start, kind, title, url, "", year, source, "", artist, ident)
            else:
                row = await _guarded(dl.start, kind, title, "", url, year, source, "", artist, "")
    if not a.get("id"):
        dl.set_names(row["id"], {"title": a.get("title") or "", "artist": a.get("artist") or ""})
    run = dl.runner(row["id"])
    if run is not None:
        asyncio.get_running_loop().run_in_executor(None, run, row["id"])
    await ctx.changed("downloads")
    return {"download": dl.get(row["id"])}


def _probe(url: str) -> str:
    """Direkte fil eller en side yt-dlp kan (HEAD gjennom netsafe)."""
    from olympus import mediaops, netsafe

    try:
        with netsafe.open_url(url, method="HEAD", max_bytes=1 << 16, timeout=10, any_host=True) as f:
            ct, name = f.content_type, f.url
    except netsafe.UnsafeURL as e:
        raise ActionError(f"Avvist: {e}.") from None
    except Exception:  # noqa: BLE001 – HEAD ikke støttet: la yt-dlp prøve
        return "ytdlp"
    if ct.startswith(("audio/", "video/")) or ct in ("application/epub+zip", "application/pdf"):
        return "direct"
    if ct == "application/x-bittorrent":
        return "torrent"
    if ct in ("application/octet-stream", "") and mediaops.kind_from_name(name) != "unknown":
        return "direct"
    return "ytdlp"


def _say_download(d: dict[str, Any]) -> str:
    d = d.get("download", d)
    return f"Startet: {d['title']} ({d['method']}) → {d['kind']}. Den dukker opp i biblioteket når den er ferdig."


async def import_local(ctx: Ctx, kind: str, folder: Path, fname: str, title: str = "") -> dict[str, Any]:
    """En fil i en egen mappe under nedlastingsmappa → inn i biblioteket med samme navneregler som nedlastingene."""
    from olympus import mediaops
    from olympus.medialib import epub_meta, read_tags

    ctx.need("dl", "lib")
    from olympus import virus
    from olympus.medialib import AUDIO_EXT, EBOOK_EXT, SUB_EXT, VIDEO_EXT

    verdict = await asyncio.to_thread(virus.scan, [folder], AUDIO_EXT | VIDEO_EXT | SUB_EXT | EBOOK_EXT)
    if not verdict.clean:
        ctx.dl._rm_staging(str(folder))
        raise ActionError(verdict.summary()[:300] + " – fila er slettet.", 422)
    f = folder / fname
    year = None
    creator = ""
    if kind in ("music", "audiobook"):
        tags = await asyncio.to_thread(read_tags, f)
        creator = tags.get("album_artist") or tags.get("artist") or "Ukjent"
        name = title or tags.get("album") or ("Singler" if kind == "music" else mediaops.clean_title(fname)[0])
        year = tags.get("year")
    elif kind == "ebook":
        meta = epub_meta(f) if f.suffix.lower() == ".epub" else None
        name = title or (meta[0] if meta else mediaops.clean_title(fname)[0])
    else:
        name, year = mediaops.clean_title(title or fname)
    try:
        d = await asyncio.to_thread(ctx.dl.add_local, kind, folder, name, year, creator)
    except ValueError as e:
        raise ActionError(f"Kunne ikke sortere inn fila: {e}", 422) from None
    await asyncio.to_thread(ctx.lib.scan)
    raw = ctx.dl._row(d["id"])
    dest = json.loads(raw["meta"] or "{}").get("path", "")
    item = _item_at(ctx.lib, dest, fname)
    await ctx.changed("library")
    await ctx.changed("downloads")
    return {"download": d, "item": item}


def _item_at(lib: Any, dest: str, fname: str) -> dict[str, Any] | None:
    if not dest:
        return None
    like = dest.rstrip("/") + "/%"
    for table, typ, title in (("tracks", "track", "title"), ("videos", "video", "title"), ("books", "book", "title")):
        r = lib.db.execute(f"SELECT id, {title} AS t FROM {table} WHERE path = ? OR path LIKE ? ORDER BY path = ? DESC LIMIT 1",
                           (dest, like, str(Path(dest) / fname))).fetchone()
        if r:
            return {"id": r["id"], "type": typ, "title": r["t"]}
    return None


async def _upload_path(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    """Zeus' utgave av opplasting: en fil som ligger på PC-en (kopieres, originalen blir liggende)."""
    import os
    import secrets

    from olympus import downloads, mediaops

    p = Path(os.path.expanduser(str(a.get("path") or "")))
    if not a.get("path") or not p.is_absolute():
        raise ActionError("Gi hele stien til fila (path).")
    if p.is_symlink() or not p.is_file():
        raise ActionError("Fant ingen vanlig fil der.", 404)
    fname = mediaops.check_upload_name(a["kind"], p.name)
    if p.stat().st_size > mediaops.UPLOAD_MAX:
        raise ActionError("Fila er større enn 8 GB.", 413)
    folder = downloads.STAGING / "opplasting" / secrets.token_hex(6)
    folder.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.copyfile, p, folder / fname)
    return await import_local(ctx, a["kind"], folder, fname, a.get("title") or "")


def _say_upload(d: dict[str, Any]) -> str:
    item = d.get("item")
    return f"Lagt inn i biblioteket: {d['download'].get('path', '')}" + (f" [{item['id']}] {item['title']}" if item else "")



READ_CHUNK = 8000


async def _read(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    """E-boktekst til Zeus: uten chapter innholdsfortegnelsen, med chapter teksten i biter (part) på ~8000 tegn –
    samme kapitler som leseren i appen (medialib.ebook_text)."""
    ctx.need("lib")
    book = ctx.lib.book(a["id"])
    if book is None:
        raise ActionError("Fant ingen bok med den id-en (bruk media_search).", 404)
    chapters = await asyncio.to_thread(ctx.lib.book_text, a["id"])
    if not chapters:
        raise ActionError("Fant ingen tekst i boka (lydbok eller bildebasert PDF?).", 409)
    titles = [t for t, _ in chapters]
    if not a.get("chapter"):
        return {"title": book.get("title"), "chapters": titles}
    n = int(a["chapter"])
    if not 1 <= n <= len(chapters):
        raise ActionError(f"Boka har {len(chapters)} kapitler.", 400)
    text = chapters[n - 1][1]
    parts = max(1, -(-len(text) // READ_CHUNK))
    part = min(max(1, int(a.get("part") or 1)), parts)
    return {"title": book.get("title"), "chapter": n, "chapter_title": titles[n - 1], "part": part, "parts": parts,
            "chapters": len(chapters), "text": text[(part - 1) * READ_CHUNK:part * READ_CHUNK]}


def _say_read(d: Any) -> str:
    if "text" not in d:
        return f"«{d['title']}» – kapitler:\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(d["chapters"], 1))
    more = f" (del {d['part']}/{d['parts']} – be om part={d['part'] + 1} for resten)" if d["parts"] > 1 else ""
    return f"«{d['title']}», kapittel {d['chapter']}/{d['chapters']}: {d['chapter_title']}{more}\n\n{d['text']}"



# --- web (søk og lesing) --------------------------------------------------------------------------
async def _web_search(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    from olympus import websearch

    try:
        return {"query": a["query"], "results": await asyncio.to_thread(websearch.search, a["query"])}
    except Exception as e:  # noqa: BLE001 – nettfeil som svar, ikke krasj
        raise ActionError(f"Søket feilet: {e}", 502) from None


async def _web_read(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    """web read: BARE treff fra siste søk (n) – en url her ignoreres aldri stille, den avvises (egne lenker: web open,
    som krever godkjenning)."""
    from olympus import websearch

    if a.get("url"):
        raise ActionError("web read tar bare n (treff fra siste søk). En egen lenke: web open url=… (må godkjennes).", 400)
    n = int(a.get("n") or 0)
    if not 1 <= n <= len(websearch.LAST):
        raise ActionError("Gi n: nummeret på et treff fra siste web search.", 400)
    return await _fetch_page(websearch.LAST[n - 1]["url"], int(a.get("part") or 1))


async def _web_open(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    """web open: en lenke Zeus/brukeren har (egress – godkjent med hele lenken på kortet)."""
    url = str(a.get("url") or "")
    if not url:
        raise ActionError("Gi url.", 400)
    return await _fetch_page(url, int(a.get("part") or 1))


async def _fetch_page(url: str, part: int) -> dict[str, Any]:
    from olympus import netsafe, websearch

    try:
        return await asyncio.to_thread(websearch.read, url, part)
    except netsafe.UnsafeURL as e:
        raise ActionError(f"Leser ikke den siden: {e}", 400) from None
    except Exception as e:  # noqa: BLE001
        raise ActionError(f"Fikk ikke hentet siden: {e}", 502) from None


def _say_web(d: Any) -> str:
    if "results" in d:
        if not d["results"]:
            return f"Ingen treff på «{d['query']}»."
        return "\n".join(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}" for i, r in enumerate(d["results"], 1)) + \
            "\n(Les et treff med web read n=…; oppgi kildene i svaret.)"
    more = f" – del {d['part']}/{d['parts']}, be om part={d['part'] + 1} for mer" if d["parts"] > 1 else ""
    return f"{d['title']}\nKilde: {d['url']}{more}\n\n{d['text']}"



async def _subs_fetch(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    """Undertekst fra OpenSubtitles, lagret ved filmen («Film.nb.srt»); biblioteket skannes så sporet dukker opp."""
    from olympus import subfetch
    from olympus.providers import load_auth

    ctx.need("lib")
    v = ctx.lib.video(a["id"])
    if v is None:
        raise ActionError("Fant ingen film/episode med den id-en.", 404)
    cfg = getattr(ctx.hub, "c", None)
    if cfg is None:
        raise ActionError("Bare tilgjengelig i Olympus-serveren.", 503)
    try:
        out = await asyncio.to_thread(subfetch.find_and_save, load_auth(cfg), v, str(a.get("lang") or "no"))
    except subfetch.NotConfigured as e:
        raise ActionError(str(e), 409) from None
    except FileNotFoundError as e:
        raise ActionError(str(e), 404) from None
    except Exception as e:  # noqa: BLE001 – nett/kvote som svar
        raise ActionError(f"OpenSubtitles: {e}", 502) from None
    await asyncio.to_thread(ctx.lib.scan)
    return {"saved": out.name, "subtitles": (ctx.lib.video(a["id"]) or {}).get("subtitles", [])}



async def _rip_status(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    from olympus.rip import RIPPER

    return await asyncio.to_thread(RIPPER.status)


async def _rip(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    """Ripp lyd-CD (FLAC, navn fra MusicBrainz) eller DVD (hovedfilmen som MKV) fra PC-ens plateleser."""
    from olympus.rip import RIPPER, drives

    ctx.need("lib")
    devs = drives()
    if not devs:
        raise ActionError("PC-en har ingen plateleser tilkoblet.", 409)

    async def done() -> None:
        await asyncio.to_thread(ctx.lib.scan)
        if ctx.hub is not None:
            await ctx.hub.send({"type": "media_changed", "what": "library"})

    cfg = getattr(ctx.hub, "c", None)
    if cfg is not None:
        from olympus.config import media_dir

        RIPPER.dirs = {"music": media_dir(cfg, "music"), "film": media_dir(cfg, "film")}
    try:
        return RIPPER.start(str(a.get("dev") or devs[0]), str(a.get("title") or ""), done)
    except (RuntimeError, ValueError) as e:
        raise ActionError(str(e), 409) from None


def _say_rip(d: Any) -> str:
    if "drives" in d:
        if not d["drives"]:
            return "PC-en har ingen plateleser tilkoblet."
        lines = [f"{x['dev']}: {({'audio': 'lyd-CD', 'dvd': 'DVD', 'data': 'dataplate', 'empty': 'tom'}).get(x.get('kind'), '?')}"
                 + (f" «{x['label']}»" if x.get("label") else "") for x in d["drives"]]
        j = d.get("job")
        if j:
            lines.append(f"Ripping: {j['title']} – {j['status']} ({round(100 * j['progress'])} %){' – ' + j['error'] if j['error'] else ''}")
        return "\n".join(lines)
    return f"Ripper {d['title'] or d['kind']} … (følg med med media rip_status)"


async def _info(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    from olympus import mediaops

    ctx.need("lib")
    return mediaops.item_info(ctx.lib, a["id"])


def _say_info(d: dict[str, Any]) -> str:
    return (f"{d['type']} [{d['id']}] {d['title']}" + (f" – {d['artist']}" if d["artist"] else "")
            + (f" · album «{d['album']}»" if d["album"] and d["type"] != "album" else "") + (f" ({d['year']})" if d["year"] else "")
            + (f" · {d['genre']}" if d["genre"] else "") + f"\n{d['files']} filer, {d['size'] / 1e6:.0f} MB i {d['folder']}")


def _item_check(ctx: Ctx, a: dict[str, Any]) -> str:
    from olympus import mediaops

    if ctx.lib is None:
        return "Mediebiblioteket er ikke tilgjengelig."
    try:
        mediaops.resolve_item(ctx.lib, str(a.get("id") or ""))
    except MediaError:
        return f"Fant ingenting med id «{a.get('id')}» i biblioteket. Bruk media_search først."
    return ""


def _item_label(ctx: Ctx, a: dict[str, Any]) -> str:
    what, files = ctx.lib.files_for(str(a.get("id") or ""))
    return f"{what} ({len(files)} filer)"


async def _edit(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    from olympus import mediaops

    ctx.need("lib")
    fields = {k: a[k] for k in mediaops.EDIT_FIELDS if k in a}
    out = await asyncio.to_thread(mediaops.edit_item, ctx.lib, a["id"], fields)
    await ctx.changed("library", id=out["id"], old_id=a["id"])
    return out


def _edit_preview(ctx: Ctx, a: dict[str, Any]) -> str:
    from olympus import mediaops

    changes = "\n".join(f"  {k}: {a[k]}" for k in mediaops.EDIT_FIELDS if k in a)
    return f"endre {_item_label(ctx, a)}\n{changes or '  (ingen felt)'}"


async def _delete(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    ctx.need("lib")
    what, files = ctx.lib.files_for(a["id"])
    if not files:
        raise ActionError("Fant ikke elementet i biblioteket.", 404)
    from olympus.mediaops import _ids_by_path

    ids = _ids_by_path(ctx.lib, files)
    gone = await asyncio.to_thread(ctx.lib.trash, files)
    await asyncio.to_thread(ctx.lib.scan)
    if not gone:
        raise ActionError(f"Fikk ikke slettet filene til {what}.", 409)
    # Det som pekte på det slettede (♥/✕, framdrift, plass i spillelister) ryddes – ellers hang det igjen i lab.db
    gone_paths = {str(p) for p in gone}
    db = ctx.lib.db
    for path, (iid, alb) in ids.items():
        if path not in gone_paths:
            continue
        db.execute("DELETE FROM user.playlist_items WHERE track_id = ?", (iid,))
        db.execute("DELETE FROM user.likes WHERE item_id = ?", (iid,))
        db.execute("DELETE FROM user.progress WHERE item_id = ?", (iid,))
        if alb and not db.execute("SELECT 1 FROM tracks WHERE album_id = ?", (alb,)).fetchone():
            db.execute("DELETE FROM user.likes WHERE item_id = ?", (f"album:{alb}",))
    if ctx.lib.book(a["id"]) is None:
        db.execute("DELETE FROM user.progress WHERE item_id = ?", (a["id"],))
    db.commit()
    await ctx.changed("library")
    return {"ok": True, "trashed": len(gone), "what": what}


def _delete_preview(ctx: Ctx, a: dict[str, Any]) -> str:
    what, files = ctx.lib.files_for(str(a.get("id") or ""))
    home = str(Path.home())
    listed = "\n".join(f"  {str(p).replace(home, '~', 1)}" for p in files[:30])
    more = f"\n  … og {len(files) - 30} til" if len(files) > 30 else ""
    return f"slette {what} ({len(files)} filer → papirkurven)\n{listed}{more}"


async def cover_bytes(ctx: Ctx, item_id: str, data: bytes) -> dict[str, Any]:
    from olympus import mediaops

    ctx.need("lib")
    out = await asyncio.to_thread(mediaops.set_cover, ctx.lib, item_id, data)
    await ctx.changed("cover", id=item_id)
    return out


async def cover_from_url(ctx: Ctx, item_id: str, url: str) -> dict[str, Any]:
    from olympus import mediaops, netsafe

    try:
        data, ctype = await asyncio.to_thread(netsafe.fetch_bytes, url, mediaops.IMAGE_MAX, 20)
    except netsafe.UnsafeURL as e:
        raise ActionError(f"Avvist: {e}.") from None
    except Exception as e:  # noqa: BLE001
        raise ActionError(f"Fikk ikke hentet bildet: {type(e).__name__}.", 502) from None
    if ctype and not ctype.startswith("image/"):
        raise ActionError("Lenken er ikke et bilde.", 415)
    return await cover_bytes(ctx, item_id, data)


async def _cover_set(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    if not a.get("url"):
        raise ActionError("Gi en lenke til bildet (url).")
    return await cover_from_url(ctx, a["id"], a["url"])


async def _cover_fetch(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    from olympus import mediaops

    ctx.need("lib")
    mediaops.resolve_item(ctx.lib, a["id"])
    found = await asyncio.to_thread(mediaops.fetch_cover, ctx.lib, a["id"])
    if found:
        await ctx.changed("cover", id=a["id"])
    return {"ok": True, "found": found}


async def _scan(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    ctx.need("lib")
    result = await asyncio.to_thread(ctx.lib.scan)
    await ctx.changed("media")
    return result


async def _mix(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    ctx.need("lib")
    items = await asyncio.to_thread(ctx.lib.songs_for_taste, max(1, min(100, int(a.get("limit") or 40))))
    return {"items": [{k: v for k, v in t.items() if k != "path"} for t in items]}


def _say_tracks(d: dict[str, Any]) -> str:
    return "\n".join(f"- [{t['id']}] {t['title']} – {t['artist']}" for t in d["items"]) or "(ingenting)"


async def _like(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    ctx.need("lib")
    lib, item = ctx.lib, a["id"]
    kind, _, key = item.partition(":") if ":" in item else ("track", "", item)
    known = (lib.track(key) is not None if kind == "track" else bool(lib.album_tracks(key)) if kind == "album"
             else bool(key) if kind == "artist" else False)
    if not known:
        raise ActionError("Ukjent sang, album eller artist.", 404)
    lib.set_like(item, max(-1, min(1, int(a.get("value", 0)))))
    await ctx.changed("taste")
    return {"value": lib.like(item)}


async def _taste(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    ctx.need("lib")
    return await asyncio.to_thread(ctx.lib.taste)


def _say_taste(t: dict[str, Any]) -> str:
    if not t["plays"] and not t["liked"]:
        return (f"Ingen lyttehistorikk ennå (biblioteket: {t['library']['tracks']} sanger). Smaken lærer seg av det "
                "brukeren spiller i appen og på PC-en, og av ♥/✕.")
    part = lambda label, xs: f"{label}: {', '.join(xs)}" if xs else ""  # noqa: E731
    lines = [f"{t['plays']} avspillinger ({t['hours']} t), {t['skipped']} hoppet over.",
             part("Favorittartister", t["artists"]), part("Sjangre", t["genres"]), part("Album", t["albums"]),
             part("♥", t["liked"]), part("✕ (unngå)", t["disliked"]), part("Hoppes ofte over", t["often_skipped"]),
             part("Nylig", t["recent"]), "; ".join(f"{k}: {', '.join(v)}" for k, v in t["by_time"].items())]
    return "\n".join(x for x in lines if x)


# --- nedlastinger -------------------------------------------------------------------------------------------
async def _dl_list(ctx: Ctx, a: dict[str, Any]) -> list[dict[str, Any]]:
    ctx.need("dl")
    return ctx.dl.list(int(a.get("limit") or 30))


def _say_dl_list(rows: list[dict[str, Any]]) -> str:
    return "\n".join(f"- [{d['id']}] {d['title']} · {d['status']} · {d['progress'] * 100:.0f} %"
                     + (f" · {d['error']}" if d["error"] else "") for d in rows) or "(ingen nedlastinger)"


def _dl_check(ctx: Ctx, a: dict[str, Any]) -> str:
    if ctx.dl is None:
        return "Nedlasting er ikke tilgjengelig her (bare i Olympus-serveren)."
    return "" if ctx.dl.get(str(a.get("id") or "")) else f"Fant ingen nedlasting «{a.get('id')}». Bruk downloads list."


def _dl_preview(ctx: Ctx, a: dict[str, Any]) -> str:
    d = ctx.dl.get(str(a.get("id") or "")) or {}
    row = ctx.dl._row(str(a.get("id") or ""))
    url = row["url"] if row is not None and row["url"] else ""
    return f"{d.get('title', a.get('id'))} · {d.get('status', '?')}" + (f"\nLenke: {url}" if url else "")


async def _dl_cancel(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    ctx.need("dl")
    if not await asyncio.to_thread(ctx.dl.cancel, a["id"]):
        raise ActionError("Fant ikke nedlastingen.", 404)
    await ctx.changed("downloads")
    return {"ok": True, "forgot": False}


async def _dl_forget(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    ctx.need("dl")
    if not await asyncio.to_thread(ctx.dl.forget, a["id"]):
        raise ActionError("Fant ikke nedlastingen.", 404)
    await ctx.changed("downloads")
    return {"ok": True, "forgot": True}


async def _guarded(fn: Any, *args: Any) -> Any:
    """Start/prøv igjen en nedlasting: nettverksfeil (Transmission nede, .torrent gir 404) blir en melding, ikke et krasj."""
    import asyncio

    import httpx

    try:
        return await asyncio.to_thread(fn, *args)
    except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
        raise ActionError(f"Fikk ikke startet nedlastingen ({type(e).__name__}: {str(e)[:120]}). Kjører Transmission?", 502) from None


async def _dl_retry(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    ctx.need("dl")
    try:
        row = await _guarded(ctx.dl.retry, a["id"])
    except KeyError:
        raise ActionError("Fant ikke nedlastingen.", 404) from None
    except ValueError as e:
        raise ActionError(f"Kan ikke prøve på nytt: {e}.", 409) from None
    run = ctx.dl.runner(row["id"])
    if run is not None:
        asyncio.get_running_loop().run_in_executor(None, run, row["id"])
    await ctx.changed("downloads")
    return {"download": row}


# --- PC-en ----------------------------------------------------------------------------------------------
_SERVICES_CACHE: dict[str, Any] = {"ts": 0.0, "val": {}}


async def _pc_info(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    """Maskinen: temperaturer, disk, VRAM, modell, tjenester, backup og antall telefoner (PC-fanen og Zeus)."""
    import asyncio
    import json as _json
    import shutil
    import socket
    import time as _time
    from pathlib import Path

    from olympus import gpu, thermal
    from olympus.statusview import SERVICES, _active

    hub = ctx.hub
    if _time.monotonic() - _SERVICES_CACHE["ts"] > 30:
        _SERVICES_CACHE["val"] = await asyncio.to_thread(lambda: {u: _active(u) for u, _ in SERVICES})
        _SERVICES_CACHE["ts"] = _time.monotonic()
    temps = thermal.read().temps
    disk = shutil.disk_usage(Path.home())
    vram = gpu.read_vram()
    info = {"model": None, "cloud": False, "provider": None}
    loaded: list[str] = []
    if hub is not None:
        hub.sync_model()
        info = hub.model_info()
        loaded = hub.manager.loaded()
    return {
        "host": socket.gethostname(),
        "gpu": {"name": gpu.short_gpu_name(), "temp": temps.get("gpu_junction"),
                "vram_used_gb": round(vram.used_gb, 1) if vram else None,
                "vram_total_gb": round(vram.total_gb, 1) if vram else None},
        "cpu": {"temp": temps.get("cpu")},
        "disk_pct": round(100 * disk.used / disk.total),
        "ai": ctx.lab.ai_on if ctx.lab is not None else True,
        "model": {"id": info["model"], "cloud": info["cloud"], "provider": info["provider"], "loaded": loaded},
        "services": _SERVICES_CACHE["val"],
        "backup": _json.loads(ctx.lab.setting("backup_last", "null")) if ctx.lab is not None else None,
        "devices": len(ctx.lab.devices()) if ctx.lab is not None else 0}


def _say_pc_info(i: dict[str, Any]) -> str:
    g, c = i.get("gpu") or {}, i.get("cpu") or {}
    deg = lambda v: f"{v:.0f}°" if isinstance(v, (int, float)) else "?"
    svc = ", ".join(f"{k} {'kjører' if v else 'NEDE'}" for k, v in (i.get("services") or {}).items())
    m = i.get("model") or {}
    b = i.get("backup") or {}
    return (f"{i.get('host')}: GPU {g.get('name') or ''} {deg(g.get('temp'))}"
            + (f", VRAM {g.get('vram_used_gb')}/{g.get('vram_total_gb')} GB" if g.get("vram_total_gb") else "")
            + f" · CPU {deg(c.get('temp'))} · disk {i.get('disk_pct')} % brukt\n"
            f"Zeus {'på' if i.get('ai') else 'av'} · modell {m.get('id')} ({'sky' if m.get('cloud') else 'lokal'}"
            f"{', lastet' if m.get('loaded') else ', ikke lastet'})\n"
            f"Tjenester: {svc or '?'}\nBackup: {('ok ' + str(b.get('date', ''))) if b.get('ok') else 'feilet eller mangler'}"
            f" · {i.get('devices')} telefoner paret")


async def _weather(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    """Været nå (MET/Yr) for stedet som er satt."""
    ctx.need("hub")
    lab = ctx.lab or ctx.hub.lab
    return {"place": lab.setting("place") or "", "weather": await ctx.hub.weather()}


PLACE_KINDS = ("By", "Tettsted", "Kommune", "Bydel", "Tettsteddel", "Grend", "Bygdelag (bygd)", "Adressenavn (veg/gate)")


def _geocode(name: str) -> tuple[str, float, float]:
    """Stedsnavn → (navn, lat, lon) fra Kartverket (bare navnet sendes). Byer og tettsteder foran gårder og bekker."""
    import httpx

    for fuzzy in ("false", "true"):
        r = httpx.get("https://ws.geonorge.no/stedsnavn/v1/sted", timeout=10,
                      params={"sok": name, "treffPerSide": 20, "utkoordsys": 4258, "fuzzy": fuzzy})
        r.raise_for_status()
        hits = r.json().get("navn") or []
        if hits:
            def rank(n: dict[str, Any]) -> int:
                t = n.get("navneobjekttype") or ""
                return PLACE_KINDS.index(t) if t in PLACE_KINDS else len(PLACE_KINDS)
            n = min(hits, key=rank)
            pt = n["representasjonspunkt"]
            label = next((x["skrivemåte"] for x in n.get("stedsnavn") or [] if x.get("skrivemåte")), name)
            kommune = next((k["kommunenavn"] for k in n.get("kommuner") or []), "")
            if kommune and kommune != label:
                label = f"{label}, {kommune}"
            return label, round(float(pt["nord"]), 4), round(float(pt["øst"]), 4)
    raise ActionError(f"Fant ikke stedet «{name}».", 404)


async def _set_place(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    """Sett stedet været gjelder for: et stedsnavn (slås opp hos Kartverket) eller lat/lon."""
    ctx.need("hub")
    lab = ctx.lab or ctx.hub.lab
    name = str(a.get("place") or "").strip()[:100]
    if a.get("lat") is not None and a.get("lon") is not None:
        lat, lon = float(a["lat"]), float(a["lon"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ActionError("Ugyldige koordinater.")
        label = name or f"{lat:.3f}, {lon:.3f}"
    elif name:
        import httpx

        try:
            label, lat, lon = await asyncio.to_thread(_geocode, name)
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            raise ActionError("Fikk ikke slått opp stedet nå – prøv igjen.", 502) from None
    else:
        raise ActionError("Gi et stedsnavn (place) eller lat og lon.")
    for k, v in (("place", label), ("lat", str(lat)), ("lon", str(lon))):
        lab.set_setting(k, v)
    ctx.hub._weather = (0.0, None)
    return await _weather(ctx, {})


def _say_weather(d: dict[str, Any]) -> str:
    w = d.get("weather")
    if not w:
        return f"Sted: {d.get('place') or 'ikke satt'} – ingen værdata nå."
    rain = f", {w['rain']} mm de neste 6 t" if w.get("rain") is not None else ""
    return f"{w.get('place') or d.get('place')}: {w.get('temp')} °C, {w.get('symbol', '').replace('_', ' ')}{rain} (MET/Yr)."


async def _pc_status(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    from olympus.mediaserver import PLAYER

    return PLAYER.status()


def _say_player(_st: Any) -> str:
    from olympus.mediaserver import PLAYER

    return PLAYER.describe()


async def _pc_play(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    from olympus.mediaserver import PLAYER

    ctx.need("lib")
    lib = ctx.lib
    if a.get("tracks"):
        songs = [t for t in (lib.track(i) for i in a["tracks"]) if t]
    elif a.get("playlist"):
        ref = a["playlist"]
        pid = next((p["id"] for p in lib.playlists() if ref in (p["id"], p["name"]) or p["name"].lower() == ref.lower()), "")
        songs = lib.playlist_tracks(pid) if pid else []
    elif a.get("album"):
        songs = lib.album_tracks(a["album"])
    elif a.get("query"):
        songs = await asyncio.to_thread(lib.songs_for, a["query"])
    else:
        raise ActionError("Si hva som skal spilles (query, album, playlist eller tracks).")
    if not songs:
        raise ActionError("Fant ingen musikk.", 404)
    index = int(a.get("index") or 0)
    if not 0 <= index < len(songs):
        raise ActionError(f"index er utenfor lista ({len(songs)} spor).")
    await PLAYER.play(lib, songs, index, float(a.get("position") or 0))
    await asyncio.sleep(0.3)
    return PLAYER.status()


async def _pc_control(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    from olympus.mediaserver import PLAYER

    fn = {"next": PLAYER.next, "prev": PLAYER.prev, "stop": PLAYER.stop, "pause": PLAYER.pause,
          "resume": PLAYER.resume}[a["action"]]
    await fn()
    if a["action"] in ("next", "prev"):
        await asyncio.sleep(0.3)
    return PLAYER.status()



# --- TV (casting) ---------------------------------------------------------------------------------
def _tv_video(lib: Any, ref: str) -> dict[str, Any]:
    """Film/episode fra id eller navn («Dune», «Severance S01E02»)."""
    v = lib.video(ref)
    if v:
        return v
    allv = lib.videos()
    items = allv["films"] + [e for s in allv["shows"] for e in s["episodes"]]
    low = ref.lower().strip()

    def names(x: dict[str, Any]) -> list[str]:
        return [str(x.get("title") or "").lower(), f"{x.get('show') or ''} {x.get('label') or ''}".strip().lower()]

    exact = [x for x in items if low in names(x)]
    hits = exact or [x for x in items if any(low in n for n in names(x))]
    if not hits:
        raise ActionError(f"Fant ingen film eller episode som passer «{ref}».", 404)
    if len(hits) > 1 and not exact:
        raise ActionError("Flere passer: " + ", ".join(x.get("title") or "" for x in hits[:6]) + ". Vær mer presis.", 409)
    return lib.video(hits[0]["id"])


def _say_tv(st: Any) -> str:
    if isinstance(st, dict) and "devices" in st:
        devs = st["devices"]
        lines = [f"- {d['name']} ({d['type']})" for d in devs] or ["(fant ingen TV-er på hjemmenettet)"]
        now = st.get("now") or {}
        return "\n".join(lines) + (f"\nSpilles nå på {now['device_name']}: {now['title']}" if now.get("playing") else "")
    if isinstance(st, dict) and st.get("playing"):
        how = "omkodes på PC-en" if st.get("mode") == "transcode" else "direkte"
        return f"{'Pause' if st.get('paused') else 'Spiller'} «{st['title']}» på {st['device_name']} ({how})."
    return "Ingenting spilles på TV-en."


async def _tv_list(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    from olympus.cast import CASTER

    devs = await CASTER.discover(force=bool(a.get("refresh")))
    return {"devices": [d.public() for d in devs], "now": CASTER.status()}


async def _tv_play(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    from olympus.cast import CASTER, CastError

    ctx.need("lib")
    v = _tv_video(ctx.lib, str(a["video"]))
    start = float(a["position"]) if a.get("position") is not None else float(v.get("position") or 0)
    try:
        return await CASTER.play(str(a.get("device") or ""), v, start, str(a.get("subtitle") or "auto"))
    except CastError as e:
        raise ActionError(str(e), 409) from None


async def _tv_control(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    from olympus.cast import CASTER, CastError

    try:
        return await CASTER.control(a["action"], a.get("position"))
    except CastError as e:
        raise ActionError(str(e), 409) from None


async def _pc_handoff(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    from olympus.mediaserver import PLAYER

    if PLAYER.current is None:
        raise ActionError("Ingenting spilles på PC-en.", 409)
    st = PLAYER.status()
    tracks = list(PLAYER.tracks[PLAYER.index:])  # hele køen herfra (statusen er avkortet til 200)
    if not tracks:
        raise ActionError("Fant ingen kø å gi videre.", 409)
    await PLAYER.stop()
    out = {"tracks": tracks, "index": 0, "position": st["position"]}
    if ctx.zeus:  # Zeus flyttet musikken: telefonen(e) får køen over websocketen og kan spille videre
        await ctx.send({"type": "handoff", **out})
    return out


async def _pc_action(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    from olympus import pcctl

    action = a["action"]
    if action == "sleep":
        if not ctx.zeus and a.get("confirm") is not True:
            raise ActionError("hvilemodus må bekreftes (confirm: true)")
        # Svar først, sov etterpå – ellers når svaret aldri telefonen
        asyncio.get_running_loop().call_later(1.0, lambda: asyncio.ensure_future(asyncio.to_thread(pcctl.run_action, "sleep")))
        return {"ok": True, "action": action}
    ok, why = await asyncio.to_thread(pcctl.run_action, action)
    if not ok:
        raise ActionError(why or "kommandoen feilet", 502)
    return {"ok": True, "action": action}


PC_LABEL = {"lock": "låse skjermen", "screen_off": "skru av skjermen", "screen_on": "skru på skjermen",
            "sleep": "sette PC-en i hvilemodus", "open_olympus": "åpne Olympus på PC-en"}


# --- modell ---------------------------------------------------------------------------------------------
async def _models(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    ctx.need("hub")
    hub = ctx.hub
    info = hub.model_info()
    return {"current": info["model"], "cloud": info["cloud"], "warning": info["warning"], "choices": hub.catalog.choices()}


def _say_models(d: dict[str, Any]) -> str:
    return (f"Nå: {d['current']}{' (sky)' if d['cloud'] else ''}\n"
            + "\n".join(f"- {c['id']}{' ☁' if c.get('cloud') else ''} – {c.get('label', '')}" for c in d["choices"][:40]))


async def _model_switch(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    from olympus.model_choices import resolve_choice

    ctx.need("hub")
    hub = ctx.hub
    ref = resolve_choice(hub.catalog, a["model"])
    if ref is None:
        raise ActionError("Ukjent modell – velg en fra model list.")
    if ctx.zeus:  # Zeus svarer nå: byttet skjer når svaret er ferdig (neste melding bruker den nye modellen)
        hub.pending_model = ref
        return {"current": hub.model_info()["model"], "next": ref, "deferred": True}
    info = await hub.set_model(ref)
    if info is None:
        raise ActionError("Zeus svarer nå – bytt når svaret er ferdig", 409)
    return {"current": info["model"], "cloud": info["cloud"], "warning": info["warning"]}


# --- minne og samtaler ------------------------------------------------------------------------------------
async def _memory_list(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    ctx.need("memory")
    m = ctx.memory
    return {"user": m.items("user"), "memory": m.items("memory"), "limits": m.limits_info()}


async def _memory_apply(ctx: Ctx, action: str, target: str, text: str = "", old_id: str = "") -> dict[str, Any]:
    ctx.need("memory")
    r = ctx.memory.apply(action, target, text, old_id=old_id)
    if not r.ok:
        raise ActionError(r.message, 404 if "Fant ingen" in r.message else 400)
    await ctx.send({"type": "memory_changed"})
    return {**await _memory_list(ctx, {}), "message": r.message}


async def _memory_add(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    return await _memory_apply(ctx, "add", a["target"], a["text"])


def _memory_target(ctx: Ctx, entry_id: str) -> str:
    ctx.need("memory")
    hit = ctx.memory.find(entry_id)
    if hit is None:
        raise ActionError("Fant ingen oppføring med den id-en.", 404)
    return hit[0]


async def _memory_edit(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    return await _memory_apply(ctx, "replace", _memory_target(ctx, a["id"]), a["text"], a["id"])


async def _memory_delete(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    return await _memory_apply(ctx, "remove", _memory_target(ctx, a["id"]), "", a["id"])


SID = S("økt-id", maxLength=64, pattern=r"^[A-Za-z0-9_-]{1,64}$")


def _sid(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value or ""):
        raise ActionError("Ugyldig økt-id.")
    return value


async def _sessions(ctx: Ctx, a: dict[str, Any]) -> list[dict[str, Any]]:
    ctx.need("hub")
    return [{"id": s.id, "title": s.title, "updated": s.updated, "messages": s.messages}
            for s in ctx.hub.sessions.list(int(a.get("limit") or 30))]


async def _session_open(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    ctx.need("hub")
    res = await ctx.hub.open_session(_sid(a["id"]))
    if res == "busy":
        raise ActionError("Zeus svarer nå – åpne samtalen når svaret er ferdig.", 409)
    if res == "missing":
        raise ActionError("Fant ikke samtalen.", 404)
    return {"ok": True, "session": a["id"]}


async def _session_new(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    ctx.need("hub")
    if not await ctx.hub.new_session():
        raise ActionError("Zeus svarer nå – start en ny samtale når svaret er ferdig.", 409)
    return {"ok": True}


async def _session_delete(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    ctx.need("hub")
    res = await ctx.hub.delete_session(_sid(a["id"]))
    if res == "busy":
        raise ActionError("Zeus svarer i denne samtalen nå.", 409)
    if res == "missing":
        raise ActionError("Fant ikke samtalen.", 404)
    return {"ok": True}


# --- registeret ---------------------------------------------------------------------------------------------
V1 = "/api/v1"
M = f"{V1}/media"
ACTIONS: list[Action] = [
    # media
    Action("media.search", "Søk i biblioteket", "Søk etter sanger, album, filmer, serier og bøker i biblioteket.",
           "GET", f"{M}/search", {"q": S(maxLength=200)}, tool="media_search", tool_actions=("search",),
           cloud_sensitive=True),
    Action("media.inspect", "Undersøk lenke", "Se hva en lenke er før nedlasting (tittel, type, størrelse, kilde). "
           "Ingenting lastes ned.", "POST", f"{M}/inspect", {"url": S("http(s)-, magnet- eller .torrent-lenke")},
           ("url",), run=_inspect, tool="media", tool_actions=("inspect",), say=_say_inspect,
           preview=lambda c, a: f"undersøke lenken (serveren kobler seg til den)\nLenke: {a.get('url', '')}"),
    Action("media.add", "Legg til fra lenke", "Last ned fra en lenke (eller et søketreff fra media_find med id) inn i "
           "biblioteket: yt-dlp for YouTube/SoundCloud/Bandcamp, Transmission for torrents, direkte for filer. "
           "Sorteres i riktig mappe med cover.", "POST", f"{M}/add",
           {"url": S(), "kind": KIND, "title": S(maxLength=200), "artist": S(maxLength=200),
            "year": I(minimum=0, maximum=3000), "id": S("treff-id fra media_find/find", maxLength=40),
            "method": {"type": "string", "enum": ["ytdlp", "torrent", "direct"], "description": "fra inspect (valgfritt)"}},
           run=_add, writes=True, tool="media", tool_actions=("add",), status=201, say=_say_download,
           preview=_add_preview, check=_add_check, ask=False),
    Action("media.upload", "Last opp fil", "Legg en fil inn i biblioteket. Appen sender den som multipart (felt «file», "
           "«kind», valgfritt «title»); Zeus gir stien til en fil på PC-en (path).", "POST", f"{M}/upload",
           {"kind": KIND, "title": S(maxLength=200), "path": S("Zeus: full sti til fila på PC-en", maxLength=1000)},
           ("kind",), run=_upload_path, writes=True, tool="media", tool_actions=("upload",), status=201,
           say=_say_upload, custom_http=True),
    Action("web.search", "Søk på nettet", "Søk på nettet (DuckDuckGo): titler, lenker og utdrag. Til ferske fakta, nyheter, "
           "priser, åpningstider – oppgi alltid kildene.", "GET", f"{V1}/web/search", {"query": S(maxLength=300)},
           ("query",), run=_web_search, tool="web", tool_actions=("search",), say=_say_web),
    Action("web.read", "Les et søketreff", "Les treff nummer n fra siste web search som ren tekst (fritt). Lange sider "
           "kommer i deler (part).", "GET", f"{V1}/web/read",
           {"n": I("treffnummer fra siste søk", minimum=1, maximum=20), "part": I("del av siden, 1 = første", minimum=1)},
           ("n",), run=_web_read, tool="web", tool_actions=("read",), say=_say_web),
    Action("web.open", "Åpne en lenke", "Les en nettside du har lenken til (ikke fra søket) – må godkjennes, med hele "
           "lenken på kortet.", "GET", f"{V1}/web/open",
           {"url": S(maxLength=2000), "part": I("del av siden, 1 = første", minimum=1)}, ("url",),
           run=_web_open, egress=True, tool="web", tool_actions=("open",), say=_say_web),
    Action("media.read", "Les bok", "Les en e-bok fra biblioteket: uten chapter → kapittellista; med chapter (1, 2 …) → "
           "teksten, i deler (part) på ~8000 tegn. Bruk dette før du oppsummerer eller svarer om en bok – aldri gjett.",
           "GET", f"{M}/book/{{id}}/read", {"id": ID, "chapter": I("kapittelnummer, 1 = første", minimum=1),
                                           "part": I("del av kapitlet, 1 = første", minimum=1)}, ("id",),
           run=_read, tool="media", tool_actions=("read",), say=_say_read, cloud_sensitive=True),
    Action("media.subtitles", "Hent undertekst", "Hent undertekst fra OpenSubtitles til en film/episode (lang: no, en, "
           "sv, da; standard no) – lagres ved filmen.", "POST", f"{M}/video/{{id}}/subtitles/fetch",
           {"id": ID, "lang": {"type": "string", "enum": ["no", "en", "sv", "da"]}}, ("id",), run=_subs_fetch,
           writes=True, tool="media", tool_actions=("subtitles",), say=lambda d: f"Lagret {d['saved']}."),
    Action("media.rip_status", "Plateleser", "Hva som ligger i PC-ens plateleser (lyd-CD/DVD) og hvordan rippingen går.",
           "GET", f"{M}/rip", run=_rip_status, tool="media", tool_actions=("rip_status",), say=_say_rip),
    Action("media.rip", "Ripp plate", "Ripp platen i PC-ens plateleser inn i biblioteket: lyd-CD → FLAC med navn fra "
           "MusicBrainz, DVD → hovedfilmen som MKV. title: navnet på filmen (DVD).", "POST", f"{M}/rip",
           {"dev": S(maxLength=20, pattern=r"^/dev/sr[0-9]+$"), "title": S(maxLength=120)}, run=_rip, writes=True,
           tool="media", tool_actions=("rip",), say=_say_rip, status=202),
    Action("media.info", "Detaljer", "Hva et element i biblioteket er: tittel, artist, album, år, sjanger, filer, "
           "størrelse og mappe.", "GET", f"{M}/item/{{id}}", {"id": ID}, ("id",), run=_info, tool="media",
           tool_actions=("info",), say=_say_info, cloud_sensitive=True),
    Action("media.edit", "Rediger", "Rett tittel, artist, album, albumartist, år eller sjanger. Album-id → gjelder alle "
           "sporene. Skrives i filene (tagger) der formatet tåler det. Id-en kan endre seg – svaret har den nye.",
           "PATCH", f"{M}/item/{{id}}", {"id": ID, "title": S(maxLength=200), "artist": S(maxLength=200),
                                         "album": S(maxLength=200), "album_artist": S(maxLength=200),
                                         "year": I(minimum=0, maximum=3000), "genre": S(maxLength=200)},
           ("id",), run=_edit, writes=True, tool="media", tool_actions=("edit",), say=_say_info,
           preview=_edit_preview, check=_item_check),
    Action("media.delete", "Slett", "Flytt et album, en sang, en film/episode eller en bok til papirkurven (kan hentes "
           "tilbake) og skann på nytt. Bruk dette – aldri shell/rm – for musikk, film og bøker.", "DELETE",
           f"{M}/item/{{id}}", {"id": ID}, ("id",), run=_delete, writes=True, danger=True, tool="media",
           tool_actions=("delete",), say=lambda d: f"Slettet {d['what']}: {d['trashed']} fil(er) i papirkurven.",
           preview=_delete_preview, check=_item_check),
    Action("media.cover.set", "Bytt cover", "Sett nytt cover/plakat: appen sender bildet (JPEG/PNG/WebP, maks 10 MB) "
           "eller {url}; Zeus gir url.", "PUT", f"{M}/item/{{id}}/cover", {"id": ID, "url": S("lenke til bildet")},
           ("id",), run=_cover_set, writes=True, tool="media", tool_actions=("cover_set",),
           say=lambda d: f"Nytt cover lagret i {d['path']}.", preview=lambda c, a: f"{_item_label(c, a)}\n  ← {a.get('url', '')}",
           check=_item_check, custom_http=True, egress=True),
    Action("media.cover.fetch", "Hent cover på nytt", "Let etter cover/plakat på nettet på nytt (MusicBrainz, Wikipedia).",
           "POST", f"{M}/item/{{id}}/cover/fetch", {"id": ID}, ("id",), run=_cover_fetch, writes=True, tool="media",
           tool_actions=("cover_fetch",), say=lambda d: "Fant et nytt cover." if d["found"] else "Fant ikke noe cover.",
           preview=lambda c, a: _item_label(c, a), check=_item_check),
    Action("media.scan", "Skann biblioteket", "Les inn nye og endrede filer (skjer også av seg selv hvert 5. minutt).",
           "POST", f"{M}/scan", run=_scan, tool="media", tool_actions=("scan",),
           say=lambda r: ", ".join(f"{k}: {v}" for k, v in r.items())),
    Action("media.mix", "Miks etter smaken", "Sanger valgt etter lyttehistorikken og ♥/✕.", "GET", f"{M}/mix",
           {"limit": I("antall sanger", minimum=1, maximum=100)}, run=_mix, tool="media", tool_actions=("mix",), say=_say_tracks,
           cloud_sensitive=True),
    Action("media.like", "♥ / ✕", "Merk en sang (id), et album («album:<id>») eller en artist («artist:<navn>»): "
           "value 1 = liker, -1 = liker ikke, 0 = nøytral.", "PUT", f"{M}/like/{{id}}",
           {"id": S(maxLength=200), "value": I(minimum=-1, maximum=1)}, ("id", "value"), run=_like, writes=True,
           tool="media", tool_actions=("like",), say=lambda d: f"Merket ({d['value']}).",
           preview=lambda c, a: f"{a.get('id')} → {'♥' if a.get('value') == 1 else '✕' if a.get('value') == -1 else 'nøytral'}"),
    Action("media.taste", "Musikksmaken", "Profilen fra det brukeren faktisk hører på (fullført/hoppet over) og ♥/✕: "
           "favorittartister, sjangre, album, morgen/kveld, nylig, ofte hoppet over. Bruk før du anbefaler musikk.",
           "GET", f"{M}/taste", run=_taste, tool="media", tool_actions=("taste",), say=_say_taste, cloud_sensitive=True),
    # nedlastinger
    Action("downloads.list", "Nedlastinger", "Nedlastingene med framdrift og status.", "GET", f"{V1}/downloads",
           {"limit": I(minimum=1, maximum=200)}, run=_dl_list, tool="downloads", tool_actions=("list",), say=_say_dl_list,
           cloud_sensitive=True),
    Action("downloads.cancel", "Avbryt", "Stopp en nedlasting (og slett det som er halvferdig).", "DELETE",
           f"{V1}/downloads/{{id}}", {"id": S(maxLength=40)}, ("id",), run=_dl_cancel, writes=True, tool="downloads",
           tool_actions=("cancel",), say=lambda d: "Avbrutt.", preview=_dl_preview, check=_dl_check, custom_http=True),
    Action("downloads.retry", "Prøv igjen", "Start en feilet eller avbrutt nedlasting på nytt.", "POST",
           f"{V1}/downloads/{{id}}/retry", {"id": S(maxLength=40)}, ("id",), run=_dl_retry, writes=True,
           tool="downloads", tool_actions=("retry",), say=_say_download, preview=_dl_preview, check=_dl_check,
           egress=True),
    Action("downloads.forget", "Fjern fra lista", "Fjern en nedlasting fra lista (avbryter den hvis den pågår). "
           "HTTP: DELETE /api/v1/downloads/{id}?forget=1.", "DELETE", f"{V1}/downloads/{{id}}",
           {"id": S(maxLength=40)}, ("id",), run=_dl_forget, writes=True, tool="downloads", tool_actions=("forget",),
           say=lambda d: "Fjernet fra lista.", preview=_dl_preview, check=_dl_check, custom_http=True),
    # PC-en
    Action("pc.info", "Maskinstatus", "Hvordan PC-en har det: temperaturer, disk, VRAM, modell, tjenester, backup. "
           "Bruk dette (ikke shell) når brukeren spør hvordan PC-en/serveren har det.", "GET", f"{V1}/pc/info",
           run=_pc_info, tool="pc", tool_actions=("info",), say=_say_pc_info, custom_http=True, cloud_sensitive=True),
    Action("pc.weather", "Været", "Været nå for stedet som er satt (MET/Yr): temperatur, symbol, nedbør.", "GET",
           f"{V1}/weather", run=_weather, tool="pc", tool_actions=("weather",), say=_say_weather,
           cloud_sensitive=True),  # stedet = hvor du bor
    Action("pc.place", "Sett sted", "Sett stedet været (og morgenbriefen) gjelder for: place = stedsnavn (slås opp hos "
           "Kartverket) eller lat + lon.", "PUT", f"{V1}/weather/place",
           {"place": S("stedsnavn, f.eks. «Bergen»", maxLength=100), "lat": {"type": "number", "minimum": -90, "maximum": 90},
            "lon": {"type": "number", "minimum": -180, "maximum": 180}},
           run=_set_place, writes=True, tool="pc", tool_actions=("place",), say=_say_weather, cloud_sensitive=True,
           preview=lambda c, a: f"sett sted for været: {a.get('place') or (str(a.get('lat')) + ', ' + str(a.get('lon')))}"
                                "\n(stedsnavnet slås opp hos Kartverket)"),
    Action("pc.status", "Spilles nå", "Hva PC-en spiller (sang, posisjon, kø).", "GET", f"{V1}/pc", run=_pc_status,
           tool="pc", tool_actions=("status",), say=_say_player, custom_http=True, cloud_sensitive=True),
    Action("pc.play", "Spill på PC-en", "Spill musikk på PC-ens lydutgang: query (sang, album, artist, spilleliste, "
           "«noe jeg liker»), album, playlist eller tracks; valgfritt index og position.", "POST", f"{V1}/pc/play",
           {"query": S(maxLength=200), "album": S(maxLength=100), "playlist": S(maxLength=100),
            "tracks": {"type": "array", "items": {"type": "string"}}, "index": I(minimum=0),
            "position": {"type": "number"}}, run=_pc_play, writes=True, ask=False, tool="pc", tool_actions=("play",),
           say=_say_player, custom_http=True),
    Action("pc.control", "Styr avspillingen", "Neste, forrige, pause, fortsett eller stopp.", "POST", f"{V1}/pc/control",
           {"action": {"type": "string", "enum": ["next", "prev", "pause", "resume", "stop"]}}, ("action",),
           run=_pc_control, writes=True, ask=False, tool="pc", tool_actions=("next", "prev", "pause", "resume", "stop"),
           say=_say_player, custom_http=True),
    Action("tv.list", "TV-ene", "TV-er på hjemmenettet (Chromecast/Google TV, smart-TV med DLNA, AirPlay) og hva som "
           "spilles på dem.", "GET", f"{V1}/tv", {"refresh": {"type": "boolean"}}, run=_tv_list, tool="tv",
           tool_actions=("list",), say=_say_tv, cloud_sensitive=True),
    Action("tv.play", "Spill på TV-en", "Spill en film/episode fra biblioteket på en TV: video (id eller navn), device "
           "(TV-navn; kan sløyfes når det bare er én), position (sekunder; ellers der du slapp).", "POST", f"{V1}/tv/play",
           {"video": S(maxLength=200), "device": S(maxLength=100), "position": {"type": "number", "minimum": 0},
            "subtitle": S("undertekst: auto (norsk, ellers engelsk), av, eller spornummer", maxLength=10)},
           ("video",), run=_tv_play, writes=True, ask=False, tool="tv", tool_actions=("play",), say=_say_tv),
    Action("tv.control", "Styr TV-en", "Pause, fortsett, stopp eller spol (seek med position i sekunder).", "POST",
           f"{V1}/tv/control", {"action": {"type": "string", "enum": ["pause", "resume", "stop", "seek"]},
                                "position": {"type": "number", "minimum": 0}}, ("action",),
           run=_tv_control, writes=True, ask=False, tool="tv", tool_actions=("pause", "resume", "stop", "seek"), say=_say_tv),
    Action("pc.handoff", "Ta med til telefonen", "PC-en stopper, og telefonen får resten av køen og posisjonen.",
           "POST", f"{V1}/pc/handoff", run=_pc_handoff, writes=True, ask=False, tool="pc", tool_actions=("handoff",),
           say=lambda d: f"Flyttet {len(d['tracks'])} spor til telefonen.", custom_http=True),
    Action("pc.action", "Fjernkontroll", "Lås skjermen, skjerm av/på, hvilemodus eller åpne Olympus på PC-en. "
           "Hvilemodus er farlig (rødt; over HTTP kreves confirm: true).", "POST", f"{V1}/pc/action",
           {"action": {"type": "string", "enum": ["lock", "screen_off", "screen_on", "sleep", "open_olympus"]},
            "confirm": {"type": "boolean"}}, ("action",), run=_pc_action, writes=True, danger_if={"action": ("sleep",)},
           tool="pc", tool_actions=("lock", "screen_off", "screen_on", "sleep", "open_olympus"),
           say=lambda d: "Gjort.", preview=lambda c, a: PC_LABEL.get(str(a.get("action")), str(a.get("action")))),
    # modell
    Action("model.list", "Modeller", "Modellene Zeus kan svare med (lokale og i skyen).", "GET", f"{V1}/models",
           run=_models, tool="model", tool_actions=("list",), say=_say_models, custom_http=True),
    Action("model.switch", "Bytt modell", "Bytt modellen Zeus svarer med. Byttet skjer når svaret er ferdig. "
           "Skymodeller sender samtalen ut av PC-en.", "POST", f"{V1}/models", {"model": S(maxLength=200)}, ("model",),
           run=_model_switch, writes=True, tool="model", tool_actions=("switch",),
           say=lambda d: f"Bytter til {d.get('next') or d.get('current')} når dette svaret er ferdig.",
           preview=lambda c, a: f"bytte til {a.get('model')}", custom_http=True),
    # plan, spillelister og lyd: ruter og Zeus-verktøy fantes fra før (med egne godkjenningskort)
    Action("plan.list", "Planen", "Avtaler i en periode.", "GET", f"{V1}/plan", tool="plan", tool_actions=("list",),
           cloud_sensitive=True),
    Action("plan.add", "Ny avtale", "Legg til en avtale.", "POST", f"{V1}/plan", writes=True, tool="plan", tool_actions=("add",)),
    Action("plan.edit", "Endre avtale", "Flytt eller endre en avtale.", "PATCH", f"{V1}/plan/{{id}}", writes=True,
           tool="plan", tool_actions=("move", "edit")),
    Action("plan.delete", "Slett avtale", "Slett en avtale (kan angres).", "DELETE", f"{V1}/plan/{{id}}", writes=True,
           danger=True, tool="plan", tool_actions=("delete",)),
    Action("playlist.list", "Spillelister", "Alle spillelistene.", "GET", f"{M}/playlists", tool="playlist", tool_actions=("list",)),
    Action("playlist.show", "Vis spilleliste", "Sporene i en spilleliste.", "GET", f"{M}/playlist/{{id}}", tool="playlist",
           tool_actions=("show",)),
    Action("playlist.create", "Ny spilleliste", "Lag en spilleliste.", "POST", f"{M}/playlists", writes=True,
           tool="playlist", tool_actions=("create",)),
    Action("playlist.add", "Endre spilleliste", "Legg til spor, sett rekkefølge eller gi nytt navn.", "PATCH",
           f"{M}/playlist/{{id}}", writes=True, tool="playlist", tool_actions=("add",)),
    Action("playlist.delete", "Slett spilleliste", "Slett en spilleliste.", "DELETE", f"{M}/playlist/{{id}}", writes=True,
           danger=True, tool="playlist", tool_actions=("delete",)),
    Action("audio.status", "Lyd", "Utganger, volum og Bluetooth.", "GET", f"{V1}/audio", tool="audio", tool_actions=("status",)),
    Action("audio.volume", "Volum", "Sett volumet (0–100).", "POST", f"{V1}/audio/volume", writes=True, tool="audio",
           tool_actions=("volume",)),
    Action("audio.mute", "Demp", "Demp eller slå på lyden.", "POST", f"{V1}/audio/mute", writes=True, tool="audio",
           tool_actions=("mute", "unmute")),
    Action("audio.output", "Lydutgang", "Velg lydutgang.", "POST", f"{V1}/audio/output", writes=True, tool="audio",
           tool_actions=("output",)),
    Action("audio.bluetooth", "Bluetooth", "Koble Bluetooth-høyttaleren til eller fra.", "POST", f"{V1}/audio/bluetooth",
           writes=True, tool="audio", tool_actions=("connect", "disconnect")),
    # Zeus' minne (samme MemoryStore.apply som memory-verktøyet)
    Action("memory.list", "Minnet", "Det Zeus husker om deg (user) og om miljøet (memory), med id per oppføring.",
           "GET", f"{V1}/memory", run=_memory_list, tool="memory", tool_actions=("list",), cloud_sensitive=True),
    Action("memory.add", "Husk", "Legg til en oppføring i minnet.", "POST", f"{V1}/memory",
           {"target": {"type": "string", "enum": ["user", "memory"]}, "text": S(maxLength=2200)}, ("target", "text"),
           run=_memory_add, writes=True, ask=False, tool="memory", tool_actions=("add",), status=201),
    Action("memory.edit", "Endre minne", "Skriv om en oppføring.", "PATCH", f"{V1}/memory/{{id}}",
           {"id": S(maxLength=20), "text": S(maxLength=2200)}, ("id", "text"), run=_memory_edit, writes=True, ask=False,
           tool="memory", tool_actions=("replace",)),
    Action("memory.delete", "Glem", "Fjern en oppføring fra minnet.", "DELETE", f"{V1}/memory/{{id}}",
           {"id": S(maxLength=20)}, ("id",), run=_memory_delete, writes=True, ask=False, tool="memory",
           tool_actions=("remove",)),
    # samtaler
    Action("session.list", "Samtaler", "Tidligere samtaler med Zeus.", "GET", f"{V1}/sessions",
           {"limit": I(minimum=1, maximum=200)}, run=_sessions, tool="session_search", tool_actions=("list",),
           cloud_sensitive=True),
    Action("session.open", "Åpne samtale", "Fortsett en tidligere samtale (ikke mens Zeus svarer).", "POST",
           f"{V1}/sessions/{{id}}/open", {"id": SID}, ("id",), run=_session_open, writes=True,
           app_only="Zeus kan ikke bytte bort samtalen han selv er i."),
    Action("session.new", "Ny samtale", "Start en ny samtale (ikke mens Zeus svarer).", "POST", f"{V1}/sessions/new",
           run=_session_new, writes=True, app_only="Zeus kan ikke avslutte samtalen han selv er i."),
    Action("session.delete", "Slett samtale", "Slett en lagret samtale.", "DELETE", f"{V1}/sessions/{{id}}", {"id": SID},
           ("id",), run=_session_delete, writes=True, danger=True,
           app_only="Samtalene er brukerens historikk – bare brukeren sletter dem."),
]
BY_NAME = {a.name: a for a in ACTIONS}

# Zeus-verktøyene som lages herfra (resten av handlingene bruker eksisterende verktøy med egne godkjenningskort).
GENERATED = {
    "media": "Mediebiblioteket (musikk, film, serier, lydbøker, e-bøker). Id-er fra media_search.",
    "downloads": "Nedlastingene.",
    "pc": "PC-en: maskinstatus, været, musikk på høyttaleren og fjernkontroll.",
    "model": "Modellen Zeus svarer med.",
    "web": "Nettet: søk (DuckDuckGo) og les sider som tekst. Oppgi kildene.",
    "tv": "TV-ene på hjemmenettet: spill film/serier fra biblioteket på TV-en (Chromecast, smart-TV, AirPlay) og styr dem.",
}


HTTP_ONLY = {"action", "confirm", "method"}  # ikke for Zeus: action er verktøyets eget felt, confirm er godkjenningen


def for_tool(tool: str, action: str) -> Action | None:
    return next((a for a in ACTIONS if a.tool == tool and action in a.tool_actions), None)


def tool_specs() -> dict[str, Any]:
    """ToolSpec-er for de genererte verktøyene: action-enum + alle parametrene (valgfrie), beskrivelse per handling."""
    from olympus.tools import ToolSpec, _obj

    out = {}
    for tool, intro in GENERATED.items():
        acts = [a for a in ACTIONS if a.tool == tool]
        props: dict[str, Any] = {}
        lines = []
        for a in acts:
            keys = [k for k in a.params if k not in HTTP_ONLY]
            for k in keys:
                spec = {kk: vv for kk, vv in a.params[k].items() if kk in ("type", "enum", "description", "items")}
                if k in props and props[k].get("description") != spec.get("description"):
                    props[k].pop("description", None)  # samme felt betyr litt ulikt per handling: forklares i teksten
                props.setdefault(k, spec)
            names = "/".join(a.tool_actions)
            need = f" (parametre: {', '.join(keys)})" if keys else ""
            ask = " Brukeren godkjenner." if a.needs_approval else ""
            lines.append(f"{names}: {a.description}{need}{ask}")
        enum = [t for a in acts for t in a.tool_actions]
        desc = intro + " action=\n" + "\n".join(f"- {line}" for line in lines)
        out[tool] = ToolSpec(tool, desc, _obj({"action": {"type": "string", "enum": enum}, **props}, ["action"]),
                             writes=any(a.writes for a in acts))
    return out


def tool_writes(tool: str, action: str | None) -> bool:
    a = for_tool(tool, str(action or ""))
    return bool(a and a.writes)


def zeus_approval(ctx: Ctx, tool: str, args: dict[str, Any], cloud: bool):
    """Godkjenningskortet når Zeus kaller en generert handling."""
    from olympus.tools import Approval

    a = for_tool(tool, str(args.get("action") or ""))
    if a is None or a.run is None:
        return Approval(False, reject=f"Ukjent handling for {tool}: {args.get('action')!r}.")
    if a.check is not None and (why := a.check(ctx, args)):
        return Approval(False, reject=why)
    if a.needs_approval or a.egress:
        try:
            preview = a.preview(ctx, args) if a.preview else "\n".join(f"  {k}: {v}" for k, v in args.items() if k != "action")
        except Exception:  # noqa: BLE001 – kortet skal aldri krasje; vis argumentene
            preview = json.dumps(args, ensure_ascii=False)[:2000]
        danger = a.is_danger(args)
        return Approval(True, f"Zeus vil: {a.label.lower()}" + (" (serveren kontakter en lenke)" if a.egress else ""),
                        danger=danger, preview=preview, preview_lang="text",
                        bind={"id": args["id"]} if "id" in args and isinstance(args.get("id"), str) else {})
    if cloud and a.cloud_sensitive:
        return Approval(True, f"{a.label} – svaret sendes til skyen", preview=a.description)
    return Approval(False)


URLISH = re.compile(r"(?i)^\s*(https?:|magnet:|ftp:|file:|//|[a-z][a-z0-9+.-]*://)")


def url_args(args: dict[str, Any]) -> list[str]:
    """Alle argumenter som ser ut som lenker (også i lister) – de skal stå i sin helhet på godkjenningskortet."""
    vals: list[Any] = []
    for v in args.values():
        vals.extend(v if isinstance(v, list) else [v])
    return [v for v in vals if isinstance(v, str) and URLISH.match(v)]


# Nedlasting fra lenker brukeren gir: Zeus spør ikke først (brukerens valg). Viruskontrollen sjekker det som kommer.
FREE_DOWNLOADS = {"media.add", "media.inspect"}


def is_url_param(name: str, spec: dict[str, Any]) -> bool:
    return "url" in name.lower() or "lenke" in str(spec.get("description", "")).lower()


def enforce_egress(a: Action | None, args: dict[str, Any], approval: Any) -> Any:
    """Sentralt (olympus.tools.Toolbox.approval): kontakter serveren en lenke/vert Zeus har valgt, må brukeren si ja – og
    kortet viser hele lenken (skjema, vert, sti og spørrestreng; lange vises i sin helhet med «se hele»)."""
    if approval.reject or (a is not None and a.name in FREE_DOWNLOADS):
        return approval
    urls = url_args(args)
    if not urls and not (a is not None and a.egress):
        return approval
    approval.needed = True
    if not approval.reason:
        approval.reason = "Zeus vil at serveren skal kontakte en lenke"
    missing = [u for u in urls if u not in approval.preview]
    if missing:
        approval.preview = (approval.preview + "\n\n" if approval.preview else "") + "\n".join(f"Lenke: {u}" for u in missing)
    return approval


async def zeus_run(ctx: Ctx, tool: str, raw: dict[str, Any]):
    from olympus.tools import ToolResult

    a = for_tool(tool, str(raw.get("action") or ""))
    if a is None or a.run is None:
        return ToolResult(False, f"Ukjent handling: {raw.get('action')!r}.", "ukjent handling")
    try:
        args = clean_args(a, raw)
        if "action" in a.params:
            args["action"] = raw["action"]
        result = await a.run(ctx, args)
    except MediaError as e:
        return ToolResult(False, str(e), str(e)[:80])
    except (OSError, ValueError, RuntimeError) as e:
        return ToolResult(False, f"{type(e).__name__}: {e}"[:500], "feilet")
    text = a.say(result) if a.say else json.dumps(result, ensure_ascii=False, default=str)
    from olympus.tools import _clip

    return ToolResult(True, _clip(text, 6000), f"{a.label.lower()}: " + text.splitlines()[0][:70] if text else a.label)


def capability_overview(tool_names: list[str]) -> str:
    """Kort oversikt til systemprompten: hva backenden kan, gruppert per verktøy (fra registeret, alltid oppdatert)."""
    names = set(tool_names)
    groups: dict[str, list[str]] = {}
    for a in ACTIONS:
        if a.tool and a.tool in names:
            groups.setdefault(a.tool, []).append(a.label.lower() + ("*" if a.needs_approval or a.egress else ""))
    if not groups:
        return ""
    lines = [f"- {tool}: {', '.join(dict.fromkeys(labels))}" for tool, labels in groups.items()]
    return ("## Det Olympus kan (samme handlinger som brukeren har knapper for i appen og på PC-en)\n"
            + "\n".join(lines) + "\n* = brukeren godkjenner først. Brukeren kan gjøre alt dette selv også – du er ett av "
            "to grensesnitt til den samme motoren.")

