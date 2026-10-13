"""Olympus på telefonen: en liten webserver med en mobil-app (PWA) og en websocket mot den samme Zeus.

Sikkerhet: Olympus kan kjøre kommandoer og lese filer, så serveren
  - lytter bare på Tailscale-adressen (100.x) og localhost – aldri på alle grensesnitt,
  - krever en hemmelig nøkkel (~/.olympus/web-token, 600) som settes som cookie første gang du åpner lenken,
  - viser hvert verktøykall som trenger godkjenning på telefonen, akkurat som i TUI-en.

Bruk:  olympus web            (skriver ut lenken med nøkkelen, og en QR-kode hvis qrencode finnes)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

from olympus import config as cfg
from olympus.agents.orchestrator import Orchestrator
from olympus.model_manager import ModelManager
from olympus.sessions import SessionStore, title_from
from olympus.web.wire import approval_to_wire, call_to_wire, to_wire, wire_preview

STATIC = Path(__file__).parent / "static"
HUB = web.AppKey("hub", object)  # huben bak appen (testene og verktøy som trenger den)
COOKIE = "olympus_web"


def token_path(c: cfg.Config) -> Path:
    return c.home / "web-token"


def get_token(c: cfg.Config) -> str:
    p = token_path(c)
    if p.exists():
        return p.read_text().strip()
    tok = secrets.token_urlsafe(24)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    return tok


_TS_CACHE: tuple[float, str | None] = (0.0, None)


def tailscale_ip() -> str | None:
    """Tailscale-adressen, hurtiglagret i 5 min: den ble spurt om (subprosess, opptil 3 s) ved hver tilkobling og
    hver endrende forespørsel fra nettleseren – inne i hendelsesløkka, så alt annet ventet."""
    global _TS_CACHE
    now = time.monotonic()
    if _TS_CACHE[0] and now - _TS_CACHE[0] < 300:
        return _TS_CACHE[1]
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3).stdout.strip()
        ip = out.splitlines()[0] if out else None
    except (OSError, subprocess.SubprocessError):
        ip = None
    _TS_CACHE = (now, ip)
    return ip


def tool_detail(call) -> str:
    """Kort linje om hva verktøyet gjør, til chatten: «add «Tannlege» 2026-10-16T10:00», «list 12.10–19.10»,
    «husk: drikker kaffen svart». Før stod det bare «⚙ plan» – handlingen manglet."""
    a = call.args
    if call.name == "shell":
        return "$ " + str(a.get("command", ""))[:200]
    action = str(a.get("action") or "")
    what = next((str(a[k]) for k in ("title", "query", "pattern", "content", "url", "ref", "path", "name", "match", "id")
                 if a.get(k)), "")
    when = " – ".join(str(a[k]) for k in ("from", "to") if a.get(k)) or str(a.get("start") or "")
    parts = [action, f"«{what[:80]}»" if what and action else what[:120], when]
    return " ".join(p for p in parts if p)[:200]


class Hub:
    """Én Olympus-«hjerne» delt av alle tilkoblinger (det er bare deg): én orkestrator, én økt om gangen."""

    def __init__(self, c: cfg.Config) -> None:
        self.c = c
        self.manager = ModelManager(c)
        self.orch = Orchestrator(c, self.manager, cwd=Path.home())
        from olympus.model_choices import ModelCatalog

        self.catalog = ModelCatalog(c, self.orch.backends)  # modellvalget (/api/v1/models, ws «model»)
        self.sessions = SessionStore(c.path("sessions_dir"))
        self.session_id: str | None = None
        self.transcript: list[dict] = []
        self.clients: set[web.WebSocketResponse] = set()
        self.task: asyncio.Task | None = None
        self.approvals: dict[str, asyncio.Future] = {}
        self.danger: dict[str, bool] = {}  # farlige kall kan bare godkjennes én gang, aldri «alltid»
        self.approval_msgs: dict[str, dict] = {}  # kortene som venter: sendes på nytt til en skjerm som kobler seg til
        self.last_used = 0.0  # sist noe ble spurt om fra telefonen (time.monotonic)
        from olympus.lab import open_lab

        self.lab = open_lab(c.home)  # planen, parede enheter og AI-bryteren (delt med Zeus' plan-verktøy)
        self.orch.toolbox.lab = self.lab
        self.lab.listeners.append(self._plan_changed)
        from olympus.medialib import open_library

        self.lib = open_library(c)  # Olympus' eget mediebibliotek (musikk, bøker, film)
        self.orch.toolbox.lib = self.lib
        from olympus.downloads import open_downloads

        self.dl = open_downloads(c, self.lab, self.lib)
        self.dl.notify = lambda text: self.send({"type": "notice", "text": text})
        self.orch.toolbox.dl = self.dl
        self.orch.toolbox.hub = self  # handlingene (olympus.actions) når modellbytte, økter og kringkasting
        self.pending_model: str | None = None  # Zeus ba om å bytte modell: skjer når svaret er ferdig
        self.device_socks: dict[str, set] = {}  # enhets-id → åpne websocketer (lukkes når enheten fjernes)
        self._weather: tuple[float, dict | None] = (0.0, None)

    def _plan_changed(self, seq: int) -> None:
        """Hver endring i planen (fra appen, PC-en eller Zeus) sendes til alle skjermer med en gang."""
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(self.send({"type": "plan_changed", "seq": seq}))

    async def idle_unloader(self, interval: float = 20) -> None:
        """Ingen ressurser når den ikke brukes: modellene lastes når en melding kommer, og frigjøres igjen etter
        web.idle_unload sekunder uten aktivitet (standard 5 min). Selve serveren bruker bare noen få MB RAM."""
        import time

        while True:
            await asyncio.sleep(interval)
            idle = float(self.c.get("web.idle_unload", 300))
            busy = self.task is not None and not self.task.done()
            if not busy and self.manager.loaded() and time.monotonic() - self.last_used > idle:
                await self.manager.unload()
                await self.send({"type": "notice", "text": "Modellen er frigjort for å spare GPU-en; neste melding laster den igjen."})

    async def game_watch(self, interval: float = 2) -> None:
        """Olymparchs spillmodus (gpu.gaming): når et spill starter, frigjøres den lokale modellen med en gang, så
        spillet får hele GPU-en. Den lastes ikke igjen før spillet er ferdig (ensure/available sier nei)."""
        from olympus.gpu import gaming

        while True:
            await asyncio.sleep(interval)
            if gaming() and self.manager.loaded():
                await self.manager.unload()
                await self.send({"type": "notice", "text": "Spillmodus: den lokale modellen er frigjort, så spillet får GPU-en."})

    async def send(self, msg: dict) -> None:
        """Til alle skjermer samtidig (før: én om gangen, så én treg telefon på mobilnett holdt igjen PC-en og de
        andre). Rekkefølgen per skjerm er den samme, for hver melding sendes ferdig før neste. En skjerm som ikke tar
        imot på 10 s regnes som borte."""
        clients = list(self.clients)

        async def one(ws) -> bool:
            try:
                await asyncio.wait_for(ws.send_json(msg), 10)
                return True
            except (ConnectionError, RuntimeError, TimeoutError):
                return False

        for ws, ok in zip(clients, await asyncio.gather(*(one(ws) for ws in clients))):
            if not ok:
                self.clients.discard(ws)

    def agent_info(self, name: str) -> dict:
        a = self.c.get("agents", {}).get(name, {})
        return {"agent": name, "display": a.get("display", name), "icon": a.get("icon", "✦"),
                "color": self.c.agent_color(name)}

    def model_info(self) -> dict:
        """Modellen Zeus svarer med nå, og om samtalen da går ut av PC-en."""
        from olympus.model_choices import cloud_warning, current

        ref = current(self.c)
        b = self.orch.backends
        cloud = b.is_cloud(ref)
        return {"model": ref, "cloud": cloud, "provider": b.provider_label(ref) if cloud else "lokal",
                "warning": cloud_warning(self.c, b, ref)}

    def state(self) -> dict:
        return {"busy": self.task is not None and not self.task.done(), "session": self.session_id,
                "ai": self.lab.ai_on, **self.model_info()}

    def sync_model(self) -> None:
        """Ta inn et modellbytte en annen prosess har lagret (f.eks. TUI-en uten hjerne)."""
        from olympus.model_choices import sync_saved

        if sync_saved(self.c):
            self.orch.backends.reset()

    async def set_model(self, wanted: str, force: bool = False) -> dict | None:
        """Bytt modell (lagres i config.toml) og si fra til alle skjermer. None = ukjent valg eller opptatt."""
        from olympus.model_choices import resolve_choice, set_model

        if not force and self.task is not None and not self.task.done():
            return None
        ref = resolve_choice(self.catalog, wanted)
        if ref is None:
            return None
        set_model(self.c, ref)
        self.orch.backends.reset()
        if not self.orch.backends.is_cloud(ref):  # frigjør andre lokale modeller (den nye lastes ved neste melding)
            for m in self.manager.loaded():
                if m != ref:
                    await self.manager.unload(m)
        info = self.model_info()
        await self.send({"type": "model", **info})
        if info["warning"]:
            await self.send({"type": "notice", "text": info["warning"]})
        await self.send({"type": "state", "state": self.state()})
        await self.send({"type": "stats", "stats": self.stats()})
        return info

    async def weather(self) -> dict | None:
        """Været fra MET/Yr for stedet i lab-innstillingene (lat/lon/sted). Mellomlagres i 30 min."""
        import time

        import httpx

        lat, lon, place = self.lab.setting("lat"), self.lab.setting("lon"), self.lab.setting("place")
        if not lat or not lon:
            return None
        ts, cached = self._weather
        if cached is not None and time.time() - ts < 1800 and cached.get("_at") == (lat, lon):
            return {k: v for k, v in cached.items() if k != "_at"}
        try:
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": "OlympusLab/1.0 (personlig assistent)"}) as c:
                r = await c.get("https://api.met.no/weatherapi/locationforecast/2.0/compact",
                                params={"lat": lat, "lon": lon})
            r.raise_for_status()
            now = r.json()["properties"]["timeseries"][0]["data"]
            out = {"place": place, "temp": now["instant"]["details"].get("air_temperature"),
                   "symbol": (now.get("next_1_hours") or now.get("next_6_hours") or {}).get("summary", {}).get("symbol_code", ""),
                   "rain": (now.get("next_6_hours") or {}).get("details", {}).get("precipitation_amount")}
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            return {k: v for k, v in cached.items() if k != "_at"} if cached and cached.get("_at") == (lat, lon) else None
        # Byttet stedet mens vi ventet? Da gjelder ikke dette svaret – ikke lagre det.
        if (self.lab.setting("lat"), self.lab.setting("lon")) == (lat, lon):
            self._weather = (time.time(), {**out, "_at": (lat, lon)})
        return out

    def net(self) -> dict | None:
        """Nettverksfart (byte/s inn og ut, alle grensesnitt unntatt lo) siden forrige kall – None første gang."""
        import time

        import psutil

        try:  # alle plattformer (før: /proc/net/dev, bare Linux)
            nics = psutil.net_io_counters(pernic=True)
        except (OSError, RuntimeError):
            return None
        rx = sum(c.bytes_recv for n, c in nics.items() if not n.lower().startswith(("lo", "loopback")))
        tx = sum(c.bytes_sent for n, c in nics.items() if not n.lower().startswith(("lo", "loopback")))
        now = time.monotonic()
        last = getattr(self, "_net_last", None)
        self._net_last = (now, rx, tx)
        if last is None or now - last[0] < 0.5:
            return getattr(self, "_net_rate", None)
        self._net_rate = {"down": max(0, int((rx - last[1]) / (now - last[0]))), "up": max(0, int((tx - last[2]) / (now - last[0])))}
        return self._net_rate

    async def brief(self) -> dict:
        """Morgenbriefen: dagens plan, været og serveren. Ingen AI – virker også når Zeus er av."""
        import shutil
        from datetime import date

        from olympus import thermal

        disk = shutil.disk_usage(Path.home())
        temps = thermal.read().temps
        return {"date": date.today().isoformat(), "events": self.lab.day(date.today().isoformat()),
                "weather": await self.weather(),
                "server": {"disk_pct": round(100 * disk.used / disk.total), "gpu": temps.get("gpu_junction"), "net": self.net(),
                           "cpu": temps.get("cpu"), "models": self.manager.loaded(), "ai": self.lab.ai_on}}

    def stats(self) -> dict:
        """Det TUI-ens panel og statuslinje viser: modeller i GPU-en, kontekst, fart, kostnad, siste svar."""
        o, m = self.orch, self.manager
        return {"loaded": m.loaded(), "usage": [list(u) for u in m.usage()], "context": list(o.context_used()),
                "messages": len(o.messages), "last_model": o.last_model, "session_tokens": o.session_tokens,
                "last_usage": o.last_usage, "active_agent": o.active_agent, "compressions": getattr(o, "compressions", 0),
                "last_answers": [list(a) for a in o.last_answers[-1:]], "session": self.session_id,
                **{k: v for k, v in self.model_info().items() if k != "warning"}}

    def history(self) -> dict:
        return {"type": "history", "session": self.session_id,
                "items": [{**e, **(self.agent_info(e["agent"]) if e.get("kind") == "agent" else {})} for e in self.transcript]}

    def save(self) -> None:
        if not self.transcript or not self.orch.messages:
            return
        if self.session_id is None:
            self.session_id = self.sessions.new_id()
            self.orch.toolbox.session_id = self.session_id
        first = next((e["text"] for e in self.transcript if e["kind"] == "user"), "Ny økt")
        self.sessions.save(self.session_id, title_from(first), self.orch.messages, self.transcript)

    async def approve(self, call, approval) -> str:
        aid = secrets.token_hex(6)
        fut = asyncio.get_running_loop().create_future()
        self.approvals[aid] = fut
        self.danger[aid] = approval.danger
        msg = {"type": "approval", "id": aid, "tool": call.name, "reason": approval.reason,
               "preview": wire_preview(approval.preview), "danger": approval.danger,
               "call": call_to_wire(call), "approval": approval_to_wire(approval)}
        self.approval_msgs[aid] = msg
        await self.send(msg)
        try:
            return await asyncio.wait_for(fut, timeout=600)
        except TimeoutError:
            return "no"
        finally:
            self.approvals.pop(aid, None)
            self.danger.pop(aid, None)
            self.approval_msgs.pop(aid, None)
            await self.send({"type": "approval_done", "id": aid})

    async def run(self, text: str, origin: str = "") -> None:
        import time

        self.sync_model()
        try:
            await self._run(text, origin)
        finally:
            self.last_used = time.monotonic()  # tidsfristen regnes fra da svaret var ferdig
            if self.pending_model:  # Zeus fikk lov å bytte modell: nå, når svaret er ferdig
                wanted, self.pending_model = self.pending_model, None
                await self.set_model(wanted, force=True)

    async def _run(self, text: str, origin: str = "") -> None:
        entry: dict | None = None
        self.transcript.append({"kind": "user", "text": text})
        await self.send({"type": "user", "text": text, "origin": origin})
        try:
            async for ev in self.orch.run(text, approve=self.approve):
                await self.send({"type": "ev", "ev": to_wire(ev)})  # full hendelse for TUI-en
                if ev.kind == "status":
                    await self.send({"type": "status", **self.agent_info(ev.agent), "text": ev.text})
                elif ev.kind == "notice":
                    await self.send({"type": "notice", "text": ev.text})
                elif ev.kind == "agent_start":
                    entry = {"kind": "agent", "agent": ev.agent, "model": ev.model, "text": ""}
                    self.transcript.append(entry)
                    await self.send({"type": "agent_start", **self.agent_info(ev.agent), "model": ev.model,
                                     "cloud": self.orch.backends.is_cloud(ev.model)})
                elif ev.kind == "chunk" and ev.chunk is not None and ev.chunk.kind == "content":
                    if entry is not None:
                        entry["text"] += ev.chunk.text
                    await self.send({"type": "chunk", "text": ev.chunk.text})
                elif ev.kind == "tool_call" and ev.call is not None:
                    if entry is not None:  # lagres i samtalen, så en gammel samtale viser hva Zeus faktisk gjorde
                        entry.setdefault("tools", []).append(f"⚙ {ev.call.name} {tool_detail(ev.call)}".strip())
                    await self.send({"type": "tool", "id": ev.call.id, "name": ev.call.name, "detail": tool_detail(ev.call)})
                elif ev.kind == "tool_result" and ev.call is not None and ev.result is not None:
                    if entry is not None and not ev.result.ok:
                        entry.setdefault("tools", []).append(f"✗ {ev.result.summary}")
                    await self.send({"type": "tool_result", "id": ev.call.id, "ok": ev.result.ok,
                                     "summary": ev.result.summary, "denied": ev.result.denied})
                    if ev.result.ok and ev.result.undo:  # gjort uten kort: kan angres med ett trykk
                        await self.send({"type": "undoable", "seq": ev.result.undo, "text": ev.result.summary})
                    # Zeus endret biblioteket (slettet, hentet, skannet, kjørte en kommando): appen og Lab's henter på nytt
                    if ev.result.ok and ev.call.name in LIBRARY_TOOLS:
                        if ev.call.name == "shell":
                            await asyncio.to_thread(self.lib.scan)  # filer kan være flyttet/slettet utenom biblioteket
                        await self.send({"type": "media_changed", "what": "library"})
                elif ev.kind == "judge_rejected":  # svaret påsto noe uten verktøy: forkastet, gjøres på nytt
                    if entry is not None:
                        entry["draft"], entry["rejected"] = True, ev.text
                    await self.send({"type": "judge_rejected", "text": ev.text, "attempt": ev.value, "of": ev.total})
                elif ev.kind == "agent_end":
                    await self.send({"type": "agent_end", "agent": ev.agent})
        except asyncio.CancelledError:
            await self.send({"type": "notice", "text": "Avbrutt."})
        except Exception as e:  # noqa: BLE001 – vis feilen på telefonen i stedet for å dø
            await self.send({"type": "error", "text": f"{type(e).__name__}: {e}"})
        finally:
            self.save()
            await self.send({"type": "done", "session": self.session_id, "state": self.state(), "stats": self.stats()})

    async def handle(self, msg: dict, origin: str = "", device: dict | None = None) -> None:
        import time

        kind = msg.get("type")
        if kind not in ("stats", "hello"):  # statusspørringer fra TUI-en er ikke bruk – ellers frigjøres aldri GPU-en
            self.last_used = time.monotonic()
        busy = self.task is not None and not self.task.done()
        if kind == "send" and str(msg.get("text", "")).strip() and not self.lab.ai_on:
            await self.send({"type": "notice", "text": "ϟ Zeus er av. Alt annet virker som vanlig – slå ham på i appen."})
            return
        if kind == "send" and str(msg.get("text", "")).strip():
            if busy:
                await self.send({"type": "notice", "text": "Vent til svaret er ferdig (eller avbryt)."})
                return
            self.task = asyncio.create_task(self.run(str(msg["text"]).strip(), origin))
        elif kind == "approve" and msg.get("id") in self.approvals:
            fut = self.approvals[msg["id"]]
            if not fut.done():
                answer = msg.get("answer") if msg.get("answer") in ("yes", "no", "always") else "no"
                if answer == "always" and self.danger.get(msg["id"]):
                    answer = "yes"
                fut.set_result(answer)
        elif kind == "interrupt" and busy:
            self.task.cancel()
        elif kind == "new" and not busy:
            await self.new_session()
        elif kind == "open" and not busy:
            await self.open_session(str(msg.get("sid", "")))
        elif kind == "stats":
            await self.send({"type": "stats", "stats": self.stats()})
        elif kind == "command" and not busy:
            await self.command(str(msg.get("name", "")), msg)

    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    async def new_session(self) -> bool:
        if self.busy():
            return False
        self.orch.reset()
        self.session_id, self.transcript = None, []
        self.orch.toolbox.session_id = None
        await self.send({"type": "cleared", "state": self.state()})
        return True

    async def open_session(self, sid: str) -> str:
        """"ok" | "busy" | "missing" – samme for websocketen (TUI) og /api/v1/sessions/{id}/open (appen)."""
        if self.busy():
            return "busy"
        data = self.sessions.load(sid) if sid and "/" not in sid and ".." not in sid else None
        if data is None:
            return "missing"
        self.orch.reset()
        self.session_id = data["id"]
        self.orch.toolbox.session_id = self.session_id
        self.orch.messages.extend(data.get("messages", []))
        self.transcript = list(data.get("transcript", []))
        await self.send(self.history())
        return "ok"

    async def delete_session(self, sid: str) -> str:
        if self.sessions.load(sid) is None:
            return "missing"
        if sid == self.session_id:
            if self.busy():
                return "busy"
            await self.new_session()
        self.sessions.delete(sid)
        await self.send({"type": "sessions_changed"})
        return "ok"

    async def command(self, name: str, msg: dict) -> None:
        """Kommandoer som endrer hjernen (ikke bare visningen): /komprimer, modellvalg, frigjøring."""
        o = self.orch
        if name == "komprimer":
            notes = [ev.text async for ev in o._maybe_compress(o.active_agent, force=True) if ev.kind == "notice"]
            text = notes[-1] if notes else "Samtalen er for kort til å komprimere."
            self.save()
        elif name == "model":
            wanted = msg.get("model")
            if not isinstance(wanted, str) or not wanted.strip() or len(wanted) > 200:
                return
            if await self.set_model(wanted) is None:
                await self.send({"type": "notice", "text": f"Ukjent modell: {wanted[:80]}. Velg en fra /modell."})
            return
        elif name == "router":  # velgeren av/på (test og innstilling)
            o.router_on = bool(msg.get("on"))
            text = f"Velgeren er {'på' if o.router_on else 'av'}."
        elif name == "unload":
            gone = await self.manager.unload(msg.get("model") or None)
            text = f"Frigjort: {', '.join(gone) or 'ingenting'}."
        else:
            return
        await self.send({"type": "notice", "text": text})
        await self.send({"type": "stats", "stats": self.stats()})


# Verktøy som kan ha endret biblioteket uten å si fra selv (handlingene i olympus.actions sender media_changed selv).
LIBRARY_TOOLS = {"media_get", "playlist", "shell", "write_file", "edit_file"}


def make_app(c: cfg.Config) -> web.Application:
    token = get_token(c)
    hub = Hub(c)

    @web.middleware
    async def auth(request: web.Request, handler):
        if request.path in ("/manifest.json", "/icon.svg", "/sw.js"):
            return await handler(request)
        if request.path.endswith((".css", ".js")):
            # Sjekk alltid om det finnes en ny versjon (ETag/Last-Modified): ellers viser telefonen gammel stil.
            resp = await auth_inner(request, handler)
            resp.headers["Cache-Control"] = "no-cache"
            return resp
        return await auth_inner(request, handler)

    def same_origin(request: web.Request) -> bool:
        """Nettlesere sender alltid Origin (og Sec-Fetch-Site) på endrende kall: begge må peke på Olympus selv.
        Uten Origin er det ikke en nettleser (Lab's/TUI med httpx) – da finnes ingen fremmed side som kan misbruke cookien."""
        site = request.headers.get("Sec-Fetch-Site")
        if site in ("cross-site", "same-site"):
            return False
        origin = request.headers.get("Origin")
        if origin is None:
            return site in (None, "same-origin", "none")
        allowed = {*hosts(c), "localhost", "127.0.0.1"}
        host = request.host.rsplit(":", 1)[0].strip("[]")
        return urlparse(origin).netloc == request.host and host in allowed

    async def auth_inner(request: web.Request, handler):
        if request.path.startswith("/api/v1/"):
            # Zeus-appen: egen nøkkel per paret enhet (kan trekkes tilbake). Nettleseren/TUI-en bruker cookien.
            if request.path == "/api/v1/pair":
                return await handler(request)
            bearer = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            device = hub.lab.device_for(bearer) if bearer else None
            if device is None and not secrets.compare_digest(request.cookies.get(COOKIE, ""), token):
                return web.json_response({"error": "ikke paret"}, status=401)
            if device is None and request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin(request):
                # Mot CSRF: en annen side (også en annen port på samme maskin) kan ikke bruke cookien til å endre noe.
                return web.json_response({"error": "feil opphav"}, status=403)
            request["device"] = device
            return await handler(request)
        if request.query.get("t") and secrets.compare_digest(request.query["t"], token):
            resp = web.HTTPFound("/")
            resp.set_cookie(COOKIE, token, max_age=365 * 24 * 3600, httponly=True, samesite="Strict")
            raise resp
        if not secrets.compare_digest(request.cookies.get(COOKIE, ""), token):
            return web.Response(status=401, text="Olympus: åpne lenken med nøkkelen (olympus web viser den).")
        return await handler(request)

    async def index(_r):
        # Versjonsnummer på stil og skript (endringstid), så telefonen aldri viser en gammel versjon fra hurtigbufferen.
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for name in ("style.css", "app.js"):
            html = html.replace(f'"/{name}"', f'"/{name}?v={int((STATIC / name).stat().st_mtime)}"')
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def state(_r):
        return web.json_response(hub.state())

    async def sessions(_r):
        return web.json_response([{"id": s.id, "title": s.title, "updated": s.updated} for s in hub.sessions.list(100)])

    async def ws(request: web.Request):
        # Mot «cross-site WebSocket hijacking»: bare siden Olympus selv serverer får koble til websocketen.
        # Både opphav og vert må være en av adressene serveren faktisk lytter på (stopper også DNS-rebinding,
        # der et fremmed domenenavn peker til maskinen).
        origin = request.headers.get("Origin")
        allowed = {*hosts(c), "localhost", "127.0.0.1"}
        host = request.host.rsplit(":", 1)[0].strip("[]")
        if request.get("device") is not None:
            pass  # Zeus-appen med enhetsnøkkel i Authorization-headeren: ingen cookie å kapre
        elif origin is None or urlparse(origin).netloc != request.host or host not in allowed:
            raise web.HTTPForbidden(text="Feil opphav.")
        sock = web.WebSocketResponse(heartbeat=30)
        await sock.prepare(request)
        hub.clients.add(sock)
        device = request.get("device")
        if device is not None:
            hub.device_socks.setdefault(device["id"], set()).add(sock)
        cid = secrets.token_hex(4)  # hver skjerm (TUI, telefon) får en id, så den kan kjenne igjen egne meldinger
        await sock.send_json({"type": "hello", "id": cid})
        await sock.send_json({"type": "state", "state": hub.state()})
        await sock.send_json({"type": "stats", "stats": hub.stats()})
        from olympus.mediaserver import PLAYER

        await sock.send_json({"type": "player", **PLAYER.status()})  # det PC-en spiller nå (også ingenting)
        if hub.transcript:  # telefonen sov og koblet seg til igjen: vis samtalen som pågår
            await sock.send_json(hub.history())
        for msg in list(hub.approval_msgs.values()):  # Zeus venter på et ja: kortet vises også på den nye skjermen
            await sock.send_json(msg)
        try:
            async for m in sock:
                if m.type == WSMsgType.TEXT:
                    try:
                        msg = json.loads(m.data)
                    except (ValueError, RecursionError):
                        continue
                    if isinstance(msg, dict):
                        with contextlib.suppress(ValueError, TypeError):
                            await hub.handle(msg, cid, device)
        finally:
            hub.clients.discard(sock)
            if device is not None:
                hub.device_socks.get(device["id"], set()).discard(sock)
        return sock

    # --- API for Zeus-appen (og Lab's på PC-en) --------------------------------------------------------
    lab = hub.lab

    async def body(request: web.Request) -> dict:
        try:
            data = await request.json()
        except (ValueError, RecursionError):
            raise web.HTTPBadRequest(text="Ugyldig JSON.") from None
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="Forventet et JSON-objekt.")
        return data

    def who(request: web.Request) -> str:
        d = request.get("device")
        return f"du ({d['name']})" if d else "du"

    async def api_pair(request: web.Request):
        data = await body(request)
        got = lab.pair(str(data.get("code") or ""), str(data.get("name") or "telefon")[:60])
        if got is None:
            return web.json_response({"error": "koden er ugyldig eller utløpt – kjør `olympus pair` på nytt"}, status=403)
        device_id, dev_token = got
        return web.json_response({"device_id": device_id, "token": dev_token, "server": socket.gethostname()})

    async def api_status(_r):
        hub.sync_model()
        info = hub.model_info()
        return web.json_response({"ai": lab.ai_on, "model": info["model"], "cloud": info["cloud"],
                                  "models": hub.manager.loaded(),
                                  "busy": hub.task is not None and not hub.task.done(),
                                  "seq": lab.changes_since(10**12)["seq"], "server": socket.gethostname()})

    async def api_ai(request: web.Request):
        on = bool((await body(request)).get("on"))
        lab.set_ai(on)
        if not on:
            await hub.manager.unload()  # AI av: GPU-en frigjøres med en gang
        await hub.send({"type": "ai", "on": on})
        return web.json_response({"ai": on})

    async def api_models(_r):
        """Modellvalget: {"current": id, "cloud": bool, "choices": [{id, label, cloud, size?, …}],
        "fallback": [{id, label, cloud, available, why}]} (reservekjeden, i rekkefølge)."""
        hub.sync_model()
        if hub.catalog.fetched == 0 or time.monotonic() - hub.catalog.fetched > hub.catalog.TTL:
            asyncio.get_running_loop().create_task(hub.catalog.refresh())  # skylistene hentes i bakgrunnen
        info = hub.model_info()
        return web.json_response({"current": info["model"], "cloud": info["cloud"], "warning": info["warning"],
                                  "choices": hub.catalog.choices(), "fallback": hub.orch.backends.fallback_info()})

    async def api_models_set(request: web.Request):
        wanted = (await body(request)).get("model")
        if not isinstance(wanted, str) or not wanted.strip() or len(wanted) > 200:
            return web.json_response({"error": "model må være en id fra /api/v1/models"}, status=400)
        if hub.task is not None and not hub.task.done():
            return web.json_response({"error": "Zeus svarer nå – bytt når svaret er ferdig"}, status=409)
        info = await hub.set_model(wanted)
        if info is None:
            return web.json_response({"error": "ukjent modell – velg en fra /api/v1/models"}, status=400)
        return web.json_response({"current": info["model"], "cloud": info["cloud"], "warning": info["warning"]})

    # Ny versjon av Zeus-appen: scripts/build_app.sh legger APK-en og versjonen i ~/.olympus/app/, telefonen spør
    # ved oppstart og henter den herfra (samme nøkkel som resten av API-et).
    app_dir = c.home / "app"

    async def api_app(_r):
        try:
            info = json.loads((app_dir / "version.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise web.HTTPNotFound(text="Ingen app-versjon publisert.") from None
        if not (app_dir / "zeus.apk").is_file():
            raise web.HTTPNotFound(text="APK-en mangler.")
        return web.json_response({"code": int(info.get("code", 0)), "name": str(info.get("name", "")),
                                  "size": (app_dir / "zeus.apk").stat().st_size})

    async def api_app_apk(_r):
        apk = app_dir / "zeus.apk"
        if not apk.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(apk, headers={"Content-Type": "application/vnd.android.package-archive",
                                              "Content-Disposition": 'attachment; filename="zeus.apk"'})

    async def api_notify(request):
        """Til telefonens bakgrunnssjekk (hvert 15. min): nedlastinger ferdige siden «since», og om Zeus venter på et ja."""
        import time as _time

        try:
            since = float(request.query.get("since", "0"))
        except ValueError:
            since = 0.0
        since = max(since, _time.time() - 24 * 3600)  # aldri mer enn et døgn tilbake
        done = [{"id": d["id"], "title": d["title"], "kind": d.get("kind"), "updated": d.get("updated")}
                for d in hub.dl.list() if d.get("status") == "ferdig" and float(d.get("updated") or 0) > since]
        return web.json_response({"now": _time.time(), "downloads_done": done[:10],
                                  "approvals_waiting": sum(1 for f in hub.approvals.values() if not f.done())})

    async def api_morning(_r):
        """Morgenoversikten telefonen viser kl. 07:30: dagens avtaler, været, «fortsett», ferdige nedlastinger og
        PC-varsler (full disk, feilet backup). Bare det som har innhold tas med."""
        import json as _json
        import time as _time
        from datetime import date

        lines: list[str] = []
        today = lab.day(date.today().isoformat())
        for e in today:
            lines.append(f"{'hele dagen' if e['all_day'] else e['start'][11:16]}  {e['title']}")
        w = await hub.weather()
        if w and w.get("temp") is not None:
            sym = str(w.get("symbol") or "")
            desc = next((n for k, n in (("rain", "regn"), ("snow", "snø"), ("sleet", "sludd"), ("cloud", "skyet"),
                                         ("fair", "lettskyet"), ("clearsky", "klarvær"), ("fog", "tåke")) if k in sym), "")
            rain = f", {w['rain']} mm" if w.get("rain") else ""
            lines.append(f"{w.get('place') or 'Været'}: {round(w['temp'])}°{', ' + desc if desc else ''}{rain}")
        row = hub.lib.db.execute("SELECT item_id FROM progress WHERE position > 60 AND (duration = 0 OR position < "
                                 "duration * 0.95) ORDER BY updated DESC LIMIT 1").fetchone()
        if row:
            item = hub.lib.book(row["item_id"]) or hub.lib.video(row["item_id"])
            if item:
                lines.append(f"Fortsett: {item.get('title')}")
        day_ago = _time.time() - 24 * 3600
        done = [d for d in hub.dl.list() if d.get("status") == "ferdig" and float(d.get("updated") or 0) > day_ago]
        if done:
            lines.append(f"Ferdig lastet ned: " + ", ".join(d["title"] for d in done[:3]) + (" …" if len(done) > 3 else ""))
        disk = shutil.disk_usage(Path.home())
        if disk.used / disk.total > 0.9:
            lines.append(f"PC-en: disken er {round(100 * disk.used / disk.total)} % full")
        backup = _json.loads(lab.setting("backup_last", "null") or "null")
        if backup and not backup.get("ok"):
            lines.append("PC-en: nattens backup feilet")
        n = len(today)
        title = f"ϟ I dag: {n} {'avtale' if n == 1 else 'avtaler'}" if n else "ϟ God morgen"
        return web.json_response({"title": title, "lines": lines, "events": n})

    async def api_plan_ics(_r):
        """Planen som .ics til andre kalendere (fra og med 30 dager tilbake)."""
        from datetime import date, timedelta

        from olympus.lab import to_ics

        body = to_ics(lab.events((date.today() - timedelta(days=30)).isoformat()))
        return web.Response(text=body, content_type="text/calendar", charset="utf-8",
                            headers={"Content-Disposition": 'attachment; filename="zeus-plan.ics"'})

    async def api_brief(_r):
        return web.json_response(await hub.brief())

    async def api_dashboard(_r):
        """Hjem-fanen (appen og Lab's på PC-en): alt viktig på ett sted, uten AI."""
        from datetime import date

        from olympus.mediaserver import PLAYER

        lib = hub.lib
        brief = await hub.brief()
        rows = lib.db.execute("SELECT item_id, position, duration, updated FROM progress WHERE position > 5 "
                              "AND (duration = 0 OR position < duration * 0.95) ORDER BY updated DESC LIMIT 6").fetchall()
        cont = []
        for r in rows:
            v = lib.video(r["item_id"])
            b = None if v else lib.book(r["item_id"])
            item = v or b
            if item:
                item.pop("path", None)
                item.pop("parts", None)
                cont.append({"type": "video" if v else "book", **item, "position": r["position"], "duration": r["duration"]})
        recent = lib.db.execute("SELECT album_id, album, album_artist, MAX(mtime) AS m, MIN(id) AS t, MAX(cover) AS c "
                                "FROM tracks GROUP BY album_id ORDER BY m DESC LIMIT 6").fetchall()
        import json as _json

        return web.json_response({**brief, "continue": cont, "pc": PLAYER.status(), "library": lib.stats(),
                                  "backup": _json.loads(lab.setting("backup_last", "null")),
                                  "recent_albums": [{"id": r["album_id"], "title": r["album"], "artist": r["album_artist"],
                                                     "cover_track": r["t"] if r["c"] else None} for r in recent],
                                  "next_events": lab.events(date.today().isoformat())[:5]})

    async def api_zeus_warm(_r):
        """Forvarming (appen/Lab's: Zeus-fanen åpnet eller du begynte å skrive): last modellen og regn ut den faste
        starten av prompten nå, så første svar kommer på ~0,5 s i stedet for ~11 s. Svarer med en gang."""
        if not lab.ai_on:
            return web.json_response({"warming": False, "reason": "Zeus er av"})

        import time as _time

        hub.last_used = _time.monotonic()  # forvarming er bruk: ellers frigjorde tomgangsvakten modellen med en gang

        async def go():
            with contextlib.suppress(Exception):
                await hub.orch.warm()
        asyncio.get_running_loop().create_task(go())
        return web.json_response({"warming": True}, status=202)

    async def api_plan(request: web.Request):
        return web.json_response({"events": lab.events(request.query.get("from"), request.query.get("to")),
                                  "seq": lab.changes_since(10**12)["seq"]})

    async def api_changes(request: web.Request):
        try:
            since = int(request.query.get("since", "0"))
        except ValueError:
            raise web.HTTPBadRequest(text="since må være et tall.") from None
        return web.json_response(lab.changes_since(since))

    async def api_plan_add(request: web.Request):
        d = await body(request)
        try:
            client = str(d.get("client") or "")[:64]  # telefonens tmp-id: et nytt forsøk lager ikke en kopi
            if d.get("repeat"):  # gjentatt: svaret er den første, med hvor mange som ble lagt inn
                evs = lab.add_series(str(d.get("title", "")), str(d.get("start", "")), d.get("end"),
                                     bool(d.get("all_day")), str(d.get("note", "")), str(d["repeat"]),
                                     d.get("until") or None, d.get("count"), who=who(request), client=client)
                return web.json_response({**evs[0], "series_count": len(evs)}, status=201)
            ev = lab.add(str(d.get("title", "")), str(d.get("start", "")), d.get("end"), bool(d.get("all_day")),
                         str(d.get("note", "")), who=who(request), client=client)
        except (ValueError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(ev, status=201)

    async def api_plan_edit(request: web.Request):
        d = await body(request)
        try:
            ev = lab.update(request.match_info["id"], who=who(request), **{k: d.get(k) for k in ("title", "start", "end",
                                                                                                  "all_day", "note")})
        except KeyError:
            raise web.HTTPNotFound() from None
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(ev)

    async def api_plan_delete(request: web.Request):
        try:
            if request.query.get("series") == "rest":  # denne og resten av serien
                gone = lab.delete_series(request.match_info["id"], who=who(request))
                return web.json_response({**gone[0], "series_deleted": len(gone)})
            return web.json_response(lab.delete(request.match_info["id"], who=who(request)))
        except KeyError:
            raise web.HTTPNotFound() from None

    async def api_log(_r):
        return web.json_response(lab.log())

    async def api_undo(request: web.Request):
        try:
            return web.json_response(lab.undo(int(request.match_info["seq"]), who=who(request)))
        except (KeyError, ValueError):
            raise web.HTTPNotFound() from None

    async def api_devices(_r):
        return web.json_response(lab.devices())

    async def api_device_delete(request: web.Request):
        if not lab.revoke(request.match_info["id"]):
            raise web.HTTPNotFound()
        for sock in list(hub.device_socks.pop(request.match_info["id"], set())):
            await sock.close(code=4001, message=b"enheten er fjernet")
        return web.json_response({"ok": True})

    @web.middleware
    async def bad_input(request: web.Request, handler):
        """Feil i det klienten sendte (typer, datoer, tall) er 400 – ikke en 500 fra dypt inne i koden."""
        import sqlite3

        try:
            return await handler(request)
        except (ValueError, TypeError, RecursionError, OverflowError, sqlite3.InterfaceError, sqlite3.ProgrammingError) as e:
            if not request.path.startswith("/api/v1/"):
                raise
            return web.json_response({"error": f"ugyldig forespørsel: {str(e)[:120]}"}, status=400)

    app = web.Application(middlewares=[auth, bad_input], client_max_size=2**20)
    app[HUB] = hub
    app.router.add_post("/api/v1/pair", api_pair)
    app.router.add_get("/api/v1/status", api_status)
    app.router.add_get("/api/v1/models", api_models)
    app.router.add_post("/api/v1/models", api_models_set)
    app.router.add_post("/api/v1/ai", api_ai)
    app.router.add_get("/api/v1/brief", api_brief)
    app.router.add_get("/api/v1/app", api_app)
    app.router.add_get("/api/v1/app/apk", api_app_apk)
    app.router.add_get("/api/v1/dashboard", api_dashboard)
    app.router.add_post("/api/v1/zeus/warm", api_zeus_warm)
    app.router.add_get("/api/v1/plan", api_plan)
    app.router.add_get("/api/v1/plan/changes", api_changes)
    app.router.add_get("/api/v1/plan/export.ics", api_plan_ics)
    app.router.add_get("/api/v1/brief/morning", api_morning)
    app.router.add_get("/api/v1/notify", api_notify)
    app.router.add_post("/api/v1/plan", api_plan_add)
    app.router.add_patch("/api/v1/plan/{id}", api_plan_edit)
    app.router.add_delete("/api/v1/plan/{id}", api_plan_delete)
    app.router.add_get("/api/v1/log", api_log)
    app.router.add_post("/api/v1/undo/{seq}", api_undo)
    app.router.add_get("/api/v1/devices", api_devices)
    app.router.add_delete("/api/v1/devices/{id}", api_device_delete)
    app.router.add_get("/api/v1/ws", ws)

    from olympus.web.media_api import add_audio_routes, add_media_routes

    async def media_changed(what: str) -> None:
        await hub.send({"type": "media_changed", "what": what})

    add_media_routes(app, hub.lib, media_changed)
    add_audio_routes(app, hub.lib, media_changed)
    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/sessions", sessions)
    app.router.add_get("/ws", ws)
    app.router.add_static("/", STATIC, show_index=False)
    app.on_shutdown.append(lambda _a: asyncio.to_thread(hub.manager.shutdown))
    app.on_cleanup.append(lambda _a: hub.orch.close())  # MCP-serverne (olymparch-mcp …) stoppes med Olympus

    def backup_root() -> Path:
        return Path(c.get("lab.backup_dir", str(c.home / "backup"))).expanduser()

    async def nightly_backup() -> None:
        import json as _json

        from olympus import backup

        last = _json.loads(hub.lab.setting("backup_last", "null"))
        if not backup.due(last):
            return
        info = await asyncio.to_thread(backup.run_backup, c.home, backup_root(), c.path("sessions_dir"))
        hub.lab.set_setting("backup_last", _json.dumps(info))
        if not info.get("ok"):
            await hub.send({"type": "notice", "text": f"⚠ Nattens backup feilet: {info.get('error', '')}"})

    async def api_backups(_r):
        import json as _json

        from olympus import backup

        return web.json_response({"last": _json.loads(hub.lab.setting("backup_last", "null")),
                                  "backups": backup.list_backups(backup_root()), "dir": str(backup_root())})

    app.router.add_get("/api/v1/backups", api_backups)

    # --- PC-fanen: status og fjernkontroll -----------------------------------------------------------
    async def api_pc_info(_r):
        """Kompakt status for PC-fanen (appen og Lab's) – samme kode som Zeus' pc info (handlingsregisteret)."""
        from olympus.actions import Ctx, _pc_info

        return web.json_response(await _pc_info(Ctx(lab=lab, lib=hub.lib, dl=hub.dl, hub=hub), {}))

    app.router.add_get("/api/v1/pc/info", api_pc_info)

    # Én spiller for alle: hver endring (start, ny sang, pause, stopp) går ut som {"type": "player", …}
    from olympus.mediaserver import PLAYER

    async def player_changed(st: dict) -> None:
        await hub.send({"type": "player", **st})

    async def player_listen(_a):
        PLAYER.listeners.append(player_changed)

    async def player_unlisten(_a):
        with contextlib.suppress(ValueError):
            PLAYER.listeners.remove(player_changed)

    app.on_startup.append(player_listen)
    app.on_cleanup.append(player_unlisten)

    async def api_find(request: web.Request):
        from olympus.sources import search_all

        hits, errors = await search_all(request.query.get("q", ""), request.query.get("kind") or None)
        for h in hits:
            hub.orch.toolbox.found[h.id] = h
        return web.json_response({"results": [h.to_dict() for h in hits], "errors": errors})

    async def api_download_start(request: web.Request):
        d = await body(request)
        res = hub.orch.toolbox.found.get(str(d.get("id", "")))
        if res is None:
            raise web.HTTPNotFound(text="Ukjent treff – søk på nytt.")
        try:
            row = await asyncio.to_thread(hub.dl.start, res.kind, res.title, res.torrent, "" if res.torrent else res.direct,
                                          res.year, res.source, res.page, res.creator, res.extra.get("identifier", ""))
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": f"{type(e).__name__}: {e}"[:200]}, status=502)
        if (runner := hub.dl.runner(row["id"])) is not None:
            asyncio.get_running_loop().run_in_executor(None, runner, row["id"])
        await hub.send({"type": "media_changed", "what": "downloads"})
        return web.json_response(row, status=201)

    app.router.add_get("/api/v1/find", api_find)
    app.router.add_post("/api/v1/downloads", api_download_start)

    # Handlingsregisteret: GET /api/v1/actions og rutene for media, nedlastinger, PC, minne og økter (olympus.actions)
    from olympus.actions import Ctx
    from olympus.web.actions_api import add_action_routes

    def make_ctx(request: web.Request) -> Ctx:
        return Ctx(lab=hub.lab, lib=hub.lib, dl=hub.dl, hub=hub, memory=hub.orch.memory, who=who(request),
                   found=hub.orch.toolbox.found)

    add_action_routes(app, make_ctx)

    async def download_watcher() -> None:
        """Transmission spørres hvert 5. sekund når noe lastes ned (ellers hvert 20.); ferdige importeres."""
        while True:
            active = 1
            with contextlib.suppress(Exception):  # én databasefeil skal aldri stoppe importen for godt
                if await hub.dl.poll():
                    await hub.send({"type": "media_changed", "what": "library"})
                active = hub.dl.db.execute("SELECT count(*) FROM downloads WHERE status IN ('laster ned', 'lastet ned', 'deler')").fetchone()[0]
            await asyncio.sleep(5 if active else 20)

    async def download_broadcaster() -> None:
        """{"type": "downloads", "items": […]} til alle skjermer når framdrift/status endrer seg – høyst én gang i sekundet."""
        from olympus.downloads import ACTIVE

        def snapshot() -> tuple[list[dict], list[tuple]]:
            items = hub.dl.list()
            return items, [(d["id"], d["status"], round(float(d["progress"] or 0), 3), d["error"], d["title"]) for d in items]

        last = None
        busy = False
        with contextlib.suppress(Exception):
            last = snapshot()[1]
        while True:
            # Hvert sekund bare mens noe lastes ned; ellers hvert 5. (før: hvert sekund døgnet rundt)
            await asyncio.sleep(1 if busy else 5)
            with contextlib.suppress(Exception):
                items, sig = snapshot()
                if sig != last:
                    await hub.send({"type": "downloads", "items": items})
                last = sig
                busy = any(d["status"] in ACTIVE for d in items)

    async def media_scanner() -> None:
        # Nye filer (fra Zeus, kopiering, ripping) dukker opp av seg selv: rask sjekk av endringstider hvert 5. min.
        # Samme løkke tar nattens backup (etter kl. 03). Ikke mens et spill kjører (spillmodus): da venter begge.
        from olympus.gpu import gaming

        while True:
            if gaming():
                await asyncio.sleep(60)
                continue
            with contextlib.suppress(Exception):
                await nightly_backup()
            with contextlib.suppress(Exception):
                before = hub.lib.stats()
                await asyncio.to_thread(hub.lib.scan)
                art = await asyncio.to_thread(hub.lib.fetch_art)  # cover/plakater til det som mangler
                if hub.lib.stats() != before or any(art.values()):
                    await hub.send({"type": "media_changed", "what": "library"})
            await asyncio.sleep(300)

    async def media_keys(_a):
        """Nøkler fra auth.json til biblioteket (TMDb-info/plakater)."""
        from olympus.providers import load_auth

        hub.lib.tmdb_key = str((load_auth(c).get("tmdb") or {}).get("key") or "")

    app.on_startup.append(media_keys)

    async def start_cast(_a):
        """TV-casting: egen strømmeport på hjemmenettet (bare /cast/<nøkkel>/…), se olympus.cast."""
        from olympus.cast import CASTER, cast_app, lan_ip
        from olympus.web.media_api import stream_remux, stream_transcode, subtitle_bytes

        CASTER.lib = hub.lib
        CASTER.host = str(c.get("cast.host", "") or "") or lan_ip()
        CASTER.port = int(c.get("cast.port", 8789))
        if not CASTER.host or not c.get("cast.enabled", True):
            return
        runner = web.AppRunner(cast_app(stream_transcode, stream_remux, subtitle_bytes), access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, CASTER.host, CASTER.port).start()
            _a["cast_runner"] = runner
        except OSError as e:
            await runner.cleanup()
            print(f"TV-casting av: fikk ikke åpnet {CASTER.host}:{CASTER.port} ({e})", file=sys.stderr)
            CASTER.host = None

    async def stop_cast(_a):
        from olympus.cast import CASTER

        await asyncio.to_thread(CASTER.close)
        if "cast_runner" in _a:
            await _a["cast_runner"].cleanup()

    app.on_startup.append(start_cast)
    app.on_cleanup.append(stop_cast)

    async def start_unloader(_a):
        _a["unloader"] = asyncio.create_task(hub.idle_unloader())
        _a["gamewatch"] = asyncio.create_task(hub.game_watch())
        _a["scanner"] = asyncio.create_task(media_scanner())
        _a["downloads"] = asyncio.create_task(download_watcher())
        _a["dl_broadcast"] = asyncio.create_task(download_broadcaster())

    async def stop_unloader(_a):
        _a["unloader"].cancel()
        _a["gamewatch"].cancel()
        _a["scanner"].cancel()
        _a["downloads"].cancel()
        _a["dl_broadcast"].cancel()

    app.on_startup.append(start_unloader)
    app.on_cleanup.append(stop_unloader)
    return app


def hosts(c: cfg.Config) -> list[str]:
    """Standard: bare localhost og Tailscale-adressen. web.host (eller $OLYMPUS_WEB_HOST, f.eks. i Docker) overstyrer –
    alt bak innlogging/paring uansett."""
    want = os.environ.get("OLYMPUS_WEB_HOST") or c.get("web.host", "auto")
    if want != "auto":
        return [want]
    ts = tailscale_ip()
    return ["127.0.0.1", *([ts] if ts else [])]


def show_link(c: cfg.Config) -> None:
    """Lenken med nøkkelen (og QR-kode hvis qrencode finnes) – bare til terminalen, aldri til journalen."""
    hs = hosts(c)
    url_host = hs[-1] if len(hs) > 1 else socket.gethostname()
    url = f"http://{url_host}:{int(c.get('web.port', 8787))}/?t={get_token(c)}"
    print(f"Olympus på telefonen: {url}")
    if len(hs) == 1:
        print("  (Tailscale er ikke oppe – bare tilgjengelig på denne maskinen. Se `olympus web --help`.)")
    try:
        qr = subprocess.run(["qrencode", "-t", "ansiutf8", url], capture_output=True, text=True, timeout=5).stdout
        if qr:
            print(qr)
    except (OSError, subprocess.SubprocessError):
        pass


def pair_url(c: cfg.Config) -> str:
    """zeus://pair?… – Zeus-appen åpner denne fra QR-koden: adressen til Olympus og en engangskode (10 min)."""
    from urllib.parse import urlencode

    from olympus.lab import open_lab

    hs = hosts(c)
    host = hs[-1] if len(hs) > 1 else "127.0.0.1"
    code = open_lab(c.home).new_pair_code()
    return "zeus://pair?" + urlencode({"h": f"{host}:{int(c.get('web.port', 8787))}", "c": code,
                                       "n": socket.gethostname()})


def show_pair(c: cfg.Config) -> None:
    url = pair_url(c)
    print("Par Zeus-appen: skann koden med kameraet på telefonen (gyldig i 10 minutter, kan brukes én gang).\n")
    try:
        qr = subprocess.run(["qrencode", "-t", "ansiutf8", url], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        qr = ""
    if not qr:
        with contextlib.suppress(ImportError):
            import io

            import qrcode

            buf = io.StringIO()
            q = qrcode.QRCode(border=1)
            q.add_data(url)
            q.print_ascii(out=buf, invert=True)
            qr = buf.getvalue()
    print(qr or "(installer qrencode for QR-kode)")
    print(f"\nEller lim inn i appen: {url}")


def run(c: cfg.Config) -> None:
    port = int(c.get("web.port", 8787))
    hs = hosts(c)
    if sys.stdout.isatty():
        show_link(c)
    else:  # som tjeneste havner utskrift i journalen: ikke legg nøkkelen der (`olympus web --link` viser den)
        print(f"Olympus på telefonen lytter på {', '.join(hs)}:{port}")
    web.run_app(make_app(c), host=hs, port=port, print=None)


__all__ = ["make_app", "run"]
