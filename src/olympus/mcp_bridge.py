"""Olympus' verktøy for modeller som bare snakker MCP (Claude Code).

En liten MCP-server (strømbar HTTP, JSON-svar) som bare lever mens ett svar skrives: den lytter på 127.0.0.1 på en
tilfeldig port og krever en hemmelig nøkkel (Authorization-header), og kjenner bare verktøyene Zeus har i denne
runden. Konfigurasjonen med nøkkelen gis til Claude Code som en fil bare eieren kan lese – ikke i argv, der alle
lokale prosesser kan se den. Hvert kall sendes til
`runner` (orkestratoren), som gjør godkjenning, logging og selve jobben – akkurat som for lokale modeller.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from olympus.backends.llamacpp import Chunk

PROTOCOL = "2025-06-18"
SERVER = "olympus"

# runner(navn, argumenter, emit) -> (tekst til modellen, feil?). emit(Event) viser kallet/resultatet i chatten.
ToolRunner = Callable[[str, dict[str, Any], Callable[[Any], None]], Awaitable[tuple[str, bool]]]


class OlympusMCP:
    def __init__(self, tools: list[dict[str, Any]], runner: ToolRunner, put: Callable[[Chunk], None]) -> None:
        self.tools = [t["function"] for t in tools if t.get("type") == "function"]
        self.runner = runner
        self.put = put
        self.path = "/mcp"
        self.token = secrets.token_urlsafe(32)
        self.port = 0
        self.config_file = ""
        self._runner: web.AppRunner | None = None

    def allowed(self) -> list[str]:
        """Verktøynavnene slik Claude Code ser dem (forhåndsgodkjent der – Olympus spør selv)."""
        return [f"mcp__{SERVER}__{t['name']}" for t in self.tools]

    def config_json(self) -> str:
        return json.dumps({"mcpServers": {SERVER: {"type": "http", "url": f"http://127.0.0.1:{self.port}{self.path}",
                                                   "headers": {"Authorization": f"Bearer {self.token}"}}}})

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post(self.path, self._handle)
        runner = self._runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        fd, self.config_file = tempfile.mkstemp(prefix="olympus-mcp-", suffix=".json")  # 0600
        with os.fdopen(fd, "w") as f:
            f.write(self.config_json())

    async def stop(self) -> None:
        if self.config_file:
            try:
                os.unlink(self.config_file)
            except OSError:
                pass
            self.config_file = ""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        auth = request.headers.get("Authorization", "")
        if not secrets.compare_digest(auth.encode(), f"Bearer {self.token}".encode()):
            return web.Response(status=401)
        if request.host not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):  # DNS-rebinding
            return web.Response(status=403)
        try:
            msg = await request.json()
        except ValueError:
            return web.json_response(_error(None, -32700, "ugyldig JSON"), status=400)
        if isinstance(msg, list):  # batch
            replies = [r for m in msg if (r := await self._dispatch(m)) is not None]
            return web.json_response(replies) if replies else web.Response(status=202)
        reply = await self._dispatch(msg)
        return web.json_response(reply) if reply is not None else web.Response(status=202)

    async def _dispatch(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        mid, method, params = msg.get("id"), msg.get("method", ""), msg.get("params") or {}
        if mid is None:  # varsel (notifications/initialized o.l.) – ingen svar
            return None
        if method == "initialize":
            return _ok(mid, {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                             "capabilities": {"tools": {}}, "serverInfo": {"name": SERVER, "version": "1"}})
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(mid, {"tools": [{"name": t["name"], "description": t.get("description", ""),
                                        "inputSchema": t.get("parameters") or {"type": "object", "properties": {}}}
                                       for t in self.tools]})
        if method == "tools/call":
            name = params.get("name", "")
            if name not in {t["name"] for t in self.tools}:
                return _ok(mid, {"content": [{"type": "text", "text": f"Ukjent verktøy: {name}"}], "isError": True})
            text, is_error = await self.runner(name, params.get("arguments") or {},
                                               lambda ev: self.put(Chunk("event", event=ev)))
            return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": is_error})
        return _error(mid, -32601, f"ukjent metode: {method}")


def _ok(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
