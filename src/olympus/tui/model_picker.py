"""Modellvalget (/modell, F7) og vinduet for å koble til sky-providers (F3). Modale skjermer."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from olympus.config import Config
from olympus.model_choices import ModelCatalog
from olympus.providers import Backends, list_models, save_auth

MODAL_CSS = """
ModalScreen { align: center middle; background: $background 70%; }
.dialog { width: 96; max-width: 96%; height: auto; max-height: 92%; padding: 1 2;
          border: round $accent; background: $surface; }
.dialog-title { text-style: bold; margin: 0 0 1 0; }
.field-label { margin: 1 0 0 0; color: $text-muted; }
.hint { color: $text-muted; margin: 0 0 0 0; }
.warn { color: $warning; margin: 1 0 0 0; }
.buttons { height: auto; margin: 1 0 0 0; align-horizontal: right; }
.buttons Button { margin: 0 0 0 1; }
.row { height: auto; }
Select { width: 1fr; }
"""


class ProvidersScreen(ModalScreen[bool]):
    """Koble til gratis providers med API-nøkkel. Returnerer True hvis noe endret seg."""

    DEFAULT_CSS = MODAL_CSS + """
    .provider { height: auto; margin: 1 0 0 0; padding: 0 1; border-left: thick $accent 40%; }
    .provider Input { width: 1fr; }
    .provider Button { min-width: 10; }
    """
    BINDINGS = [("escape", "close", "Lukk")]

    def __init__(self, config: Config, backends: Backends, catalog: ModelCatalog) -> None:
        super().__init__()
        self.config = config
        self.backends = backends
        self.catalog = catalog
        self.changed = False

    def _providers(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (p, s) for p, s in self.config.get("providers", {}).items()
            if s.get("cost") != "betalt" and s.get("type") in ("openai", "claude-code", "codex")
        ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Label("⚙ Providers", classes="dialog-title")
            yield Static(
                "Skymodeller Zeus kan svare med når du bytter modell (/modell). Bare gratis alternativer og "
                "abonnement vises. Lag en nøkkel hos provideren (lenken), lim den inn og trykk Lagre. "
                "Nøkler lagres i ~/.olympus/auth.json, kun lesbar for deg.",
                classes="hint",
            )
            for prov, spec in self._providers():
                with Vertical(classes="provider"):
                    yield Static(id=f"status-{prov}")
                    yield Static(Text(spec.get("note", ""), style="dim"))
                    if spec.get("type") == "openai":
                        yield Static(Text(f"Nøkkel: {spec.get('signup', '')}", style="dim underline"))
                        with Horizontal(classes="row"):
                            yield Input(placeholder="lim inn API-nøkkel", password=True, id=f"key-{prov}")
                            yield Button("Lagre", variant="primary", id=f"save-{prov}")
                            yield Button("Fjern", id=f"remove-{prov}")
            with Horizontal(classes="buttons"):
                yield Button("Lukk", id="close")

    def on_mount(self) -> None:
        for prov, _ in self._providers():
            self._status(prov)

    def _status(self, prov: str, extra: str = "") -> None:
        spec = self.config.get("providers", {})[prov]
        ok, why = self.backends.available(f"{prov}/x")
        t = Text()
        t.append(spec.get("label", prov), style="bold")
        t.append(f"  {spec.get('cost', '')}", style="dim")
        if spec.get("may_train"):
            t.append("  · kan trene på data", style="yellow")
        t.append("   ")
        if ok:
            n = len(self.catalog.cache.get(prov, []))
            t.append("✓ tilkoblet" + (f" · {n} modeller" if n else ""), style="green")
        else:
            t.append("✗ ikke tilkoblet", style="red")
        if extra:
            t.append(f"   {extra}", style="dim")
        self.query_one(f"#status-{prov}", Static).update(t)

    @on(Button.Pressed)
    async def _pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "close":
            self.dismiss(self.changed)
            return
        action, _, prov = bid.partition("-")
        if action == "save":
            key = self.query_one(f"#key-{prov}", Input).value.strip()
            if not key:
                self.notify("Lim inn en nøkkel først.", severity="warning")
                return
            self._status(prov, "tester nøkkelen …")
            spec = self.config.get("providers", {})[prov]
            try:
                models = await list_models(spec, key)
            except Exception as e:  # feil nøkkel, nettverk …
                self._status(prov, f"nøkkelen virket ikke: {e}")
                return
            save_auth(self.config, prov, key)
            self.catalog.cache[prov] = models
            self.catalog.errors.pop(prov, None)
            self.backends.reset()
            self.changed = True
            self.query_one(f"#key-{prov}", Input).value = ""
            self._status(prov, "lagret")
        elif action == "remove":
            save_auth(self.config, prov, None)
            self.catalog.invalidate(prov)
            self.backends.reset()
            self.changed = True
            self._status(prov, "fjernet")

    def action_close(self) -> None:
        self.dismiss(self.changed)


class ModelPicker(ModalScreen[str | None]):
    """Søkbar liste over modellene Zeus kan svare med: lokale først, så skymodeller gruppert per provider.
    Returnerer valgt id (eller None). Zeus er hjernen uansett – bare modellen som skriver svarene byttes."""

    DEFAULT_CSS = MODAL_CSS + """
    #picker-list { height: auto; max-height: 30; margin: 1 0 0 0; }
    """
    BINDINGS = [("escape", "cancel", "Avbryt")]

    def __init__(self, catalog: ModelCatalog, current: str, choices: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.catalog = catalog
        self.current = current
        self.fixed = choices  # valg fra hjernen (olympus-web); ellers fra katalogen her

    def compose(self) -> ComposeResult:
        from textual.widgets import OptionList

        with Vertical(classes="dialog"):
            yield Label("ϟ Hvem skal svare for Zeus?", classes="dialog-title")
            yield Static(Text("Minne, verktøy, plan, godkjenninger og økter blir alltid på PC-en. Velger du en ☁ "
                              "skymodell, sendes samtalen dit til du bytter tilbake.", style="dim"), classes="hint")
            yield Input(placeholder="søk, f.eks. qwen, groq, gemini, opus …", id="filter")
            yield OptionList(id="picker-list")
            yield Static("Henter skymodeller …", id="picker-status", classes="hint")

    def on_mount(self) -> None:
        self._fill("")
        self.query_one("#filter", Input).focus()
        self.load()

    def _choices(self) -> list[dict[str, Any]]:
        return self.fixed if self.fixed is not None else self.catalog.choices()

    @work(exclusive=True)
    async def load(self) -> None:
        if self.fixed is None:
            await self.catalog.refresh()
            self._fill(self.query_one("#filter", Input).value)
        errs = [f"{p}: {e[:60]}" for p, e in self.catalog.errors.items()]
        self.query_one("#picker-status", Static).update(Text(
            ("Kunne ikke hente lista fra " + "; ".join(errs) + "\n" if errs else "")
            + "Enter velger · Esc avbryter · F3 i hovedvinduet kobler til flere sky-providers", style="dim"))

    def _fill(self, query: str) -> None:
        from textual.widgets import OptionList
        from textual.widgets.option_list import Option

        q = query.lower().strip()
        ol = self.query_one("#picker-list", OptionList)
        ol.clear_options()
        first = None
        group = None
        for ch in self._choices():
            hay = f"{ch['id']} {ch.get('label', '')} {ch.get('note', '')} {ch.get('provider', '')}".lower()
            if q and q not in hay:
                continue
            head = "☁ " + ch.get("provider", "") if ch.get("cloud") else "⚡ Lokalt på PC-en"
            if head != group:
                group = head
                sub = "samtalen sendes ut" if ch.get("cloud") else "gratis · ingenting forlater maskinen"
                ol.add_option(Option(Text.assemble((head, "bold"), (f"   {sub}", "dim")), disabled=True))
            line = Text()
            line.append("✓ " if ch["id"] == self.current else "  ", style="green")
            line.append(("☁ " if ch.get("cloud") else "") + ch["id"], style="bold" if ch["id"] == self.current else "")
            extra = " · ".join(x for x in (ch.get("size", ""), ch.get("note", "")) if x)
            if extra:
                line.append(f"\n      {extra}", style="dim")
            ol.add_option(Option(line, id=ch["id"]))
            if first is None or ch["id"] == self.current:
                first = ch["id"]
        if first is not None:
            ol.highlighted = ol.get_option_index(first)

    @on(Input.Changed, "#filter")
    def _filter(self, event: Input.Changed) -> None:
        self._fill(event.value)

    @on(Input.Submitted, "#filter")
    def _submit(self) -> None:
        from textual.widgets import OptionList

        ol = self.query_one("#picker-list", OptionList)
        if ol.highlighted is not None:
            opt = ol.get_option_at_index(ol.highlighted)
            if opt.id:
                self.dismiss(opt.id)

    def on_key(self, event) -> None:
        # Pil opp/ned i søkefeltet flytter i listen.
        from textual.widgets import OptionList

        if event.key in ("up", "down") and self.focused is self.query_one("#filter", Input):
            ol = self.query_one("#picker-list", OptionList)
            (ol.action_cursor_up if event.key == "up" else ol.action_cursor_down)()
            event.stop()

    def on_option_list_option_selected(self, event) -> None:
        if event.option.id:
            self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)
