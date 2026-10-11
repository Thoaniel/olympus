"""Modellbehandler: laster og fjerner språkmodeller etter behov innenfor et
VRAM-budsjett, med LRU-utkastelse.

Hver lastet modell er en egen llama-server-prosess på sin egen port.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from olympus.backends.llamacpp import LlamaServer, port_open, port_serves
from olympus.config import Config
from olympus.gpu import GiB, read_vram


class VramError(RuntimeError):
    pass


@dataclass
class Slot:
    model_id: str
    server: LlamaServer
    size_gb: float  # GiB, målt etter lasting når mulig
    last_used: float
    measured: bool = False


StatusCallback = Callable[[str], None]


class ModelManager:
    def __init__(self, config: Config, on_status: StatusCallback | None = None) -> None:
        self.config = config
        self.on_status = on_status or (lambda _msg: None)
        self.slots: OrderedDict[str, Slot] = OrderedDict()
        self._lock = asyncio.Lock()

    # --- info ------------------------------------------------------------
    def loaded(self) -> list[str]:
        return list(self.slots)

    def footprint_gb(self, model_id: str) -> float:
        """Anslått VRAM i GiB. size_gb i config er desimal-GB (som på Hugging Face)."""
        m = self.config.model(model_id)
        weights = float(m.get("size_gb", 0.0)) * 1e9 / GiB
        return weights + float(self.config.get("vram.overhead_gb", 0.8))

    def usage(self) -> list[tuple[str, float, bool]]:
        """(modell, GiB, målt?) for hver lastet modell, eldst brukt først."""
        return [(s.model_id, s.size_gb, s.measured) for s in self.slots.values()]

    def _keep_warm(self) -> set[str]:
        return set(self.config.get("vram.keep_warm", []))

    def _our_usage_gb(self) -> float:
        return sum(s.size_gb for s in self.slots.values())

    PORT_RANGE = 16

    def _ports(self) -> range:
        base = int(self.config.get("llamacpp.port", 8770))
        return range(base, base + self.PORT_RANGE)

    def _free_port(self) -> int:
        used = {s.server.port for s in self.slots.values()}
        host = self.config.get("llamacpp.host")
        for port in self._ports():
            if port not in used and not port_open(host, port):
                return port
        raise VramError(f"Ingen ledige porter i {self._ports().start}–{self._ports().stop - 1}.")

    def _find_shared(self, model_id: str) -> LlamaServer | None:
        """En llama-server med modellen som en annen Olympus-instans allerede kjører."""
        host = self.config.get("llamacpp.host")
        ours = {s.server.port for s in self.slots.values()}
        for port in self._ports():
            if port not in ours and port_serves(host, port, model_id):
                server = self._make_server(model_id)
                server.port = port
                server.external = True
                return server
        return None

    def other_instances(self) -> list[str]:
        """Modeller som andre Olympus-instanser har lastet (for feilmeldinger)."""
        host = self.config.get("llamacpp.host")
        ours = {s.server.port for s in self.slots.values()}
        found = []
        for port in self._ports():
            if port in ours or not port_open(host, port):
                continue
            for mid, m in self.config.get("models", {}).items():
                if m.get("kind") == "llm" and port_serves(host, port, mid):
                    found.append(mid)
        return found

    def _smaller_alternatives(self, model_id: str) -> list[str]:
        m = self.config.model(model_id)
        need = self.footprint_gb(model_id)
        return [
            mid
            for mid, other in self.config.get("models", {}).items()
            if mid != model_id
            and other.get("repo") == m.get("repo")
            and self.footprint_gb(mid) < need
        ]

    def check_fit(self, model_id: str) -> None:
        """Hev VramError med et konkret forslag dersom modellen ikke får plass
        selv etter at alle Olympus-modeller er fjernet."""
        need = self.footprint_gb(model_id)
        budget = float(self.config.get("vram.budget_gb", 15.0))
        msg: list[str] = []
        if need > budget:
            msg.append(f"{model_id} trenger ~{need:.1f} GB, VRAM-budsjettet er {budget:.1f} GB.")
        vram = read_vram()
        if vram is not None:
            # VRAM brukt av andre enn Olympus (skrivebord, Ollama, spill …)
            foreign = max(0.0, vram.used_gb - self._our_usage_gb())
            avail = vram.total_gb - foreign
            if need > avail:
                others = self.other_instances()
                who = (
                    f"en annen Olympus-instans har lastet {', '.join(others)}"
                    if others
                    else "andre programmer (spill, Ollama, ComfyUI …)"
                )
                msg.append(
                    f"{model_id} trenger ~{need:.1f} GB, men bare {avail:.1f} GB er ledig — "
                    f"{foreign:.1f} GB brukes av {who}."
                )
        if msg:
            alts = self._smaller_alternatives(model_id)
            if alts:
                msg.append("Prøv en mindre kvantisering: " + ", ".join(alts) + " (`/model <id>`).")
            raise VramError("\n".join(msg))

    # --- lasting ---------------------------------------------------------
    async def ensure(self, model_id: str) -> LlamaServer:
        """Sørg for at modellen er lastet og returner serveren."""
        async with self._lock:
            slot = self.slots.get(model_id)
            if slot is not None and slot.server.running():
                slot.last_used = time.monotonic()
                self.slots.move_to_end(model_id)
                return slot.server
            if slot is not None:  # prosessen har dødd
                self.slots.pop(model_id)

            shared = await asyncio.to_thread(self._find_shared, model_id)
            if shared is not None:
                self.on_status(f"Bruker {model_id} fra en annen Olympus-instans")
                self.slots[model_id] = Slot(model_id, shared, 0.0, time.monotonic(), True)
                return shared

            need = self.footprint_gb(model_id)
            if not self.slots:
                # En prosess som nettopp ble stoppet (forrige Olympus, en test) kan holde VRAM i noen sekunder til.
                # Vent på den før vi sier at modellen ikke får plass.
                await self._wait_free(need, timeout=float(self.config.get("vram.release_wait", 20)))
            self.check_fit(model_id)
            budget = float(self.config.get("vram.budget_gb", 15.0))
            while self.slots and self._our_usage_gb() + need > budget:
                await self._evict_lru()
            while self.slots and not await self._wait_free(need):
                await self._evict_lru()
            # Også uten egne modeller å kaste ut: en prosess som nettopp ble stoppet (forrige Olympus, en test) kan
            # holde VRAM i noen sekunder til. llama.cpp tilpasser seg det som er ledig ved oppstart – starter vi for
            # tidlig, havner modellen stille på CPU-en og blir mange ganger tregere.
            await self._wait_free(need, timeout=float(self.config.get("vram.release_wait", 20)))

            for attempt in (1, 2):
                server = self._make_server(model_id)
                self.on_status(f"Laster {model_id} …")
                before = read_vram()
                await server.start()
                try:
                    await server.wait_healthy(float(self.config.get("llamacpp.startup_timeout", 180)))
                except BaseException:
                    server.stop()
                    self.on_status(f"{model_id} avbrutt")
                    raise
                after = read_vram()
                spilled = self._spilled(model_id, before, after)
                if not spilled or attempt == 2:
                    break
                # Fikk ikke plass på GPU-en: vent til minnet er frigjort og prøv én gang til.
                self.on_status(f"{model_id} fikk ikke plass på GPU-en – venter på ledig minne og prøver igjen …")
                server.stop()
                await asyncio.sleep(2)
                await self._wait_free(need, timeout=30)
            if spilled:
                self.on_status(f"⚠ {model_id} kjører delvis på CPU (bare {spilled:.1f} GiB på GPU-en) – svarene blir "
                               "trege. Lukk andre programmer som bruker GPU-en, eller velg en mindre modell.")
            size, measured = need, False
            if before is not None and after is not None:
                delta = (after.used - before.used) / GiB
                # Forkast urimelige målinger (andre programmer kan ha endret bruk samtidig).
                if 0.5 * need < delta < 1.5 * need:
                    size, measured = delta, True
            self.slots[model_id] = Slot(model_id, server, size, time.monotonic(), measured)
            self.on_status(f"{model_id} klar")
            return server

    def _make_server(self, model_id: str) -> LlamaServer:
        c = self.config
        return LlamaServer(
            binary=c.get("llamacpp.binary"),
            model_path=c.model_path(model_id),
            host=c.get("llamacpp.host"),
            port=self._free_port(),
            ctx_size=int(c.get("llamacpp.ctx_size")),
            gpu_layers=int(c.get("llamacpp.gpu_layers")),
            extra_args=list(c.get("llamacpp.extra_args", [])) + self._model_args(model_id) + self._lora_args(model_id),
            log_path=c.path("logs_dir") / f"llama-{model_id}.log",
            alias=model_id,
        )

    async def _wait_free(self, need: float, timeout: float = 5.0) -> bool:
        """Vent til minst `need` GiB er ledig. Driveren frigjør VRAM litt etter at
        prosessen er borte, så uten venting ville vi kastet ut én modell for mye."""
        deadline = time.monotonic() + timeout
        while True:
            vram = read_vram()
            if vram is None or vram.free_gb >= need:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.25)

    def _spilled(self, model_id: str, before, after) -> float:
        """GiB som faktisk havnet på GPU-en, hvis det er klart mindre enn modellfilen (ellers 0 = alt i orden)."""
        if before is None or after is None:
            return 0.0
        try:
            weights = self.config.model_path(model_id).stat().st_size / GiB
        except (OSError, KeyError):
            return 0.0
        on_gpu = (after.used - before.used) / GiB
        return on_gpu if on_gpu < 0.75 * weights else 0.0

    def _model_args(self, model_id: str) -> list[str]:
        """Egne serverargumenter per modell: models.<id>.args (f.eks. MTP: ["--spec-type", "draft-mtp"]) og
        models.<id>.draft (en liten modell med samme tokenizer som gjetter tokens, spekulativ dekoding)."""
        m = self.config.model(model_id)
        args = [str(a) for a in m.get("args", [])]
        draft = m.get("draft")
        if draft:
            try:
                path = self.config.model_path(draft)
            except KeyError:
                path = None
            if path is not None and path.exists():
                args += ["--model-draft", str(path), "-ngld", "99"]
        return args

    def _lora_args(self, model_id: str) -> list[str]:
        """Trent LoRA-adapter for modellen (models.<id>.lora), hvis den finnes og er slått på."""
        m = self.config.model(model_id)
        lora = m.get("lora")
        if not lora or not m.get("lora_enabled", True):
            return []
        p = Path(lora).expanduser()
        return ["--lora", str(p)] if p.exists() else []

    async def _evict_lru(self) -> None:
        """Fjern minst nylig brukte modell, men spar varme modeller så lenge det går."""
        warm = self._keep_warm()
        victim = next((m for m in self.slots if m not in warm), next(iter(self.slots)))
        model_id, slot = victim, self.slots.pop(victim)
        self.on_status(f"Fjerner {model_id} fra VRAM …")
        await asyncio.to_thread(slot.server.stop)

    async def unload(self, model_id: str | None = None) -> list[str]:
        """Fjern én eller alle modeller. Returnerer hvilke som ble fjernet."""
        async with self._lock:
            ids = [model_id] if model_id else list(self.slots)
            removed = []
            for mid in ids:
                slot = self.slots.pop(mid, None)
                if slot is not None:
                    await asyncio.to_thread(slot.server.stop)
                    removed.append(mid)
            return removed

    def shutdown(self) -> None:
        for slot in self.slots.values():
            slot.server.stop()
        self.slots.clear()
