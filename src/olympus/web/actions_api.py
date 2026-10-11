"""HTTP-sida av handlingsregisteret (olympus.actions): GET /api/v1/actions, en generisk rute per handling, og de
få rutene som må skrives for hånd (opplasting som strøm, cover som bilde-bytes, DELETE /downloads/{id}?forget=1).

Brukerens eget trykk i appen eller Lab's er godkjenningen – derfor ingen godkjenningskort her (appen bekrefter de
farlige selv). Feil blir {"error": "…"} med riktig status.
"""

from __future__ import annotations

import asyncio
import secrets
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import BodyPartReader, web

from olympus import actions
from olympus.actions import ACTIONS, Action, ActionError, Ctx, clean_args

UPLOAD_FIELD_MAX = 1000


def err(e: ActionError) -> web.Response:
    return web.json_response({"error": str(e)}, status=e.status)


async def _body(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except (ValueError, RecursionError):
        raise ActionError("Ugyldig JSON.") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ActionError("Forventet et JSON-objekt.")
    return data


def add_action_routes(app: web.Application, make_ctx: Callable[[web.Request], Ctx]) -> None:
    r = app.router

    async def list_actions(_r: web.Request) -> web.Response:
        return web.json_response([a.public() for a in ACTIONS])

    def generic(action: Action):
        async def handler(request: web.Request) -> web.Response:
            try:
                raw: dict[str, Any] = dict(request.query)
                if request.method in ("POST", "PUT", "PATCH"):
                    raw.update(await _body(request))
                raw.update(request.match_info)
                args = clean_args(action, raw)
                result = await action.run(make_ctx(request), args)  # type: ignore[misc]
            except ActionError as e:
                return err(e)
            return web.json_response(result, status=action.status)

        return handler

    r.add_get("/api/v1/actions", list_actions)
    for a in ACTIONS:
        if a.run is not None and not a.custom_http:
            r.add_route(a.method, a.path, generic(a))

    # --- for hånd -------------------------------------------------------------------------------------
    async def download_delete(request: web.Request) -> web.Response:
        """DELETE /downloads/{id} avbryter; ?forget=1 fjerner den også fra lista."""
        forget = request.query.get("forget", "") in ("1", "true", "ja")
        a = actions.BY_NAME["downloads.forget" if forget else "downloads.cancel"]
        try:
            return web.json_response(await a.run(make_ctx(request), clean_args(a, dict(request.match_info))))  # type: ignore[misc]
        except ActionError as e:
            return err(e)

    async def cover_put(request: web.Request) -> web.Response:
        """PUT /media/item/{id}/cover: bildet selv (image/jpeg|png|webp, maks 10 MB) eller JSON {"url"}."""
        from olympus import mediaops

        ctx = make_ctx(request)
        item = request.match_info["id"]
        ctype = (request.content_type or "").lower()
        try:
            if ctype == "application/json":
                url = (await _body(request)).get("url")
                if not isinstance(url, str) or not url.strip():
                    raise ActionError("Gi url til bildet.")
                return web.json_response(await actions.cover_from_url(ctx, item, url.strip()))
            if ctype not in ("image/jpeg", "image/png", "image/webp"):
                raise ActionError("Send bildet som image/jpeg, image/png eller image/webp – eller JSON {\"url\"}.", 415)
            if (request.content_length or 0) > mediaops.IMAGE_MAX:
                raise ActionError("Bildet er større enn 10 MB.", 413)
            data = bytearray()
            async for chunk in request.content.iter_chunked(1 << 16):
                data += chunk
                if len(data) > mediaops.IMAGE_MAX:
                    raise ActionError("Bildet er større enn 10 MB.", 413)
            return web.json_response(await actions.cover_bytes(ctx, item, bytes(data)))
        except ActionError as e:
            return err(e)

    async def upload(request: web.Request) -> web.Response:
        """POST /media/upload (multipart: file, kind, title?): strømmes rett til disk (aldri hele fila i minnet),
        maks 8 GB, bare lyd/video/undertekst/e-bok, så inn i riktig mappe med vanlige navneregler og skann."""
        from olympus import downloads, mediaops

        ctx = make_ctx(request)
        folder = downloads.STAGING / "opplasting" / secrets.token_hex(6)
        fields: dict[str, str] = {}
        fname = ""
        allowed = set().union(*mediaops.UPLOAD_EXT.values())
        try:
            if not (request.content_type or "").startswith("multipart/"):
                raise ActionError("Send fila som multipart/form-data (felt «file», «kind», valgfritt «title»).", 415)
            if (request.content_length or 0) > mediaops.UPLOAD_MAX + 1024 * 1024:
                raise ActionError("Fila er større enn 8 GB.", 413)
            reader = await request.multipart()
            while (part := await reader.next()) is not None:
                if not isinstance(part, BodyPartReader):
                    raise ActionError("Nøstet multipart støttes ikke.")
                if part.name == "file" and not fname:
                    fname = mediaops.safe_filename(part.filename or "fil")
                    if Path(fname).suffix.lower() not in allowed:
                        raise ActionError("Filtypen er ikke lov (lyd, video, undertekster eller e-bøker).", 415)
                    folder.mkdir(parents=True, exist_ok=True)
                    size = 0
                    with open(folder / fname, "wb") as f:
                        while chunk := await part.read_chunk(1 << 20):
                            size += len(chunk)
                            if size > mediaops.UPLOAD_MAX:
                                raise ActionError("Fila er større enn 8 GB.", 413)
                            f.write(chunk)
                elif part.name in ("kind", "title"):
                    data = bytearray()
                    while chunk := await part.read_chunk(4096):
                        data += chunk
                        if len(data) > UPLOAD_FIELD_MAX:
                            raise ActionError(f"{part.name} er for lang.")
                    fields[str(part.name)] = data.decode("utf-8", "replace").strip()
                else:
                    while await part.read_chunk(1 << 16):  # ukjente felt leses forbi, ikke lagres
                        pass
            if not fname:
                raise ActionError("Mangler fila (felt «file»).")
            kind = fields.get("kind", "")
            mediaops.check_upload_name(kind, fname)
            title = fields.get("title", "")
            if len(title) > mediaops.FIELD_MAX or any(ord(c) < 32 for c in title):
                raise ActionError("Ugyldig tittel.")
            if not await asyncio.to_thread(mediaops.real_media, folder / fname, kind):
                raise ActionError("Fila ser ikke ut til å være ekte lyd, video eller e-bok – stoppet.", 415)
            result = await actions.import_local(ctx, kind, folder, fname, title)
        except ActionError as e:
            shutil.rmtree(folder, ignore_errors=True)
            return err(e)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return web.json_response(result, status=201)

    r.add_delete("/api/v1/downloads/{id}", download_delete)
    r.add_put("/api/v1/media/item/{id}/cover", cover_put)
    r.add_post("/api/v1/media/upload", upload)
