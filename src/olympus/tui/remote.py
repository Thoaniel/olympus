"""TUI-en som skjerm mot den felles hjernen (olympus-web): samme samtale og samme modell som telefonen.

RemoteOrchestrator og RemoteManager oppfører seg som de lokale klassene for det TUI-en bruker, men samtalen kjøres
i hjernen og hendelsene strømmes hit. Alt som bare er oppslag i config/filer (backends, minne, lekser …) leses
lokalt gjennom den lokale orkestratoren. Turer som startes på telefonen vises her også («følg»).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

import aiohttp

from olympus.web.wire import approval_from_wire, call_from_wire, from_wire

DONE = object()


class BrainClient:
    def __init__(self, url: str, token: str, on_message: Callable[[dict], Any]) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.on_message = on_message
        self.id = ""
        self.session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.task: asyncio.Task | None = None

    async def connect(self, timeout: float = 2.0) -> bool:
        from olympus.web.server import COOKIE

        self.session = aiohttp.ClientSession(headers={"Cookie": f"{COOKIE}={self.token}"})
        try:
            self.ws = await asyncio.wait_for(self.session.ws_connect(f"{self.url}/ws", origin=self.url, heartbeat=30),
                                             timeout)
            hello = await asyncio.wait_for(self.ws.receive_json(), timeout)
            self.id = hello.get("id", "")
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
            await self.close()
            return False
        self.task = asyncio.create_task(self._listen())
        return True

    async def _listen(self) -> None:
        assert self.ws is not None
        async for m in self.ws:
            if m.type == aiohttp.WSMsgType.TEXT:
                with contextlib.suppress(ValueError):
                    res = self.on_message(m.json())
                    if asyncio.iscoroutine(res):
                        await res
        self.on_message({"type": "disconnected"})

    async def send(self, msg: dict) -> None:
        if self.ws is not None and not self.ws.closed:
            await self.ws.send_json(msg)

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
        if self.ws is not None:
            await self.ws.close()
        if self.session is not None:
            await self.session.close()


class RemoteOrchestrator:
    """Orkestrator-fasade: kjører turer i hjernen. Ukjente attributter hentes fra den lokale orkestratoren
    (backends, minne, lekser, verktøy, mappe …), som bare brukes til oppslag."""

    def __init__(self, local: Any, client: BrainClient) -> None:
        object.__setattr__(self, "_local", local)
        object.__setattr__(self, "client", client)
        object.__setattr__(self, "stats", {})
        object.__setattr__(self, "queue", None)
        object.__setattr__(self, "approval_ids", {})

    def __getattr__(self, name: str) -> Any:
        return getattr(self._local, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("stats", "queue", "approval_ids"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._local, name, value)

    # --- tilstand fra hjernen ---
    @property
    def messages(self) -> list:
        return [None] * int(self.stats.get("messages", 0))  # bare antallet brukes her

    def context_used(self) -> tuple[int, int]:
        c = self.stats.get("context") or [0, 0]
        return int(c[0]), int(c[1])

    @property
    def last_model(self):
        return self.stats.get("last_model")

    @property
    def session_tokens(self) -> dict:
        return self.stats.get("session_tokens", {})

    @property
    def last_usage(self) -> dict:
        return self.stats.get("last_usage", {})

    @property
    def active_agent(self) -> str:
        return self.stats.get("active_agent") or self._local.active_agent

    @property
    def compressions(self) -> int:
        return int(self.stats.get("compressions", 0))

    @property
    def last_answers(self) -> list:
        return [tuple(a) for a in self.stats.get("last_answers", [])]

    async def teach(self, *_a, **_k):
        return None  # trening skjer ikke med 27B lokalt

    def reset(self) -> None:
        self._local.reset()
        asyncio.ensure_future(self.client.send({"type": "new"}))

    async def _maybe_compress(self, _agent: str, force: bool = False) -> AsyncIterator:
        await self.client.send({"type": "command", "name": "komprimer"})
        if False:  # pragma: no cover – gjør dette til en async generator; svaret kommer som melding fra hjernen
            yield None

    # --- turer ---
    def feed(self, item: Any) -> None:
        if self.queue is not None:
            self.queue.put_nowait(item)

    async def run(self, text: str, *, approve=None) -> AsyncIterator:
        self.queue = asyncio.Queue()
        await self.client.send({"type": "send", "text": text})
        async for ev in self._consume(approve):
            yield ev

    async def follow(self, approve=None) -> AsyncIterator:
        """En tur som er startet et annet sted (telefonen): vis den her også."""
        if self.queue is None:
            self.queue = asyncio.Queue()
        async for ev in self._consume(approve):
            yield ev

    async def _consume(self, approve) -> AsyncIterator:
        try:
            while True:
                item = await self.queue.get()
                if item is DONE:
                    return
                if isinstance(item, dict) and item.get("type") == "approval":
                    call, appr = call_from_wire(item["call"]), approval_from_wire(item["approval"])
                    self.approval_ids[call.id] = item["id"]
                    answer = await approve(call, appr) if approve is not None else "no"
                    if answer != "__done__":
                        await self.client.send({"type": "approve", "id": item["id"], "answer": answer})
                    continue
                yield item
        finally:
            self.queue = None


class RemoteManager:
    """Modellhåndtering-fasade: hjernen eier modellene; her vises bare hva som er lastet."""

    def __init__(self, local: Any, orch: RemoteOrchestrator) -> None:
        self._local, self._orch = local, orch

    def loaded(self) -> list[str]:
        return list(self._orch.stats.get("loaded", []))

    def usage(self) -> list[tuple]:
        return [tuple(u) for u in self._orch.stats.get("usage", [])]

    def footprint_gb(self, model_id: str) -> float:
        return self._local.footprint_gb(model_id)

    async def ensure(self, model_id: str):
        return None  # hjernen laster selv når den trenger det

    async def unload(self, model_id: str | None = None) -> list[str]:
        await self._orch.client.send({"type": "command", "name": "unload", "model": model_id or ""})
        return []

    def shutdown(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._local, name)


def handle_turn_message(orch: RemoteOrchestrator, msg: dict) -> bool:
    """Legg meldinger som hører til en pågående tur i køen. True = håndtert her."""
    kind = msg.get("type")
    if kind == "ev":
        orch.feed(from_wire(msg["ev"]))
        return orch.queue is not None
    if kind == "approval":
        orch.feed(msg)
        return orch.queue is not None
    if kind == "done":
        if msg.get("stats"):
            orch.stats = msg["stats"]
        orch.feed(DONE)
        return True
    return False
