"""Orkestrator: Zeus svarer, med verktøy og godkjenning. Gir en strøm av hendelser som TUI-en, webhuben og
CLI-et viser hver på sin måte.

Zeus er alltid hjernen på PC-en (minne, verktøy, plan, godkjenninger, økter). Modellen som skriver svarene er
agents.zeus.model – lokal, eller en skymodell brukeren har byttet til (/modell)."""

from __future__ import annotations

import json
from datetime import datetime
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from olympus.backends.llamacpp import Chunk
from olympus.chat import system_prompt
from olympus.config import ZEUS, Config
from olympus.model_manager import ModelManager
from olympus.providers import Backends, QuotaError
from olympus.tools import OUTBOUND_AFTER_WEB, Approval, Toolbox, ToolCall, ToolResult

# Spør brukeren om et verktøykall. Svar: "yes" | "no" | "always" (resten av økten).
Approver = Callable[[ToolCall, Approval], Awaitable[str]]

TOOL_GUIDE = """## Verktøy
Du har verktøy for å lese filer, søke, endre filer og kjøre kommandoer i brukerens arbeidsmappe.
- Les relevante filer og se på faktiske feilmeldinger før du foreslår endringer. Ikke gjett innholdet i en fil.
- Endringer og kommandoer som påvirker systemet krever brukerens godkjenning; Olympus spør automatisk.
  Ikke spør om lov i teksten først («Skal jeg opprette …?») – kall verktøyet direkte, så får brukeren
  godkjenningen med selve innholdet foran seg.
- Bare lese? Kjør én enkel kommando uten rør, omdirigering eller anførselstegn (`df -h /`, `systemctl status x`,
  `journalctl -u x -n 50`, `docker ps`, `tailscale status`) – den kjøres med en gang. Med `|`, `>`, `2>&1`, `&&`
  eller `;` må brukeren godkjenne hver gang. Utdata kuttes uansett, så du trenger ikke `head`/`tail`.
- Hvis brukeren avslår et kall, ikke prøv det samme igjen. Forklar heller hva brukeren kan gjøre selv.
- Bruk edit_file for små endringer og write_file bare for nye filer eller full omskriving.
- Når brukeren ber deg LAGE noe (nettside, skript, program, prosjekt, config): skriv filene med write_file
  i arbeidsmappen (eller en undermappe med et passende navn) i stedet for å lime inn koden i svaret.
- Varig minne: lærer du noe om brukeren som vil gjelde senere (preferanser, oppsett, navn), lagre det med
  memory (target=user). Lærdom om miljøet og hva som virket → target=memory. Ikke lagre midlertidige detaljer.
- Viser brukeren til noe fra tidligere samtaler, bruk session_search før du gjetter.
- Avslutt med et kort svar til brukeren om hva du fant eller gjorde: hvilke filer, hvor, og hvordan de brukes
  (f.eks. «Åpne med `xdg-open nettside/index.html`»). Ikke gjenta hele filinnholdet i svaret."""

NO_TOOLS_GUIDE = """## Filer
Du kan ikke skrive filer selv her. Når du lager filer (nettside, skript, prosjekt), skriv hver fil som en egen
kodeblokk med filstien etter språket på første linje, f.eks. ```html nettside/index.html – da kan brukeren lagre
alle filene med ett klikk. Bruk relative stier."""


@dataclass
class Event:
    """kind: status | agent_start | chunk | round | tool_call | tool_result | agent_end | notice | judge_rejected
    (judge_rejected: et svar som påsto at noe var gjort uten verktøy, forkastes og gjøres på nytt – navnet beholdes
    fordi appene allerede viser det som «forkastet»)."""

    kind: str
    agent: str = ""
    text: str = ""
    model: str = ""
    chunk: Chunk | None = None
    call: ToolCall | None = None
    result: ToolResult | None = None
    value: int = 0
    total: int = 0


@dataclass
class Orchestrator:
    config: Config
    manager: ModelManager
    cwd: Path = field(default_factory=Path.cwd)
    messages: list[dict[str, Any]] = field(default_factory=list)
    active_agent: str = ZEUS
    last_usage: dict[str, int] = field(default_factory=dict)
    last_ctx_limit: int = 0
    backends: Backends = field(init=False)
    toolbox: Toolbox = field(init=False)
    lessons: Any = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.backends = Backends(self.config, self.manager)
        self.backends.cwd = self.cwd
        from olympus.library import Library

        self.toolbox = Toolbox(self.cwd, timeout=float(self.config.get("tools.shell_timeout", 120)))
        from olympus.lab import open_lab

        self.toolbox.lab = open_lab(self.config.home)
        from olympus.medialib import open_library

        self.toolbox.lib = open_library(self.config)
        from olympus.config import media_dir

        self.toolbox.media_dirs = {k: media_dir(self.config, k) for k in ("music", "audiobook", "podcast")}
        self.toolbox.library = Library(self.config)
        from olympus.lessons import Lessons

        self.lessons = Lessons(self.config)
        import asyncio as _asyncio

        self.warm_lock = _asyncio.Lock()
        # Velgeren (olympus.router): et hint om sannsynlig verktøy på brukermeldingen. Kan slås av/på i kjøring.
        self.router_on = bool(self.config.get("tools.router", True))
        self.toolbox.undo_instead = bool(self.config.get("tools.undo_instead", True))
        # Siste svar per runde: (agent, modell, spørsmål, svar). Brukes av /feil.
        self.last_answers: list[tuple[str, str, str, str]] = []
        self.last_ctx_limit = int(self.config.get("llamacpp.ctx_size", 0))
        # Kontekst som i Hermes: varig minne (frosset per økt), søk i tidligere økter, komprimering ved behov.
        from olympus.memory import MemoryStore
        from olympus.sessions import SessionIndex, SessionStore

        self.memory = MemoryStore(self.config.home / "memory",
                                  {"memory": int(self.config.get("memory.agent_chars", 2200)),
                                   "user": int(self.config.get("memory.user_chars", 1375))})
        self.memory_snapshot = self.memory.snapshot()
        self.toolbox.memory = self.memory
        self.toolbox.sessions = SessionIndex(SessionStore(self.config.path("sessions_dir")))
        self.last_model: str | None = None  # modellen som svarte sist (for kostnadspanelet)
        # MCP-klienten: verktøy fra lokale MCP-servere (olymparch-mcp m.fl.). Serverne startes først når de trengs.
        from olympus.mcp_client import McpHub

        self.mcp = McpHub(self.config)
        self.toolbox.mcp = self.mcp
        self.turn_mcp: list[str] = []  # MCP-verktøyene som tilbys i denne runden
        self.session_tokens: dict[str, int] = {}  # tokens i økta per kostnadsklasse (gratis, betalt …)

    def tainted(self) -> bool:
        """Har samtalen fått inn innhold utenfra (nettside, søk, MCP-resultat)? Da gjelder strengere godkjenning
        resten av samtalen (olympus.tools.Toolbox.approval)."""
        return any(m.get("_tainted") for m in self.messages)

    def reset(self) -> None:
        self.messages.clear()
        self.toolbox.web_tainted = False
        self.last_usage = {}
        self.last_model = None
        self.session_tokens = {}
        self.memory_snapshot = self.memory.snapshot()  # ny økt: nytt øyeblikksbilde av minnet
        self.toolbox.session_allow.clear()

    async def close(self) -> None:
        """Stopp det orkestratoren har startet selv (MCP-serverne). Modellene stoppes av ModelManager."""
        await self.mcp.close()

    def tool_names(self, agent: str, model_id: str) -> list[str]:
        if not self.config.get("tools.enabled", True) or not self.backends.supports_tools(model_id):
            return []
        return list(self.config.agent(agent).get("tools", []))

    def context_used(self) -> tuple[int, int]:
        return int(self.last_usage.get("total_tokens", 0)), self.last_ctx_limit

    async def _helper_call(self, fn):
        """Kjør en hjelpejobb (oppsummering) med Zeus' modell; svarer ikke skymodellen, prøves den lokale.
        Returnerer (resultat, eventuell melding)."""
        import asyncio

        ref, _ = self.backends.resolve(ZEUS)
        chain = [ref] + [m for m in self.backends.fallbacks() if m != ref and self.backends.available(m)[0]]
        limit = float(self.config.get("compression.cloud_timeout", 30))
        last: QuotaError | None = None
        for i, model in enumerate(chain):
            try:
                server = await self.backends.get(model)
                if self.backends.is_cloud(model):
                    result = await asyncio.wait_for(fn(server), limit)
                else:
                    result = await fn(server)
                return result, None if i == 0 else f"{last} Brukte {model}."
            except QuotaError as e:
                last = e
            except TimeoutError:
                last = QuotaError(f"{model} svarte ikke innen {limit:.0f} s.")
        assert last is not None
        raise last

    # --- hovedløp --------------------------------------------------------
    async def run(self, text: str, *, approve: Approver | None = None) -> AsyncIterator[Event]:
        agent = ZEUS
        self.last_answers = []
        # Komprimer først hvis samtalen nærmer seg modellens grense.
        async for ev in self._maybe_compress(agent):
            yield ev
        self.messages.append({"role": "user", "content": text})
        outputs: list[tuple[str, str]] = []
        self.turn_tools: list[dict[str, str]] = []
        self.turn_denied: set[tuple[str, str]] = set()  # (verktøy, handling) brukeren har sagt nei til i denne runden
        self.turn_denials = 0
        self._batch_yes, self._batch_no = {}, set()
        # Innhold utenfra (nettet, MCP) gjelder hele samtalen – også etter lagring/åpning av økta (_tainted i historikken)
        self.toolbox.web_tainted = self.tainted()
        try:
            spec = self.config.agent(agent)
            model_id, why = self.backends.resolve(agent)
            if why:
                yield Event("notice", agent=agent, text=f"{spec['display']} bruker {model_id} fordi {why}.")
            payload = self._payload(agent, text, model_id)
            self.turn_mcp = []
            if self.mcp.enabled and self.tool_names(agent, model_id):
                # Bare noen få MCP-verktøy per runde: når meldingen handler om PC-en, eller de står i «always»
                self.turn_mcp, notes = await self.mcp.select(text)
                for note in notes:
                    yield Event("notice", agent=agent, text=note)
                if self.turn_mcp and payload[-1]["role"] == "user":
                    servers = sorted({t.server for n in self.turn_mcp if (t := self.mcp.get(n))})
                    payload[-1] = {**payload[-1], "content": payload[-1]["content"] + (
                        f"\n\n(PC-verktøy fra {', '.join(servers)} er med denne gangen (mcp_*): bruk dem for "
                        "innstillinger, vinduer, apper, varsler og pakker i stedet for shell.)")}
            attempt, answer = 0, ""
            while True:
                self.active_agent = agent
                self.last_ctx_limit = self.backends.context_limit(model_id)
                yield Event("agent_start", agent=agent, model=model_id, value=attempt)
                reply: list[str] = []
                tools_before = len(self.turn_tools)
                async for ev in self._agent_loop(agent, model_id, payload, reply, approve):
                    if ev.kind == "agent_start":
                        model_id = ev.model
                    yield ev
                answer = "".join(reply)
                yield Event("agent_end", agent=agent, model=model_id)
                wrote = any(is_write(t) for t in self.turn_tools[tools_before:])
                refused = any(t["outcome"].startswith("avslått") for t in self.turn_tools)  # et nei: ikke tving fram et nytt forsøk
                if (answer and not wrote and not refused and attempt == 0 and self.tool_names(agent, model_id)
                        and claims_action(answer)):
                    # Strenghet: påstår at noe er gjort uten å ha kalt et verktøy. Svaret forkastes, og samme modell
                    # må gjøre det ordentlig (eller si ærlig at det ikke er gjort).
                    attempt += 1
                    yield Event("judge_rejected", agent=agent, model=model_id, value=attempt, total=0, text=(
                        "Svaret sa at noe var gjort, men ingen verktøy ble brukt – ingenting var endret. Prøver igjen."))
                    payload = [*payload, {"role": "assistant", "content": answer}, {"role": "user", "content": (
                        "Du skrev at du har gjort en endring, men du kalte ikke noe verktøy, så INGENTING er endret. "
                        "Kall verktøyet nå (brukeren godkjenner), eller si ærlig at det ikke er gjort. Svaret ditt "
                        "erstatter det forrige: begynn direkte med saken – ikke «du har rett», ikke beklag, ikke nevn "
                        "denne beskjeden eller noe «verktøynotat».")}]
                    continue
                if answer and not wrote and attempt == 1 and claims_action(answer) and self.tool_names(agent, model_id):
                    yield Event("notice", agent=agent, text="Obs: ingen verktøy ble brukt – ingenting er endret.")
                break
            if answer:
                outputs.append((agent, answer))
                self.last_answers.append((agent, model_id, text, answer))
        finally:
            self._record(outputs)

    def _memory_allowed(self, model_id: str) -> bool:
        """Minnet (USER.md/MEMORY.md) går til skymodeller bare når memory.share_with_cloud er på (standard: på –
        brukeren ba om samme kontekst uansett modell). Slå av for å holde minnet helt lokalt."""
        return not self.backends.is_cloud(model_id) or bool(self.config.get("memory.share_with_cloud", True))

    def history_tokens(self) -> int:
        """Grovt anslag (≈3,5 tegn per token) – nok til å vite når det er på tide å komprimere."""
        return int(sum(len(str(m.get("content") or "")) for m in self.messages) / 3.5)

    async def _maybe_compress(self, agent: str, force: bool = False) -> AsyncIterator[Event]:
        """Som Hermes' ContextCompressor: når historikken passerer en andel (standard 50 %) av modellens
        kontekstvindu, oppsummeres midtpartiet med hjelpemodellen. Første runde og de siste meldingene beholdes
        ordrett. Gjelder alle modeller (lokal og sky) – grensen følger modellen som skal svare."""
        c = self.config
        if not force and not c.get("compression.enabled", True):
            return
        first_n = int(c.get("compression.protect_first", 2))
        last_n = int(c.get("compression.protect_last", 6))
        if len(self.messages) <= first_n + last_n + 1:
            return
        model = self.backends.resolve(agent or ZEUS)[0]
        limit = self.backends.context_limit(model) or int(c.get("llamacpp.ctx_size", 16384))
        reserve = 3000  # systemprompt, verktøy og plass til svaret
        used = self.history_tokens()
        if not force and used + reserve < float(c.get("compression.threshold", 0.5)) * limit:
            return
        # Kutt bare foran en brukermelding, så et verktøykall aldri skilles fra svaret sitt.
        lo, hi = first_n, len(self.messages) - last_n
        while lo < hi and self.messages[lo]["role"] != "user":
            lo += 1
        while hi > lo and self.messages[hi]["role"] != "user":
            hi -= 1
        if hi - lo < 2:
            return
        first_n, last_n = lo, len(self.messages) - hi
        middle = self.messages[first_n:-last_n]
        transcript = "\n\n".join(f"{'Bruker' if m['role'] == 'user' else 'Verktøy' if m['role'] == 'tool' else 'Assistent'}: "
                                   f"{m.get('content') or ''}" for m in middle if m.get("content"))
        helper = ZEUS
        yield Event("status", agent=helper, text=f"komprimerer samtalen ({used:,} tokens) …".replace(",", " "))
        prompt = ("Oppsummer denne delen av en samtale mellom en bruker og en AI-assistent, så samtalen kan fortsette "
                  "uten den. Ta med: hva brukeren ville, beslutninger og konklusjoner, viktige fakta, filnavn, kommandoer, "
                  "tall og feilmeldinger, og hva som gjenstår. Kortfattet, stikkord er greit, samme språk som samtalen.")
        try:
            summary, _note = await self._helper_call(lambda srv: srv.complete(
                [{"role": "system", "content": prompt}, {"role": "user", "content": transcript[-60000:]}],
                temperature=0.2, max_tokens=900))
        except Exception as e:  # noqa: BLE001 – uten sammendrag fortsetter samtalen som før
            yield Event("notice", agent=helper, text=f"Kunne ikke komprimere samtalen ({e}).")
            return
        summary = (summary or "").strip()
        if not summary:
            return
        was_tainted = any(m.get("_tainted") for m in middle)
        self.messages[first_n:-last_n] = [
            {"role": "user", "content": f"[Sammendrag av {len(middle)} tidligere meldinger i samtalen]\n{summary}",
             **({"_tainted": True} if was_tainted else {})},
            {"role": "assistant", "content": "Notert – jeg har sammendraget av det vi har snakket om."},
        ]
        after = self.history_tokens()
        self.compressions = getattr(self, "compressions", 0) + 1
        yield Event("notice", agent=helper, text=f"Samtalen er komprimert: {len(middle)} meldinger → sammendrag "
                                                 f"(~{used:,} → ~{after:,} tokens). Første og siste meldinger står ordrett."
                    .replace(",", " "))

    async def _agent_loop(
        self,
        agent: str,
        model_id: str,
        payload: list[dict[str, Any]],
        reply: list[str],
        approve: Approver | None,
    ) -> AsyncIterator[Event]:
        """Zeus: strøm svar, kjør verktøykall med godkjenning, gjenta.

        Gratiskvote (429): vent hvis provideren ber om noen få sekunder, ellers fortsetter
        neste modell i fallback-kjeden *samme* oppgave med det som allerede er gjort."""
        import asyncio

        c = self.config
        spec = c.agent(agent)
        attempts = [model_id]
        if self.backends.is_cloud(model_id):
            attempts += [m for m in self.backends.fallbacks() if m != model_id and self.backends.available(m)[0]]
        max_steps = int(c.get("tools.max_steps", 12))
        max_wait = float(c.get("tools.quota_wait", 10))
        messages = list(payload)
        n = 0
        current = attempts[0]
        waited = 0
        step = 0
        while step <= max_steps:
            names = self.tool_names(agent, current)
            self.toolbox.cloud = self.backends.is_cloud(current)
            free_cloud = self.backends.is_cloud(current) and "gratis" in self.backends.cost(current)
            self.toolbox.max_read_lines = int(c.get("tools.cloud_read_lines", 120)) if free_cloud else None
            try:
                server = await self.backends.get(current)
                async with self.warm_lock:  # pågår en forvarming: vent på den, så starten gjenbrukes
                    pass
                # Etter to nei i samme runde: ingen flere verktøy – svar brukeren (ellers spurte modellen 20+ ganger)
                tools = self.toolbox.specs(names) if names and step < max_steps and self.turn_denials < 2 else None
                if tools and self.turn_mcp:  # sist: starten av prompten (Olympus' verktøy) er den samme hver gang
                    tools += self.mcp.specs(self.turn_mcp)
                pending: dict[int, dict[str, str]] = {}
                round_text: list[str] = []
                # Tak på svarlengden for lokale modeller: en modell i løkke (9B gjorde det i testen) skal ikke kunne
                # skrive til konteksten er full og holde GPU-en i gang i minutter.
                cap = {} if self.backends.is_cloud(current) else {"extra": {"max_tokens": int(c.get("tools.max_reply_tokens", 3000))}}
                if tools and not self.backends.openai_tools(current):
                    # Claude Code: verktøyene går via MCP, og hele verktøyløkka skjer inne i ett kall
                    cap["tool_runner"] = self._mcp_runner(agent, approve)
                async for chunk in server.stream_chat(
                    messages,
                    temperature=float(spec.get("temperature", 0.7)),
                    thinking=self.backends.thinking(current),
                    tools=tools,
                    **cap,
                ):
                    if chunk.kind == "event":
                        yield chunk.event
                        continue
                    if chunk.kind == "tool_call":
                        t = pending.setdefault(int(chunk.tool.get("index", 0)), {"id": "", "name": "", "arguments": ""})
                        t["id"] = t["id"] or chunk.tool.get("id", "")
                        t["name"] += chunk.tool.get("name", "") if not t["name"] else ""
                        t["arguments"] += chunk.tool.get("arguments", "")
                        if chunk.tool.get("extra_content"):
                            t["extra_content"] = chunk.tool["extra_content"]
                        continue
                    if chunk.kind == "content":
                        reply.append(chunk.text)
                        round_text.append(chunk.text)
                    elif chunk.kind == "usage":
                        self.last_usage = chunk.usage
                        self.last_model = current
                        kind = self.backends.cost(current)
                        self.session_tokens[kind] = self.session_tokens.get(kind, 0) + int(chunk.usage.get("total_tokens", 0))
                    yield Event("chunk", agent=agent, chunk=chunk)
            except QuotaError as e:
                if e.retry_after is not None and e.retry_after <= max_wait and waited < 2:
                    waited += 1
                    yield Event("notice", agent=agent, text=f"{e} Venter {e.retry_after:.0f} s og prøver igjen.")
                    await asyncio.sleep(e.retry_after + 0.5)
                    continue
                if n + 1 >= len(attempts):
                    raise
                n += 1
                waited = 0
                current = attempts[n]
                where = "skymodell" if self.backends.is_cloud(current) else "lokal"
                self.last_ctx_limit = self.backends.context_limit(current)
                yield Event("notice", agent=agent, text=f"{e} Bytter til {where} {current} og fortsetter.")
                yield Event("agent_start", agent=agent, model=current)
                continue
            if not pending:
                return
            calls = [
                ToolCall(t["id"] or f"call_{step}_{i}", t["name"], t["arguments"])
                for i, t in sorted(pending.items())
            ]
            extras = [t.get("extra_content") for _, t in sorted(pending.items())]
            messages.append({
                "role": "assistant",
                "content": "".join(round_text) or None,
                "tool_calls": [
                    {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments or "{}"},
                     **({"extra_content": ex} if ex else {})}
                    for tc, ex in zip(calls, extras)
                ],
            })
            await self._batch_approve(calls, approve)
            for call in calls:
                key = (call.name, str(call.args.get("action") or ""))
                if id(call) in self._batch_no:  # avslått på det samlede kortet
                    result = ToolResult(False, "Brukeren avslo dette. Ikke prøv igjen – skriv kort hva som ikke ble gjort.",
                                        "avslått", denied=True)
                    self._log_tool(call, "avslått av brukeren – ingenting ble gjort")
                    yield Event("tool_call", agent=agent, call=call)
                    yield Event("tool_result", agent=agent, call=call, result=result)
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": result.output})
                    continue
                if key in self.turn_denied and self.toolbox.approval(call).needed:
                    # Samme verktøy etter et nei: ikke spør brukeren igjen, si fra til modellen i stedet.
                    result = ToolResult(False, "Brukeren har allerede sagt nei til dette i denne runden. Ikke spør igjen – "
                                               "skriv et kort svar til brukeren om at det ikke ble gjort.", "avslått (allerede nei)",
                                        denied=True)
                    self._log_tool(call, "avslått – brukeren hadde allerede sagt nei")
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": result.output})
                    self.turn_denials += 1
                    continue
                yield Event("tool_call", agent=agent, call=call)
                result = None
                async for item in self._run_tool_with_notices(call, approve, agent):
                    if isinstance(item, Event):
                        yield item
                    else:
                        result = item
                assert result is not None
                if result.denied:
                    self.turn_denied.add(key)
                    self.turn_denials += 1
                yield Event("tool_result", agent=agent, call=call, result=result)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result.output})
            self._batch_yes, self._batch_no = {}, set()  # godkjenninger gjelder bare denne omgangen
            yield Event("round", agent=agent, model=current)
            step += 1
        yield Event("notice", agent=agent, text=f"Stoppet etter {max_steps} verktøysteg.")

    async def teach(self, lid: int, agent: str, question: str, wrong: str, correction: str) -> str | None:
        """La læreren skrive et riktig svar til treningsparet. Returnerer svaret, eller None."""
        teacher = self.config.get("training.teacher", "")
        if not teacher or not self.backends.available(teacher)[0]:
            return None
        lib = self.toolbox.library
        context = ""
        if "library_search" in self.config.agent(agent).get("tools", []) and lib is not None and lib.ready():
            hits = lib.search(question + " " + correction, 3)
            context = "\n\n".join(f"[{h.title} § {h.section}] {h.snippet}" for h in hits)
        system = (
            system_prompt(self.config, self.cwd, agent)
            + "\n\nDu skriver et fasitsvar. Et tidligere svar var feil; brukeren har gitt en rettelse. "
            "Skriv det riktige, fullstendige svaret på spørsmålet, i samme stil og språk som agenten skal bruke. "
            "Bygg på rettelsen. Ikke nevn at det finnes et tidligere svar."
        )
        user = f"Spørsmål:\n{question}\n\nFeil svar:\n{wrong[:3000]}\n\nRettelse fra brukeren:\n{correction}"
        if context:
            user += f"\n\nFra Arch Wiki:\n{context}"
        try:
            server = await self.backends.get(teacher)
            chosen = await server.complete(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0.3, max_tokens=2000,
            )
        except Exception:
            return None
        if chosen.strip():
            self.lessons.set_chosen(lid, agent, chosen.strip(), teacher)
            return chosen.strip()
        return None

    def _mcp_runner(self, agent: str, approve: Approver | None):
        """Kjører verktøykall som kommer via MCP (Claude Code) med samme regler som den vanlige løkka: godkjenning,
        logg, «ikke spør igjen etter nei», og ingen flere verktøy etter to nei i samme runde."""
        n = 0

        async def run(name: str, args: dict[str, Any], emit) -> tuple[str, bool]:
            nonlocal n
            n += 1
            call = ToolCall(f"mcp_{n}", name, json.dumps(args, ensure_ascii=False))
            key = (call.name, str(call.args.get("action") or ""))
            if self.turn_denials >= 2 or (key in self.turn_denied and self.toolbox.approval(call).needed):
                self._log_tool(call, "avslått – brukeren hadde allerede sagt nei")
                self.turn_denials += 1
                return ("Brukeren har allerede sagt nei i denne runden. Ikke bruk flere verktøy – skriv et kort svar "
                        "til brukeren om at det ikke ble gjort.", True)
            emit(Event("tool_call", agent=agent, call=call))
            result = None
            async for item in self._run_tool_with_notices(call, approve, agent):
                if isinstance(item, Event):
                    emit(item)
                else:
                    result = item
            assert result is not None
            if result.denied:
                self.turn_denied.add(key)
                self.turn_denials += 1
            emit(Event("tool_result", agent=agent, call=call, result=result))
            return result.output, not result.ok

        return run

    async def _batch_approve(self, calls: list[ToolCall], approve: Approver | None) -> None:
        """Flere kall i samme omgang som krever ja → ett kort med alt («Zeus vil gjøre 3 ting»): ja godkjenner alle,
        nei avslår alle. Før kom kortene etter hverandre."""
        from olympus.tools import Approval

        # Nøkkel = selve kall-objektet (id()), ikke id-en modellen har laget (to kall kunne hatt samme id), og
        # forhåndsvisningen som ble godkjent: endrer den seg før kallet kjøres (et tidligere kall i samme omgang
        # endret planen), spørres det på nytt.
        self._batch_yes: dict[int, str] = {}
        self._batch_no: set[int] = set()
        if approve is None or self.turn_denials >= 2:
            return
        ask = [(c, a) for c in calls for a in [self.toolbox.approval(c)] if a.needed and not a.reject
               and (c.name, str(c.args.get("action") or "")) not in self.turn_denied]
        if len(ask) < 2:
            return
        combined = Approval(True, f"Zeus vil gjøre {len(ask)} ting", danger=any(a.danger for _, a in ask),
                            preview="\n".join(f"{i}. {a.reason}" + (f"\n   " + a.preview.replace("\n", "\n   ") if a.preview else "")
                                              for i, (_, a) in enumerate(ask, 1)), preview_lang="text")
        answer = await approve(ask[0][0], combined)
        if answer in ("yes", "always"):
            self._batch_yes = {id(c): a.preview for c, a in ask}
        else:
            self._batch_no = {id(c) for c, _ in ask}
            self.turn_denials += 1
            self.turn_denied.update((c.name, str(c.args.get("action") or "")) for c, _ in ask)

    async def _run_tool_with_notices(self, call: ToolCall, approve: Approver | None, agent: str):
        """Som _run_tool, men et MCP-kall kan sende meldinger til chatten mens det pågår («venter på godkjenning i
        skrivebordet» når OS-serveren viser sitt eget kort). Gir Event-er underveis og til slutt ToolResult."""
        import asyncio

        if not call.name.startswith("mcp_"):
            yield await self._run_tool(call, approve)
            return
        side: asyncio.Queue[str] = asyncio.Queue()
        self.toolbox.mcp_notice = side.put_nowait
        task = asyncio.ensure_future(self._run_tool(call, approve))
        try:
            while True:
                getter = asyncio.ensure_future(side.get())
                await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
                if getter.done():
                    yield Event("notice", agent=agent, text=getter.result())
                else:
                    getter.cancel()
                if task.done():
                    break
            while not side.empty():
                yield Event("notice", agent=agent, text=side.get_nowait())
        finally:
            self.toolbox.mcp_notice = None
            if not task.done():
                task.cancel()  # avbrutt: kallet avbrytes også (serveren får notifications/cancelled)
        yield task.result()

    async def _run_tool(self, call: ToolCall, approve: Approver | None) -> ToolResult:
        approval = self.toolbox.approval(call)
        batched = getattr(self, "_batch_yes", {}).pop(id(call), None)  # godkjent på det samlede kortet – brukes én gang
        if approval.reject:  # fant ikke / ugyldig: avvises uten å kjøres (og uten å omgå godkjenningen)
            self._log_tool(call, "feilet: " + approval.reject[:120])
            return ToolResult(False, approval.reject, "avvist")
        if approval.bind:  # kjør med akkurat det brukeren så på kortet (samme avtale/spilleliste)
            call = ToolCall(call.id, call.name, json.dumps({**call.args, **approval.bind}, ensure_ascii=False))
        if approval.needed and batched is not None and batched == approval.preview:
            pass  # godkjent på det samlede kortet, og kallet er det samme som ble vist
        elif approval.needed:
            answer = await approve(call, approval) if approve is not None else "no"
            if answer == "always" and not approval.danger:
                self.toolbox.session_allow.add(call.name)
                self.toolbox.session_allow.add(f"{call.name}:{call.args.get('action') or ''}")
            if answer not in ("yes", "always"):
                self._log_tool(call, "avslått av brukeren – ingenting ble gjort")
                return ToolResult(False, "Brukeren avslo dette kallet. Ikke prøv det samme igjen.", "avslått", denied=True)
        result = await self.toolbox.run(call)
        if result.ok and f"{call.name}:{call.args.get('action') or ''}" in OUTBOUND_AFTER_WEB:
            # Alt hentet utenfra (sider, søketreff, leksikon, torrenttitler) kan inneholde prompt-injeksjon:
            # videre utgående kall må godkjennes i resten av runden (se Toolbox.approval)
            self.toolbox.web_tainted = True
        self._log_tool(call, ("ok: " if result.ok else "feilet: ") + (result.summary or "").strip().replace("\n", " ")[:120],
                       result.output)
        return result

    def _log_tool(self, call: ToolCall, outcome: str, output: str = "") -> None:
        """Hva verktøyene faktisk gjorde denne runden – havner i historikken som ekte kall, så modellen både vet hva
        den gjorde (kommando, sti, id) og hva som kom ut (kort utdrag), og ikke «husker» handlinger den bare skrev om.
        Hemmeligheter vaskes bort. Til skymodeller sendes bare den korte versjonen (se _payload)."""
        if not hasattr(self, "turn_tools"):
            self.turn_tools = []
        full = {k: (redact(v)[:300] if isinstance(v, str) else v) for k, v in call.args.items() if k not in ("content", "patch", "diff")}
        brief = ({k: v for k, v in call.args.items() if k not in ("content", "text", "url", "path")}
                 if call.name in HISTORY_ARG_TOOLS else
                 {k: v for k, v in call.args.items() if k == "action" and isinstance(v, str)})
        excerpt = redact(output.strip())[:400]
        entry = {"id": call.id, "name": call.name, "args": json.dumps(full, ensure_ascii=False)[:600],
                 "brief_args": json.dumps(brief, ensure_ascii=False)[:500], "outcome": outcome,
                 "excerpt": excerpt, "action": str(call.args.get("action") or "")}
        if call.name.startswith("mcp_"):  # MCP: serverens risiko sier om kallet endret noe (dommeren, is_write)
            t = self.mcp.get(call.name)
            entry["writes"] = t is None or t.risk != "read"
            # Til skyen går bare «ok»/«feilet» – aldri serverens tekst – også i senere runder (for_model); utdraget
            # (_local) får bare lokale modeller
            entry["excerpt"] = (outcome.split(":", 1)[1].strip() + "\n" + excerpt).strip() if ":" in outcome else excerpt
            entry["outcome"] = outcome.split(":", 1)[0]
        self.turn_tools.append(entry)

    def _payload(self, agent: str, original: str, model_id: str = "") -> list[dict[str, Any]]:
        history = self.messages[:-1]  # siste er brukermeldingen for denne runden
        if not model_id or not self.backends.openai_tools(model_id):
            history = text_only(history)  # skymodeller via Claude Code/Anthropic tar ikke OpenAI-verktøymeldinger
        history = for_model(history, cloud=bool(model_id) and self.backends.is_cloud(model_id))
        system = system_prompt(self.config, self.cwd, agent)
        if self.memory_snapshot and self._memory_allowed(model_id):  # fast for hele økten (prefiks-cache)
            system += "\n\n" + self.memory_snapshot
        if model_id and (names := self.tool_names(agent, model_id)):
            system += "\n\n" + TOOL_GUIDE
            from olympus.actions import capability_overview

            if overview := capability_overview(names):  # fra handlingsregisteret: hele backenden, alltid oppdatert
                system += "\n\n" + overview
        elif model_id and "write_file" in self.config.agent(agent).get("tools", []):
            system += "\n\n" + NO_TOOLS_GUIDE
        # Rettelser som passer spørsmålet legges på brukermeldingen, ikke i systemprompten: starten av prompten
        # (systemprompt + verktøy, ~8000 tokens) skal være lik hver gang, så modellen kan gjenbruke den.
        lessons = self.lessons.relevant(agent, original) if self.lessons else []
        user = original + ("\n\n" + self.lessons.as_prompt(lessons) if lessons else "")
        if self.router_on and model_id and self.tool_names(agent, model_id):
            from olympus import router

            if hint := router.hint(original):  # bare når velgeren er sikker; Zeus beholder alle verktøyene
                user += "\n\n" + hint
        # Klokkeslettet hører hjemme her, ikke i systemprompten (den skal være lik hele dagen). Uten det gjettet
        # Gemma («Klokka er 14:30» kl. 08).
        user += f"\n\n(Klokka er {datetime.now():%H:%M}.)"
        return [
            {"role": "system", "content": system},
            *history,
            {"role": "user", "content": user},
        ]

    async def warm(self, agent: str = "zeus") -> bool:
        """Forvarming: last modellen og regn ut den faste starten (systemprompt + verktøy) før brukeren sender.
        Første svar tar da ~0,5 s i stedet for ~11 s. Gjør ingenting for skymodeller eller når det allerede er gjort
        for denne modellprosessen. Returnerer True hvis det ble varmet nå."""
        ref, _ = self.backends.resolve(agent)
        if not ref or self.backends.is_cloud(ref):
            return False
        server = await self.backends.get(ref)
        if getattr(server, "warmed", False):
            return False
        server.warmed = True
        p = self._payload(agent, "·", ref)
        names = self.tool_names(agent, ref)
        try:
            async with self.warm_lock:  # et ekte spørsmål venter på dette i stedet for å regne starten en gang til
                async for _ in server.stream_chat([p[0], {"role": "user", "content": "·"}], temperature=0.0,
                                                  thinking=False, tools=self.toolbox.specs(names) if names else None,
                                                  extra={"max_tokens": 1}):
                    pass
        except Exception:  # noqa: BLE001 – forvarming er bare en bonus
            server.warmed = False
            return False
        return True

    def _record(self, outputs: list[tuple[str, str]]) -> None:
        """Legg svaret i historikken som én assistentmelding (også ved avbrudd)."""
        if not outputs:
            if self.messages and self.messages[-1]["role"] == "user":
                self.messages.pop()
            return
        text = "\n\n".join(out for _, out in outputs)
        text = FAKE_NOTE.sub("", text).lstrip()
        # Verktøykallene lagres som ekte kall + kort resultat (ikke utdata). Uten dem så modellen bare tekstsvar i
        # historikken, sluttet å kalle verktøy og skrev «Ferdig – lagt inn» uten at noe var gjort.
        tools = getattr(self, "turn_tools", [])
        for t in tools:
            # Skysikkert som standard (navn, handling, «ok: …»); kommando og utdrag ligger i _local_* og legges bare
            # inn for lokale modeller (for_model). Glemmer en vei å filtrere, er det kortversjonen som går ut.
            self.messages.append({"role": "assistant", "content": None, "_local_args": t["args"], "tool_calls": [
                {"id": t["id"], "type": "function", "function": {"name": t["name"], "arguments": t.get("brief_args", "{}")}}]})
            body = t["outcome"] + (f"\n{t['excerpt']}" if t.get("excerpt") else "")
            self.messages.append({"role": "tool", "tool_call_id": t["id"], "content": t["outcome"], "_local": body})
        if not tools:
            for m in reversed(self.messages):  # fast tekst, ingen data: trygt å legge på brukerens melding
                if m["role"] == "user" and isinstance(m.get("content"), str):
                    if NO_TOOLS_NOTE not in m["content"]:
                        m["content"] += NO_TOOLS_NOTE
                    break
        self.messages.append({"role": "assistant", "content": text,
                              **({"_tainted": True} if self.toolbox.web_tainted else {})})


HISTORY_ARG_TOOLS = {"plan", "playlist", "media_search", "media_find", "audio", "downloads", "media", "pc", "model"}
SECRET = re.compile(r"(?i)\b(token|passord|password|passwd|secret|api[_-]?key|authorization|bearer|cookie)(\s*[=:]\s*|\s+)(\S+)")
BEARER = re.compile(r"(?i)\bbearer\s+\S+")
LONG_KEY = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def redact(text: str) -> str:
    """Fjern det som ligner nøkler og passord før noe lagres i historikken."""
    text = BEARER.sub("Bearer ***", text)
    return LONG_KEY.sub("***", SECRET.sub(lambda m: m.group(0) if m.group(3).startswith("***") else f"{m.group(1)}{m.group(2)}***", text))


def for_model(history: list[dict[str, Any]], cloud: bool) -> list[dict[str, Any]]:
    """Historikken slik modellen skal se den. Lagret er den skysikre kortversjonen; bare en lokal modell får
    kommandoer, stier og utdrag fra maskinen (_local_*). Interne felt fjernes alltid."""
    out = []
    for m in history:
        m2 = {k: v for k, v in m.items() if not k.startswith("_")}
        if not cloud and m.get("role") == "tool" and "_local" in m:
            m2["content"] = m["_local"]
        if not cloud and m.get("tool_calls") and "_local_args" in m:
            m2["tool_calls"] = [{**tc, "function": {**tc["function"], "arguments": m["_local_args"]}} for tc in m["tool_calls"]]
        out.append(m2)
    return out
NO_TOOLS_NOTE = "\n\n(Systemnotat: ingen verktøy ble kjørt i denne runden – ingenting ble endret.)"
FAKE_NOTE = re.compile(r"^\s*\[Verktøy brukt i denne runden:[^\]]*\]\s*|\(Systemnotat[^)]*\)", re.MULTILINE)
CLAIM = re.compile(
    r"\b(jeg har |har nå |er nå )?(lagt (inn|til)|slettet|fjernet|flyttet|endret|opprettet|laget spillelist|"
    r"startet nedlasting|spiller nå|satt volum|koblet til|"
    r"jeg (legger (inn|til)|sletter|flytter|endrer|oppretter|lager|bytter|starter|laster ned)|har byttet|byttet til|"
    r"vent(er)? på godkjenning)\b", re.IGNORECASE)


WRITE_ACTIONS = {"add", "move", "edit", "delete", "create", "cancel", "volume", "output", "connect", "disconnect"}
WRITE_TOOLS = {"media_get", "write_file", "edit_file", "shell", "memory"}


def is_write(t: dict[str, Any]) -> bool:
    """Et verktøykall som faktisk endret noe (ikke avslått, ikke feilet, ikke bare lesing)."""
    if not t["outcome"].startswith("ok"):
        return False
    action = t.get("action")
    if action is None:  # eldre oppføringer
        try:
            action = json.loads(t["args"]).get("action")
        except ValueError:
            action = None
    action = action or None
    if t["name"].startswith("mcp_"):
        return bool(t.get("writes", True))
    from olympus import actions

    if t["name"] in actions.GENERATED:  # registeret vet hva som endrer noe (pc play, media edit …)
        return actions.tool_writes(t["name"], action)
    if t["name"] == "memory" and action == "list":
        return False
    return t["name"] in WRITE_TOOLS or action in WRITE_ACTIONS


def text_only(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Historikk uten verktøymeldinger (for modeller som ikke tar dem)."""
    return [m for m in messages if m["role"] in ("user", "assistant", "system") and isinstance(m.get("content"), str)]


def claims_action(answer: str) -> bool:
    """Svaret sier at en endring er gjort («Ferdig – lagt inn …»). Brukes bare når ingen verktøy ble kalt."""
    head = answer.strip()[:400]
    if FAKE_NOTE.search(answer):
        return True  # modellen skrev selv et «verktøynotat» – da har den ikke kjørt noe
    m = CLAIM.search(head)
    if not m or re.search(r"\b(skal jeg|vil du|kan jeg)\b", head, re.I):
        return False
    # «jeg starter ikke nedlastingen», «jeg bytter ikke modell», «ingenting er lagt inn»: et nei, ikke en påstand
    near = head[max(0, m.start() - 30):m.end() + 30].lower()
    return not re.search(r"\b(ikke|ingen|ingenting|aldri)\b", near)


__all__ = ["Event", "Orchestrator", "claims_action"]
