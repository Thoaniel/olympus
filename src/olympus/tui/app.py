"""Olympus' Textual-app."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Button, Footer, Markdown, Static
from textual.worker import Worker, WorkerState

from olympus import __version__
from olympus.agents.orchestrator import Orchestrator
from olympus.backends.llamacpp import BackendError
from olympus.config import Config
from olympus.gpu import read_vram, short_gpu_name
from olympus.model_choices import (
    ModelCatalog,
    cloud_warning,
    current,
    resolve_choice,
    set_model,
)
from olympus.model_manager import ModelManager, VramError
from olympus.sessions import SessionStore, title_from
from olympus.tui import banner
from olympus.tui.chrome import ModelBar, Rail, SessionPanel, SettingsScreen, Welcome
from olympus.tui.model_picker import ModelPicker, ProvidersScreen
from olympus.tui.widgets import (
    AgentMessage,
    PromptInput,
    SaveFiles,
    StatusBar,
    ToolApproval,
    ToolLine,
    UserMessage,
)

RAIL_MIN_WIDTH = 70  # under dette vises ikke engang skinnene (samtalen trenger hver kolonne)
SESSIONS_MIN_WIDTH = 150  # øktpanelet vises fra denne bredden (samtalen beholder ~80 kolonner)

COMMANDS: dict[str, str] = {
    "/help": "vis kommandoer og hurtigtaster",
    "/new": "start en ny samtale",
    "/retry": "send forrige melding på nytt",
    "/modell": "bytt modell Zeus svarer med (F7) · /modell <id> · /modell lokal",
    "/minne": "vis hva Zeus husker om deg og om arbeidet (USER.md og MEMORY.md)",
    "/komprimer": "oppsummer eldre deler av samtalen nå (skjer ellers automatisk ved 50 % av kontekstvinduet)",
    "/lagre": "lagre filene fra siste svar: /lagre [mappe] (standard: arbeidsmappen)",
    "/providers": "koble til sky-modeller (F3)",
    "/models": "list modeller i config og om de er lastet ned",
    "/feil": "rett et feil svar: /feil <hva som er riktig>",
    "/lekser": "vis lagrede rettelser · /lekser slett <id>",
    "/bibliotek": "søk i biblioteket (Arch Wiki + egne dokumenter): /bibliotek <søk>",
    "/lab": "Olympus Lab's: hjem, zeus, plan, media og PC – som Zeus-appen (F6)",
    "/vram": "vis VRAM-bruk og lastede modeller",
    "/unload": "fjern språkmodeller fra VRAM: /unload [modell]",
    "/usage": "vis kontekstbruk for samtalen",
    "/quit": "avslutt Olympus",
}


def short_path(p: Path) -> str:
    home = Path.home()
    try:
        return "~/" + str(p.relative_to(home)) if p != home else "~"
    except ValueError:
        return str(p)


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:g}M"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


from olympus.tui.navigation import HelpScreen, OlympusCommands, click, cmd_action
from olympus.tui.system_panel import MIN_WIDTH as PANEL_MIN_WIDTH
from olympus.tui.system_panel import SystemPanel


class OlympusApp(App):
    TITLE = "Olympus"
    COMMANDS_HELP = COMMANDS  # /kommandoene (modulens tabell), brukt av hjelp og palett
    COMMANDS = App.COMMANDS | {OlympusCommands}  # Ctrl+P: Textuals egne + alle Olympus-handlinger
    BINDINGS = [
        Binding("ctrl+c", "interrupt", "Avbryt", priority=True),
        Binding("ctrl+c", "clear_prompt", "Tøm", priority=True, show=False),  # når ingenting genereres
        Binding("ctrl+q", "quit", "Avslutt", priority=True, show=False),
        Binding("ctrl+p", "command_palette", "Kommandoer"),
        Binding("f1", "help", "Hjelp"),
        Binding("f2", "settings", "Innstillinger"),
        Binding("f3", "providers", "Sky-modeller", show=False),
        Binding("ctrl+o", "toggle_sessions", "Økter"),
        Binding("f4", "toggle_panel", "Panel"),
        Binding("f7", "pick_model", "Modell", priority=True),
        Binding("f6", "lab", "Lab's", show=False, priority=True),  # ellers tar meldingsfeltet F6 først
        Binding("ctrl+l", "new_session", "Ny økt"),
        Binding("ctrl+r", "run_cmd('/retry')", "Prøv igjen", show=False),
        Binding("escape", "focus_prompt", "Til meldingen", show=False),
        Binding("pageup", "log_page(-1)", "Rull opp", priority=True, show=False),
        Binding("pagedown", "log_page(1)", "Rull ned", priority=True, show=False),
        Binding("ctrl+home", "log_home", "Toppen", show=False),
        Binding("ctrl+end", "log_end", "Bunnen", show=False),
    ]

    def __init__(self, config: Config) -> None:
        self.config = config
        self.manager = ModelManager(config, on_status=self._on_manager_status)
        self.orch = Orchestrator(config, self.manager)
        self.loading: str | None = None
        self.load_error = False
        self._gen_worker: Worker | None = None
        self._last: str | None = None  # forrige melding (/retry)
        self._tool_answer: asyncio.Future[str] | None = None
        self._current_msg: AgentMessage | None = None
        # Panelene kan legges sammen til en smal skinne (F4 / Ctrl+O / klikk). Tilstanden huskes mellom oppstarter.
        self.brain = None  # BrainClient når TUI-en er en skjerm mot den felles hjernen (olympus-web)
        self.remote = False
        self._ui_path = config.path("sessions_dir").parent / "ui.json"
        ui_state = self._load_ui_state()
        self._panel_hidden = bool(ui_state.get("panel_collapsed", False))
        self._sessions_hidden = bool(ui_state.get("sessions_collapsed", False))
        self._sections_collapsed = set(ui_state.get("sections_collapsed", []))
        self.sessions = SessionStore(config.path("sessions_dir"))
        self.session_id: str | None = None  # lages ved første melding
        self._transcript: list[dict] = []  # det som vises (hvem sa hva), lagres med økten
        self.gen_stats: dict = {}  # fart for panelet: {"live", "tps", "ttft"}
        self._g: dict | None = None  # svaret som strømmes nå: start, første token, antall, modell
        self._ticker = None
        self._pipeline: Static | None = None
        self._pipeline_text: Text | None = None
        ui = config.theme["ui"]
        self.CSS = f"""
        Screen {{ background: {ui["background"]}; color: {ui["text"]}; }}
        * {{ link-style: none; link-color: initial; link-background: initial;
             link-style-hover: bold; link-color-hover: {ui["accent"]}; link-background-hover: {ui["surface"]}; }}
        #body {{ height: 1fr; }}
        #log:focus {{ background: {ui["surface"]} 30%; }}
        #syspanel:focus {{ border-left: vkey {ui["accent"]}; }}
        Footer {{ background: {ui["surface"]}; }}
        #log {{ height: 1fr; width: 1fr; padding: 0 2; scrollbar-size-vertical: 1; }}
        #syspanel {{ width: 20%; min-width: 26; height: 1fr; padding: 1 1 0 2; background: {ui["surface"]};
                     border-left: vkey {ui["accent"]} 30%; scrollbar-size: 0 0; }}
        #sessions {{ width: 26; height: 1fr; background: {ui["surface"]}; border-right: vkey {ui["accent"]} 30%;
                     padding: 1 0 0 1; }}
        #sessions-head {{ height: 1; margin: 0 1 1 0; }}
        #sessions-list {{ height: 1fr; border: none; background: {ui["surface"]}; padding: 0; scrollbar-size: 0 0; }}
        #sessions:focus-within {{ border-right: vkey {ui["accent"]}; }}
        .rail {{ width: 3; height: 1fr; background: {ui["surface"]}; padding: 1 0 0 0; display: none; }}
        .rail:hover {{ background: {ui["accent"]} 20%; }}
        #sessions-rail {{ border-right: vkey {ui["accent"]} 30%; }}
        #syspanel-rail {{ border-left: vkey {ui["accent"]} 30%; }}
        #welcome {{ width: 100%; padding: 1 0 0 0; content-align: center top; text-wrap: nowrap; text-overflow: ellipsis; }}
        #banner {{ width: 100%; content-align: center top; text-align: center; margin: 1 0 0 0;
                   text-wrap: nowrap; text-overflow: clip; }}
        #banner.compact {{ margin: 0; }}
        #banner-info {{ width: 100%; text-align: center; color: {ui["muted"]}; margin: 1 0 1 0; }}
        .user-msg {{ margin: 1 0 0 0; color: {ui["text"]}; }}
        .sys-msg {{ margin: 1 0 0 2; color: {ui["muted"]}; }}
        .pipeline {{ margin: 0 0 0 2; color: {ui["muted"]}; text-style: italic; }}
        .err-msg {{ margin: 1 0 0 2; color: {ui["error"]}; }}
        Markdown {{ background: transparent; }}
        #suggest {{ height: auto; max-height: 8; padding: 0 2; color: {ui["muted"]}; display: none; }}
        #prompt {{ height: auto; min-height: 3; max-height: 10; margin: 0 1;
                   background: {ui["surface"]}; border: round {ui["accent"]} 40%; }}
        #prompt:focus {{ border: round {ui["accent"]}; }}
        #bottombar {{ height: 1; background: {ui["surface"]}; }}
        #modelbar {{ width: auto; height: 1; padding: 0 1 0 2; }}
        StatusBar {{ width: 1fr; height: 1; padding: 0 2 0 1; text-align: right; color: {ui["muted"]};
                     text-wrap: nowrap; text-overflow: ellipsis; }}
        #topbar {{ height: 2; background: {ui["surface"]}; }}
        #title {{ width: 1fr; height: 1; padding: 0 2; color: {ui["accent"]}; text-style: bold; }}
        #topbar Button {{ min-width: 5; height: 1; margin: 0 1 0 0; background: {ui["surface"]}; border: none; }}
        #topbar Button:hover {{ background: {ui["accent"]} 30%; }}
        #topbar #model.cloud {{ color: {ui["background"]}; background: {ui["error"]}; text-style: bold; }}
        """
        super().__init__()
        # Eget Textual-tema: innebygde widgets (rullefelt, Ctrl+P, lister, markeringer) bruker ellers
        # Textuals standardblå, som skjærer seg mot gullet.
        from textual.theme import Theme

        self.register_theme(Theme(
            name="olympus", primary=ui["accent"], secondary=ui["muted"], accent=ui["accent"],
            warning=ui["accent"], error=ui["error"], success=ui.get("success", "#8fbf6f"),
            foreground=ui["text"], background=ui["background"], surface=ui["surface"], panel=ui["surface"],
            dark=True,
        ))
        self.theme = "olympus"
        self.catalog = ModelCatalog(config, self.orch.backends)  # modellvalget (/modell, F7)
        from olympus.thermal import Thermal

        self._thermal = Thermal(config)

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("ϟ ZEUS · OLYMPUS", id="title")
            yield Button("", id="model", tooltip="Modellen Zeus svarer med (F7 bytter)", compact=True)
            yield Button("⚙ innstillinger", id="settings", tooltip="Modell, sky-modeller og utseende (F2)", compact=True)
        with Horizontal(id="body"):
            yield SessionPanel(self.sessions, id="sessions")
            yield Rail("ØKTER", "»", "app.toggle_sessions", "Vis øktene (Ctrl+O)", id="sessions-rail", classes="rail")
            with VerticalScroll(id="log"):
                yield Static(id="banner")
                yield Static(id="banner-info")
                yield Welcome(id="welcome")
            yield Rail("SYSTEM", "«", "app.toggle_panel", "Vis systempanelet (F4)", id="syspanel-rail", classes="rail")
            yield SystemPanel(self.config, self.manager, self.orch, self._sections_collapsed, id="syspanel")
        yield Static(id="suggest")
        yield PromptInput(id="prompt", placeholder="Skriv en melding  ·  / for kommandoer  ·  F1 hjelp")
        with Horizontal(id="bottombar"):  # én linje: hvem som svarer (venstre) og status (høyre)
            yield ModelBar(id="modelbar")
            yield StatusBar(id="status")
        yield Footer(id="footer", show_command_palette=False)

    def _layout_panel(self, width: int | None = None) -> None:
        """Panelet vises når terminalen er bred nok og brukeren ikke har skjult det."""
        w = width or self.size.width
        panel = self.query_one("#syspanel", SystemPanel)
        show = w >= PANEL_MIN_WIDTH and not self._panel_hidden
        if panel.display != show:
            panel.display = show
            if show:
                panel.refresh_panel()
        # Øktpanelet trenger enda mer plass: samtalen skal alltid ha minst ~80 kolonner.
        sessions = self.query_one("#sessions", SessionPanel)
        sessions.display = w >= SESSIONS_MIN_WIDTH and not self._sessions_hidden
        # Sammenlagt (eller for trangt) blir panelet en smal skinne – men ikke på helt smale skjermer.
        self.query_one("#syspanel-rail").display = not show and w >= RAIL_MIN_WIDTH
        self.query_one("#sessions-rail").display = not sessions.display and w >= RAIL_MIN_WIDTH

    def _load_ui_state(self) -> dict:
        try:
            import json

            return json.loads(self._ui_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_ui_state(self) -> None:
        import json

        panel = self.query_one("#syspanel", SystemPanel)
        state = {"panel_collapsed": self._panel_hidden, "sessions_collapsed": self._sessions_hidden,
                 "sections_collapsed": sorted(panel.collapsed)}
        try:
            self._ui_path.parent.mkdir(parents=True, exist_ok=True)
            self._ui_path.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            pass  # oppsettet er bare bekvemmelighet; aldri en grunn til å feile

    def action_toggle_section(self, key: str) -> None:
        self.query_one("#syspanel", SystemPanel).toggle(key)

    def action_toggle_sessions(self) -> None:
        self._sessions_hidden = not self._sessions_hidden
        self.save_ui_state()
        self.call_after_refresh(self._apply_banner)
        if not self._sessions_hidden and self.size.width < SESSIONS_MIN_WIDTH:
            self.notify(f"Øktpanelet trenger minst {SESSIONS_MIN_WIDTH} kolonner (nå {self.size.width}). "
                        "Ctrl+P → «Åpne økt» virker alltid.")
        self._layout_panel()
        sessions = self.query_one("#sessions", SessionPanel)
        if sessions.display:
            sessions.reload()
            sessions.query_one("#sessions-list").focus()
        else:
            self.query_one(PromptInput).focus()

    def action_fill_prompt(self, text: str) -> None:
        self.fill_prompt(text)

    # --- innstillinger ------------------------------------------------------
    def action_settings(self, tab: str = "models") -> None:
        def done(result: tuple[str, str] | None) -> None:
            if not result:
                return
            kind, arg = result
            if kind == "model":
                self.run_worker(self.switch_model(arg))
            elif kind == "providers":
                self.action_providers()

        self.push_screen(SettingsScreen(self, tab), done)

    # --- økter -------------------------------------------------------------
    def _save_session(self) -> None:
        if self.remote:  # hjernen lagrer økten; her oppdateres bare listen
            self.session_id = self.orch.stats.get("session") or self.session_id
            panel = self.query_one("#sessions", SessionPanel)
            panel.current = self.session_id
            if panel.display:
                panel.reload()
            return
        if not self._transcript or not self.orch.messages:
            return
        if self.session_id is None:
            self.session_id = self.sessions.new_id()
        self.orch.toolbox.session_id = self.session_id
        first = next((e["text"] for e in self._transcript if e["kind"] == "user"), "Ny økt")
        self.sessions.save(self.session_id, title_from(first), self.orch.messages, self._transcript)
        panel = self.query_one("#sessions", SessionPanel)
        panel.current = self.session_id
        if panel.display:
            panel.reload()

    async def open_session(self, sid: str) -> None:
        data = self.sessions.load(sid)
        if data is None:
            self.notify("Fant ikke økten.", severity="error")
            return
        if self._generating:
            self.notify("Vent til svaret er ferdig, eller avbryt med Ctrl+C.", severity="warning")
            return
        if self.remote:
            await self.brain.send({"type": "open", "sid": sid})  # hjernen svarer med «history» til alle skjermer
            return
        await self._reset_session()
        self.session_id = sid
        self.orch.toolbox.session_id = sid
        self.orch.messages.extend(data.get("messages", []))
        self._transcript = list(data.get("transcript", []))
        await self._render_transcript()

    async def _clear_view(self) -> None:
        log = self.query_one("#log", VerticalScroll)
        for child in list(log.children)[3:]:
            await child.remove()
        self._transcript = []
        self.query_one("#welcome").display = True
        self._apply_banner()

    async def _render_transcript(self) -> None:
        sid = self.session_id
        self.query_one("#welcome").display = False
        self._apply_banner()
        for e in self._transcript:
            if e.get("kind") == "user":
                await self._log(UserMessage(e.get("text", ""), self.config.agent_color("user")))
            elif e.get("kind") == "agent":
                agent = e.get("agent", "zeus")
                try:
                    a = self.config.agent(agent)
                except KeyError:
                    a = {"icon": "✦", "display": agent}
                msg = AgentMessage(a["icon"], a["display"], self.config.agent_color(agent))
                await self._log(msg)
                msg.set_header(e.get("model", ""))
                for t in e.get("tools", []):  # hva Zeus gjorde (lagret siden okt. 2026), som i appen
                    await msg.add(Static(Text(f"│ {t}", style="dim")))
                await msg.body.update(e.get("text", ""))
                if e.get("draft"):
                    msg.fold_as_draft(e.get("rejected", ""))
        panel = self.query_one("#sessions", SessionPanel)
        panel.current = sid
        panel.reload()
        self.query_one("#log", VerticalScroll).scroll_end(animate=False)
        self.query_one(PromptInput).focus()
        self.refresh_status()

    def on_resize(self, event) -> None:
        self._layout_panel(event.size.width)  # self.size er ikke alltid oppdatert ennå
        self.call_after_refresh(self._apply_banner)
        self.query_one("#settings", Button).label = "⚙ innstillinger" if event.size.width >= 100 else "⚙"
        self.call_after_refresh(self.refresh_status)

    # --- navigasjon -------------------------------------------------------
    def action_run_cmd(self, line: str) -> None:
        """Kjør en /kommando (fra klikk, palett, hjelp eller tast)."""
        self.run_worker(self.run_command(line), group="cmd")

    def fill_prompt(self, text: str) -> None:
        """Legg tekst i meldingsfeltet og gi det fokus (for kommandoer som trenger et argument)."""
        prompt = self.query_one(PromptInput)
        prompt.text = text
        prompt.move_cursor(prompt.document.end)
        prompt.focus()

    def action_pick_model(self) -> None:
        """F7 / klikk på modellen: velg hvem som svarer for Zeus."""
        self.run_worker(self._open_picker(), group="picker", exclusive=True)

    def action_focus_prompt(self) -> None:
        self.query_one(PromptInput).focus()

    def action_help(self) -> None:
        def done(cmd: str | None) -> None:
            if not cmd:
                return
            if cmd in {"/feil", "/bibliotek", "/unload"}:
                self.fill_prompt(cmd + " ")
            else:
                self.action_run_cmd(cmd)

        self.push_screen(HelpScreen(COMMANDS), done)

    def action_log_page(self, direction: int) -> None:
        log = self.query_one("#log", VerticalScroll)
        log.scroll_page_down() if direction > 0 else log.scroll_page_up()

    def action_log_home(self) -> None:
        self.query_one("#log", VerticalScroll).scroll_home()

    def action_log_end(self) -> None:
        self.query_one("#log", VerticalScroll).scroll_end()

    def action_toggle_panel(self) -> None:
        self._panel_hidden = not self._panel_hidden
        self.save_ui_state()
        if not self._panel_hidden and self.size.width < PANEL_MIN_WIDTH:
            self.notify(f"Systempanelet trenger minst {PANEL_MIN_WIDTH} kolonner (nå {self.size.width}).")
        self._layout_panel()
        self.call_after_refresh(self._apply_banner)

    def action_lab(self) -> None:
        """Olympus Lab's: samme app som Zeus på telefonen (faner 1–5). z tilbake til chatten."""
        from olympus.tui.lab_view import Api, LabScreen
        from olympus.web.server import get_token

        if isinstance(self.screen, LabScreen):
            return
        url = self.config.get("web.brain_url") or f"http://127.0.0.1:{int(self.config.get('web.port', 8787))}"
        self.push_screen(LabScreen(Api(url, get_token(self.config))))

    def _maybe_lab(self) -> None:
        """Olympus starter i Lab's (som Zeus-appen på telefonen); z går til chatten."""
        if self.config.get("general.open_labs", True):
            self.action_lab()

    async def on_mount(self) -> None:
        self.call_after_refresh(self._maybe_lab)
        self._layout_panel()
        self._banner_mode = self._wanted_banner_mode()
        self._banner_lines = banner.banner_lines(self._banner_mode)
        self._style_banner()
        if self.config.get("general.startup_animation", True) and self._banner_mode != "compact":
            self._banner_shown = 0
            self._banner_timer = self.set_interval(0.035, self._animate_banner)
        else:
            self._draw_banner(len(self._banner_lines))
        self.refresh_all()
        self.query_one(PromptInput).focus()
        self.set_interval(2.0, self.refresh_status)
        self.set_interval(5.0, self._check_terminal)
        await self._connect_brain()
        if not self.remote:
            self.run_worker(self._preload(), group="load")
        self.run_worker(self._index_library(), group="library", thread=False)

    def _check_terminal(self) -> None:
        """Lukkes terminalen uten at SIGHUP kommer fram, blir stdin en slettet pty: avslutt i stedet for å spinne."""
        import os

        try:
            gone = os.readlink("/proc/self/fd/0").endswith("(deleted)")
        except OSError:
            return
        if gone and (hook := getattr(self, "_terminal_gone", None)):
            hook()

    # --- én felles hjerne (olympus-web) -----------------------------------------
    async def _connect_brain(self) -> None:
        """Er hjernen oppe, blir TUI-en en skjerm mot den: samme samtale og modell som telefonen. Ellers lokalt."""
        if self.config.get("general.brain", "auto") == "local":
            return
        from olympus.tui.remote import BrainClient, RemoteManager, RemoteOrchestrator
        from olympus.web.server import get_token

        url = self.config.get("web.brain_url") or f"http://127.0.0.1:{int(self.config.get('web.port', 8787))}"
        client = BrainClient(url, get_token(self.config), self._on_brain)
        if not await client.connect():
            return
        self.brain = client
        self.orch = RemoteOrchestrator(self.orch, client)
        self.manager = RemoteManager(self.manager, self.orch)
        panel = self.query_one("#syspanel", SystemPanel)
        panel.orch, panel.manager = self.orch, self.manager
        self.remote = True
        self.set_interval(2.0, lambda: self.run_worker(client.send({"type": "stats"}), group="stats", exclusive=True))
        self.notify("◆ Koblet til hjernen – samme samtale som på telefonen.", timeout=3)

    async def _on_brain(self, msg: dict) -> None:
        from olympus.tui.remote import handle_turn_message

        kind = msg.get("type")
        if kind == "stats":
            self.orch.stats = msg["stats"]
            return
        if kind == "user" and msg.get("origin") != self.brain.id and not self._generating:
            # En tur startet på telefonen: følg den her også. Køen lages med en gang, så ingen hendelser går tapt.
            import asyncio as _asyncio

            self.orch.queue = _asyncio.Queue()
            await self._follow_remote(msg.get("text", ""))
            return
        if handle_turn_message(self.orch, msg):
            return
        if kind == "approval_done":
            fut = getattr(self, "_tool_answer", None)
            if fut is not None and not fut.done():
                fut.set_result("__done__")  # besvart på telefonen
        elif kind == "notice":
            self.notify(msg.get("text", ""), timeout=4)
        elif kind in ("state", "model"):
            st = msg.get("state", {}) if kind == "state" else msg
            ref = st.get("model")
            if isinstance(ref, str) and ref and ref != self.current_model():
                self.config.data.setdefault("agents", {}).setdefault("zeus", {})["model"] = ref  # hjernen har lagret
                self.orch.backends.reset()
            self.refresh_all()
        elif kind == "cleared" and not self._generating:
            await self._clear_view()
        elif kind == "history" and not self._generating:
            await self._clear_view()
            self.session_id = msg.get("session")
            self._transcript = list(msg.get("items", []))
            await self._render_transcript()
        elif kind == "disconnected":
            self.notify("Mistet kontakten med hjernen (olympus-web). Start Olympus på nytt for å koble til igjen.",
                        severity="warning", timeout=8)

    async def _follow_remote(self, text: str) -> None:
        self.refresh_all()
        self.query_one("#welcome").display = False
        self._transcript.append({"kind": "user", "text": text})
        self._apply_banner()
        await self._log(UserMessage(text, self.config.agent_color("user")))
        self._gen_worker = self.run_worker(self._generate(text, follow=True), group="gen", exclusive=True)
        self.refresh_status()

    def _draw_banner(self, n: int) -> None:
        ui = self.config.theme["ui"]
        lines = self._banner_lines[-n:] if n else []
        # Fyll opp med tomme linjer så høyden er stabil under animasjonen.
        padded = [""] * (len(self._banner_lines) - len(lines)) + lines
        self.query_one("#banner", Static).update(
            banner.render(padded, ui["gradient_start"], ui["gradient_end"])
        )

    def _wanted_banner_mode(self) -> str:
        """Det store tempelet hører til velkomsten på en stor skjerm. Når samtalen er i gang, eller det er
        trangt, krymper banneret til én linje (som Claude Code), så samtalen eier skjermen."""
        if self._transcript:
            return "compact"
        w, h = self.size.width, self.size.height
        log_w = w - (26 if w >= SESSIONS_MIN_WIDTH and not self._sessions_hidden else 3) \
            - (max(26, w // 5) if w >= PANEL_MIN_WIDTH and not self._panel_hidden else 3) - 4
        if log_w >= 66 and h >= 44:
            return "full"
        if log_w >= 66 and h >= 28:
            return "logo"
        return "compact"

    def _style_banner(self) -> None:
        compact = self._banner_mode == "compact"
        self.query_one("#banner").set_class(compact, "compact")
        self.query_one("#banner-info").display = not compact

    def _apply_banner(self) -> None:
        mode = self._wanted_banner_mode()
        if mode == getattr(self, "_banner_mode", None):
            return
        timer = getattr(self, "_banner_timer", None)
        if timer is not None:
            timer.stop()
        self._banner_mode = mode
        self._banner_lines = banner.banner_lines(mode)
        self._style_banner()
        self._draw_banner(len(self._banner_lines))

    def _animate_banner(self) -> None:
        self._banner_shown += 1
        self._draw_banner(self._banner_shown)
        if self._banner_shown >= len(self._banner_lines):
            self._banner_timer.stop()

    def refresh_all(self) -> None:
        """Oppdater alle felt: modellknapp, banner, modellinje og statuslinje."""
        ref = self.current_model()
        cloud = self.orch.backends.is_cloud(ref)
        btn = self.query_one("#model", Button)
        btn.set_class(cloud, "cloud")
        btn.label = f"☁ {ref} · sky" if cloud else f"ϟ {ref}"
        self._update_banner_info()
        self.query_one(ModelBar).update_bar(self)
        self.query_one(Welcome).update_welcome(self)
        self.refresh_status()

    def current_model(self) -> str:
        return current(self.config)

    # --- modellvalg ---------------------------------------------------------
    async def _open_picker(self) -> None:
        choices = None
        if self.remote:  # hjernen eier valget; samme liste som telefonen og `olympus zeus` ser
            choices = await self.brain_models()
        self.push_screen(ModelPicker(self.catalog, self.current_model(), choices),
                         lambda ref: self.run_worker(self.switch_model(ref)) if ref else None)

    async def brain_models(self) -> list[dict] | None:
        import aiohttp

        from olympus.web.server import COOKIE

        try:
            async with aiohttp.ClientSession(cookies={COOKIE: self.brain.token}) as s, \
                    s.get(self.brain.url + "/api/v1/models", timeout=aiohttp.ClientTimeout(total=5)) as r:
                return (await r.json()).get("choices")
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None

    async def switch_model(self, wanted: str) -> None:
        """Bytt modellen Zeus svarer med. Lagres (agents.zeus.model) og gjelder til du bytter tilbake."""
        if self._generating:
            self.notify("Vent til svaret er ferdig, eller avbryt med Ctrl+C.", severity="warning")
            return
        if self.remote:
            await self.brain.send({"type": "command", "name": "model", "model": wanted})
            return  # hjernen lagrer, og svarer med «model» (+ advarsel ved sky) til alle skjermer
        ref = resolve_choice(self.catalog, wanted)
        if ref is None:
            await self.say(f"Ukjent modell: {wanted}. Åpne valget med /modell (F7).", "err-msg")
            return
        set_model(self.config, ref)
        self.orch.backends.reset()
        b = self.orch.backends
        if not b.is_cloud(ref):  # frigjør andre lokale modeller og last den nye i bakgrunnen
            for m in self.manager.loaded():
                if m != ref:
                    await self.manager.unload(m)
            self.run_worker(self._preload(), group="load", exclusive=True)
        self.refresh_all()
        warn = cloud_warning(self.config, b, ref)
        if warn:
            await self.say(Text(warn, style=f"bold {self.config.theme['ui']['error']}"))
        else:
            await self.say(f"ϟ Zeus svarer nå med {ref} – lokalt, ingenting forlater PC-en.")

    def _update_banner_info(self) -> None:
        info = Text(justify="center")
        info.append(f"v{__version__}", style="bold")
        info.append(f"  ·  {short_gpu_name() or 'ingen AMD-GPU funnet'}")
        info.append("\n\n")
        ref = self.current_model()
        info.append("ϟ Zeus", style=click("app.pick_model", f"bold {self.config.agent_color('zeus')}"))
        if self.orch.backends.is_cloud(ref):
            info.append(f"  ☁ {ref} · sky – samtalen sendes ut", style=click("app.pick_model", self.config.theme["ui"]["error"]))
        else:
            info.append(f"  {ref} · lokal", style=click("app.pick_model", "dim"))
        self.query_one("#banner-info", Static).update(info)

    # --- status ----------------------------------------------------------
    def _on_manager_status(self, msg: str) -> None:
        if msg.startswith("Laster "):
            self.loading = msg.removeprefix("Laster ").removesuffix(" …")
        elif msg.endswith((" klar", " avbrutt")):
            self.loading = None
        self.call_later(self.refresh_status)

    def refresh_status(self) -> None:
        """Høyre del av bunnlinjen. Viktigst først (kuttes bakfra ved liten bredde); hvert segment er klikkbart.
        Det panelet allerede viser, står ikke her, og agentnavnet står i svarets overskrift."""
        ui = self.config.theme["ui"]
        try:
            panel_shown = self.query_one("#syspanel", SystemPanel).display
        except NoMatches:  # kalt fra en bakgrunnsjobb etter at skjermen er revet ned (avslutning)
            return
        segs: list[tuple[str, object]] = []
        if self._generating:
            segs.append(("Ctrl+C avbryter", click("app.interrupt", ui["accent"])))
        loaded = self.manager.loaded()
        if self.loading:
            segs.append((f"laster {self.loading} …", ui["accent"]))
        elif self.load_error and not loaded:
            segs.append(("ingen modell lastet", click(cmd_action("/models"), ui["error"])))
        temp, level = self._thermal.short()
        if temp and level != "ok":   # bare når det er varmt; normale temperaturer står i panelet
            segs.append((f"▲ {temp}", click("app.toggle_panel", {"het": f"bold {ui['error']}", "varm": ui["accent"]}.get(level, ""))))
        if not panel_shown:
            used, ctx = self.orch.context_used()
            vram = read_vram()
            if loaded and not self.loading:
                segs.append((" ".join(f"{m}✓" for m in loaded), click(cmd_action("/vram"))))
            segs.append((f"VRAM {vram.short() if vram else '?'}", click("app.toggle_panel")))
            segs.append((f"ctx {fmt_tokens(used)}/{fmt_tokens(ctx)}", click(cmd_action("/usage"))))
        segs.append((short_path(self.orch.cwd), ""))
        t = Text(no_wrap=True, overflow="ellipsis")
        for i, (text, style) in enumerate(segs):
            if i:
                t.append("  ·  ", style="dim")
            t.append(text, style=style)
        self.query_one(StatusBar).update(t)
        self.query_one(ModelBar).update_bar(self)
        panel = self.query_one("#syspanel", SystemPanel)
        if panel.display:
            panel.refresh_panel()

    @property
    def _generating(self) -> bool:
        return self._gen_worker is not None and self._gen_worker.state == WorkerState.RUNNING

    # --- logg ------------------------------------------------------------
    async def _log(self, widget) -> None:
        log = self.query_one("#log", VerticalScroll)
        at_bottom = log.max_scroll_y - log.scroll_y < 4
        await log.mount(widget)
        if at_bottom:
            log.scroll_end(animate=False)

    async def say(self, text: str | Text, cls: str = "sys-msg") -> None:
        await self._log(Static(text if isinstance(text, Text) else Text(text), classes=cls))

    async def _index_library(self) -> None:
        """Oppdater bibliotekindeksen i bakgrunnen (bare nye/endrede filer)."""
        try:
            await asyncio.to_thread(self.orch.toolbox.library.build)
        except Exception as e:  # skal aldri stoppe appen
            self.log(f"bibliotek: {e}")

    async def _preload(self) -> None:
        """Last Zeus' lokale modell mens brukeren skriver. Er en skymodell valgt, lastes ingenting."""
        b = self.orch.backends
        helpers = b.local_models_needed(["zeus"])
        try:
            for model_id in helpers:
                if b.available(model_id)[0]:
                    await self.manager.ensure(model_id)
        except (BackendError, VramError, KeyError) as e:
            self.load_error = True
            self.loading = None
            await self.say(str(e), "err-msg")
        self.refresh_status()

    # --- input -----------------------------------------------------------
    def on_text_area_changed(self, event: PromptInput.Changed) -> None:
        text = event.text_area.text
        suggest = self.query_one("#suggest", Static)
        if text.startswith("/") and " " not in text and "\n" not in text:
            matches = [c for c in COMMANDS if c.startswith(text)]
            if matches:
                body = Text()
                for i, c in enumerate(matches):
                    act = "app.fill_prompt(" + repr(c + " ") + ")"
                    body.append(("\n" if i else "") + c.ljust(10), style=click(act, "bold"))
                    body.append(f"  {COMMANDS[c]}", style=click(act, "dim"))
                suggest.update(body)
                suggest.display = True
                return
        suggest.display = False

    def on_prompt_input_tab_complete(self, _: PromptInput.TabComplete) -> None:
        prompt = self.query_one(PromptInput)
        matches = [c for c in COMMANDS if c.startswith(prompt.text.strip())]
        if matches:
            prefix = matches[0] if len(matches) == 1 else _common_prefix(matches)
            prompt.text = prefix + (" " if len(matches) == 1 else "")
            prompt.move_cursor(prompt.document.end)

    async def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        self.query_one("#suggest", Static).display = False
        text = event.value
        if text.startswith("/"):
            await self.run_command(text)
            return
        await self.start_generation(text)

    # --- generering ------------------------------------------------------
    async def start_generation(self, text: str) -> None:
        if self._generating:
            self.notify("Vent til svaret er ferdig, eller avbryt med Ctrl+C.", severity="warning")
            return
        self._last = text
        self.refresh_all()
        self.query_one("#welcome").display = False
        self._transcript.append({"kind": "user", "text": text})
        self._apply_banner()
        await self._log(UserMessage(text, self.config.agent_color("user")))
        self._gen_worker = self.run_worker(
            self._generate(text), group="gen", exclusive=True
        )
        self.refresh_status()

    def _agent_label(self, name: str) -> tuple[str, str]:
        a = self.config.agent(name)
        return f"{a['icon']} {a['display']}", self.config.agent_color(name)

    @staticmethod
    def _tool_detail(call) -> str:
        a = call.args
        if call.name == "shell":
            cmd = str(a.get("command", "")).replace("\n", " ⏎ ")
            return f"$ {cmd[:110]}{'…' if len(cmd) > 110 else ''}"
        if call.name == "search_files":
            return f"«{a.get('pattern', '')}» i {a.get('path') or '.'}"
        if call.name == "library_search":
            return f"«{a.get('query', '')}»"
        if call.name == "library_read":
            return str(a.get("ref", "")) + (f" § {a['section']}" if a.get("section") else "")
        return str(a.get("path") or ".")

    async def _approve_tool(self, call, approval) -> str:
        """Vis godkjenning i feeden og vent på svar (y/n/a)."""
        self._tool_answer = asyncio.get_running_loop().create_future()
        widget = ToolApproval(approval.reason, approval.preview, approval.preview_lang, approval.danger, call.name)
        target = self._current_msg
        if target is not None:
            await target.add(widget)
        else:
            await self._log(widget)
        self.query_one("#log", VerticalScroll).scroll_end(animate=False)
        self.bell()
        try:
            return await self._tool_answer
        finally:
            self._tool_answer = None
            await widget.remove()
            self.query_one(PromptInput).focus()

    def on_tool_approval_answered(self, event: ToolApproval.Answered) -> None:
        fut = getattr(self, "_tool_answer", None)
        if fut is not None and not fut.done():
            fut.set_result(event.answer)

    async def _generate(self, text: str, follow: bool = False) -> None:
        log = self.query_one("#log", VerticalScroll)
        pipeline = Static("", classes="pipeline")
        self._pipeline, self._pipeline_text = pipeline, None
        await self._log(pipeline)
        if self._ticker is None:
            self._ticker = self.set_interval(1 / 8, self._tick_gen)  # 8 fps holder for en spinner
        msg: AgentMessage | None = None
        entry: dict | None = None  # visningsutdraget for svaret som strømmes (lagres med økten)
        turn: list[tuple[AgentMessage, dict]] = []  # svarene i denne runden
        stream = None
        model = ""
        waiting = False  # headeren viser «laster modell …» til første token
        try:
            tool_lines: dict[str, ToolLine] = {}
            events = (self.orch.follow(approve=self._approve_tool) if follow else
                      self.orch.run(text, approve=self._approve_tool))
            async for ev in events:
                match ev.kind:
                    case "status":
                        label, color = self._agent_label(ev.agent)
                        self._pipeline_text = Text.assemble((label, color), f" {ev.text}")
                        self._tick_gen()
                    case "notice":
                        await self.say(ev.text)
                    case "agent_start":
                        a = self.config.agent(ev.agent)
                        msg = AgentMessage(a["icon"], a["display"], self.config.agent_color(ev.agent))
                        entry = {"kind": "agent", "agent": ev.agent, "model": ev.model, "text": ""}
                        self._transcript.append(entry)
                        turn.append((msg, entry))
                        self._current_msg = msg
                        await self._log(msg)
                        cloud = self.orch.backends.is_cloud(ev.model)
                        model = f"☁ {ev.model} · sendes til {self.orch.backends.provider_label(ev.model)}" if cloud else ev.model
                        waiting = cloud or ev.model not in self.manager.loaded()
                        msg.set_header(f"{model} · venter …" if cloud else f"{model} · laster modell …" if waiting else model)
                        self._g = {"t0": time.monotonic(), "first": None, "n": 0, "msg": msg,
                                   "model": model.split(" · ")[0], "state": "venter" if cloud else "laster modell" if waiting else "tenker"}
                        self._tick_gen()
                        stream = Markdown.get_stream(msg.body)
                        self.refresh_status()
                    case "chunk":
                        assert msg is not None and stream is not None and ev.chunk is not None
                        if waiting:
                            waiting = False
                            msg.set_header(model.split(" · ")[0])
                        if self._g is not None:
                            self._g["n"] += 1
                            if self._g["first"] is None:
                                self._g["first"] = time.monotonic()
                        if ev.chunk.kind == "content":
                            await stream.write(ev.chunk.text)
                            if entry is not None:
                                entry["text"] += ev.chunk.text
                        elif ev.chunk.kind == "reasoning":
                            await msg.add_reasoning(ev.chunk.text)
                        if log.max_scroll_y - log.scroll_y < 4:
                            log.scroll_end(animate=False)
                    case "tool_call":
                        assert msg is not None and ev.call is not None
                        if stream is not None:
                            await stream.stop()
                            stream = None
                        line = ToolLine(ev.call.name, self._tool_detail(ev.call))
                        tool_lines[ev.call.id] = line
                        await msg.add(line)
                        if log.max_scroll_y - log.scroll_y < 6:
                            log.scroll_end(animate=False)
                    case "tool_result":
                        if ev.call is not None and ev.result is not None and ev.call.id in tool_lines:
                            r = ev.result
                            await tool_lines[ev.call.id].finish(r.ok, r.summary, r.output, r.denied)
                    case "round":
                        assert msg is not None
                        stream = Markdown.get_stream(await msg.new_body())
                    case "judge_rejected":
                        # Svaret påsto at noe var gjort uten verktøy: det foldes som utkast, og Zeus prøver igjen.
                        if turn:
                            m, e = turn[-1]
                            m.fold_as_draft(ev.text)
                            e["draft"], e["rejected"] = True, ev.text
                    case "agent_end":
                        if stream is not None:
                            await stream.stop()
                            stream = None
                        if msg is not None:
                            cloud = self.orch.backends.is_cloud(ev.model)
                            name = f"☁ {ev.model}" if cloud else ev.model
                            msg.set_header(self._finish_stats(name))
                            await self._offer_files(msg, entry)
        except asyncio.CancelledError:
            if msg is not None:
                msg.set_header("avbrutt")
            else:
                await self.say("Avbrutt.")
            raise
        except (BackendError, VramError, KeyError, OSError, ValueError) as e:
            if msg is not None:
                msg.set_header("feil")
            await self.say(str(e), "err-msg")
        except Exception as e:  # uventet: vis det og logg det, men ikke krasj appen
            import traceback

            if msg is not None:
                msg.set_header("feil")
            log_path = self.config.path("logs_dir") / "crash.log"
            with log_path.open("a", encoding="utf-8") as f:
                f.write("".join(traceback.format_exception(e)))
            await self.say(f"Uventet feil: {type(e).__name__}: {e} (detaljer i {short_path(log_path)})", "err-msg")
        finally:
            if stream is not None:
                await stream.stop()
            await pipeline.remove()
            self._pipeline = None
            self._g = None
            if self._ticker is not None:
                self._ticker.stop()
                self._ticker = None
            if self.gen_stats:
                self.gen_stats["live"] = False
            self._current_msg = None
            try:
                self._save_session()
            except OSError as e:
                self.notify(f"Kunne ikke lagre økten: {e}", severity="warning")
            self.call_later(self.refresh_all)
            # Er modellen frigjort underveis, last den inn igjen i bakgrunnen.
            if not set(self.orch.backends.local_models_needed(["zeus"])) <= set(self.manager.loaded()):
                self.run_worker(self._preload(), group="load", exclusive=True)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """«^c Avbryt» vises i bunnlinjen bare mens noe genereres (ellers er det bare støy)."""
        if action == "interrupt":
            return self._generating
        if action == "clear_prompt":
            return not self._generating
        return True

    def on_worker_state_changed(self, event) -> None:
        if event.worker is self._gen_worker:
            self.refresh_bindings()

    async def _offer_files(self, msg: AgentMessage, entry: dict | None) -> None:
        """Svar med filmerkede kodeblokker (typisk fra modeller som ikke kan skrive filer selv) får en
        «⤓ Lagre filer»-linje. Filer som allerede ligger der med samme innhold (skrevet av write_file) hoppes over."""
        from olympus.codefiles import extract_files

        files = extract_files(entry["text"]) if entry else []
        base = self.orch.cwd
        fresh = []
        for rel, content in files:
            target = base / rel
            try:
                if target.exists() and target.read_text(encoding="utf-8") == content:
                    continue
            except OSError:
                pass
            fresh.append((rel, content))
        if fresh:
            await msg.add(SaveFiles(fresh, base))

    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def _spin(self) -> str:
        return self.SPINNER[int(time.monotonic() * 10) % len(self.SPINNER)]

    def _tick_gen(self) -> None:
        """Spinneren og tallene som viser at noe skjer: «⠋ laster modell · 4 s», så «⠙ 38 tok/s»."""
        now = time.monotonic()
        if self._pipeline is not None and self._pipeline_text is not None:
            self._pipeline.update(Text.assemble((self._spin() + " ", "bold"), self._pipeline_text))
        g = self._g
        if g is None:
            return
        if g["first"] is None:
            g["msg"].set_header(f"{g['model']} · {self._spin()} {g['state']} · {now - g['t0']:.0f} s")
            return
        span = now - g["first"]
        tps = g["n"] / span if span > 0.3 else 0.0
        g["msg"].set_header(f"{g['model']} · {self._spin()} {tps:.0f} tok/s" if tps else f"{g['model']} · {self._spin()}")
        self.gen_stats = {"live": True, "tps": tps, "ttft": g["first"] - g["t0"]}

    def _finish_stats(self, name: str) -> str:
        """Overskriften når svaret er ferdig: «qwopus-coder-9b · 6.2 s · 41 tok/s»."""
        g = self._g
        if g is None or g["first"] is None:
            return name
        end = time.monotonic()
        tokens = int(self.orch.last_usage.get("completion_tokens", 0) or 0) or g["n"]
        span = end - g["first"]
        tps = tokens / span if span > 0.2 else 0.0
        self.gen_stats = {"live": False, "tps": tps, "ttft": g["first"] - g["t0"]}
        self._g = None
        return f"{name} · {end - g['t0']:.1f} s" + (f" · {tps:.0f} tok/s" if tps else "")

    def _copy_selection(self) -> bool:
        """Ctrl+C kopierer når noe er markert (i samtalen eller i meldingsfeltet), som i en vanlig terminal.
        Textual har egen markering med musen, men vår Ctrl+C (avbryt/tøm) har prioritet og tok tasten."""
        text = self.screen.get_selected_text()
        focused = self.focused
        if not text and isinstance(focused, PromptInput):
            text = focused.selected_text
        if not text:
            return False
        self.copy_to_clipboard(text)  # OSC 52: kitty legger det på utklippstavla
        if not self.is_headless:  # og systemets utklippstavle i tillegg, i tilfelle OSC 52 ikke slipper gjennom (tmux)
            from olympus.sysutil import copy_text

            copy_text(text)
        self.screen.clear_selection()
        self.notify(f"Kopiert ({len(text)} tegn)", timeout=1.5)
        return True

    def action_interrupt(self) -> None:
        if self._copy_selection():
            return
        if self._generating and self._gen_worker is not None:
            self._gen_worker.cancel()
            return
        self.action_clear_prompt()

    def action_clear_prompt(self) -> None:
        if self._copy_selection():
            return
        prompt = self.query_one(PromptInput)
        if prompt.text:
            prompt.clear()
        else:
            self.notify("Ctrl+Q eller /quit for å avslutte.", timeout=2)

    async def action_new_session(self) -> None:
        await self.run_command("/new")

    # --- kommandoer ------------------------------------------------------
    async def run_command(self, line: str) -> None:
        cmd, _, arg = line.partition(" ")
        arg = arg.strip()
        match cmd:
            case "/help":
                lines = [f"{c:<10} {d}" for c, d in COMMANDS.items()]
                lines += [
                    "",
                    "Enter sender · Shift+Enter/Alt+Enter/Ctrl+J ny linje · ↑/↓ historikk",
                    "Tab fullfører kommandoer · Ctrl+C avbryter · Ctrl+L ny økt · Ctrl+Q avslutter",
                ]
                await self.say("\n".join(lines))
            case "/new":
                await self._reset_session()
            case "/retry":
                if not self._last:
                    await self.say("Ingenting å prøve på nytt.")
                    return
                msgs = self.orch.messages
                if msgs and msgs[-1]["role"] == "assistant":
                    while msgs and msgs.pop()["role"] != "user":  # hele runden, også verktøykallene
                        pass
                await self.start_generation(self._last)
            case "/minne":
                m = self.orch.memory
                t = Text()
                for target, title in (("user", "Om deg (USER.md)"), ("memory", "Zeus' notater (MEMORY.md)")):
                    entries = m.entries(target)
                    t.append(f"{title} – {m.size(target)}/{m.limits[target]} tegn\n", style="bold")
                    t.append("\n".join(f"  • {e}" for e in entries) + "\n\n" if entries else "  (tomt)\n\n", style="")
                t.append(f"Filene ligger i {short_path(m.root)} og kan redigeres direkte. Endringer gjelder fra neste økt.",
                         style="dim")
                await self.say(t)
            case "/komprimer":
                if self._generating:
                    await self.say("Vent til svaret er ferdig.", "err-msg")
                    return
                if self.remote:
                    await self.brain.send({"type": "command", "name": "komprimer"})
                    return
                done = False
                async for ev in self.orch._maybe_compress(self.orch.active_agent, force=True):
                    if ev.kind == "notice":
                        await self.say(ev.text)
                        done = True
                if not done:
                    await self.say("Samtalen er for kort til å komprimere.")
                self._save_session()
                self.refresh_status()
            case "/lagre":
                offers = list(self.query(SaveFiles))
                if not offers:
                    await self.say("Fant ingen filer å lagre i svarene. Be om filer med navn, f.eks. «lag index.html og style.css».")
                    return
                target = Path(arg).expanduser() if arg else None
                if target is not None and not target.is_absolute():
                    target = self.orch.cwd / target
                saved = offers[-1].save(target)
                await self.say(f"Lagret {len(saved)} filer i {short_path(offers[-1].base)}.")
            case "/modell" | "/model":
                if arg:
                    await self.switch_model(arg)
                else:
                    self.action_pick_model()
            case "/lab":
                self.action_lab()
            case "/providers":
                self.action_providers()
            case "/feil":
                await self._cmd_wrong(arg)
            case "/lekser":
                await self._cmd_lessons(arg)
            case "/bibliotek" | "/library":
                await self._cmd_library(arg)
            case "/models":
                from olympus.models import status_rows

                rows = [f"{r[0]:<20} {r[1]:<6} {r[2]:>9}  {r[3]:<12} {r[4]}" for r in status_rows(self.config)]
                loaded = ", ".join(self.manager.loaded()) or "ingen"
                await self.say("\n".join(rows + ["", f"Lastet i VRAM: {loaded}"]))
            case "/vram":
                await self._cmd_vram()
            case "/unload":
                removed = await self.manager.unload(arg or None)
                await self.say("Fjernet: " + (", ".join(removed) or "ingenting var lastet"))
            case "/usage":
                used, ctx = self.orch.context_used()
                pct = 100 * used / ctx if ctx else 0
                await self.say(
                    f"Kontekst: {used} / {ctx} tokens ({pct:.0f} %) · "
                    f"{len(self.orch.messages)} meldinger i økten"
                )
            case "/quit" | "/exit":
                self.exit()
            case _:
                await self.say(f"Ukjent kommando: {cmd}. Skriv /help.", "err-msg")
        self.refresh_status()

    async def _reset_session(self) -> None:
        """Avbryt generering, tøm historikken og feeden (banneret blir stående)."""
        if self._gen_worker is not None:
            self._gen_worker.cancel()
        self.orch.reset()
        self.session_id = None
        self.orch.toolbox.session_id = None
        self._transcript = []
        log = self.query_one("#log", VerticalScroll)
        for child in list(log.children)[3:]:  # banner, banner-info og velkomsten blir stående
            await child.remove()
        self.query_one("#welcome").display = True
        self._apply_banner()
        log.scroll_home(animate=False)
        panel = self.query_one("#sessions", SessionPanel)
        panel.current = None
        if panel.display:
            panel.reload()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "settings":
            self.action_settings()
        elif event.button.id == "model":
            self.action_pick_model()

    def action_providers(self) -> None:
        def done(changed: bool | None) -> None:
            if changed:
                self.call_after_refresh(self.refresh_all)

        self.push_screen(ProvidersScreen(self.config, self.orch.backends, self.catalog), done)

    async def _cmd_wrong(self, arg: str) -> None:
        """Lagre en rettelse for siste svar og be agenten svare på nytt med den."""
        o = self.orch
        if not arg:
            await self.say("Bruk: /feil <hva som er riktig>, f.eks. «/feil multilib aktiveres ved å fjerne # foran "
                           "[multilib] og Include-linjen i /etc/pacman.conf».", "err-msg")
            return
        if not o.last_answers:
            await self.say("Det finnes ikke noe svar å rette ennå.", "err-msg")
            return
        agent, model, question, answer = o.last_answers[-1]
        lid = o.lessons.add(agent, question, answer, arg, model)

        async def teach() -> None:
            chosen = await o.teach(lid, agent, question, answer, arg)
            if chosen:
                self.notify(f"Læreren skrev fasitsvar til treningspar #{lid}.", timeout=4)

        self.run_worker(teach(), group="teach")
        a = self.config.agent(agent)
        await self.say(Text.assemble(
            (f"{a['icon']} {a['display']} har lært: ", "bold"), arg,
            (f"\n(lekse #{lid}; brukes ved lignende spørsmål. Lagret som treningseksempel. Svarer på nytt …)", "dim"),
        ))
        retry = f"{question}\n\n(Rettelse fra brukeren til forrige svar: {arg})"
        await self.start_generation(retry)

    async def _cmd_lessons(self, arg: str) -> None:
        o = self.orch
        parts = arg.split()
        if len(parts) == 2 and parts[0] == "slett" and parts[1].isdigit():
            ok = o.lessons.remove(int(parts[1]))
            await self.say(f"Lekse #{parts[1]} er slettet." if ok else f"Fant ikke lekse #{parts[1]}.")
            return
        lessons = o.lessons.all()
        if not lessons:
            await self.say("Ingen lekser ennå. Når et svar er feil: /feil <hva som er riktig>.")
            return
        t = Text(f"✎ {len(lessons)} lekser\n", style="bold")
        for les in lessons[:30]:
            a = self.config.get("agents", {}).get(les.agent) or {"icon": "·", "display": les.agent}
            t.append(f"  #{les.id} {a['icon']} {a['display']}", style=self.config.agent_color(les.agent))
            t.append(f"  «{les.question[:60]}»\n", style="dim")
            t.append(f"      → {les.correction[:140]}\n")
        stats = o.lessons.stats()
        need = int(self.config.get("training.min_pairs_for_lora", 200))
        if stats:
            t.append("Treningspar (med fasit fra lærer): " + ", ".join(
                f"{self.config.get('agents', {}).get(a, {}).get('display', a)} {n} ({c})" for a, (n, c) in stats.items()
            ) + f" · LoRA anbefales fra ~{need} per agent\n", style="dim")
        t.append("Slett med /lekser slett <id>. Treningsdata: ~/.olympus/training/<agent>.jsonl", style="dim")
        await self.say(t)

    async def _cmd_library(self, arg: str) -> None:
        lib = self.orch.toolbox.library
        if not arg:
            t = Text("≡ Biblioteket\n", style="bold")
            for name, path in lib.sources().items():
                t.append(f"  {name:<12} {short_path(path)}\n", style="dim")
            t.append("Søk med /bibliotek <ord>. Legg egne .md/.txt-filer i ~/.olympus/library/docs/. "
                     "Zeus slår opp her selv.", style="dim")
            await self.say(t)
            return
        hits = await asyncio.to_thread(lib.search, arg, 8)
        if not hits:
            await self.say(f"Ingen treff for «{arg}». Prøv engelske ord for Arch Wiki.")
            return
        t = Text(f"≡ «{arg}»\n", style="bold")
        for h in hits:
            t.append(f"  {h.title}", style="bold")
            t.append(f" § {h.section or 'innledning'}", style="")
            t.append(f"  [{h.id}]\n", style="dim")
            t.append(f"     {h.snippet[:160]}\n", style="dim")
        t.append("Be Zeus lese en av dem, f.eks. «les bibliotek-id " + str(hits[0].id) + "».", style="dim")
        await self.say(t)

    async def _cmd_vram(self) -> None:
        vram = read_vram()
        if not vram:
            await self.say("Fant ingen amdgpu-VRAM i /sys/class/drm.")
            return
        lines = [
            f"VRAM: {vram.used_gb:.2f} / {vram.total_gb:.2f} GiB brukt ({vram.free_gb:.2f} ledig)"
            f" · budsjett {self.config.get('vram.budget_gb')} GiB"
        ]
        warm = set(self.config.get("vram.keep_warm", []))
        for model, size, measured in self.manager.usage():
            tag = "målt" if measured else "anslått"
            lines.append(f"  {model:<20} {size:5.2f} GiB ({tag}){'  · varm' if model in warm else ''}")
        if len(lines) == 1:
            lines.append("  Ingen modeller lastet av Olympus.")
        await self.say("\n".join(lines))

    def _handle_exception(self, error: Exception) -> None:
        """Skriv krasj til ~/.olympus/logs/crash.log før Textual avslutter."""
        import traceback
        from datetime import datetime

        try:
            log = self.config.path("logs_dir") / "crash.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
                f.write("".join(traceback.format_exception(error)))
        except OSError:
            pass
        super()._handle_exception(error)

    async def on_unmount(self) -> None:
        if self.brain is not None:
            await self.brain.close()
        import contextlib

        with contextlib.suppress(Exception):  # lokal hjerne: MCP-serverne den startet stoppes
            await self.orch.close()
        self.manager.shutdown()


def _common_prefix(words: list[str]) -> str:
    out = words[0]
    for w in words[1:]:
        while not w.startswith(out):
            out = out[:-1]
    return out


def run(config: Config) -> None:
    import atexit
    import signal

    from olympus import procs

    procs.reap_orphans()  # servere fra Olympus-instanser som døde brått
    # textual-image må finne terminalens grafikkstøtte før appen starter.
    try:
        import textual_image.renderable  # noqa: F401
    except Exception:
        pass
    app = OlympusApp(config)

    def cleanup() -> None:
        app.manager.shutdown()

    def hard_exit_soon() -> None:
        # Textual kan bli hengende i en løkke mot en død terminal (100 % CPU i timevis): avslutt hardt om litt.
        import os
        import threading

        t = threading.Timer(3.0, lambda: os._exit(0))
        t.daemon = True
        t.start()

    def on_signal(_signum, _frame) -> None:
        # Vinduet ble lukket (SIGHUP) eller prosessen bedt om å stoppe: rydd først.
        cleanup()
        hard_exit_soon()
        app.exit()

    app._terminal_gone = lambda: (cleanup(), hard_exit_soon(), app.exit())

    atexit.register(cleanup)
    signal.signal(signal.SIGHUP, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    try:
        app.run()
    finally:
        app.manager.shutdown()
