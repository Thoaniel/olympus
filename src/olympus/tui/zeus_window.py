"""`olympus zeus`: eget vindu som viser hva Zeus gjør akkurat nå – og lar deg styre ham.

Kobler seg til den ene hjernen (olympus-web) over samme websocket som appen og Lab's. Viser tilstand (ledig,
tenker, bruker verktøy, venter på deg, av), modell og kontekst, en strøm av alt som skjer (fra PC, telefon og
Zeus selv) og godkjenninger som venter – med taster for å svare, stoppe, slå av og på, frigjøre GPU-en og bytte
modell (F7: lokal eller ☁ sky; Zeus' hjerne blir på PC-en uansett).

Godkjenninger vises aldri avkortet uten at du ser det: er forhåndsvisningen for lang for panelet, står det hvor mye
som er skjult, og ctrl+j virker først når du har åpnet hele (ctrl+v)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any

import aiohttp
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

GOLD, MUTED, OK, BAD, LINE, DIM = "#d4a73a", "#8a8696", "#8fbf6f", "#e0605e", "#34344a", "#5a5868"

# Zeus' ansikt i fire tilstander – tegnet med samme strek, bare lynet og øynene endrer seg.
FACES = {
    # krone · øyne · munn · skjegg – bare øyne, munn og tegnet ved siden av endrer seg med tilstanden
    "ledig": ("─", "─", "╰───╯", " "),
    "tenker": ("◉", "◉", "╰───╯", "ϟ"),
    "verktøy": ("◉", "◉", "═════", "⚙"),
    "venter": ("◉", "◉", " ─── ", "?"),
    "av": ("─", "─", " z z ", " "),
}
FACE = """
    ▲   ▲   ▲   ▲     {s}
  ╭─┴───┴───┴───┴─╮
  │   {l}       {r}   │
  │       ╻       │
  │     {m}     │
  ╰╮ ║║║║║║║║║║║ ╭╯
   ╰╮║║║║║║║║║║║╭╯
    ╰─╮║║║║║║║╭─╯
      ╰───────╯"""


def face(state: str) -> str:
    left, right, mouth, side = FACES[state]
    return FACE.format(l=left, r=right, m=mouth, s=side).strip("\n")


STATE_TEXT = {"ledig": ("● ledig", OK), "tenker": ("ϟ tenker …", GOLD), "verktøy": ("⚙ bruker verktøy", GOLD),
              "venter": ("‖ venter på deg", BAD), "av": ("○ AI er av", MUTED)}
STEPS = ["forespørsel", "modell", "verktøy", "svar"]


from olympus.tui.approvals import (  # noqa: E402,F401 – felles regler
    PREVIEW_LIMIT,
    ApprovalGate,
    preview_parts,
)


class FullPreview(ModalScreen[str | None]):
    """Hele forhåndsvisningen av et verktøykall, rullbar. ctrl+j godkjenner, ctrl+n avslår, Esc lukker."""

    DEFAULT_CSS = f"""
    FullPreview {{ align: center middle; background: #0b0b0f 70%; }}
    FullPreview > Vertical {{ width: 92%; height: 90%; border: round {BAD}; border-title-color: {BAD}; background: #111118; }}
    FullPreview VerticalScroll {{ height: 1fr; padding: 0 1; }}
    FullPreview #fp-keys {{ height: 1; padding: 0 1; }}
    """
    BINDINGS = [
        Binding("ctrl+j", "answer('yes')", "godkjenn", priority=True),
        Binding("ctrl+n", "answer('no')", "avslå", priority=True),
        Binding("escape,ctrl+v", "answer(None)", "lukk", priority=True),
    ]

    def __init__(self, pending: dict[str, Any]) -> None:
        super().__init__()
        self.pending = pending

    def compose(self) -> ComposeResult:
        p = self.pending
        with Vertical() as v:
            v.border_title = "ϟ ZEUS VIL – HELE FORHÅNDSVISNINGEN" + ("  ·  FARLIG" if p.get("danger") else "")
            with VerticalScroll():
                yield Static(Text(f"verktøy: {p.get('tool') or '?'}\n", style=MUTED)
                             + Text(f"{p.get('reason')}\n\n", style=f"bold {BAD if p.get('danger') else GOLD}")
                             + Text(str(p.get("preview") or "")))
            keys = Text()
            for k, what in (("ctrl+j", "godkjenn"), ("ctrl+n", "avslå"), ("esc", "lukk"), ("pgup/pgdn", "rull")):
                keys.append(f" {k}", style=f"bold {GOLD}")
                keys.append(f" {what}  ", style=MUTED)
            yield Static(keys, id="fp-keys")

    def on_mount(self) -> None:
        self.query_one(VerticalScroll).focus()

    def action_answer(self, answer: str | None) -> None:
        self.dismiss(answer)


class ModelChooser(ModalScreen[str | None]):
    """F7: velg modellen Zeus svarer med (lista kommer fra hjernen, /api/v1/models)."""

    DEFAULT_CSS = f"""
    ModelChooser {{ align: center middle; background: #0b0b0f 70%; }}
    ModelChooser > Vertical {{ width: 80; max-width: 95%; height: auto; max-height: 85%; border: round {GOLD};
                              border-title-color: {GOLD}; background: #111118; padding: 0 1; }}
    ModelChooser OptionList {{ height: auto; max-height: 24; border: none; background: #111118; }}
    """
    BINDINGS = [Binding("escape,f7", "pick(None)", "lukk", priority=True)]

    def __init__(self, choices: list[dict[str, Any]], current: str) -> None:
        super().__init__()
        self.choices, self.current = choices, current

    def compose(self) -> ComposeResult:
        opts: list[Option] = []
        group = None
        for c in self.choices:
            head = f"☁ {c.get('provider', '')} – samtalen sendes ut" if c.get("cloud") else "⚡ lokalt på PC-en"
            if head != group:
                group = head
                opts.append(Option(Text(head, style=f"bold {BAD if c.get('cloud') else OK}"), disabled=True))
            t = Text("✓ " if c["id"] == self.current else "  ", style=OK)
            t.append(("☁ " if c.get("cloud") else "") + str(c["id"]), style="bold" if c["id"] == self.current else "")
            extra = " · ".join(str(x) for x in (c.get("size"), c.get("note")) if x)
            if extra:
                t.append(f"  {extra}", style=MUTED)
            opts.append(Option(t, id=str(c["id"])))
        with Vertical() as v:
            v.border_title = "ϟ HVEM SVARER FOR ZEUS?"
            yield Static(Text("Minne, verktøy, plan og godkjenninger blir på PC-en uansett.", style=MUTED))
            yield OptionList(*opts)

    def on_mount(self) -> None:
        ol = self.query_one(OptionList)
        ol.focus()
        try:
            ol.highlighted = ol.get_option_index(self.current)
        except Exception:  # noqa: BLE001 – gjeldende modell finnes ikke i lista (f.eks. ikke koblet til)
            pass

    def on_option_list_option_selected(self, e: OptionList.OptionSelected) -> None:
        self.dismiss(e.option.id)

    def action_pick(self, ref: str | None) -> None:
        self.dismiss(ref)


def _bar(frac: float, width: int = 18) -> Text:
    n = round(max(0.0, min(1.0, frac)) * width)
    return Text("▮" * n, style=BAD if frac > 0.85 else GOLD) + Text("▯" * (width - n), style=LINE)


class ZeusWindow(App):
    TITLE = "Zeus"
    CSS = f"""
    Screen {{ background: #0b0b0f; }}
    #top {{ height: 1; padding: 0 1; }}
    #main {{ height: 1fr; }}
    #left {{ width: 36; border: round {LINE}; border-title-color: {GOLD}; border-title-style: bold; padding: 0 1; }}
    #face {{ height: auto; }}
    #brain {{ height: auto; margin-top: 1; }}
    #feed {{ overflow-x: hidden; border: round {LINE}; border-title-color: {GOLD}; border-title-style: bold; background: #0b0b0f;
             scrollbar-color: {LINE}; scrollbar-color-hover: {GOLD}; }}
    #ask {{ height: auto; border: round {BAD}; border-title-color: {BAD}; border-title-style: bold; padding: 0 1;
            display: none; }}
    #say {{ border: tall {LINE}; background: #111118; }}
    #say:focus {{ border: tall {GOLD}; }}
    #keys {{ height: 1; padding: 0 1; background: #111118; }}
    """
    BINDINGS = [
        Binding("ctrl+j", "approve(True)", "godkjenn", priority=True),
        Binding("ctrl+n", "approve(False)", "avslå", priority=True),
        Binding("ctrl+v", "view_preview", "se hele", priority=True),
        Binding("ctrl+s", "interrupt", "stopp svaret", priority=True),
        Binding("f2", "toggle_ai", "AI på/av", priority=True),
        Binding("f3", "unload", "frigjør GPU", priority=True),
        Binding("f5", "new", "ny økt", priority=True),
        Binding("f7", "pick_model", "modell", priority=True),
        Binding("ctrl+q", "quit", "lukk", priority=True),
    ]

    # Godkjenningene bor i ApprovalGate (olympus.tui.approvals); disse egenskapene er snarveier til den.
    pending = property(lambda self: self.gate.pending, lambda self, v: setattr(self.gate, "pending", v))
    viewed = property(lambda self: self.gate.viewed)
    arrived = property(lambda self: self.gate.arrived)
    displayed = property(lambda self: self.gate.displayed)
    armed = property(lambda self: self.gate.armed, lambda self, v: setattr(self.gate, "armed", v))
    GRACE = property(lambda self: self.gate.GRACE, lambda self, v: setattr(self.gate, "GRACE", v))
    CONFIRM = property(lambda self: self.gate.CONFIRM, lambda self, v: setattr(self.gate, "CONFIRM", v))

    def __init__(self, base: str, token: str) -> None:
        super().__init__()
        self.base, self.token = base.rstrip("/"), token
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.session: aiohttp.ClientSession | None = None
        self.state = "ledig"
        self.step = -1
        self.ai = True
        self.connected = False
        self.stats: dict[str, Any] = {}
        self.gate = ApprovalGate()  # godkjenningsreglene (felles med zeus-fanen i Lab's)
        self.answer = ""
        self.agent = "zeus"
        self.model = ""  # modellen Zeus svarer med (fra hjernen)
        self.cloud = False

    def compose(self) -> ComposeResult:
        yield Static(id="top")
        with Horizontal(id="main"):
            with Vertical(id="left") as v:
                v.border_title = "ϟ ZEUS"
                yield Static(id="face")
                yield Static(id="brain")
            with Vertical():
                feed = RichLog(id="feed", wrap=True, markup=False, auto_scroll=True)
                feed.border_title = "≡ HVA SKJER"
                yield feed
                yield Static(id="ask")
                yield Input(placeholder="skriv til Zeus …  (⏎ send)", id="say")
        yield Static(id="keys")

    async def on_mount(self) -> None:
        self.render_all()
        self.run_worker(self.listen(), exclusive=True, group="ws")
        self.set_interval(1.0, self.render_all)  # klokke og «tenker»-tid; billig

    # --- forbindelsen ------------------------------------------------------------------------------
    async def listen(self) -> None:
        origin = self.base
        while True:
            try:
                async with aiohttp.ClientSession(cookies={"olympus_web": self.token}) as s:
                    self.session = s
                    async with s.ws_connect(self.base.replace("http", "ws", 1) + "/api/v1/ws", headers={"Origin": origin},
                                            heartbeat=25) as ws:
                        self.ws, self.connected = ws, True
                        await self.fetch_status()
                        await ws.send_json({"type": "stats"})
                        self.render_all()
                        async for m in ws:
                            if m.type == aiohttp.WSMsgType.TEXT:
                                self.on_server(json.loads(m.data))
            except (aiohttp.ClientError, OSError, ValueError):
                pass
            self.ws, self.connected = None, False
            self.render_all()
            await asyncio.sleep(3)  # serveren startet på nytt: koble til igjen av seg selv

    async def fetch_status(self) -> None:
        if self.session is None:
            return
        try:
            async with self.session.get(self.base + "/api/v1/status") as r:
                st = await r.json()
            self.ai = bool(st.get("ai", True))
            self.model, self.cloud = str(st.get("model") or ""), bool(st.get("cloud"))
            if not self.ai:
                self.state = "av"
        except (aiohttp.ClientError, ValueError):
            pass

    async def send(self, msg: dict[str, Any]) -> None:
        if self.ws is None:
            self.notify("Ingen kontakt med Olympus-serveren.", severity="error", markup=False)
            return
        await self.ws.send_json(msg)

    # --- hendelser fra hjernen ---------------------------------------------------------------------
    def log_line(self, icon: str, text: str, style: str = "", who: str = "") -> None:
        t = Text(f"{dt.datetime.now():%H:%M:%S} ", style=DIM)
        t.append(f"{icon} ", style=GOLD if style == "" else style)
        if who:
            t.append(f"{who} ", style=MUTED)
        t.append(text, style=style)
        self.query_one("#feed", RichLog).write(t)

    def on_server(self, m: dict[str, Any]) -> None:
        kind = m.get("type")
        if kind == "user":
            self.log_line("›", str(m.get("text", "")), "bold")
            self.state, self.step, self.answer = "tenker", 0, ""
        elif kind == "agent_start":
            self.agent = m.get("agent", "zeus")
            self.state, self.step = "tenker", 1
            self.log_line("ϟ", f"{m.get('display') or self.agent} svarer med {m.get('model') or 'modellen'}", MUTED)
        elif kind == "chunk":
            self.answer += m.get("text", "")
            self.state, self.step = "tenker", 3 if self.step >= 2 else 1
        elif kind == "tool":
            self.state, self.step = "verktøy", 2
            self.log_line("⚙", f"{m.get('name')} {m.get('detail') or ''}".strip(), GOLD)
        elif kind == "tool_result":
            ok, denied = m.get("ok"), m.get("denied")
            self.log_line("✓" if ok else "✗", str(m.get("summary") or ""), OK if ok else BAD)
            if denied:
                self.log_line("·", "du avslo – ingenting ble gjort", MUTED)
        elif kind == "approval":
            self.gate.add(m)
            self.state = "venter"
            self.log_line("‖", f"{m.get('reason')} – venter på deg (ctrl+j / ctrl+n)", BAD)
            self.bell()
        elif kind == "approval_done":
            self.gate.done(m.get("id"))
            if not self.pending and self.state == "venter":
                self.state = "verktøy"
        elif kind == "judge_rejected":  # svaret påsto noe uten verktøy: forkastet, Zeus prøver igjen
            self.log_line("↺", str(m.get("text", ""))[:200], BAD)
        elif kind == "model":
            self.model, self.cloud = str(m.get("model") or ""), bool(m.get("cloud"))
            self.log_line("ϟ", f"svarer nå med {'☁ ' if self.cloud else ''}{self.model}", BAD if self.cloud else MUTED)
        elif kind == "notice":
            self.log_line("·", str(m.get("text", "")), MUTED)
        elif kind == "error":
            self.log_line("✗", str(m.get("text", "")), BAD)
        elif kind == "done":
            if self.answer.strip():
                self.log_line("ϟ", self.answer.strip().replace("\n", " ")[:400], "", who="svar:")
            self.state, self.step, self.answer = ("ledig" if self.ai else "av"), -1, ""
            self.gate.clear()
        elif kind == "stats":
            self.stats = m.get("stats") or {}
            if self.stats.get("model"):
                self.model, self.cloud = str(self.stats["model"]), bool(self.stats.get("cloud"))
        elif kind == "ai":
            self.ai = bool(m.get("on"))
            self.state = "ledig" if self.ai else "av"
            self.log_line("◉", "Zeus er slått på" if self.ai else "Zeus er slått av – GPU-en er frigjort", MUTED)
        self.render_all()

    # --- visning ------------------------------------------------------------------------------------
    def render_all(self) -> None:
        top = Text("ϟ ZEUS · KONTROLL", style=f"bold {GOLD}")
        label, color = STATE_TEXT[self.state]
        top.append("   ")
        top.append(label, style=f"bold {color}")
        if self.model:
            top.append("   ")
            top.append(f" ☁ {self.model} · sky – samtalen sendes ut " if self.cloud else f"ϟ {self.model} · lokal",
                       style=f"bold #0b0b0f on {BAD}" if self.cloud else MUTED)
        top.append("    ● tilkoblet" if self.connected else "   ○ kobler til Olympus …", style=OK if self.connected else BAD)
        top.append(f"   {dt.datetime.now():%H:%M:%S}", style=DIM)
        self.query_one("#top", Static).update(top)

        face_t = Text(face(self.state), style=f"bold {color}" if self.state != "ledig" else GOLD)
        self.query_one("#face", Static).update(face_t)

        b = Text()
        for i, s in enumerate(STEPS):
            on = i == self.step
            b.append("▶ " if on else "  ", style=GOLD)
            b.append(f"{s}\n", style=f"bold {GOLD}" if on else (MUTED if i < self.step else DIM))
        st = self.stats
        loaded = ", ".join(st.get("loaded") or []) or "ingen lastet"
        b.append("\nsvarer  ", style=MUTED)
        if self.cloud:
            b.append(f"☁ {self.model}\n", style=f"bold {BAD}")
            b.append("        sky – samtalen sendes ut\n", style=BAD)
        else:
            b.append(f"{self.model or '?'} · lokal\n")
        b.append("i GPU   ", style=MUTED)
        b.append(f"{loaded}\n")
        ctx = st.get("context") or [0, 0]
        if ctx and ctx[1]:
            b.append("kontekst ", style=MUTED)
            b.append_text(_bar(ctx[0] / ctx[1], 14))
            b.append(f" {ctx[0] // 1000}k/{ctx[1] // 1000}k\n", style=MUTED)
        tokens = sum((st.get("session_tokens") or {}).values()) if isinstance(st.get("session_tokens"), dict) else 0
        b.append("økt     ", style=MUTED)
        b.append(f"{st.get('messages', 0)} meld. · {tokens / 1000:.1f}k tokens\n")
        self.query_one("#brain", Static).update(b)

        ask = self.query_one("#ask", Static)
        p = self.gate.current()
        ask.display = p is not None
        if p is not None:
            ask.border_title = self.gate.title(p)
            ask.update(self.gate.card(p))

        keys = Text(no_wrap=True, overflow="crop")
        for k, v in (("⏎", "send"), ("ctrl+j/n", "godkjenn/avslå"), ("ctrl+v", "se hele"), ("ctrl+s", "stopp"),
                     ("F2", "AI av" if self.ai else "AI på"), ("F3", "frigjør GPU"), ("F5", "ny økt"), ("F7", "modell"),
                     ("ctrl+q", "lukk")):
            keys.append(f" {k}", style=f"bold {GOLD}")
            keys.append(f" {v}  ", style=MUTED)
        self.query_one("#keys", Static).update(keys)

    # --- styring ------------------------------------------------------------------------------------
    async def on_input_submitted(self, e: Input.Submitted) -> None:
        text = e.value.strip()
        if not text:
            return
        e.input.value = ""
        if not self.ai:
            self.notify("Zeus er av – trykk F2 for å slå ham på.", severity="warning", markup=False)
            return
        await self.send({"type": "send", "text": text})

    def must_view(self, p: dict[str, Any]) -> bool:
        return self.gate.must_view(p)

    async def action_approve(self, yes: bool, aid: str | None = None) -> None:
        """Svar på godkjenningen panelet viser (aldri en som kom etter at du så på skjermen)."""
        verdict, p = self.gate.decide(yes, aid)
        if verdict in ("view", "confirm"):
            self.notify(self.gate.notice(verdict), severity="warning", markup=False)
            if verdict == "view":
                self.bell()
            self.render_all()
            return
        if verdict != "send" or p is None:
            return
        await self.send({"type": "approve", "id": p["id"], "answer": "yes" if yes else "no"})
        self.log_line("✓" if yes else "✗", f"du {'godkjente' if yes else 'avslo'}: {p.get('reason')}", OK if yes else BAD)

    async def action_interrupt(self) -> None:
        await self.send({"type": "interrupt"})
        self.log_line("■", "stoppet svaret", MUTED)

    async def action_toggle_ai(self) -> None:
        if self.session is None:
            return
        try:
            async with self.session.post(self.base + "/api/v1/ai", json={"on": not self.ai}, headers={"Origin": self.base}) as r:
                self.ai = bool((await r.json()).get("ai"))
        except (aiohttp.ClientError, ValueError):
            self.notify("Fikk ikke kontakt med serveren.", severity="error", markup=False)
        self.state = "ledig" if self.ai else "av"
        self.render_all()

    async def action_unload(self) -> None:
        await self.send({"type": "command", "name": "unload"})

    def action_view_preview(self) -> None:
        """ctrl+v: hele forhåndsvisningen i et rullbart vindu; godkjenn/avslå også derfra."""
        p = self.gate.view()
        if p is None:
            return
        self.render_all()

        def done(answer: str | None) -> None:
            if answer is not None:  # gjelder akkurat kallet som ble vist i vinduet
                self.run_worker(self.action_approve(answer == "yes", str(p.get("id"))))

        self.push_screen(FullPreview(p), done)

    async def action_pick_model(self) -> None:
        """F7: velg modell via hjernen (samme liste som Zeus-appen og TUI-en)."""
        if self.session is None:
            self.notify("Ingen kontakt med Olympus-serveren.", severity="error", markup=False)
            return
        try:
            async with self.session.get(self.base + "/api/v1/models") as r:
                data = await r.json()
        except (aiohttp.ClientError, ValueError):
            self.notify("Fikk ikke modellista fra serveren.", severity="error", markup=False)
            return
        self.model, self.cloud = str(data.get("current") or ""), bool(data.get("cloud"))

        def done(ref: str | None) -> None:
            if ref and ref != self.model:
                self.run_worker(self.set_model(ref))

        self.push_screen(ModelChooser(list(data.get("choices") or []), self.model), done)

    async def set_model(self, ref: str) -> None:
        assert self.session is not None
        try:
            async with self.session.post(self.base + "/api/v1/models", json={"model": ref},
                                         headers={"Origin": self.base}) as r:
                data = await r.json()
        except (aiohttp.ClientError, ValueError):
            self.notify("Fikk ikke kontakt med serveren.", severity="error", markup=False)
            return
        if "error" in data:
            self.notify(str(data["error"]), severity="error", markup=False)
            return
        self.model, self.cloud = str(data.get("current") or ""), bool(data.get("cloud"))
        if data.get("warning"):
            self.log_line("☁", str(data["warning"]), BAD)
        self.render_all()

    async def action_new(self) -> None:
        await self.send({"type": "new"})
        self.query_one("#feed", RichLog).clear()
        self.log_line("·", "ny økt", MUTED)


def run(config: Any) -> int:
    from olympus.web.server import get_token

    ZeusWindow(f"http://127.0.0.1:{int(config.get('web.port', 8787))}", get_token(config)).run()
    return 0
