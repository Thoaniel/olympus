"""Gjenbrukbare TUI-widgets: input med historikk, meldinger og statuslinje."""

from __future__ import annotations

import time
from pathlib import Path

from rich.text import Text
from textual import events
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Collapsible, Markdown, Static, TextArea


class PromptInput(TextArea):
    """Flerlinjers input. Enter sender, Shift/Alt+Enter eller Ctrl+J gir ny linje,
    pil opp/ned blar i historikken når markøren står på første/siste linje,
    Tab fullfører slash-kommandoer."""

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    class TabComplete(Message):
        pass

    def __init__(self, **kwargs) -> None:
        super().__init__(soft_wrap=True, show_line_numbers=False, tab_behavior="focus", **kwargs)
        self.prompt_history: list[str] = []
        self._hist_pos: int | None = None
        self._draft = ""

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            value = self.text.strip()
            if value:
                if not self.prompt_history or self.prompt_history[-1] != value:
                    self.prompt_history.append(value)
                self._hist_pos = None
                self.clear()
                self.post_message(self.Submitted(value))
            return
        if event.key in ("shift+enter", "alt+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if event.key == "tab" and self.text.startswith("/"):
            event.prevent_default()
            event.stop()
            self.post_message(self.TabComplete())
            return
        row = self.cursor_location[0]
        if event.key == "up" and row == 0 and self.prompt_history:
            event.prevent_default()
            event.stop()
            if self._hist_pos is None:
                self._draft = self.text
                self._hist_pos = len(self.prompt_history)
            self._hist_pos = max(0, self._hist_pos - 1)
            self._set(self.prompt_history[self._hist_pos])
            return
        if event.key == "down" and self._hist_pos is not None and row == self.document.line_count - 1:
            event.prevent_default()
            event.stop()
            self._hist_pos += 1
            if self._hist_pos >= len(self.prompt_history):
                self._hist_pos = None
                self._set(self._draft)
            else:
                self._set(self.prompt_history[self._hist_pos])
            return
        await super()._on_key(event)

    def _set(self, value: str) -> None:
        self.text = value
        self.move_cursor(self.document.end)


class UserMessage(Static):
    def __init__(self, text: str, color: str) -> None:
        body = Text()
        body.append("› ", style=f"bold {color}")
        body.append(text)
        super().__init__(body, classes="user-msg")


def _calm_code_theme() -> None:
    """Textuals kodetema understreker funksjonsnavn, så de ser ut som lenker. Behold fargen, dropp streken."""
    from pygments.token import Token
    from textual.highlight import HighlightTheme

    for tok in (Token.Name.Function, Token.Name.Function.Magic):
        HighlightTheme.STYLES[tok] = HighlightTheme.STYLES.get(tok, "").replace(" underline", "")


_calm_code_theme()


class AgentMessage(Vertical):
    """Header med ikon/navn i agentens farge, valgfri sammenfoldet tenking,
    og Markdown-kropp som strømmes inn."""

    DEFAULT_CSS = """
    AgentMessage { height: auto; margin: 1 0 0 0; }
    AgentMessage > .agent-header { height: 1; }
    AgentMessage > Markdown { margin: 0 0 0 2; padding: 0; }
    /* Inline-kode: bare farge, ingen bakgrunn (ellers blir punktlister til rader av oransje flekker). */
    AgentMessage MarkdownBlock > .code_inline { background: transparent !important; color: $text-warning !important; }
    AgentMessage > Collapsible { margin: 0 0 0 2; padding: 0; border: none; }
    AgentMessage.draft > * { display: none; }
    AgentMessage.draft > .agent-header { display: block; }
    AgentMessage.draft { margin: 0; }
    """

    def __init__(self, icon: str, name: str, color: str) -> None:
        super().__init__()
        self.icon, self.agent_name, self.color = icon, name, color
        self.body = Markdown("")
        self.reasoning: Static | None = None
        self._reasoning_text = ""
        self.header = Static(classes="agent-header")

    def compose(self):
        yield self.header
        yield self.body

    def on_mount(self) -> None:
        self.set_header("")

    def fold_as_draft(self, note: str = "") -> None:
        """Zeus avviste svaret: det blir en sammenfoldet linje (klikk for å se utkastet), med Zeus' innvending."""
        self._foldable = True
        self._note = note
        self.add_class("draft")
        self.set_header(getattr(self, "_suffix", ""))

    def on_click(self, event) -> None:
        if getattr(self, "_foldable", False) and (event.widget is self.header or self.has_class("draft")):
            self.toggle_class("draft")
            self.set_header(self._suffix)

    def set_header(self, suffix: str) -> None:
        self._suffix = suffix
        if getattr(self, "_foldable", False):
            folded = self.has_class("draft")
            t = Text()
            t.append("▸ " if folded else "▾ ", style="dim")
            t.append(f"{self.icon} utkast fra {self.agent_name}", style=self.color)
            if suffix:
                t.append(f"  {suffix}", style="dim")
            if note := getattr(self, "_note", ""):
                t.append(f"  · ✗ forkastet: {note.splitlines()[0][:120]}", style="dim")
            if folded:
                t.append("  · klikk for å vise", style="dim")
            self.header.update(t)
            return
        t = Text()
        t.append(f"{self.icon} {self.agent_name}", style=f"bold {self.color}")
        if suffix:
            t.append(f"  {suffix}", style="dim")
        self.header.update(t)

    async def add_reasoning(self, text: str) -> None:
        if self.reasoning is None:
            self.reasoning = Static("", classes="reasoning")
            await self.mount(
                Collapsible(self.reasoning, title="tenker …", collapsed=True), before=self.body
            )
        self._reasoning_text += text
        self.reasoning.update(Text(self._reasoning_text, style="dim italic"))

    async def add(self, widget) -> None:
        """Legg til en widget (f.eks. et verktøykall) etter det som er strømmet så langt."""
        await self.mount(widget)

    async def new_body(self) -> Markdown:
        """Ny tekstdel etter verktøykall, så rekkefølgen blir tekst → verktøy → tekst."""
        self.body = Markdown("")
        self.reasoning = None
        self._reasoning_text = ""
        await self.mount(self.body)
        return self.body


class SaveFiles(Static):
    """«⤓ Lagre 3 filer i ~/mappe: index.html, style.css …» under et svar med filmerkede kodeblokker.
    Klikk (eller /lagre [mappe]) skriver dem; etterpå vises hvor de ble lagret, og HTML kan åpnes."""

    DEFAULT_CSS = """
    SaveFiles { margin: 1 0 0 2; height: auto; color: $accent; }
    SaveFiles:hover { background: $accent 15%; }
    """

    def __init__(self, files: list[tuple[str, str]], base: Path) -> None:
        super().__init__()
        self.files, self.base = files, base
        self.saved: list[Path] = []
        self._confirm_overwrite = False  # første klikk når noe finnes fra før: vis hva som overskrives

    def on_mount(self) -> None:
        self.show()

    def show(self) -> None:
        def short_path(p: Path) -> str:
            return str(p).replace(str(Path.home()), "~", 1)

        names = ", ".join(f for f, _ in self.files[:4]) + (" …" if len(self.files) > 4 else "")
        if not self.saved:
            from olympus.codefiles import existing

            n = len(self.files)
            t = Text.assemble(("⤓ ", "bold"), (f"Lagre {n} fil{'er' if n > 1 else ''}", "bold underline"),
                              (f" i {short_path(self.base)}: {names}", ""))
            old = existing(self.base, self.files)
            if old:
                t.append(f"\n  ⚠ finnes fra før: {', '.join(old[:4])}", style="bold red")
                t.append("  – klikk igjen for å overskrive" if self._confirm_overwrite else "  – klikk for å se valg",
                         style="red")
            self.update(t)
            self.tooltip = "Klikk, eller skriv /lagre <mappe> for et annet sted"
        else:
            html = next((p for p in self.saved if p.suffix in (".html", ".htm")), None)
            t = Text.assemble(("✓ ", "bold green"), (f"Lagret i {short_path(self.base)}: {names}", ""))
            if html:
                t.append("   ")
                t.append("åpne i nettleseren", style="bold underline")
            self.update(t)
            self.tooltip = "Klikk for å åpne" if html else None

    def save(self, base: Path | None = None, overwrite: bool = False) -> list[Path]:
        from olympus.codefiles import save_files

        if base is not None:
            self.base = base
        self.saved = save_files(self.base, self.files, overwrite=overwrite)
        self.show()
        return self.saved

    def on_click(self) -> None:
        if not self.saved:
            from olympus.codefiles import existing

            if existing(self.base, self.files) and not self._confirm_overwrite:
                self._confirm_overwrite = True  # andre klikk overskriver; ingenting skrives før det
                self.show()
                return
            self.save(overwrite=self._confirm_overwrite)
            self.app.notify(f"Lagret {len(self.saved)} filer.", timeout=2)
            return
        html = next((p for p in self.saved if p.suffix in (".html", ".htm")), None)
        if html:
            from olympus.sysutil import open_path

            open_path(html)


class StatusBar(Static):
    pass


class ToolLine(Vertical):
    """Ett verktøykall i feeden: ⚙ navn  detalj  → ✓/✗ oppsummering, med sammenfoldet utdata."""

    DEFAULT_CSS = """
    ToolLine { height: auto; margin: 0 0 0 2; }
    ToolLine > .tool-head { height: auto; }
    ToolLine > Collapsible { margin: 0 0 0 2; padding: 0; border: none; }
    """

    def __init__(self, name: str, detail: str) -> None:
        super().__init__()
        self.tool_name, self.detail = name, detail
        self.head = Static(classes="tool-head")

    def compose(self):
        yield self.head

    def on_mount(self) -> None:
        self.set_state("…", "dim")

    def set_state(self, summary: str, style: str) -> None:
        t = Text()
        t.append("⚙ ", style="#8fa8c8")
        t.append(self.tool_name, style="bold #8fa8c8")
        if self.detail:
            t.append(f"  {self.detail}")
        t.append(f"  {summary}", style=style)
        self.head.update(t)

    async def finish(self, ok: bool, summary: str, output: str, denied: bool = False) -> None:
        if denied:
            self.set_state("✗ avslått", "yellow")
        else:
            self.set_state(("✓ " if ok else "✗ ") + summary, "green" if ok else "red")
        if output.strip():
            await self.mount(Collapsible(Static(Text(output, style="dim")), title="utdata", collapsed=True))


class ToolApproval(Vertical):
    """Godkjenning i feeden. Poster Answered("yes" | "no" | "always").

    Samme regler som `olympus zeus` og Lab's (tui/approvals.py):
    - ingen valg er markert når kortet dukker opp, så et Enter ment for meldingsfeltet godkjenner aldri noe;
    - ventetid (0,7 s) etter at kortet dukker opp før et ja teller;
    - farlige kall krever to trykk innen 3 s;
    - lang forhåndsvisning: «ja» virker først når den er åpnet i sin helhet (v)."""

    DEFAULT_CSS = """
    ToolApproval { height: auto; margin: 0 0 0 4; padding: 0 1; border-left: thick $warning; }
    ToolApproval.danger { border-left: thick $error; }
    ToolApproval > OptionList { height: auto; max-height: 5; border: none; }
    ToolApproval > .preview { height: auto; max-height: 20; overflow-y: auto; }
    ToolApproval > .preview.full { max-height: 200; }
    ToolApproval > .note { height: auto; }
    """
    GRACE = 0.7
    CONFIRM = 3.0
    LONG = 18  # linjer: lengre forhåndsvisning må åpnes helt (v) før ja virker

    class Answered(Message):
        def __init__(self, answer: str) -> None:
            super().__init__()
            self.answer = answer

    BINDINGS = [("y", "answer('yes')", "Ja"), ("n", "answer('no')", "Nei"),
                ("a", "answer('always')", "Ja for økten"), ("v", "view", "Se hele"), ("escape", "answer('no')", "Nei")]

    def __init__(self, reason: str, preview: str, lang: str, danger: bool, tool: str) -> None:
        super().__init__(classes="danger" if danger else "")
        self.reason, self.preview, self.lang, self.danger, self.tool = reason, preview, lang, danger, tool
        self.shown_at = 0.0
        self.armed_at = 0.0
        self.long = preview.count("\n") + 1 > self.LONG or len(preview) > 2400
        self.viewed = not self.long

    def compose(self):
        from rich.syntax import Syntax
        from textual.widgets import OptionList
        from textual.widgets.option_list import Option

        head = Text()
        head.append("⚠ " if self.danger else "? ", style="bold red" if self.danger else "bold yellow")
        head.append(self.reason, style="bold")
        head.append(f"   ({self.tool})", style="dim")
        yield Static(head)
        if self.preview:
            yield Static(Syntax(self.preview, self.lang, theme="ansi_dark", word_wrap=True), classes="preview")
        yield Static(self._note(), classes="note")
        opts = [Option(Text.assemble(("y", "bold"), "  Ja, kjør" + ("  (to trykk – farlig)" if self.danger else "")), id="yes"),
                Option(Text.assemble(("n", "bold"), "  Nei"), id="no")]
        if not self.danger:
            opts.append(Option(Text.assemble(("a", "bold"), f"  Ja, og ikke spør om {self.tool} igjen i denne økten"), id="always"))
        yield OptionList(*opts)

    def _note(self) -> Text:
        if self.long and not self.viewed:
            return Text("⚠ Lang forhåndsvisning – trykk v for å se hele før du kan godkjenne.", style="bold red")
        if self.danger and time.monotonic() - self.armed_at <= self.CONFIRM:
            return Text("FARLIG: trykk y igjen innen 3 s for å godkjenne.", style="bold red")
        return Text("")

    def on_mount(self) -> None:
        from textual.widgets import OptionList

        ol = self.query_one(OptionList)
        ol.highlighted = None  # ingenting markert: Enter alene godkjenner aldri
        ol.focus()
        self.shown_at = time.monotonic()

    def on_option_list_option_selected(self, event) -> None:
        event.stop()
        self.action_answer(event.option.id or "no")

    def action_view(self) -> None:
        self.viewed = True
        for w in self.query(".preview"):
            w.add_class("full")
        self.query_one(".note", Static).update(self._note())

    def action_answer(self, answer: str) -> None:
        if answer == "always" and self.danger:
            return
        if answer in ("yes", "always"):
            if time.monotonic() - self.shown_at < self.GRACE:
                return  # tastetrykk ment for noe annet idet kortet dukket opp
            if not self.viewed:
                self.query_one(".note", Static).update(self._note())
                return
            if self.danger and time.monotonic() - self.armed_at > self.CONFIRM:
                self.armed_at = time.monotonic()
                self.query_one(".note", Static).update(self._note())
                return
        self.post_message(self.Answered(answer))
