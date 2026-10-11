"""Navigasjon i Olympus-TUI-en: kommandopalett (Ctrl+P), hjelpeskjerm (F1) og klikkbare tekstsegmenter.

Fire lag med oppdagbarhet (etter tui-design-skillen): tastehint i bunnlinjen (Footer), F1-hjelp med alle
taster og kommandoer, Ctrl+P-palett med søk i alt Olympus kan, og klikk som snarvei – aldri som eneste vei.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.style import Style
from rich.text import Text
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

if TYPE_CHECKING:
    from olympus.tui.app import OlympusApp


def click(action: str, base: str | Style = "") -> Style:
    """Stil som gjør et tekstsegment klikkbart: kjører en app-handling, f.eks. "app.run_cmd('/vram')"."""
    style = Style.parse(base) if isinstance(base, str) else base
    return style + Style(meta={"@click": action})


def cmd_action(line: str) -> str:
    """Handlingsstreng som kjører en /kommando."""
    return "app.run_cmd(" + repr(line) + ")"


# Tastene, gruppert som i hjelpen. Samme kilde brukes av F1 og bunnlinjen (definer én gang).
KEYS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Overalt", [
        ("Ctrl+P", "kommandopalett – søk i alt Olympus kan"),
        ("F1", "hjelp (dette vinduet)"),
        ("F2", "innstillinger: modell, sky, utseende"),
        ("F3", "koble til sky-modeller (nøkler)"),
        ("F7", "bytt modell Zeus svarer med (lokal eller ☁ sky)"),
        ("F6", "Olympus Lab's: plan, musikk, film, bøker, server"),
        ("Ctrl+O", "legg sammen / fold ut øktene"),
        ("F4", "legg sammen / fold ut systempanelet"),
        ("Ctrl+L", "ny økt"),
        ("Ctrl+R", "send forrige melding på nytt"),
        ("Ctrl+C", "avbryt svaret som genereres"),
        ("Ctrl+Q", "avslutt"),
    ]),
    ("Fokus og rulling", [
        ("Tab / Shift+Tab", "flytt fokus: økter → samtale → panel → melding"),
        ("Esc", "tilbake til meldingsfeltet"),
        ("PgUp / PgDn", "rull samtalen (også mens du skriver)"),
        ("Ctrl+Home / Ctrl+End", "til toppen / bunnen av samtalen"),
        ("1–5 (i panelet)", "fold en seksjon inn/ut"),
    ]),
    ("Meldingsfeltet", [
        ("Enter", "send"),
        ("Shift/Alt+Enter, Ctrl+J", "ny linje"),
        ("↑ / ↓", "historikk"),
        ("/ + Tab", "fullfør kommando"),
    ]),
    ("Mus", [
        ("Klikk", "modellinja, status, panel"),
        ("Klikk ▾ » « på kant", "fold seksjoner og paneler"),
        ("Dra + Ctrl+C", "marker tekst med musen og kopier"),
        ("Shift + dra", "terminalens egen markering (forbi Olympus)"),
    ]),
]


class HelpScreen(ModalScreen[str | None]):
    """F1: alle taster og alle /kommandoer. Enter eller klikk på en kommando kjører den; Esc lukker."""

    BINDINGS: ClassVar = [Binding("escape", "dismiss(None)", "Lukk"), Binding("f1", "dismiss(None)", "Lukk", show=False)]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > Vertical { width: 92%; max-width: 130; height: 86%; border: round $accent; background: $surface; padding: 0 1; }
    HelpScreen #help-keys { height: auto; margin: 1 0; }
    HelpScreen .help-col { width: 1fr; height: auto; }
    HelpScreen #help-cmds { height: 1fr; border: none; }
    HelpScreen .help-title { text-style: bold; color: $accent; }
    """

    def __init__(self, commands: dict[str, str]) -> None:
        super().__init__()
        self.commands = commands

    def compose(self):
        def column(groups: list[tuple[str, list[tuple[str, str]]]]) -> Text:
            t = Text()
            kw = max(len(k) for _, rows in groups for k, _ in rows) + 2  # tastekolonnen akkurat bred nok
            for gi, (group, rows) in enumerate(groups):
                t.append(("\n" if gi else "") + group + "\n", style="bold")
                for k, what in rows:
                    t.append(f"  {k:<{kw}}", style="bold")
                    t.append(f"{what}\n", style="dim")
            return t

        half = (len(KEYS) + 1) // 2
        with Vertical():
            yield Static("Hjelp · Esc lukker · Enter/klikk på en kommando kjører den", classes="help-title")
            with Horizontal(id="help-keys"):
                yield Static(column(KEYS[:half]), classes="help-col")
                yield Static(column(KEYS[half:]), classes="help-col")
            yield Static("Kommandoer", classes="help-title")
            yield OptionList(*[Option(Text.assemble((c.ljust(12), "bold"), (d, "dim")), id=c) for c, d in self.commands.items()],
                             id="help-cmds")

    def on_mount(self) -> None:
        self.query_one("#help-cmds", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class OlympusCommands(Provider):
    """Ctrl+P: alle Olympus-handlinger, søkbare. Hver kommando viser tasten sin der den har en."""

    def _items(self, searching: bool = True) -> list[tuple[str, str, object]]:
        app: OlympusApp = self.app  # type: ignore[assignment]
        items: list[tuple[str, str, object]] = []
        # /kommandoer (de som tar argument fylles inn i meldingsfeltet i stedet for å kjøres)
        needs_arg = {"/feil", "/bibliotek", "/unload"}
        for c, desc in app.COMMANDS_HELP.items():
            if c in needs_arg:
                items.append((f"{c} …", desc, lambda c=c: app.fill_prompt(c + " ")))
            else:
                items.append((c, desc, lambda c=c: app.action_run_cmd(c)))
        # modellvalget: hele lista ved søk («modell groq»), ellers én rad
        items.append(("Bytt modell …", "F7 · lokal eller ☁ sky", app.action_pick_model))
        if searching:
            for ch in app.catalog.choices():
                items.append((f"Modell: {'☁ ' if ch['cloud'] else ''}{ch['id']}", ch.get("note", ""),
                              lambda ref=ch["id"]: app.run_worker(app.switch_model(ref))))
        # lagrede økter (bare ved søk, så lista uten søk holder seg kort)
        if searching:
            from olympus.sessions import when

            for sinfo in app.sessions.list(50):
                items.append((f"Åpne økt: {sinfo.title}", when(sinfo.updated),
                              lambda sid=sinfo.id: app.run_worker(app.open_session(sid))))
        else:
            items.append(("Åpne økt …", "søk «økt» + tittel, eller Ctrl+O", app.action_toggle_sessions))
        # app-handlinger med tast
        items += [
            ("Systempanel: legg sammen / fold ut", "F4 · temperatur, VRAM, fart, kontekst og kostnad", app.action_toggle_panel),
            ("Hjelp", "F1 · alle taster og kommandoer", app.action_help),
            ("Tilbake til lokal modell", "/modell lokal", lambda: app.action_run_cmd("/modell lokal")),
            ("Innstillinger", "F2 · modell, sky-modeller, utseende", app.action_settings),
            ("Sky-modeller", "F3 · koble til gratis sky-modeller", app.action_providers),
            ("Olympus Lab's", "F6 · plan, musikk, film, bøker, server", app.action_lab),
            ("Øktpanel: legg sammen / fold ut", "Ctrl+O · lagrede samtaler", app.action_toggle_sessions),
            ("Ny økt", "Ctrl+L", app.action_new_session),
            ("Send forrige melding på nytt", "Ctrl+R", lambda: app.action_run_cmd("/retry")),
            ("Fokus: meldingsfeltet", "Esc", app.action_focus_prompt),
            ("Fokus: samtalen", "Tab", lambda: app.query_one("#log").focus()),
            ("Til bunnen av samtalen", "Ctrl+End", app.action_log_end),
            ("Til toppen av samtalen", "Ctrl+Home", app.action_log_home),
        ]
        return items

    @staticmethod
    def _line(name, help_text: str) -> Content:
        """Én linje per treff: navnet, så forklaringen dempet (to linjer per treff ga plass til bare ni)."""
        return Content.assemble(name, ("  " + help_text, "dim"))

    async def discover(self) -> Hits:
        for name, help_text, cb in self._items(searching=False):
            yield DiscoveryHit(self._line(name, help_text), cb, text=name)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_text, cb in self._items():
            score = max(matcher.match(name), matcher.match(help_text) * 0.8)
            if score > 0:
                yield Hit(score, self._line(matcher.highlight(name), help_text), cb, text=name)
