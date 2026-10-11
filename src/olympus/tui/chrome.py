"""Rammeverket rundt samtalen: modellinja ved meldingsfeltet, velkomstskjerm, øktpanel og innstillinger.

Alt her kan nås med tastatur og klikkes med mus. Teksten er vanlig terminaltekst (ingen grafikk), i samme
dempede gull som resten av Olympus.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Label,
    OptionList,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option

from olympus.sessions import SessionStore, when
from olympus.tui.navigation import click

if TYPE_CHECKING:
    from olympus.tui.app import OlympusApp

EXAMPLES = [
    "Hva står på planen min i dag?",
    "Spill noe rolig på høyttalerne",
    "Hvorfor starter ikke tjenesten min? Sjekk journalctl for ",
    "Hvor mye ledig diskplass har PC-en?",
]


class ModelBar(Static):
    """«ϟ Zeus · qwen36-27b» – modellen Zeus svarer med. Sky vises tydelig. Klikk eller F7 for å bytte."""

    def update_bar(self, app: OlympusApp) -> None:
        ui = app.config.theme["ui"]
        ref = app.current_model()
        cloud = app.orch.backends.is_cloud(ref)
        t = Text(no_wrap=True)
        t.append("ϟ Zeus ", style=click("app.pick_model", f"bold {app.config.agent_color('zeus')}"))
        if cloud:
            t.append(f" ☁ {ref} ", style=click("app.pick_model", f"bold {ui['background']} on {ui['error']}"))
            t.append(" sky – samtalen sendes ut", style=click("app.pick_model", f"bold {ui['error']}"))
        else:
            t.append(ref, style=click("app.pick_model", ui["text"]))
            t.append(" · lokal", style=click("app.pick_model", ui["muted"]))
        if app.size.width >= 120:
            t.append("  F7 bytt", style=f"dim {ui['muted']}")
        self.update(t)


class Welcome(Static):
    """Startskjermen under banneret: eksempler å prøve og de viktigste snarveiene. Borte når samtalen starter."""

    def update_welcome(self, app: OlympusApp) -> None:
        ui = app.config.theme["ui"]
        t = Text(justify="left")
        t.append("Prøv for eksempel\n", style=f"bold {ui['accent']}")
        for text in EXAMPLES:
            t.append("  ϟ ", style=app.config.agent_color("zeus"))
            t.append(f"{text.rstrip()}{' …' if text.endswith(' ') else ''}\n",
                     style=click(f"app.fill_prompt({text!r})", ui["text"]))
        t.append("\n")
        # Viktigst først: på smal skjerm kuttes linja bakfra.
        for key, what, action in (("Ctrl+P", "alle kommandoer", "app.command_palette"), ("F1", "hjelp", "app.help"),
                                  ("F7", "bytt modell", "app.pick_model"), ("Ctrl+O", "økter", "app.toggle_sessions"),
                                  ("F2", "innstillinger", "app.settings")):
            t.append(f"  {key} ", style=click(action, f"bold {ui['accent']}"))
            t.append(f"{what}   ", style=click(action, ui["muted"]))
        self.update(t)


class Rail(Static):
    """Et sammenlagt panel: en smal skinne med pil og loddrett navn. Klikk (eller tasten) folder det ut igjen."""

    def __init__(self, label: str, arrow: str, action: str, hint: str, **kw) -> None:
        super().__init__(**kw)
        self.label, self.arrow, self.action, self.hint = label, arrow, action, hint
        self.tooltip = hint

    def on_mount(self) -> None:
        ui = self.app.config.theme["ui"]  # type: ignore[attr-defined]
        t = Text(justify="center")
        t.append(self.arrow + "\n\n", style=f"bold {ui['accent']}")
        t.append("\n".join(self.label), style=ui["muted"])
        self.update(t)

    async def on_click(self) -> None:
        await self.app.run_action(self.action)


class SessionList(OptionList):
    """Øktlisten. Et klikk på ✕ ved en økt sletter (med bekreftelse) i stedet for å åpne den."""

    async def _on_click(self, event) -> None:
        sid = event.style.meta.get("delete_session")
        if sid:
            event.stop()
            event.prevent_default()  # Textual kaller ellers også OptionList._on_click (som åpner økten)
            panel = self.parent
            if isinstance(panel, SessionPanel):
                panel.request_delete(sid)
        # Ellers: ingenting her – Textual kaller OptionList._on_click selv (åpner økten).


class SessionPanel(Vertical):
    """Lagrede økter til venstre. Enter/klikk åpner; Del eller ✕ sletter (to ganger, så ingenting forsvinner ved et
    uhell); «+ Ny økt» øverst."""

    BINDINGS: ClassVar = [Binding("delete", "delete_session", "Slett økt")]

    def __init__(self, store: SessionStore, **kw) -> None:
        super().__init__(**kw)
        self.store = store
        self.current: str | None = None
        self._armed: str | None = None  # økten som er markert for sletting (Del én gang til)

    def compose(self):
        yield Static(id="sessions-head")
        yield SessionList(id="sessions-list")

    def on_mount(self) -> None:
        self.reload()

    def reload(self) -> None:
        ui = self.app.config.theme["ui"]  # type: ignore[attr-defined]
        width = max(10, (self.size.width or 26) - 3)
        head = Text("ØKTER", style=f"bold {ui['accent']}")
        head.append(" " + "─" * max(0, width - 8), style=ui["muted"])
        head.append(" «", style=click("app.toggle_sessions", f"bold {ui['accent']}"))  # legg sammen
        self.query_one("#sessions-head", Static).update(head)
        ol = self.query_one("#sessions-list", OptionList)
        keep = ol.highlighted
        ol.clear_options()
        ol.add_option(Option(Text("+ Ny økt", style=f"bold {ui['accent']}"), id="__new__"))
        room = max(6, width - 5)  # tittelen kuttes til én linje, med plass til ✕ til høyre
        saved = self.store.list()
        if not saved:  # tom tilstand som sier hva som skjer, i stedet for en tom liste
            ol.add_option(Option(Text("\nIngen lagrede økter ennå. De lagres av seg selv når du sender en melding.",
                                      style=ui["muted"]), disabled=True))
        for s in saved:
            t = Text()
            cur = s.id == self.current
            armed = s.id == self._armed
            title = s.title if len(s.title) <= room else s.title[: room - 1].rstrip() + "…"
            t.append("● " if cur else "  ", style=ui["accent"])
            t.append(f"{title:<{room}}", style="bold" if cur else "")
            mark = Style(meta={"delete_session": s.id})  # fanges av SessionList før raden velges
            t.append(" ✕", style=Style.parse(f"bold {ui['error']}" if armed else ui["muted"]) + mark)
            t.append(f"\n  {when(s.updated)}", style=ui["muted"])
            if armed:
                t.append("  slett? ✕ igjen", style=Style.parse(ui["error"]) + mark)
            ol.add_option(Option(t, id=s.id))
        if keep is not None and keep < ol.option_count:
            ol.highlighted = keep

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        app: OlympusApp = self.app  # type: ignore[assignment]
        if event.option.id == "__new__":
            app.run_worker(app.action_new_session())
        elif event.option.id:
            app.run_worker(app.open_session(event.option.id))

    def action_delete_session(self) -> None:
        ol = self.query_one("#sessions-list", OptionList)
        if ol.highlighted is None:
            return
        sid = ol.get_option_at_index(ol.highlighted).id
        if sid and sid != "__new__":
            self.request_delete(sid)

    def request_delete(self, sid: str) -> None:
        """Første gang: marker økten (✕ blir rød). Andre gang: slett."""
        if self._armed != sid:
            self._armed = sid
            self.reload()
            return
        self._armed = None
        self.store.delete(sid)
        if sid == self.current:
            self.current = None
        self.reload()
        self.app.notify("Økten er slettet.", timeout=2)


class SettingsScreen(ModalScreen[tuple[str, str] | None]):
    """F2: alle innstillinger i ett vindu. Returnerer en handling appen utfører, f.eks. ("model", "qwen36-27b")."""

    BINDINGS: ClassVar = [Binding("escape", "dismiss(None)", "Lukk"), Binding("f2", "dismiss(None)", "Lukk", show=False)]
    DEFAULT_CSS = """
    SettingsScreen { align: center middle; }
    SettingsScreen > Vertical { width: 86%; max-width: 110; height: 80%; border: round $accent; background: $surface; padding: 0 1; }
    SettingsScreen .title { text-style: bold; color: $accent; padding: 0 0 1 0; }
    SettingsScreen .hint { color: $text-muted; padding: 1 0 0 0; }
    SettingsScreen OptionList { height: auto; max-height: 16; border: none; }
    SettingsScreen .row { height: 3; }
    SettingsScreen .row Label { width: 1fr; padding: 1 0 0 0; }
    SettingsScreen Button { margin: 1 1 0 0; }
    """

    def __init__(self, app: OlympusApp, tab: str = "models") -> None:
        super().__init__()
        self.olympus = app
        self.start_tab = tab

    def compose(self):
        app = self.olympus
        with Vertical():
            yield Static("Innstillinger · Esc lukker · ←/→ bytter fane", classes="title")
            with TabbedContent(initial=self.start_tab):
                with TabPane("Modell", id="models"):
                    cur = app.current_model()
                    opts = []
                    for ch in app.catalog.choices():
                        t = Text()
                        on = ch["id"] == cur
                        t.append("● " if on else "○ ", style="bold" if on else "dim")
                        t.append(("☁ " if ch["cloud"] else "") + ch["id"], style="bold" if on else "")
                        t.append(f"  {ch.get('note', '')}", style="dim")
                        opts.append(Option(t, id=ch["id"]))
                    yield OptionList(*opts, id="model-list")
                    yield Static("Enter bytter modellen Zeus svarer med (F7 overalt). Zeus' minne, verktøy og økter blir "
                                 "på PC-en uansett; ☁ betyr at samtalen sendes til provideren.", classes="hint")
                with TabPane("Sky-modeller", id="cloud"):
                    yield Static("Koble til sky-modeller (Groq, Gemini, OpenRouter …) med egne nøkler. Zeus bruker dem "
                                 "bare når du bytter til dem med /modell, og faller tilbake til lokalt når en gratiskvote "
                                 "er brukt opp.")
                    yield Button("☁ Koble til / endre nøkler", id="providers")
                with TabPane("Utseende", id="look"):
                    for sid, label, value in (
                        ("sw-panel", "Systempanel til høyre (F4)", not app._panel_hidden),
                        ("sw-sessions", "Øktpanel til venstre (Ctrl+O)", not app._sessions_hidden),
                    ):
                        with Horizontal(classes="row"):
                            yield Label(label)
                            yield Switch(value=value, id=sid)
                    yield Static("Panelene skjules automatisk når terminalen er for smal.", classes="hint")

    def on_mount(self) -> None:
        pane = self.query_one(TabbedContent).active_pane
        if pane is not None:
            for w in pane.query("OptionList, Button, Switch"):
                w.focus()
                break

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "model-list" and event.option.id:
            self.dismiss(("model", event.option.id))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss({"providers": ("providers", "")}.get(event.button.id or "", None))

    def on_switch_changed(self, event: Switch.Changed) -> None:
        app = self.olympus
        if event.switch.id == "sw-panel" and event.value == app._panel_hidden:
            app.action_toggle_panel()
        elif event.switch.id == "sw-sessions" and event.value == app._sessions_hidden:
            app.action_toggle_sessions()


__all__ = ["ModelBar", "Rail", "SessionPanel", "SettingsScreen", "Welcome"]
