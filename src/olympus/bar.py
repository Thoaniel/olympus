"""`olympus bar`: bro for Olymparch-baren. JSON-linjer på stdin/stdout mot den lokale Olympus-serveren.

Baren (Quickshell/QML) har ingen WebSocket, men kan kjøre en prosess og lese/skrive linjer. Denne prosessen
  - kobler seg til websocketen akkurat som `olympus zeus` (Origin + nøkkel-cookien fra ~/.olympus/web-token),
  - skriver hver melding fra serveren uendret som én JSON-linje på stdout,
  - holder en kompakt statuslinje {"type": "bar", …} oppdatert (skrives bare når noe endrer seg),
  - videresender JSON-linjer fra stdin til websocketen, gjør HTTP-kall mot /api/v1/… for baren, og tar korte
    kommandoer ({"cmd": "approve" | "deny" | "ai" | "model" | "undo" | "plan_add" | "pair"}),
  - kobler til igjen av seg selv (1, 2, 5, 10 s) og avslutter når stdin lukkes.

Nøkkelen skrives aldri ut. stdout får bare JSON-linjer; alt annet går til stderr. Protokollen: docs/bar.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import aiohttp

BACKOFF = (1, 2, 5, 10)
POLL = 10.0  # sekunder mellom HTTP-oppslagene for det websocketen ikke sender selv
ONLINE = 180  # en telefon regnes som tilkoblet hvis den har vært i kontakt de siste 3 minuttene
ACTIVE_DL = ("laster ned", "lastet ned", "deler")
RATE_RE = re.compile(r"429|rate.?limit|kvote|quota|for fort", re.I)


def _short(text: Any, n: int) -> str:
    t = str(text or "")
    return t if len(t) <= n else t[: n - 1] + "…"


def split_plan_text(text: str) -> tuple[str, str] | None:
    """«Tannlege fre 10:30» → («Tannlege», «fre 10:30»): det lengste tidsuttrykket på slutten som forstås."""
    from olympus.dates import parse_when

    words = text.split()
    for i in range(1, len(words)):
        when = " ".join(words[i:])
        if parse_when(when) is not None:
            return " ".join(words[:i]), when
    return None


class Bar:
    def __init__(self, base: str, token: str, write: Callable[[str], None], *,
                 backoff: tuple[float, ...] = BACKOFF, poll: float = POLL, pair: Callable[[], dict] | None = None) -> None:
        self.base = base.rstrip("/")
        self._token = token
        self._write = write
        self.backoff = backoff
        self.poll_every = poll
        self.pair = pair
        self.session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.connected = asyncio.Event()
        self._last_bar = ""
        self._plan_day = ""
        self._bg: set[asyncio.Task] = set()
        self.st: dict[str, Any] = {
            "ai": None, "model": None, "provider": None, "cloud": None, "answering": None, "busy": False,
            "approvals": {}, "plan": None, "media": None, "downloads": None, "phones": None, "last_error": None}

    # --- ut ----------------------------------------------------------------------------------------
    def emit(self, obj: dict) -> None:
        self._write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")

    def status(self, status: str, detail: Any = None, **extra: Any) -> None:
        self.emit({"type": "bridge", "status": status, "detail": detail, **extra})

    def bar(self) -> dict:
        s = self.st
        state = ("off" if s["ai"] is False else "approval" if s["approvals"] else "thinking" if s["busy"] else "idle")
        return {"type": "bar", "connected": self.ws is not None, "ai": s["ai"], "model": s["model"],
                "provider": s["provider"], "cloud": s["cloud"], "answering": s["answering"], "state": state,
                "approvals": list(s["approvals"].values()), "plan": s["plan"], "media": s["media"],
                "downloads": s["downloads"], "phones": s["phones"], "last_error": s["last_error"]}

    def push_bar(self) -> None:
        b = self.bar()
        key = json.dumps(b, sort_keys=True, ensure_ascii=False)
        if key != self._last_bar:
            self._last_bar = key
            self.emit(b)

    # --- statuslinja fra serverens meldinger ----------------------------------------------------------
    def _model(self, d: dict) -> None:
        for k in ("model", "provider", "cloud"):
            if k in d:
                self.st[k] = d[k]

    def absorb(self, m: dict) -> None:
        s, kind = self.st, m.get("type")
        if kind in ("state", "cleared") or (kind == "done" and isinstance(m.get("state"), dict)):
            st = m.get("state") or {}
            if "ai" in st:
                s["ai"] = bool(st["ai"])
            s["busy"] = bool(st.get("busy"))
            self._model(st)
        if kind in ("stats", "done") and isinstance(m.get("stats"), dict):
            self._model(m["stats"])
            if m["stats"].get("last_model"):
                s["answering"] = m["stats"]["last_model"]
        if kind == "model":
            self._model(m)
        elif kind == "ai":
            s["ai"] = bool(m.get("on"))
        elif kind == "user":
            s["busy"] = True
            s["last_error"] = None  # en ny melding: den forrige feilen gjelder ikke lenger
        elif kind == "agent_start":
            s["busy"], s["answering"] = True, m.get("model") or s["answering"]
        elif kind == "approval" and m.get("id"):
            s["approvals"][m["id"]] = {"id": m["id"], "tool": m.get("tool"), "reason": _short(m.get("reason"), 200),
                                       "danger": bool(m.get("danger")), "preview": _short(m.get("preview"), 400)}
        elif kind == "approval_done":
            s["approvals"].pop(m.get("id"), None)
        elif kind == "error":
            s["last_error"] = {"text": _short(m.get("text"), 300), "at": time.time()}
        elif kind == "notice" and RATE_RE.search(str(m.get("text", ""))):
            s["last_error"] = {"text": _short(m.get("text"), 300), "at": time.time()}
        elif kind == "player":
            cur = m.get("current") or None
            s["media"] = None if not cur else {
                "title": cur.get("title"), "artist": cur.get("artist"), "album": cur.get("album"),
                "playing": bool(m.get("playing")), "paused": bool(m.get("paused")),
                "position": m.get("position"), "duration": m.get("duration")}
        elif kind == "downloads" and isinstance(m.get("items"), list):
            self._downloads(m["items"])
        elif kind == "plan_changed":
            self._spawn(self.refresh_plan(force=True))

    def _downloads(self, items: list) -> None:
        act = [d for d in items if isinstance(d, dict) and d.get("status") in ACTIVE_DL]
        prog = [float(d.get("progress") or 0) for d in act]
        self.st["downloads"] = {"active": len(act), "progress": round(sum(prog) / len(prog), 3) if prog else None,
                                "items": [{"id": d.get("id"), "title": d.get("title"), "status": d.get("status"),
                                           "progress": d.get("progress")} for d in act[:5]]}

    # --- HTTP -------------------------------------------------------------------------------------------
    async def http(self, method: str, path: str, body: Any = None) -> tuple[int, Any, str | None]:
        """(status, json-svar eller None, feil eller None). Bare GET/POST mot /api/v1/ – alt annet: 400 uten kall."""
        if method not in ("GET", "POST") or not isinstance(path, str) or not path.startswith("/api/v1/") \
                or ".." in path or "\\" in path or any(ch in path for ch in "\r\n\t #") or "//" in path:
            return 400, None, "only GET/POST to /api/v1/… is allowed"
        if self.session is None:  # rett etter oppstart: gi forbindelsen litt tid før vi gir opp
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.connected.wait(), 3)
        if self.session is None:
            return 503, None, "not connected"
        try:
            kw: dict[str, Any] = {"timeout": aiohttp.ClientTimeout(total=30)}
            if method == "POST":
                kw["json"] = body if body is not None else {}
            async with self.session.request(method, self.base + path, **kw) as r:
                text = await r.text()
                try:
                    data = json.loads(text) if text else None
                except ValueError:
                    data = None
                err = None
                if r.status >= 400:
                    err = (data.get("error") if isinstance(data, dict) else None) or _short(text, 200) or r.reason
                return r.status, data, err
        except (aiohttp.ClientError, OSError, TimeoutError) as e:
            return 502, None, f"{type(e).__name__}: {e}"[:200]

    async def refresh_status(self) -> None:
        code, d, _ = await self.http("GET", "/api/v1/status")
        if code == 200 and isinstance(d, dict):
            self.st["ai"] = bool(d.get("ai"))
            self.st["busy"] = bool(d.get("busy"))
            self._model(d)

    async def refresh_phones(self) -> None:
        code, d, _ = await self.http("GET", "/api/v1/devices")
        if code == 200 and isinstance(d, list):
            now = time.time()
            self.st["phones"] = [{"id": x.get("id"), "name": x.get("name"), "last_seen": x.get("last_seen"),
                                  "online": bool(x.get("last_seen")) and now - float(x["last_seen"]) < ONLINE}
                                 for x in d if isinstance(x, dict)]

    async def refresh_plan(self, force: bool = False) -> None:
        from datetime import date, timedelta

        today = date.today()
        if not force and self._plan_day == today.isoformat():
            return
        code, d, _ = await self.http("GET", f"/api/v1/plan?from={today.isoformat()}&to={(today + timedelta(days=1)).isoformat()}")
        if code == 200 and isinstance(d, dict):
            self._plan_day = today.isoformat()
            self.st["plan"] = [{"id": e.get("id"), "title": e.get("title"), "start": e.get("start"), "end": e.get("end"),
                                "all_day": bool(e.get("all_day"))} for e in d.get("events", []) if isinstance(e, dict)]
            self.push_bar()

    async def refresh_downloads(self) -> None:
        code, d, _ = await self.http("GET", "/api/v1/downloads")
        if code == 200 and isinstance(d, list):
            self._downloads(d)

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # --- websocketen ------------------------------------------------------------------------------------
    async def ws_loop(self) -> None:
        from olympus.web.server import COOKIE

        tries = 0
        while True:
            detail = None
            try:
                async with aiohttp.ClientSession(headers={"Cookie": f"{COOKIE}={self._token}"}) as s:
                    self.session = s
                    async with s.ws_connect(self.base.replace("http", "ws", 1) + "/api/v1/ws",
                                            headers={"Origin": self.base}, heartbeat=25) as ws:
                        self.ws, tries = ws, 0
                        self.st["approvals"] = {}  # serveren sender kortene som venter på nytt
                        self.connected.set()
                        self.status("connected", self.base)
                        self._spawn(self.poll_once(first=True))
                        async for m in ws:
                            if m.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    msg = json.loads(m.data)
                                except ValueError:
                                    continue
                                self.emit(msg)
                                if isinstance(msg, dict):
                                    self.absorb(msg)
                                    self.push_bar()
                            elif m.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                                break
                        detail = f"closed ({ws.close_code})"
            except aiohttp.WSServerHandshakeError as e:
                detail = f"handshake {e.status}" + (" (wrong token?)" if e.status in (401, 403) else "")
            except (aiohttp.ClientError, OSError, TimeoutError) as e:
                detail = type(e).__name__ + (f": {e}" if str(e) else "")
            finally:
                self.ws, self.session = None, None
                self.connected.clear()
            wait = self.backoff[min(tries, len(self.backoff) - 1)]
            tries += 1
            self.status("disconnected", _short(detail, 200), retry_in=wait)
            self.st["approvals"], self.st["busy"] = {}, False
            self.push_bar()
            await asyncio.sleep(wait)

    async def poll_once(self, first: bool = False) -> None:
        jobs = [self.refresh_status(), self.refresh_phones(), self.refresh_plan(force=first)]
        if first:
            jobs.append(self.refresh_downloads())
        await asyncio.gather(*jobs, return_exceptions=True)
        self.push_bar()

    async def poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_every)
            if self.ws is not None:
                await self.poll_once()

    # --- inn -----------------------------------------------------------------------------------------------
    async def send_ws(self, msg: dict) -> bool:
        if self.ws is None or self.ws.closed:
            self.status("error", "not connected: message dropped", sent=msg.get("type"))
            return False
        try:
            await self.ws.send_json(msg)
            return True
        except (ConnectionError, RuntimeError, aiohttp.ClientError) as e:
            self.status("error", f"send failed: {type(e).__name__}")
            return False

    async def handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):
            self.status("error", "malformed JSON line ignored")
            return
        if not isinstance(msg, dict):
            self.status("error", "expected a JSON object")
            return
        if "cmd" in msg:
            await self.command(msg)
        elif msg.get("type") == "http":
            rid = msg.get("rid")
            code, data, err = await self.http(str(msg.get("method", "GET")).upper(), msg.get("path"), msg.get("body"))
            self.emit({"type": "http", "rid": rid, "status": code, "json": data, "error": err})
        elif isinstance(msg.get("type"), str):
            await self.send_ws(msg)
        else:
            self.status("error", "line needs \"type\" or \"cmd\"")

    def reply(self, msg: dict, ok: bool, result: Any = None, error: str | None = None) -> None:
        self.emit({"type": "cmd", "cmd": msg.get("cmd"), "rid": msg.get("rid"), "ok": ok, "result": result,
                   "error": error})

    async def _http_cmd(self, msg: dict, method: str, path: str, body: Any = None) -> None:
        code, data, err = await self.http(method, path, body)
        self.reply(msg, code < 300, data, err)

    async def command(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd in ("approve", "deny", "always"):
            aid = msg.get("id")
            if not isinstance(aid, str) or not aid:
                self.reply(msg, False, error="id missing")
                return
            answer = {"approve": "yes", "deny": "no", "always": "always"}[cmd]
            self.reply(msg, await self.send_ws({"type": "approve", "id": aid, "answer": answer}))
        elif cmd == "ai":
            if not isinstance(msg.get("on"), bool):
                self.reply(msg, False, error="on must be true/false")
                return
            await self._http_cmd(msg, "POST", "/api/v1/ai", {"on": msg["on"]})
        elif cmd == "model":
            if not isinstance(msg.get("id"), str) or not msg["id"].strip():
                self.reply(msg, False, error="id missing")
                return
            await self._http_cmd(msg, "POST", "/api/v1/models", {"model": msg["id"]})
        elif cmd == "undo":
            code, log, err = await self.http("GET", "/api/v1/log")
            if code != 200 or not isinstance(log, list):
                self.reply(msg, False, error=err or "could not read the change log")
            elif not log:
                self.reply(msg, False, error="nothing to undo")
            else:
                await self._http_cmd(msg, "POST", f"/api/v1/undo/{int(log[0]['seq'])}")
        elif cmd == "plan_add":
            title, start = msg.get("title"), msg.get("start")
            if not title and isinstance(msg.get("text"), str):
                got = split_plan_text(msg["text"])
                if got is None:
                    self.reply(msg, False, error="found no date/time at the end of the text (e.g. «Tannlege fre 10:30»)")
                    return
                title, start = got
            if not isinstance(title, str) or not isinstance(start, str) or not title.strip() or not start.strip():
                self.reply(msg, False, error="give text, or title + start")
                return
            body = {"title": title.strip(), "start": start.strip(), "client": f"bar-{time.time_ns()}"}
            for k in ("end", "all_day", "note"):
                if k in msg:
                    body[k] = msg[k]
            await self._http_cmd(msg, "POST", "/api/v1/plan", body)
        elif cmd == "pair":
            if self.pair is None:
                self.reply(msg, False, error="pairing not available")
                return
            try:
                self.reply(msg, True, await asyncio.to_thread(self.pair))
            except Exception as e:  # noqa: BLE001
                self.reply(msg, False, error=f"{type(e).__name__}: {e}"[:200])
        else:
            self.status("error", f"unknown cmd: {_short(cmd, 40)}")

    # --- hele bro-prosessen ------------------------------------------------------------------------------
    async def run(self, lines: AsyncIterator[str]) -> int:
        self.push_bar()
        tasks = [asyncio.create_task(self.ws_loop()), asyncio.create_task(self.poll_loop())]
        try:
            async for line in lines:
                try:
                    await self.handle_line(line)
                except Exception as e:  # noqa: BLE001 – én rar linje skal aldri ta ned baren
                    self.status("error", f"{type(e).__name__}: {_short(e, 160)}")
        finally:
            for t in [*tasks, *self._bg]:
                t.cancel()
            for t in [*tasks, *self._bg]:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        return 0


async def stdin_lines() -> AsyncIterator[str]:
    """stdin linje for linje uten å blokkere hendelsesløkka (en tråd leser; virker for rør, filer og terminal)."""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue[str | None] = asyncio.Queue()

    def reader() -> None:
        try:
            for line in sys.stdin:
                loop.call_soon_threadsafe(q.put_nowait, line)
        except (OSError, ValueError, UnicodeDecodeError):
            pass
        finally:
            loop.call_soon_threadsafe(q.put_nowait, None)

    threading.Thread(target=reader, daemon=True, name="bar-stdin").start()
    while (line := await q.get()) is not None:
        yield line


def pair_info(c: Any) -> dict:
    """Engangskode for Zeus-appen (som `olympus pair`): zeus://-lenken og QR-koden som SVG hvis qrencode finnes."""
    import subprocess

    from olympus.web.server import pair_url

    url = pair_url(c)
    svg = None
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        # the one-time code goes in on stdin: as an argument it would be visible in ps
        svg = subprocess.run(["qrencode", "-t", "SVG", "-o", "-"], input=url, capture_output=True, text=True,
                             timeout=5).stdout or None
    return {"url": url, "expires_in": 600, "qr_svg": svg}


def run(c: Any) -> int:
    from olympus.web.server import get_token

    out = sys.stdout
    sys.stdout = sys.stderr  # alt annet som skrives ut havner på stderr: stdout er bare JSON-linjer

    def write(s: str) -> None:
        out.write(s)
        out.flush()

    bar = Bar(f"http://127.0.0.1:{int(c.get('web.port', 8787))}", get_token(c), write, pair=lambda: pair_info(c))
    try:
        return asyncio.run(bar.run(stdin_lines()))
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return 0
