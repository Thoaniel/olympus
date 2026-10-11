"""Olympus Lab's på PC-en: samme app som Zeus på telefonen, i terminalen – samme fem faner i samme rekkefølge.

  1 hjem · 2 zeus · 3 plan · 4 media · 5 PC

Alt hentes fra den ene Olympus-serveren (/api/v1) som telefonen bruker, og websocketen holder alt likt med en gang:
samtalen med Zeus (det du skriver på telefonen vises her, og omvendt), planen, biblioteket og den ene spilleren på
PC-en. Samme logikk i hver fane: Enter = hovedhandlingen som står i bunnlinja, Esc = tilbake, bunnlinja viser bare
taster som virker nå, og `?` åpner hjelpen. `z` går til den vanlige Olympus-chatten.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import time
from typing import Any

import httpx
from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import ContentSwitcher, DataTable, Input, Select, Static

from olympus.dates import parse_when
from olympus.tui.approvals import ApprovalGate, preview_parts

GOLD, MUTED, OK, BAD, LINE, DIM = "#d4a73a", "#8a8696", "#8fbf6f", "#e0605e", "#34344a", "#5a5868"
TABS = [("hjem", "⌂"), ("zeus", "ϟ"), ("plan", "▦"), ("media", "♪"), ("pc", "◈")]
LABEL = {"pc": "PC"}
SUBS = [("musikk", "m", "♪"), ("film", "f", "▶"), ("boker", "b", "§")]  # undervalg i media
SUB_LABEL = {"boker": "bøker"}
DAYS = ["man", "tir", "ons", "tor", "fre", "lør", "søn"]
MONTHS = ["januar", "februar", "mars", "april", "mai", "juni", "juli", "august", "september", "oktober", "november",
          "desember"]
VERBS = {"add": "la til", "update": "endret", "delete": "slettet", "undo": "angret"}
STATE_TEXT = {"ledig": ("● ledig", OK), "tenker": ("ϟ tenker …", GOLD), "verktøy": ("⚙ bruker verktøy", GOLD),
              "venter": ("‖ venter på deg", BAD), "av": ("○ Zeus er av", MUTED)}
FEED_MAX = 300  # meldinger i samtalefeeden (eldre fjernes fra skjermen, ikke fra økta)


# --- tekst og datoer --------------------------------------------------------------------------------
def fmt_day(day: dt.date, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    if day == today:
        return "i dag"
    if day == today + dt.timedelta(days=1):
        return "i morgen"
    return f"{DAYS[day.weekday()]} {day.day}.{day.month}"


def fmt_when(iso: str) -> str:
    day = dt.date.fromisoformat(iso[:10])
    return fmt_day(day) + (f" kl {iso[11:16]}" if len(iso) >= 16 else " (hele dagen)")


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def who_label(who: str) -> str:
    """«du (Pixel 9)» → «du · telefon», «du (sdk_gphone…)» → «du · emulator»."""
    m = re.fullmatch(r"(.*?)\s*\((.+)\)", who)
    if not m:
        return "du · PC" if who == "du" else who
    return f"{m.group(1)} · {'emulator' if m.group(2).startswith('sdk_') else 'telefon'}"


def bar(frac: float, width: int = 10) -> str:
    n = round(max(0.0, min(1.0, frac)) * width)
    return "━" * n + "─" * (width - n)


def clock(seconds: float) -> str:
    s = int(seconds or 0)
    return f"{s // 3600}:{s % 3600 // 60:02}:{s % 60:02}" if s >= 3600 else f"{s // 60}:{s % 60:02}"


def short_sink(label: str) -> str:
    label = re.sub(r"\s*\[([^\]]*)\]", r" · \1", label)  # behold skjermnavnet: to HDMI-utganger ellers like
    for word in (" Digital Stereo", " Analog Stereo", " Controller", " Audio", " (HDMI)", " (IEC958)"):
        label = label.replace(word, "")
    return label.strip()[:40]


def keyline(pairs: list[tuple[str, str]], width: int) -> Text:
    """Taster i gull, forklaring dempet; kuttes bakfra så det alltid får plass på én linje."""
    extra = [("1–5", "faner"), ("?", "hjelp")]
    t = Text(no_wrap=True, overflow="crop")
    for key, what in pairs + extra:
        piece = f" {key} {what}  "
        if len(t.plain) + len(piece) > width - 1:
            break
        t.append(f" {key}", style=f"bold {GOLD}")
        t.append(f" {what}  ", style=MUTED)
    return t


def now_playing(p: dict[str, Any], pos: float, width: int = 40, keys: bool = False) -> Text:
    """Det PC-en spiller: tittel – artist, posisjon og en linje med framdrift."""
    t = Text()
    cur = p.get("current")
    if not cur:
        t.append("Ingenting spilles på PC-en.\n", style=MUTED)
        return t
    dur = float(p.get("duration") or cur.get("duration") or 0)
    t.append("‖ " if p.get("paused") else "▶ ", style=GOLD)
    t.append(str(cur.get("title") or "?"), style=f"bold {GOLD}")
    if cur.get("like") == 1:
        t.append(" ♥", style=BAD)
    t.append(f"\n  {cur.get('artist') or ''}" + (f" · {cur['album']}" if cur.get("album") else "") + "\n", style=MUTED)
    t.append(f"  {clock(pos)} ", style=MUTED)
    t.append(bar(pos / dur if dur else 0, max(6, width - 18)), style=GOLD if not p.get("paused") else DIM)
    t.append(f" {clock(dur)}" + ("  pauset" if p.get("paused") else "") + "\n", style=MUTED)
    queue, idx = p.get("queue") or [], int(p.get("index") or 0)
    nxt = queue[idx + 1] if idx + 1 < len(queue) else None
    if nxt:
        t.append("  neste: ", style=DIM)
        t.append(f"{nxt.get('title')} – {nxt.get('artist') or ''}\n", style=MUTED)
    if keys:
        t.append("\n")
        for k, what in (("␣", "pause" if not p.get("paused") else "fortsett"), ("n", "neste"), ("p", "forrige"), ("s", "stopp")):
            t.append(f" {k}", style=f"bold {GOLD}")
            t.append(f" {what}  ", style=MUTED)
    return t


class Api:
    """Olympus' API med nettleser-nøkkelen (cookie) – samme server som telefonen bruker."""

    def __init__(self, base: str, token: str) -> None:
        self.base = base.rstrip("/") + "/api/v1"
        self.cookies = {"olympus_web": token}

    async def call(self, method: str, path: str, body: dict | None = None) -> Any:
        async with httpx.AsyncClient(timeout=15, cookies=self.cookies) as c:
            r = await c.request(method, self.base + path, json=body)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def get(self, path: str) -> Any:
        return await self.call("GET", path)


# --- dialoger -----------------------------------------------------------------------------------
DIALOG_CSS = f"""
#dlg {{ width: 72; max-width: 100%; height: auto; border: round {GOLD}; border-title-color: {GOLD}; border-title-style: bold;
        background: #0b0b0f; padding: 1 2; }}
#dlg Input {{ border: tall {LINE}; background: #111118; }}
#dlg Input:focus {{ border: tall {GOLD}; }}
.lbl {{ color: {MUTED}; margin-top: 1; }}
.hint {{ color: {MUTED}; margin-top: 1; }}
"""


REPEAT_CHOICES = [("nei", "none"), ("hver dag", "daily"), ("hverdager (man–fre)", "weekdays"), ("hver uke", "weekly"),
                  ("annenhver uke", "biweekly"), ("hver måned", "monthly")]


class EventDialog(ModalScreen[tuple[str, str, str] | None]):
    """Ny/endre avtale: Hva, Når (og for nye: Gjentas) og en linje som viser hva det blir. Svar: (hva, når, gjentas)."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Avbryt")]
    DEFAULT_CSS = "EventDialog { align: center middle; }" + DIALOG_CSS

    def __init__(self, title: str = "", when: str = "", heading: str = "▦ Ny avtale") -> None:
        super().__init__()
        self.title_, self.when, self.heading = title, when, heading

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg") as v:
            v.border_title = self.heading
            yield Static("Hva", classes="lbl")
            yield Input(value=self.title_, placeholder="f.eks. Tannlege", id="hva")
            yield Static("Når", classes="lbl")
            yield Input(value=self.when, placeholder="i morgen 18 · fre 10:30 · 12.10 20:00", id="nar")
            if self.heading == "▦ Ny avtale":
                yield Static("Gjentas", classes="lbl")
                yield Select(REPEAT_CHOICES, value="none", allow_blank=False, id="gjentas")
            yield Static("", id="preview", classes="hint")
            yield Static(Text.assemble(("⏎", f"bold {GOLD}"), (" lagre   ", MUTED), ("tab", f"bold {GOLD}"),
                                       (" neste felt   ", MUTED), ("esc", f"bold {GOLD}"), (" avbryt", MUTED)), classes="hint")

    def on_mount(self) -> None:
        self.update_preview()

    def on_input_changed(self, _e: Input.Changed) -> None:
        self.update_preview()

    def on_select_changed(self, _e: Select.Changed) -> None:
        self.update_preview()

    def repeat(self) -> str:
        sel = self.query("#gjentas")
        return "" if not sel or sel.first(Select).value in ("none", Select.BLANK) else str(sel.first(Select).value)

    def update_preview(self) -> None:
        when = parse_when(self.query_one("#nar", Input).value)
        p = self.query_one("#preview", Static)
        rep = self.repeat()
        if when:
            label = next(k for k, v in REPEAT_CHOICES if v == (rep or "none"))
            p.update(Text(f"→ {fmt_when(when)}" + (f"  ↻ {label}, {'ett år' if rep == 'monthly' else 'tre måneder'} fram"
                                                   if rep else ""), style=GOLD))
        elif self.query_one("#nar", Input).value.strip():
            p.update(Text("forstår ikke tidspunktet – prøv «i morgen 18» eller «12.10 20:00»", style=BAD))
        else:
            p.update(Text("", style=MUTED))

    def on_input_submitted(self, e: Input.Submitted) -> None:
        if e.input.id == "hva":
            self.query_one("#nar", Input).focus()
            return
        title = self.query_one("#hva", Input).value.strip()
        when = parse_when(self.query_one("#nar", Input).value)
        if title and when:
            self.dismiss((title, when, self.repeat()))
        else:
            self.notify("Skriv både hva og når.", severity="warning", markup=False)


class ConfirmDialog(ModalScreen[bool]):
    BINDINGS = [Binding("j,y", "dismiss(True)", "Ja"), Binding("n,escape", "dismiss(False)", "Nei")]
    DEFAULT_CSS = "ConfirmDialog { align: center middle; }" + DIALOG_CSS

    def __init__(self, heading: str, text: str) -> None:
        super().__init__()
        self.heading, self.text = heading, text

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg") as v:
            v.border_title = self.heading
            yield Static(Text(self.text))
            yield Static(Text.assemble(("j", f"bold {GOLD}"), (" ja   ", MUTED), ("n", f"bold {GOLD}"), (" nei", MUTED)),
                         classes="hint")


class SeriesDeleteDialog(ModalScreen[bool | None]):
    """Slette en avtale som gjentas: bare denne (d) eller denne og alle senere (r)."""

    BINDINGS = [Binding("d", "dismiss(False)", "Bare denne"), Binding("r", "dismiss(True)", "Resten"),
                Binding("escape,n", "dismiss(None)", "Avbryt")]
    DEFAULT_CSS = "SeriesDeleteDialog { align: center middle; }" + DIALOG_CSS

    def __init__(self, title: str) -> None:
        super().__init__()
        self.title_ = title

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg") as v:
            v.border_title = "↻ Slette avtale som gjentas?"
            yield Static(Text(f"«{self.title_}» gjentas. Begge kan angres (u)."))
            yield Static(Text.assemble(("d", f"bold {GOLD}"), (" bare denne   ", MUTED), ("r", f"bold {BAD}"),
                                       (" denne og alle senere   ", MUTED), ("esc", f"bold {GOLD}"), (" avbryt", MUTED)),
                         classes="hint")


class InfoDialog(ModalScreen[None]):
    BINDINGS = [Binding("escape,enter,q,question_mark", "dismiss(None)", "Lukk")]
    DEFAULT_CSS = "InfoDialog { align: center middle; }" + DIALOG_CSS

    def __init__(self, heading: str, body: Text) -> None:
        super().__init__()
        self.heading, self.body = heading, body

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg") as v:
            v.border_title = self.heading
            yield Static(self.body)
            yield Static(Text.assemble(("esc", f"bold {GOLD}"), (" lukk", MUTED)), classes="hint")


KINDS = ("music", "film", "show", "audiobook", "ebook")
KIND_LABEL = {"music": "musikk", "film": "film", "show": "serie", "audiobook": "lydbok", "ebook": "e-bok",
              "unknown": "ukjent"}
DL_STATUS = {"laster ned": ("↓", GOLD), "lastet ned": ("↓", GOLD), "deler": ("⇅", OK), "ferdig": ("✓", OK),
             "feilet": ("✗", BAD), "avbrutt": ("·", DIM)}


def human_size(n: Any) -> str:
    if not n:
        return "ukjent størrelse"
    n = float(n)
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB" if n >= 1e6 else f"{n / 1e3:.0f} kB"


def inspect_card(d: dict[str, Any]) -> Text:
    """Forhåndsvisningen av en lenke (fra /media/inspect): alt fra serveren som ren tekst."""
    t = Text()
    t.append(str(d.get("title") or "(uten tittel)"), style=f"bold {GOLD}")
    if d.get("artist"):
        t.append(f" – {d['artist']}")
    if d.get("year"):
        t.append(f" ({d['year']})", style=MUTED)
    t.append("\n")
    bits = [KIND_LABEL.get(str(d.get("kind")), str(d.get("kind"))), {"ytdlp": "yt-dlp", "torrent": "torrent",
            "direct": "direkte"}.get(str(d.get("method")), str(d.get("method"))), human_size(d.get("size"))]
    if d.get("duration"):
        bits.append(clock(float(d["duration"])))
    if d.get("count"):
        bits.append(f"{d['count']} filer/spor")
    t.append(" · ".join(bits) + f" · fra {d.get('source') or '?'}\n", style=MUTED)
    for w in d.get("warnings") or []:
        t.append(f"⚠ {w}\n", style=BAD)
    return t


class AddDialog(ModalScreen[dict | None]):
    """a: lim inn en lenke → ⏎ undersøker den (ingenting lastes ned) → velg type og tittel → ⏎ starter."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Avbryt")]
    DEFAULT_CSS = "AddDialog { align: center middle; }" + DIALOG_CSS

    def __init__(self, call: Any) -> None:
        super().__init__()
        self.call = call  # LabScreen.call: feil blir en melding, aldri et krasj
        self.info: dict[str, Any] | None = None
        self.busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg") as v:
            v.border_title = "♪ Legg til fra lenke"
            yield Static("Lenke", classes="lbl")
            yield Input(placeholder="https://… · magnet:… · .torrent", id="url")
            yield Static("", id="card", classes="hint")
            yield Static("Type", classes="lbl")
            yield Input(placeholder="music · film · show · audiobook · ebook", id="kind")
            yield Static("Tittel (valgfri)", classes="lbl")
            yield Input(placeholder="fra kilden hvis tom", id="title")
            yield Static(Text.assemble(("⏎", f"bold {GOLD}"), (" undersøk / start   ", MUTED), ("tab", f"bold {GOLD}"),
                                       (" neste felt   ", MUTED), ("esc", f"bold {GOLD}"), (" avbryt", MUTED)), classes="hint")

    def on_input_changed(self, e: Input.Changed) -> None:
        if e.input.id == "url" and self.info is not None and e.value.strip() != self.info.get("url"):
            self.info = None  # ny lenke: må undersøkes på nytt
            self.query_one("#card", Static).update(Text(""))

    async def on_input_submitted(self, e: Input.Submitted) -> None:
        if self.busy:
            return
        url = self.query_one("#url", Input).value.strip()
        if not url:
            self.notify("Lim inn en lenke først.", severity="warning", markup=False)
            return
        if self.info is None or e.input.id == "url":
            await self.inspect(url)
        else:
            await self.start(url)

    async def inspect(self, url: str) -> None:
        card = self.query_one("#card", Static)
        card.update(Text("undersøker lenken … (ingenting lastes ned)", style=MUTED))
        self.busy = True
        try:
            d = await self.call("POST", "/media/inspect", {"url": url})
        finally:
            self.busy = False
        if not d:
            card.update(Text("Fikk ikke undersøkt lenken.", style=BAD))
            return
        self.info = d
        card.update(inspect_card(d))
        if d.get("kind") in KINDS:
            self.query_one("#kind", Input).value = str(d["kind"])
        self.query_one("#title", Input).value = str(d.get("title") or "")[:200]
        self.query_one("#kind" if d.get("kind") not in KINDS else "#title", Input).focus()

    async def start(self, url: str) -> None:
        kind = self.query_one("#kind", Input).value.strip().lower()
        kind = {"musikk": "music", "serie": "show", "lydbok": "audiobook", "e-bok": "ebook", "bok": "ebook"}.get(kind, kind)
        if kind not in KINDS:
            self.notify("Type må være music, film, show, audiobook eller ebook.", severity="warning", markup=False)
            self.query_one("#kind", Input).focus()
            return
        body: dict[str, Any] = {"url": url, "kind": kind}
        if title := self.query_one("#title", Input).value.strip():
            body["title"] = title[:200]
        if self.info and self.info.get("method") in ("ytdlp", "torrent", "direct"):
            body["method"] = self.info["method"]
        if self.info and self.info.get("artist"):
            body["artist"] = str(self.info["artist"])[:200]
        self.busy = True
        try:
            r = await self.call("POST", "/media/add", body)
        finally:
            self.busy = False
        if r:
            self.dismiss(r.get("download") or {})


class DownloadsScreen(ModalScreen[None]):
    """D: nedlastingene med framdrift, live fra websocketen. c avbryt · r prøv igjen · x fjern fra lista."""

    BINDINGS = [Binding("escape,q", "dismiss(None)", "Lukk"), Binding("c", "act('cancel')", show=False),
                Binding("r", "act('retry')", show=False), Binding("x", "act('forget')", show=False)]
    DEFAULT_CSS = ("DownloadsScreen { align: center middle; }" + DIALOG_CSS
                   + "#dlg { width: 100; height: 80%; } #dl-table { height: 1fr; }")

    def __init__(self, lab: Any) -> None:
        super().__init__()
        self.lab = lab
        self.items: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg") as v:
            v.border_title = "↓ Nedlastinger"
            yield LabTable(id="dl-table", cursor_type="row", zebra_stripes=True)
            yield Static("", id="dl-keys", classes="hint")

    async def on_mount(self) -> None:
        self.query_one("#dl-table").focus()
        items = await self.lab.call("GET", "/downloads")
        self.show(items if isinstance(items, list) else self.lab.downloads)

    def show(self, items: list[dict[str, Any]]) -> None:
        self.items = list(items)
        table = self.query_one("#dl-table", DataTable)
        cursor = table.cursor_row
        table.clear(columns=True)
        table.add_column("", width=2)
        table.add_column("tittel")
        table.add_column("type", width=8)
        table.add_column("framdrift", width=18)
        table.add_column("status", width=22)
        for d in self.items:
            icon, color = DL_STATUS.get(str(d.get("status")), ("?", MUTED))
            prog = float(d.get("progress") or 0)
            status = str(d.get("status") or "")
            if d.get("error") and status == "feilet":
                status = f"feilet: {d['error']}"
            table.add_row(Text(icon, style=color), str(d.get("title") or ""), KIND_LABEL.get(str(d.get("kind")), str(d.get("kind"))),
                          Text(f"{bar(prog)} {prog * 100:>3.0f} %", style=GOLD if prog < 1 else OK),
                          Text(status[:60], style=BAD if d.get("status") == "feilet" else MUTED), key=str(d.get("id")))
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1))
        self.render_keys()

    def selected(self) -> dict[str, Any] | None:
        table = self.query_one("#dl-table", DataTable)
        if not table.row_count:
            return None
        key = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        return next((d for d in self.items if str(d.get("id")) == key), None)

    def valid(self, d: dict[str, Any] | None) -> list[tuple[str, str]]:
        if d is None:
            return []
        st = d.get("status")
        keys = []
        if st in ("laster ned", "lastet ned", "deler"):
            keys.append(("c", "avbryt"))
        if st in ("feilet", "avbrutt") and d.get("method") != "opplasting":
            keys.append(("r", "prøv igjen"))
        keys.append(("x", "fjern fra lista"))
        return keys

    def render_keys(self) -> None:
        t = Text()
        if not self.items:
            t.append("Ingen nedlastinger. Trykk a i media for å legge til fra en lenke.\n", style=MUTED)
        for k, what in [*self.valid(self.selected()), ("esc", "lukk")]:
            t.append(f" {k}", style=f"bold {GOLD}")
            t.append(f" {what}  ", style=MUTED)
        self.query_one("#dl-keys", Static).update(t)

    def on_data_table_row_highlighted(self, _e: DataTable.RowHighlighted) -> None:
        self.render_keys()

    async def action_act(self, what: str) -> None:
        d = self.selected()
        if d is None or not any(k == {"cancel": "c", "retry": "r", "forget": "x"}[what] for k, _ in self.valid(d)):
            return
        did = str(d["id"])
        if what == "retry":
            r = await self.lab.call("POST", f"/downloads/{did}/retry")
        else:
            r = await self.lab.call("DELETE", f"/downloads/{did}" + ("?forget=1" if what == "forget" else ""))
        if r is not None:
            self.notify({"cancel": "Avbrutt.", "retry": "Prøver igjen.", "forget": "Fjernet fra lista."}[what], markup=False)
            items = await self.lab.call("GET", "/downloads")
            if isinstance(items, list):
                self.show(items)


class EditDialog(ModalScreen[dict | None]):
    """e: rett tittel, artist, album, år og sjanger – feltene som gjelder for typen."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Avbryt")]
    DEFAULT_CSS = "EditDialog { align: center middle; }" + DIALOG_CSS
    FIELDS = {"album": [("title", "Album"), ("artist", "Albumartist"), ("year", "År"), ("genre", "Sjanger")],
              "track": [("title", "Tittel"), ("artist", "Artist"), ("album", "Album"), ("year", "År"), ("genre", "Sjanger")],
              "video": [("title", "Tittel"), ("year", "År")],
              "book": [("title", "Tittel"), ("artist", "Forfatter")]}

    def __init__(self, info: dict[str, Any]) -> None:
        super().__init__()
        self.info = info
        self.fields = self.FIELDS.get(str(info.get("type")), self.FIELDS["track"])

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg") as v:
            v.border_title = "✎ Rediger"
            yield Static(Text(f"{self.info.get('files')} filer · {self.info.get('folder')}", style=MUTED))
            for key, label in self.fields:
                yield Static(label, classes="lbl")
                val = self.info.get(key)
                yield Input(value="" if val is None else str(val), id=f"f-{key}")
            yield Static(Text.assemble(("⏎", f"bold {GOLD}"), (" lagre   ", MUTED), ("tab", f"bold {GOLD}"),
                                       (" neste felt   ", MUTED), ("esc", f"bold {GOLD}"), (" avbryt", MUTED)), classes="hint")

    def on_input_submitted(self, _e: Input.Submitted) -> None:
        out: dict[str, Any] = {}
        for key, label in self.fields:
            raw = self.query_one(f"#f-{key}", Input).value.strip()
            old = self.info.get(key)
            if key == "year":
                if not raw:
                    continue
                if not raw.isdigit() or not 0 <= int(raw) <= 3000:
                    self.notify("År må være et tall (f.eks. 1999).", severity="warning", markup=False)
                    return
                if int(raw) != old:
                    out[key] = int(raw)
            elif raw != (old or ""):
                if len(raw) > 200:
                    self.notify(f"{label} kan være maks 200 tegn.", severity="warning", markup=False)
                    return
                out[key] = raw
        self.dismiss(out)


class PlaceDialog(ModalScreen[str | None]):
    """v på hjem: stedet været gjelder for (slås opp hos Kartverket)."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Avbryt")]
    DEFAULT_CSS = "PlaceDialog { align: center middle; }" + DIALOG_CSS

    def __init__(self, current: str = "") -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg") as v:
            v.border_title = "☁ Sted for været"
            yield Input(placeholder=f"nå: {self.current}" if self.current else "f.eks. Oslo, Bergen, Tromsø",
                        id="sted")
            yield Static(Text.assemble(("⏎", f"bold {GOLD}"), (" lagre   ", MUTED), ("esc", f"bold {GOLD}"), (" avbryt   ", MUTED),
                                       ("bare stedsnavnet sendes til Kartverket", DIM)), classes="hint")

    def on_input_submitted(self, e: Input.Submitted) -> None:
        if e.value.strip():
            self.dismiss(e.value.strip())


WEATHER_WORDS = (("clearsky", "klart"), ("fair", "lettskyet"), ("partlycloudy", "delvis skyet"), ("cloudy", "skyet"),
                 ("fog", "tåke"), ("heavyrain", "kraftig regn"), ("lightrain", "lett regn"), ("rain", "regn"),
                 ("heavysnow", "kraftig snø"), ("lightsnow", "lett snø"), ("snow", "snø"), ("sleet", "sludd"))


def weather_word(symbol: str) -> str:
    return next((w for k, w in WEATHER_WORDS if symbol.startswith(k)), symbol.split("_")[0])


class CoverDialog(ModalScreen[tuple[str, str] | None]):
    """c: lim inn en bildelenke – eller ⏎ i tomt felt for å lete på nettet på nytt."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Avbryt")]
    DEFAULT_CSS = "CoverDialog { align: center middle; }" + DIALOG_CSS

    def __init__(self, what: str) -> None:
        super().__init__()
        self.what = what

    def compose(self) -> ComposeResult:
        with Vertical(id="dlg") as v:
            v.border_title = "▣ Cover"
            yield Static(Text(self.what, style=GOLD))
            yield Static("Lenke til bilde (JPEG, PNG eller WebP, maks 10 MB)", classes="lbl")
            yield Input(placeholder="https://…", id="url")
            yield Static(Text.assemble(("⏎", f"bold {GOLD}"), (" bruk bildet   ", MUTED), ("⏎ i tomt felt", f"bold {GOLD}"),
                                       (" let på nettet på nytt   ", MUTED), ("esc", f"bold {GOLD}"), (" avbryt", MUTED)),
                         classes="hint")

    def on_input_submitted(self, e: Input.Submitted) -> None:
        url = e.value.strip()
        if url and not url.startswith(("http://", "https://")):
            self.notify("Bare http(s)-lenker.", severity="warning", markup=False)
            return
        self.dismiss(("url", url) if url else ("fetch", ""))


def help_text() -> Text:
    t = Text()
    t.append("Olympus Lab's er samme app som Zeus på telefonen – samme faner, samme samtale, samme spiller.\n\n", style=MUTED)
    rows = [("1–5", "bytt fane: hjem · zeus · plan · media · PC"), ("⏎", "hovedhandlingen (står nederst)"),
            ("esc", "tilbake (ut av skrivefeltet, ellers til hjem)"), ("r", "oppdater nå"),
            ("zeus", "⏎ send · ctrl+j godkjenn · ctrl+n avslå · ctrl+v se hele · ctrl+s stopp · F5 ny samtale"),
            ("hjem", "v sted for været"),
            ("plan", "n ny (kan gjentas) · ⏎ endre · d slett (↻: bare denne eller resten) · u angre siste endring"),
            ("media", "m musikk · f film · b bøker · ⏎ spill på PC-en / info"),
            ("", "a legg til fra lenke · D nedlastinger · e rediger · c cover · x slett · S skann"),
            ("spiller", "␣ pause/fortsett · n neste · p forrige · s stopp (hjem, media, PC)"),
            ("PC", "⏎ bruk utgang · + − volum · m demp · c Bluetooth · d fjern telefon · l lås skjermen"),
            ("z", "til den vanlige Olympus-chatten")]
    for k, v in rows:
        t.append(f"  {k:<9}", style=f"bold {GOLD}")
        t.append(f"{v}\n")
    t.append("\n✦ betyr at Zeus gjorde det. Det du skriver til Zeus her, ser du på telefonen – og omvendt.\n", style=MUTED)
    t.append("Godkjenninger: lange forhåndsvisninger må åpnes (ctrl+v) før de kan godkjennes; farlige krever to trykk.",
             style=MUTED)
    return t


class LabTable(DataTable):
    """Tabell i Lab's-stil: gull markør i stedet for Textuals blå, rolige rader."""

    DEFAULT_CSS = f"""
    LabTable {{ height: 1fr; background: #0b0b0f; scrollbar-color: {LINE}; scrollbar-color-hover: {GOLD};
                scrollbar-color-active: {GOLD}; scrollbar-background: #0b0b0f; }}
    LabTable > .datatable--header {{ color: {MUTED}; background: #0b0b0f; text-style: bold; }}
    LabTable > .datatable--odd-row {{ background: #0b0b0f; }}
    LabTable > .datatable--even-row {{ background: #101017; }}
    LabTable > .datatable--hover {{ background: #16161f; }}
    LabTable > .datatable--cursor {{ background: #1e1a12; color: {GOLD}; text-style: none; }}
    LabTable:focus > .datatable--cursor {{ background: #3a3020; color: {GOLD}; text-style: bold; }}
    LabTable:focus {{ background-tint: 0%; }}
    LabTable > .datatable--header-cursor, LabTable > .datatable--header-hover {{ background: #0b0b0f; }}
    """

    def add_row(self, *cells: Any, **kw: Any) -> Any:  # type: ignore[override]
        # Tekst fra serveren (titler, navn) tolkes ellers som Rich-markup: «[/]» i en tittel krasjet skjermen.
        return super().add_row(*(Text(c) if isinstance(c, str) else c for c in cells), **kw)

    def on_focus(self) -> None:
        if isinstance(self.screen, LabScreen):
            self.screen.render_chrome()


class ChatInput(Input):
    """Skrivefeltet til Zeus. Tomt felt: 1–5 bytter fane som ellers (du slipper Esc først)."""

    async def _on_key(self, event: events.Key) -> None:
        if not self.value and event.character in ("1", "2", "3", "4", "5") and isinstance(self.screen, LabScreen):
            event.stop()
            event.prevent_default()
            self.screen.action_tab(TABS[int(event.character) - 1][0])
            return
        await super()._on_key(event)


# --- kort på hjem ---------------------------------------------------------------------------------
class Card(Static, can_focus=True):
    """Et kort på dashbordet: ramme med gulltittel, Enter hopper til fanen det handler om."""

    DEFAULT_CSS = f"""
    Card {{ border: round {LINE}; border-title-color: {MUTED}; border-title-style: bold; padding: 0 1; height: auto;
            min-height: 4; }}
    Card:focus {{ border: round {GOLD}; border-title-color: {GOLD}; }}
    """

    def __init__(self, title: str, target: str, **kw: Any) -> None:
        super().__init__("", **kw)
        self.border_title = title
        self.target = target  # «fane» eller «fane:undervalg»

    def on_focus(self) -> None:
        if isinstance(self.screen, LabScreen):
            self.screen.render_chrome()


class LabScreen(Screen):
    BINDINGS = [
        *[Binding(str(i + 1), f"tab('{name}')", LABEL.get(name, name), show=False) for i, (name, _) in enumerate(TABS)],
        Binding("z", "zeus_chat", "Olympus-chat", show=False),
        Binding("escape", "back", "Tilbake", show=False),
        Binding("r", "reload", "Oppdater", show=False),
        Binding("question_mark", "help", "Hjelp", show=False),
        Binding("n", "key_n", show=False),
        Binding("p", "prev", show=False),
        Binding("d", "delete", show=False),
        Binding("u", "undo", show=False),
        Binding("s", "stop", show=False),
        Binding("space", "pause", show=False),
        Binding("plus,equals_sign", "volume(10)", show=False),
        Binding("minus", "volume(-10)", show=False),
        Binding("m", "key_m", show=False),
        Binding("f", "sub('film')", show=False),
        Binding("b", "sub('boker')", show=False),
        Binding("c", "key_c", show=False),
        Binding("a", "add_media", show=False),
        Binding("D", "downloads", show=False),
        Binding("e", "edit_item", show=False),
        Binding("x", "delete_item", show=False),
        Binding("S", "scan", show=False),
        Binding("l", "lock", show=False),
        Binding("v", "place", show=False),
        # zeus: samme taster og regler som `olympus zeus`
        Binding("ctrl+j", "approve(True)", show=False, priority=True),
        Binding("ctrl+n", "approve(False)", show=False, priority=True),
        Binding("ctrl+v", "view_preview", show=False, priority=True),
        Binding("ctrl+s", "interrupt", show=False, priority=True),
        Binding("f2", "toggle_ai", show=False),
        Binding("f5", "new_chat", show=False),
    ]
    DEFAULT_CSS = f"""
    LabScreen {{ background: #0b0b0f; }}
    #lab-head, #lab-tabs, #lab-keys, #lab-now {{ height: 1; padding: 0 1; }}
    #lab-tabs {{ margin-bottom: 1; }}
    #lab-now {{ display: none; background: #16130c; }}
    #lab-keys {{ background: #111118; }}
    #lab-body {{ height: 1fr; padding: 0 1; }}
    .panel {{ border: round {LINE}; border-title-color: {GOLD}; border-title-style: bold; border-subtitle-color: {DIM};
              border-subtitle-align: right; height: 1fr; padding: 0 1; }}
    .empty {{ color: {MUTED}; padding: 1 1; display: none; }}
    .legend {{ color: {MUTED}; height: auto; }}
    #home-grid, #pc-grid {{ grid-size: 2; grid-gutter: 0 1; height: auto; }}
    #home-grid.narrow, #pc-grid.narrow {{ grid-size: 1; }}
    #home-strip {{ color: {MUTED}; padding: 0 1; height: auto; }}
    .panel > LabTable {{ height: auto; max-height: 100%; }}
    #pc-play, #pc-lyd, #pc-status, #pc-dev, #log-panel {{ height: auto; }}
    #pc-status, #pc-dev {{ min-height: 6; }}
    #log-panel {{ max-height: 9; }}
    #media-sub {{ height: 1; margin-bottom: 1; }}
    #zeus-panel {{ border-subtitle-color: {MUTED}; }}
    #zeus-feed {{ height: 1fr; scrollbar-color: {LINE}; scrollbar-color-hover: {GOLD}; scrollbar-background: #0b0b0f; }}
    #zeus-feed > Static {{ height: auto; margin-top: 1; }}
    #zeus-feed > Static.tool {{ margin-top: 0; }}
    #zeus-feed > Static.first {{ margin-top: 1; }}
    #zeus-ask {{ height: auto; max-height: 50%; border: round {BAD}; border-title-color: {BAD}; border-title-style: bold;
                 padding: 0 1; display: none; }}
    #zeus-say {{ border: tall {LINE}; background: #111118; }}
    #zeus-say:focus {{ border: tall {GOLD}; }}
    """

    # Godkjenningene: samme regler og samme kode som `olympus zeus` (olympus.tui.approvals.ApprovalGate)
    pending = property(lambda self: self.gate.pending)

    def __init__(self, api: Api) -> None:
        super().__init__()
        self.api = api
        self.tab = "hjem"
        self.sub = "musikk"
        self.data: dict[str, Any] = {}
        self.ok = True
        self.updated = ""
        # den ene spilleren på PC-en (fra /pc og {"type": "player"}); posisjonen telles videre lokalt
        self.player: dict[str, Any] = {}
        self.player_at = 0.0
        # samtalen med Zeus (samme som telefonen)
        self.ws: Any = None
        self.cid = ""
        self.zstate = "ledig"
        self.ai = True
        self.model, self.cloud = "", False
        self.answer: Static | None = None
        self.answer_text = ""
        self.gate = ApprovalGate()
        self.unseen = 0  # meldinger i samtalen mens du var i en annen fane
        self.downloads: list[dict[str, Any]] = []  # fra {"type": "downloads"} på websocketen

    def compose(self) -> ComposeResult:
        yield Static(id="lab-head")
        yield Static(id="lab-tabs")
        with ContentSwitcher(initial="hjem", id="lab-body"):
            with VerticalScroll(id="hjem"):
                with Grid(id="home-grid"):
                    yield Card("▦ I DAG", "plan", id="card-plan")
                    yield Card("♪ SPILLES NÅ", "pc", id="card-now")
                    yield Card("↻ FORTSETT", "media:film", id="card-fortsett")
                    yield Card("◈ PC-EN", "pc", id="card-pc")
                yield Static(id="home-strip")
            with Vertical(id="zeus"):
                with Vertical(id="zeus-panel", classes="panel") as v:
                    v.border_title = "ϟ samtalen – den samme som på telefonen"
                    yield VerticalScroll(id="zeus-feed")
                yield Static(id="zeus-ask")
                yield ChatInput(placeholder="skriv til Zeus …  (⏎ send)", id="zeus-say")
            with Vertical(id="plan"):
                with Vertical(id="plan-panel", classes="panel") as v:
                    v.border_title = "neste 31 dager"
                    yield LabTable(id="plan-table", cursor_type="row", zebra_stripes=True)
                    yield Static(id="plan-empty", classes="empty")
                    yield Static(id="plan-legend", classes="legend")
                with Vertical(id="log-panel", classes="panel") as v:
                    v.border_title = "≡ siste endringer"
                    yield Static(id="plan-log")
            with Vertical(id="media"):
                with Vertical(id="media-panel", classes="panel") as v:
                    yield Static(id="media-sub")
                    yield LabTable(id="media-table", cursor_type="row", zebra_stripes=True)
                    yield Static(id="media-empty", classes="empty")
            with VerticalScroll(id="pc"):
                with Vertical(id="pc-play", classes="panel") as v:
                    v.border_title = "♪ PC-avspilling"
                    yield Static(id="pc-now")
                with Vertical(id="pc-lyd", classes="panel") as v:
                    v.border_title = "lyd på denne PC-en"
                    yield LabTable(id="pc-table", cursor_type="row")
                with Grid(id="pc-grid"):
                    with Vertical(id="pc-status", classes="panel") as v:
                        v.border_title = "status"
                        yield Static(id="pc-info")
                    with Vertical(id="pc-dev", classes="panel") as v:
                        v.border_title = "telefoner med tilgang"
                        yield LabTable(id="dev-table", cursor_type="row")
                        yield Static(id="dev-empty", classes="empty")
        yield Static(id="lab-now")
        yield Static(id="lab-keys")

    async def on_mount(self) -> None:
        self.query_one("#card-plan").focus()  # Enter på hjem åpner da «I DAG» med en gang
        self.render_chrome()
        self.render_zeus()
        self.clear_feed()
        await self.action_reload()
        self.set_interval(15, self.action_reload)  # reserve hvis websocketen er nede
        self.set_interval(1, self.tick)  # framdriften i sangen (bare tekst, ingen kall)
        self.run_worker(self.live(), group="live")

    # --- websocketen: samtalen, spilleren og endringer ---------------------------------------------
    async def live(self) -> None:
        """Samme websocket som appen: samtalen med Zeus, spilleren på PC-en og endringer fra telefonen."""
        import aiohttp

        origin = self.api.base.removesuffix("/api/v1")
        while True:
            try:
                async with aiohttp.ClientSession(cookies=self.api.cookies) as s, s.ws_connect(
                        origin.replace("http", "ws", 1) + "/api/v1/ws", headers={"Origin": origin}, heartbeat=25) as ws:
                    self.ws = ws
                    self.clear_feed()  # hjernen sender historikken på nytt
                    async for m in ws:
                        if m.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            msg = m.json()
                        except ValueError:
                            continue
                        if isinstance(msg, dict):
                            try:
                                self.on_server(msg)
                            except Exception as e:  # noqa: BLE001 – én rar melding skal ikke ta ned Lab's
                                self.log_line("✗", f"kunne ikke vise en hendelse: {type(e).__name__}", BAD)
            except (aiohttp.ClientError, OSError, ValueError):
                pass
            self.ws = None
            self.render_chrome()
            await asyncio.sleep(5)

    def on_server(self, m: dict[str, Any]) -> None:
        kind = m.get("type")
        if kind == "player":
            self.set_player(m)
            return
        if kind == "downloads":
            self.set_downloads(m.get("items") or [])
            return
        if kind in ("plan_changed", "ai", "model") or (kind == "media_changed" and m.get("what") != "pc"):
            self.run_worker(self.action_reload(), exclusive=True, group="reload")
        self.on_zeus(m)

    def set_downloads(self, items: list[dict[str, Any]]) -> None:
        self.downloads = [d for d in items if isinstance(d, dict)]
        for scr in self.app.screen_stack:
            if isinstance(scr, DownloadsScreen):
                scr.show(self.downloads)
        self.render_chrome()

    def active_downloads(self) -> int:
        return sum(1 for d in self.downloads if d.get("status") in ("laster ned", "lastet ned"))

    def set_player(self, p: dict[str, Any]) -> None:
        self.player = {k: v for k, v in p.items() if k != "type"}
        self.player_at = time.monotonic()
        self.data["pc"] = self.player
        self.render_player()
        self.render_chrome()

    def player_pos(self) -> float:
        p = self.player
        pos = float(p.get("position") or 0)
        if p.get("playing"):
            pos += time.monotonic() - self.player_at
        dur = float(p.get("duration") or 0)
        return min(pos, dur) if dur else pos

    def tick(self) -> None:
        if self.player.get("playing"):
            self.render_player()

    def render_player(self) -> None:
        """Spilleren vises tre steder: kortet på hjem, PC-fanen og linja nederst (andre faner)."""
        w = self.size.width or 120
        pos = self.player_pos()
        card = self.query_one("#card-now", Card)
        card.update(now_playing(self.player, pos, max(24, (w - 6) // 2 - 4)) if self.player.get("current") else
                    Text.assemble(("Ingenting spilles på PC-en.\n", MUTED), ("Velg noe i 4 media og trykk ⏎.\n", DIM)))
        self.query_one("#pc-now", Static).update(
            now_playing(self.player, pos, min(80, w - 8), keys=True) if self.player.get("current")
            else Text.assemble(("Ingenting spilles på PC-en.\n", MUTED),
                               ("Velg et album eller en spilleliste i 4 media og trykk ⏎ – eller be Zeus: «spill noe jeg liker».",
                                DIM)))
        cur = self.player.get("current")
        now = self.query_one("#lab-now", Static)
        now.display = bool(cur) and self.tab not in ("hjem", "pc")
        if cur:
            dur = float(self.player.get("duration") or 0)
            t = Text.assemble(("‖ " if self.player.get("paused") else "▶ ", GOLD), (str(cur.get("title")), f"bold {GOLD}"),
                              (f" – {cur.get('artist') or ''}  ", GOLD), (f"{clock(pos)}/{clock(dur)}", MUTED),
                              no_wrap=True, overflow="ellipsis")
            if self.tab == "media":  # tastene bare der de virker (ikke i skrivefeltet eller planen)
                t.append_text(Text.assemble(("   ␣", f"bold {GOLD}"), (" fortsett" if self.player.get("paused") else " pause", MUTED),
                                            ("  s", f"bold {GOLD}"), (" stopp", MUTED)))
            now.update(t)

    # --- samtalen ------------------------------------------------------------------------------------
    def clear_feed(self) -> None:
        feed = self.query_one("#zeus-feed", VerticalScroll)
        feed.remove_children()
        feed.mount(Static(Text.assemble(("Ingen samtale ennå.\n", MUTED),
                                        ("Skriv under og trykk ⏎. Samtalen er den samme som i Zeus-appen: det du skriver her, ser "
                                         "du på telefonen – og omvendt.", DIM)), classes="hint-empty"))
        self.answer, self.answer_text = None, ""

    def add_feed(self, t: Text, classes: str = "") -> Static:
        feed = self.query_one("#zeus-feed", VerticalScroll)
        for hint in feed.query(".hint-empty"):
            hint.remove()
        at_end = feed.scroll_offset.y >= feed.max_scroll_y - 1
        last = next((c for c in reversed(feed.children) if not c.has_class("hint-empty")), None)
        if classes == "tool" and (last is None or not last.has_class("tool")):
            classes = "tool first"  # første verktøylinje etter en melding: luft over
        w = Static(t, classes=classes)
        feed.mount(w)
        kids = list(feed.children)
        if len(kids) > FEED_MAX:
            for old in kids[:len(kids) - FEED_MAX]:
                old.remove()
        if at_end:
            feed.call_after_refresh(feed.scroll_end, animate=False)
        if self.tab != "zeus":
            self.unseen += 1
        return w

    def log_line(self, icon: str, text: str, style: str = MUTED) -> None:
        t = Text(f"{icon} ", style=style)
        t.append(text, style=style)
        self.add_feed(t, "tool")

    def user_line(self, text: str, mine: bool) -> None:
        t = Text()
        t.append("› ", style=f"bold {GOLD}")
        t.append(text, style="bold")
        t.append("   du · " + ("her" if mine else "telefonen / en annen skjerm"), style=DIM)
        self.add_feed(t)

    def agent_text(self, text: str, draft: bool = False) -> Any:
        """Zeus' svar: overskrift og Markdown (trygt – Rich-markup i teksten tolkes ikke)."""
        head = Text()
        head.append("ϟ Zeus", style=f"bold {DIM if draft else GOLD}")
        if draft:
            head.append("  forkastet – påsto noe uten å gjøre det; prøver igjen", style=DIM)
            return Group(head, Text(text.strip() or "…", style=DIM))
        return Group(head, Markdown(text.strip() or "…"))

    def on_zeus(self, m: dict[str, Any]) -> None:
        kind = m.get("type")
        if kind == "hello":
            self.cid = str(m.get("id") or "")
        elif kind == "history":
            self.clear_feed()
            for e in m.get("items") or []:
                if e.get("kind") == "user":
                    self.user_line(str(e.get("text") or ""), False)
                elif e.get("kind") == "agent":
                    self.add_feed(self.agent_text(str(e.get("text") or ""), bool(e.get("draft"))))
            self.unseen = 0
        elif kind == "cleared":
            self.clear_feed()
            self.log_line("·", "ny samtale")
        elif kind == "user":
            self.user_line(str(m.get("text") or ""), bool(self.cid) and m.get("origin") == self.cid)
            self.zstate, self.answer, self.answer_text = "tenker", None, ""
        elif kind == "agent_start":
            self.zstate = "tenker"
            self.answer, self.answer_text = None, ""
        elif kind == "chunk":
            self.answer_text += str(m.get("text") or "")
            if self.answer is None:
                self.answer = self.add_feed(self.agent_text(self.answer_text))
            else:
                self.answer.update(self.agent_text(self.answer_text))
                feed = self.query_one("#zeus-feed", VerticalScroll)
                if feed.scroll_offset.y >= feed.max_scroll_y - 3:
                    feed.call_after_refresh(feed.scroll_end, animate=False)
            self.zstate = "tenker"
        elif kind == "tool":
            self.zstate = "verktøy"
            self.answer, self.answer_text = None, ""  # svaret etter verktøyet blir en ny boble under
            self.log_line("⚙", f"{m.get('name')} {m.get('detail') or ''}".strip(), MUTED)
        elif kind == "tool_result":
            ok = bool(m.get("ok"))
            self.log_line("  ✓" if ok else "  ✗", str(m.get("summary") or ""), OK if ok else BAD)
            if m.get("denied"):
                self.log_line("  ·", "du avslo – ingenting ble gjort", DIM)
        elif kind == "undoable":  # gjort uten kort: kan angres med u
            self.last_undo = (int(m.get("seq") or 0), str(m.get("text") or ""))
            self.log_line("  ↶", "trykk u for å angre", DIM)
        elif kind == "judge_rejected":
            if self.answer is not None:
                self.answer.update(self.agent_text(self.answer_text, draft=True))
            self.answer, self.answer_text = None, ""
        elif kind == "approval":
            self.gate.add(m)
            self.zstate = "venter"
            self.log_line("‖", f"{m.get('reason')} – venter på deg", BAD)
            self.app.bell()
            if self.tab != "zeus":
                self.notify("ϟ Zeus venter på godkjenning – trykk 2.", severity="warning", markup=False)
        elif kind == "approval_done":
            self.gate.done(m.get("id"))
            if not self.pending and self.zstate == "venter":
                self.zstate = "verktøy"
        elif kind == "notice":
            self.log_line("·", str(m.get("text") or ""))
        elif kind == "error":
            self.log_line("✗", str(m.get("text") or ""), BAD)
        elif kind == "done":
            self.zstate, self.answer, self.answer_text = ("ledig" if self.ai else "av"), None, ""
            self.gate.clear()
        elif kind == "state":
            st = m.get("state") or {}
            self.ai = bool(st.get("ai", True))
            self.model, self.cloud = str(st.get("model") or self.model), bool(st.get("cloud"))
            self.zstate = "tenker" if st.get("busy") else ("ledig" if self.ai else "av")
        elif kind == "stats":
            st = m.get("stats") or {}
            if st.get("model"):
                self.model, self.cloud = str(st["model"]), bool(st.get("cloud"))
        elif kind == "model":
            self.model, self.cloud = str(m.get("model") or ""), bool(m.get("cloud"))
            self.log_line("ϟ", f"svarer nå med {'☁ ' if self.cloud else ''}{self.model}", BAD if self.cloud else MUTED)
        elif kind == "ai":
            self.ai = bool(m.get("on"))
            self.zstate = "ledig" if self.ai else "av"
        else:
            return
        self.render_zeus()
        self.render_chrome()

    def must_view(self, p: dict[str, Any]) -> bool:
        return self.gate.must_view(p)

    def render_zeus(self) -> None:
        label, color = STATE_TEXT[self.zstate]
        panel = self.query_one("#zeus-panel")
        model = (f"☁ {self.model} · sky – samtalen sendes ut" if self.cloud else f"{self.model} · lokal") if self.model else ""
        panel.border_subtitle = f"{label}   {model}"
        panel.styles.border_subtitle_color = color if self.zstate != "ledig" else (BAD if self.cloud else MUTED)
        ask = self.query_one("#zeus-ask", Static)
        p = self.gate.current()  # id-en som vises er den eneste som kan godkjennes
        ask.display = p is not None
        if p is not None:
            ask.border_title = self.gate.title(p)
            ask.update(self.gate.card(p))

    async def zsend(self, msg: dict[str, Any]) -> bool:
        if self.ws is None:
            self.notify("Ingen kontakt med Olympus-serveren.", severity="error", markup=False)
            return False
        try:
            await self.ws.send_json(msg)
        except (ConnectionError, RuntimeError):
            self.notify("Ingen kontakt med Olympus-serveren.", severity="error", markup=False)
            return False
        return True

    async def on_input_submitted(self, e: Input.Submitted) -> None:
        if e.input.id != "zeus-say":
            return
        text = e.value.strip()
        if not text:
            return
        if not self.ai:
            self.notify("Zeus er av – trykk F2 for å slå ham på.", severity="warning", markup=False)
            return
        if await self.zsend({"type": "send", "text": text}):
            e.input.value = ""  # meldingen vises når hjernen sender den tilbake (som på telefonen)

    async def action_approve(self, yes: bool, aid: str | None = None) -> None:
        """ctrl+j/ctrl+n: svar på godkjenningen som vises – bare i zeus-fanen (eller fra «se hele»-vinduet, som åpnes
        der). Enter, mellomrom og andre taster godkjenner aldri."""
        if self.tab != "zeus":
            if self.pending:
                self.notify("Gå til 2 zeus for å se hva Zeus vil før du svarer.", severity="warning", markup=False)
            return
        verdict, p = self.gate.decide(yes, aid)
        if verdict in ("view", "confirm"):
            self.notify(self.gate.notice(verdict), severity="warning", markup=False)
            if verdict == "view":
                self.app.bell()
            self.render_zeus()
            return
        if verdict != "send" or p is None:
            return
        if await self.zsend({"type": "approve", "id": p["id"], "answer": "yes" if yes else "no"}):
            self.log_line("✓" if yes else "✗", f"du {'godkjente' if yes else 'avslo'}: {p.get('reason')}", OK if yes else BAD)

    def action_view_preview(self) -> None:
        if self.tab != "zeus":
            return
        from olympus.tui.zeus_window import FullPreview

        p = self.gate.view()
        if p is None:
            return
        self.render_zeus()

        def done(answer: str | None) -> None:
            if answer is not None:  # gjelder akkurat kallet som ble vist i vinduet
                self.run_worker(self.action_approve(answer == "yes", str(p.get("id"))))

        self.app.push_screen(FullPreview(p), done)

    async def action_interrupt(self) -> None:
        if self.tab == "zeus" and self.zstate not in ("ledig", "av"):
            await self.zsend({"type": "interrupt"})

    async def action_toggle_ai(self) -> None:
        if self.tab != "zeus":
            return
        r = await self.call("POST", "/ai", {"on": not self.ai})
        if r is not None:
            self.ai = bool(r.get("ai"))
            self.zstate = "ledig" if self.ai else "av"
            self.render_zeus()
            self.render_chrome()

    async def action_new_chat(self) -> None:
        if self.tab != "zeus":
            return
        if self.zstate not in ("ledig", "av"):
            self.notify("Vent til svaret er ferdig (ctrl+s stopper det).", markup=False)
            return
        await self.zsend({"type": "new"})

    def on_resize(self, _e: Any) -> None:
        self.query_one("#home-grid").set_class(self.size.width < 90, "narrow")
        self.query_one("#pc-grid").set_class(self.size.width < 90, "narrow")
        self.render_chrome()
        self.render_player()

    # --- topp, faner, taster -----------------------------------------------------------------------
    def render_chrome(self) -> None:
        w = self.size.width or 120
        head = Text(no_wrap=True, overflow="crop")
        head.append("ϟ ZEUS", style=f"bold {GOLD}")
        status = "● tilkoblet" if self.ok else "○ ingen kontakt"
        head.append(" " * max(2, w - 2 - len("ϟ ZEUS") - len(status)))
        head.append(status, style=OK if self.ok else BAD)
        self.query_one("#lab-head", Static).update(head)
        tabs = Text(no_wrap=True, overflow="crop")
        for i, (name, icon) in enumerate(TABS):
            on = name == self.tab
            label = f" {i + 1} {icon} {LABEL.get(name, name)} "
            tabs.append(label, style=f"bold {GOLD} on #2a2418" if on else MUTED)
            if name == "zeus" and self.pending and not on:
                tabs.append("‖ ", style=f"bold {BAD}")
            elif name == "zeus" and self.unseen and not on:
                tabs.append("• ", style=GOLD)
            tabs.append(" ")
        self.query_one("#lab-tabs", Static).update(tabs)
        self.query_one("#lab-keys", Static).update(keyline(self.keys(), w))
        now = self.query_one("#lab-now", Static)
        now.display = bool(self.player.get("current")) and self.tab not in ("hjem", "pc")

    def focused_table(self) -> DataTable | None:
        f = self.focused
        return f if isinstance(f, DataTable) else None

    def keys(self) -> list[tuple[str, str]]:
        """Bare taster som faktisk gjør noe akkurat nå – samme rekkefølge overalt: ⏎ først, så resten, esc sist."""
        playing = bool(self.player.get("current"))
        player = ([("␣", "fortsett" if self.player.get("paused") else "pause"), ("n", "neste"), ("p", "forrige"),
                   ("s", "stopp")] if playing else [])
        table = self.focused_table()
        has_rows = bool(table and table.display and table.row_count)
        if self.tab == "hjem":
            f = self.focused
            target = f.target.split(":")[0] if isinstance(f, Card) else ""
            return [("⏎", f"åpne {LABEL.get(target, target)}"), ("tab", "neste kort"), *player, ("v", "sted for været")]
        if self.tab == "zeus":
            pairs: list[tuple[str, str]] = []
            if self.pending:
                p = self.pending[0]
                pairs.append(("ctrl+j", "godkjenn") if not self.must_view(p) else ("ctrl+v", "se hele først"))
                pairs.append(("ctrl+n", "avslå"))
                if not self.must_view(p) and str(p.get("id")) not in self.gate.viewed:
                    if preview_parts(str(p.get("preview") or ""))[1]:
                        pairs.append(("ctrl+v", "se hele"))
            pairs.append(("⏎", "send"))
            if self.zstate not in ("ledig", "av"):
                pairs.append(("ctrl+s", "stopp svaret"))
            else:
                pairs.append(("F5", "ny samtale"))
            pairs.append(("F2", "slå av Zeus" if self.ai else "slå på Zeus"))
            pairs.append(("esc", "ut av feltet" if isinstance(self.focused, Input) else "til hjem"))
            return pairs
        if self.tab == "plan":
            pairs = [("⏎", "endre"), ("n", "ny avtale"), ("d", "slett")] if has_rows else [("n", "ny avtale")]
            log = self.own_log()
            if log:
                undo = {"add": "fjern «{}» igjen", "delete": "hent tilbake «{}»", "update": "tilbakestill «{}»",
                        "undo": "gjør om «{}»"}.get(log[0]["action"], "angre «{}»")
                pairs.append(("u", undo.format(log[0]["title"][:24])))
            return [*pairs, ("esc", "til hjem")]
        if self.tab == "media":
            main = {"musikk": "spill på PC-en", "film": "info", "boker": "info"}[self.sub]
            subs = [(k, SUB_LABEL.get(s, s)) for s, k, _ in SUBS if s != self.sub]
            n = self.active_downloads()
            item = [("e", "rediger"), ("c", "cover"), ("x", "slett")] if self.media_item() else []
            if self.sub == "boker" and item and (self.data.get("books", {}).get(self.media_item() or "") or {}).get("kind") == "ebook":
                item = [("e", "rediger"), ("x", "slett")]  # e-bøker har ikke eget cover
            return [*([("⏎", main)] if has_rows else []), ("a", "legg til"), *item,
                    ("D", f"nedlastinger ({n})" if n else "nedlastinger"), ("S", "skann"), *subs, *player, ("esc", "til hjem")]
        # pc
        pairs = []
        if table is not None and table.id == "pc-table" and has_rows:
            key = self.selected() or ""
            if key.startswith("o:"):
                pairs += [("⏎", "bruk utgang"), ("+ −", "volum"), ("m", "demp")]
            elif key.startswith("b:"):
                pairs += [("⏎", "koble til/fra")]
        elif table is not None and table.id == "dev-table" and has_rows:
            pairs.append(("d", "fjern tilgang"))
        pairs += [*player, ("tab", "neste liste"), ("l", "lås skjermen"), ("esc", "til hjem")]
        return pairs

    def warm_zeus(self) -> None:
        """Forvarming (Zeus-fanen åpnet / du begynner å skrive): modellen lastes og den faste starten regnes ut nå,
        så svaret kommer på under et sekund i stedet for ~11. Høyst én gang i minuttet."""
        now = time.monotonic()
        if now - getattr(self, "_warmed_at", -1e9) < 60:
            return
        self._warmed_at = now

        async def go() -> None:
            try:
                await self.api.call("POST", "/zeus/warm")
            except Exception:  # noqa: BLE001 – bare en bonus
                pass
        self.run_worker(go(), group="warm", exclusive=True)

    def on_input_changed(self, e: Input.Changed) -> None:
        if e.input.id == "zeus-say" and e.value:
            self.warm_zeus()

    def action_tab(self, name: str) -> None:
        name, _, sub = name.partition(":")
        came_from = self.tab
        self.tab = name
        if sub:
            self.sub = sub
        self.query_one(ContentSwitcher).current = name
        if name == "zeus":
            if came_from != "zeus":
                self.gate.shown_again()  # godkjenninger som kom mens du var i en annen fane: ventetiden starter nå
            self.unseen = 0
            self.warm_zeus()
            self.query_one("#zeus-say", Input).focus()
            feed = self.query_one("#zeus-feed", VerticalScroll)
            feed.call_after_refresh(feed.scroll_end, animate=False)
        elif name == "hjem":
            self.query_one("#card-plan").focus()
        elif name == "pc":
            self.query_one("#pc-table").focus()
        else:
            self.query_one(f"#{name}-table").focus()
        self.render_chrome()
        self.render_player()
        self.run_worker(self.action_reload(), exclusive=True, group="reload")

    def action_back(self) -> None:
        """Esc: ut av skrivefeltet (zeus), ellers tilbake til hjem."""
        if self.tab == "zeus" and isinstance(self.focused, Input):
            self.query_one("#zeus-feed", VerticalScroll).focus()
            self.render_chrome()
        elif self.tab != "hjem":
            self.action_tab("hjem")

    def action_zeus_chat(self) -> None:
        self.app.pop_screen()

    def action_help(self) -> None:
        self.app.push_screen(InfoDialog("? Hjelp · Olympus Lab's", help_text()))

    async def call(self, method: str, path: str, body: dict | None = None) -> Any:
        """Handlinger (ikke innlasting): en feil fra serveren blir en melding, aldri et krasj."""
        try:
            return await self.api.call(method, path, body)
        except httpx.HTTPStatusError as e:
            try:
                why = e.response.json().get("error") or e.response.text
            except ValueError:
                why = e.response.text
            self.notify(f"Serveren sa nei: {str(why)[:160]}", severity="error", markup=False)
        except (httpx.HTTPError, OSError):
            self.ok = False
            self.render_chrome()
            self.notify("Fikk ikke kontakt med Olympus-serveren.", severity="error", markup=False)
        return None

    def own_log(self) -> list[dict[str, Any]]:
        """Bare endringer gjort her på PC-en – u skal ikke angre noe telefonen eller Zeus gjorde."""
        return [r for r in self.data.get("log") or [] if r.get("who") == "du"]

    # --- data --------------------------------------------------------------------------------------
    async def action_reload(self) -> None:
        try:
            loader = {"hjem": self.load_home, "zeus": self.load_zeus, "plan": self.load_plan, "media": self.load_media,
                      "pc": self.load_pc}[self.tab]
            self.set_player(await self.api.get("/pc"))
            await loader()
            self.ok, self.updated = True, dt.datetime.now().strftime("%H:%M")
        except (httpx.HTTPError, OSError):
            self.ok = False
        self.render_chrome()

    def show_empty(self, name: str, empty: bool, text: str) -> None:
        table = self.query_one(f"#{name}-table", DataTable)
        msg = self.query_one(f"#{name}-empty", Static)
        table.display = not empty
        msg.display = empty
        msg.update(Text(text, style=MUTED))

    async def load_zeus(self) -> None:
        st = await self.api.get("/status")
        self.ai = bool(st.get("ai", True))
        self.model, self.cloud = str(st.get("model") or ""), bool(st.get("cloud"))
        if not self.ai:
            self.zstate = "av"
        elif self.zstate == "av":
            self.zstate = "ledig"
        self.render_zeus()

    async def load_home(self) -> None:
        d = await self.api.get("/dashboard")
        today = dt.date.today()
        # I DAG
        t = Text()
        evs = d.get("events") or []
        if not evs:
            t.append("Ingen avtaler i dag.\n", style=MUTED)
        for e in evs:
            t.append(f"{(e['start'][11:16] or 'hele dagen'):<11}", style=MUTED)
            t.append(f"{e['title']}", style=GOLD if e.get("source") == "zeus" else "")
            t.append(" ✦\n" if e.get("source") == "zeus" else "\n", style=GOLD)
        nxt = [e for e in d.get("next_events", []) if e["start"][:10] > today.isoformat()]
        wn = max((len(fmt_when(e["start"])) for e in nxt[:3]), default=0)  # titlene i én kolonne
        for i, e in enumerate(nxt[:3]):
            t.append("Neste: " if i == 0 else "       ", style=DIM)
            t.append(f"{fmt_when(e['start']):<{wn}}  ", style=MUTED)
            t.append(f"{e['title']}\n")
        w = d.get("weather")
        self.data["weather"] = w
        if w:
            temp = f"{w['temp']:.0f}°" if isinstance(w.get("temp"), (int, float)) else "?"
            rain = f" · {w['rain']:.1f} mm" if isinstance(w.get("rain"), (int, float)) and w["rain"] > 0 else ""
            t.append(f"☁ {temp} {weather_word(w.get('symbol') or '')}{rain} · {w.get('place') or ''}", style=MUTED)
            t.append("   v bytt sted\n", style=DIM)
        else:
            t.append("☁ v velg sted for været\n", style=DIM)
        self.query_one("#card-plan", Card).update(t)
        # FORTSETT
        t = Text()
        cont = d.get("continue") or []
        if not cont:
            t.append("Ingenting påbegynt.\n", style=MUTED)
            t.append("Filmer og bøker du er midt i, havner her.\n", style=DIM)
        for c in cont[:4]:
            frac = c["position"] / c["duration"] if c.get("duration") else 0
            t.append(f"{'▶' if c['type'] == 'video' else '§'} {c['title'][:34]:<35}")
            t.append(f"{bar(frac)}\n", style=GOLD)
        self.query_one("#card-fortsett", Card).update(t)
        # PC-EN (kompakt)
        s = d.get("server") or {}
        b = d.get("backup") or {}
        t = Text()
        t.append("● ", style=OK)
        t.append(f"disk {s.get('disk_pct')} % · GPU {round(s.get('gpu') or 0)}° · CPU {round(s.get('cpu') or 0)}°\n")
        t.append(f"Zeus {'på' if s.get('ai') else 'av'}" + (f" · {self.model}" if self.model else "") + "\n", style=MUTED)
        if b:
            t.append(("backup i natt ✓" if b.get("ok") else f"backup feilet: {b.get('error', '')[:40]}") + "\n",
                     style=MUTED if b.get("ok") else BAD)
        self.query_one("#card-pc", Card).update(t)
        lib = d.get("library") or {}
        strip = Text("Bibliotek: ", style=MUTED)
        strip.append(" · ".join([plural(lib.get("tracks", 0), "sang", "sanger"), plural(lib.get("albums", 0), "album", "album"),
                                 plural(lib.get("films", 0), "film", "filmer"), plural(lib.get("episodes", 0), "episode", "episoder"),
                                 plural(lib.get("books", 0), "bok", "bøker"),
                                 plural(lib.get("playlists", 0), "spilleliste", "spillelister")]), style=MUTED)
        self.query_one("#home-strip", Static).update(strip)
        self.render_player()

    def on_data_table_row_highlighted(self, _e: DataTable.RowHighlighted) -> None:
        self.render_chrome()  # tastene avhenger av raden (utgang eller Bluetooth)

    def on_key(self, event: Any) -> None:
        if self.tab == "hjem" and event.key == "enter" and isinstance(self.focused, Card):
            self.action_tab(self.focused.target)
            event.stop()

    async def load_plan(self) -> None:
        today = dt.date.today()
        d = await self.api.get(f"/plan?from={today.isoformat()}&to={(today + dt.timedelta(days=31)).isoformat()}")
        evs = d["events"]
        log = await self.api.get("/log")
        self.data["plan"], self.data["log"] = evs, log
        self.query_one("#plan-panel").border_title = f"neste 31 dager · {plural(len(evs), 'avtale', 'avtaler')}"
        table = self.query_one("#plan-table", DataTable)
        cursor = table.cursor_row
        table.clear(columns=True)
        table.add_column("dag", width=12)
        table.add_column("tid", width=10)
        table.add_column("hva")
        table.add_column("", width=3)
        for e in evs:
            mark = Text.assemble(("✦", GOLD) if e["source"] == "zeus" else "", ("↻", MUTED) if e.get("series") else "")
            table.add_row(fmt_day(dt.date.fromisoformat(e["start"][:10]), today), e["start"][11:16] or "hele dagen", e["title"],
                          mark, key=e["id"])
        if evs:
            table.move_cursor(row=min(cursor, len(evs) - 1))
        self.show_empty("plan", not evs, "Ingen avtaler de neste 31 dagene.\n\nTrykk n for å legge inn en – eller be Zeus: «legg inn tannlege fredag 10».")
        legend = self.query_one("#plan-legend", Static)
        legend.display = any(e["source"] == "zeus" for e in evs)
        legend.update(Text.assemble(("✦", GOLD), ("  = lagt inn av Zeus", MUTED)))
        lt = Text(no_wrap=True, overflow="ellipsis")
        tw = min(30, max((len(r["title"]) for r in log[:6]), default=0))
        for row in log[:6]:
            ts = dt.datetime.fromtimestamp(row["ts"])
            lt.append(f"{fmt_day(ts.date(), today):<9}{ts:%H:%M}  ", style=MUTED)
            lt.append(f"{VERBS.get(row['action'], row['action']):<8}", style=GOLD if row["who"] == "zeus" else "")
            lt.append(f"{row['title'][:tw]:<{tw}}  ")
            lt.append(f"{'✦ Zeus' if row['who'] == 'zeus' else who_label(row['who'])}\n", style=DIM)
        self.query_one("#plan-log", Static).update(lt if log else Text("Ingen endringer ennå.", style=MUTED))

    # --- media: musikk · film · bøker i én fane -------------------------------------------------------
    def render_sub(self) -> None:
        t = Text(no_wrap=True, overflow="crop")
        for name, key, icon in SUBS:
            on = name == self.sub
            t.append(f" {key} ", style=f"bold {GOLD}" if not on else f"bold #0b0b0f on {GOLD}")
            t.append(f" {icon} {SUB_LABEL.get(name, name)} ", style=f"bold {GOLD} on #2a2418" if on else MUTED)
            t.append("   ")
        sub = {"musikk": "⏎ spilles på denne PC-en", "film": "spilles i Zeus-appen på telefonen",
               "boker": "leses og høres i Zeus-appen på telefonen"}[self.sub]
        self.query_one("#media-panel").border_subtitle = sub
        self.query_one("#media-sub", Static).update(t)

    async def action_sub(self, name: str) -> None:
        if self.tab != "media" or name == self.sub:
            return
        self.sub = name
        table = self.query_one("#media-table", DataTable)
        table.move_cursor(row=0)
        await self.action_reload()

    async def load_media(self) -> None:
        self.render_sub()
        await {"musikk": self.load_music, "film": self.load_films, "boker": self.load_books}[self.sub]()

    async def load_music(self) -> None:
        albums, lists = await self.api.get("/media/albums"), await self.api.get("/media/playlists")
        self.data["albums"], self.data["lists"] = albums, lists
        self.query_one("#media-panel").border_title = (f"♪ musikk · {plural(len(albums), 'album', 'album')} · "
                                                       f"{plural(len(lists), 'spilleliste', 'spillelister')}")
        pc = self.player.get("current") or {}
        table = self.query_one("#media-table", DataTable)
        cursor = table.cursor_row
        table.clear(columns=True)
        table.add_column("", width=2)
        table.add_column("album / spilleliste")
        table.add_column("artist")
        table.add_column("", width=2)
        table.add_column("spor", width=6)
        for p in lists:
            table.add_row(Text("≡", style=GOLD), p["name"], Text("spilleliste", style=MUTED), "", str(p["tracks"]),
                          key=f"p:{p['id']}")
        for a in albums:
            playing = pc and pc.get("album_id") == a["id"]
            table.add_row(Text("▶", style=GOLD) if playing else "♪", a["title"], a["artist"],
                          Text("♥", style=BAD) if a.get("like") == 1 else "", str(a["tracks"]), key=f"a:{a['id']}")
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1))
        self.show_empty("media", not (albums or lists),
                        "Ingen musikk ennå.\n\nLegg filer i ~/Musikk på denne PC-en – eller be Zeus finne noe: «finn Satie på Internet Archive».")

    async def load_films(self) -> None:
        v = await self.api.get("/media/videos")
        rows = [*v["films"], *(e for s in v["shows"] for e in s["episodes"])]
        self.data["videos"] = {x["id"]: x for x in rows}
        self.query_one("#media-panel").border_title = f"▶ film og serier · {len(rows)}"
        table = self.query_one("#media-table", DataTable)
        cursor = table.cursor_row
        table.clear(columns=True)
        table.add_column("", width=2)
        table.add_column("tittel")
        table.add_column("år / episode", width=16)
        table.add_column("lengde", width=8)
        table.add_column("kommet til", width=12)
        for f in rows:
            label = f["title"] if f["kind"] == "film" else f"{f['show']}  {f['title']}"
            sub = str(f["year"] or "") if f["kind"] == "film" else f"S{f['season'] or 0:02}E{f['episode'] or 0:02}"
            table.add_row("▶", label, sub, clock(f["duration"]),
                          Text(bar(f["position"] / f["duration"]), style=GOLD) if f["position"] > 5 and f["duration"] else "",
                          key=f["id"])
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1))
        self.show_empty("media", not rows, "Ingen filmer eller serier ennå.\n\nLegg filer i ~/Filmer og ~/Serier på denne PC-en – eller be Zeus: «finn Metropolis fra 1927».")

    async def load_books(self) -> None:
        books = await self.api.get("/media/books")
        self.data["books"] = {b["id"]: b for b in books}
        self.query_one("#media-panel").border_title = f"§ bøker og lydbøker · {len(books)}"
        table = self.query_one("#media-table", DataTable)
        cursor = table.cursor_row
        table.clear(columns=True)
        table.add_column("", width=2)
        table.add_column("tittel")
        table.add_column("forfatter")
        table.add_column("type", width=8)
        table.add_column("kommet til", width=14)
        for b in books:
            pos = ((Text(bar(b["position"] / b["duration"]), style=GOLD) if b["kind"] == "audio" and b["duration"]
                    else f"kapittel {int(b['position']) + 1}") if b["position"] else "")
            table.add_row("⏵" if b["kind"] == "audio" else "§", b["title"], b["author"], "lydbok" if b["kind"] == "audio" else b["format"],
                          pos, key=b["id"])
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1))
        self.show_empty("media", not books, "Ingen bøker ennå.\n\nE-bøker i ~/Bøker og lydbøker i ~/Lydbøker på denne PC-en – eller be Zeus: «finn Peer Gynt som lydbok».")

    # --- PC: avspilling, lyd, status, telefoner ------------------------------------------------------
    async def load_pc(self) -> None:
        a, info, dev = await asyncio.gather(self.api.get("/audio"), self.api.get("/pc/info"), self.api.get("/devices"))
        self.data["audio"], self.data["info"], self.data["devices"] = a, info, dev
        self.model, self.cloud = str((info.get("model") or {}).get("id") or self.model), bool((info.get("model") or {}).get("cloud"))
        table = self.query_one("#pc-table", DataTable)
        keep = table.cursor_row if table.row_count else None
        table.clear(columns=True)
        table.add_column("status", width=10)
        table.add_column("utgang / enhet")
        table.add_column("volum", width=16)
        default_row = 0
        for i, o in enumerate(a["outputs"]):
            on = o["default"]
            if on:
                default_row = i
            vol = (Text("dempet", style=BAD) if o["muted"] else
                   Text(f"{bar(o['volume'] / 100)} {o['volume']:>3} %") if on else Text(f"{'':10} {o['volume']:>3} %", style=DIM))
            table.add_row(Text("✓ i bruk", style=OK) if on else Text("○", style=DIM),
                          Text(short_sink(o["label"]), style="bold" if on else ""), vol, key=f"o:{o['name']}")
        for b in a["bluetooth"]:
            table.add_row(Text("✓ koblet", style=OK) if b["connected"] else Text("○ ᛒ", style=DIM), f"Bluetooth: {b['name']}",
                          "", key=f"b:{b['mac']}")
        if not a["bluetooth"]:
            table.add_row(Text("  ᛒ", style=DIM), Text("ingen Bluetooth – par i systemets innstillinger", style=DIM), "", key="none")
        if table.row_count:
            table.move_cursor(row=keep if keep is not None and keep < table.row_count else default_row)
        # status
        t = Text()
        g, cpu, m = info.get("gpu") or {}, info.get("cpu") or {}, info.get("model") or {}
        t.append(f"{info.get('host', 'PC')}  ", style="bold")
        t.append(f"disk {info.get('disk_pct')} %\n", style=MUTED)
        t.append(f"GPU {round(g.get('temp') or 0)}°", style="")
        if g.get("vram_total_gb"):
            t.append(f" · VRAM {g.get('vram_used_gb')}/{g.get('vram_total_gb')} GB", style=MUTED)
        t.append(f" · CPU {round(cpu.get('temp') or 0)}°\n")
        t.append("Zeus ", style=MUTED)
        t.append("på" if info.get("ai") else "av", style=OK if info.get("ai") else BAD)
        if m.get("cloud"):
            t.append(f" · ☁ {m.get('id')} (sky)\n", style=BAD)
        else:
            t.append(f" · {m.get('id') or '?'} · {'lastet' if m.get('loaded') else 'lastes ved behov'}\n", style=MUTED)
        svc = info.get("services") or {}
        for name, label in (("olympus-web", "server"), ("transmission", "nedlasting"), ("tailscaled", "Tailscale")):
            t.append("● " if svc.get(name) else "○ ", style=OK if svc.get(name) else BAD)
            t.append(f"{label}  ", style=MUTED)
        t.append("\n")
        b = info.get("backup") or {}
        if b:
            day = b.get("date", "")
            when = "i natt" if day == dt.date.today().isoformat() else (fmt_day(dt.date.fromisoformat(day)) if day else "")
            t.append(f"backup {when} ✓" if b.get("ok") else f"backup {when} feilet", style=MUTED if b.get("ok") else BAD)
        else:
            t.append("ingen backup ennå", style=DIM)
        self.query_one("#pc-info", Static).update(t)
        # telefoner
        dt_ = self.query_one("#dev-table", DataTable)
        dt_.clear(columns=True)
        dt_.add_column("enhet")
        dt_.add_column("sist sett", width=12)
        old = dt.datetime.now().timestamp() - 7 * 86400
        for d in dev:
            name = d["name"]
            name = f"emulator ({name[:12]}…)" if name.startswith("sdk_") else f"telefon ({name})"
            seen = d["last_seen"] or d["created"]
            dt_.add_row(name, Text(dt.datetime.fromtimestamp(seen).strftime("%d.%m %H:%M"), style=DIM if seen < old else ""),
                        key=d["id"])
        self.show_empty("dev", not dev, "Ingen telefoner er paret.\nKjør «olympus pair» og skann koden.")
        self.query_one("#pc-dev").border_title = f"telefoner med tilgang · {len(dev)}"

    # --- handlinger --------------------------------------------------------------------------------
    def selected(self) -> str | None:
        table = self.focused_table()
        if table is None:
            tables = self.query(f"#{self.tab}-table")
            table = tables.first(DataTable) if tables else None
        if table is None or table.row_count == 0 or not table.display:
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    async def action_key_n(self) -> None:
        """n: ny avtale i plan, neste sang ellers (der spilleren vises)."""
        if self.tab == "plan":
            await self.action_new()
        elif self.tab in ("hjem", "media", "pc"):
            await self.player_control("next")

    async def action_key_c(self) -> None:
        """c: cover i media, Bluetooth i PC."""
        if self.tab == "media":
            await self.action_cover()
        elif self.tab == "pc":
            await self.action_bluetooth()

    def media_item(self) -> str | None:
        """Id-en til elementet som er valgt i media (album, film/episode, bok) – ikke spillelister."""
        if self.tab != "media":
            return None
        key = self.selected()
        if not key:
            return None
        if self.sub == "musikk":
            kind, _, ident = key.partition(":")
            return ident if kind == "a" and ident else None
        return key

    async def action_add_media(self) -> None:
        if self.tab != "media":
            return

        async def done(d: dict | None) -> None:
            if d:
                self.notify(f"↓ Startet «{d.get('title', '')}» – D viser framdriften.", markup=False)
                items = await self.call("GET", "/downloads")
                if isinstance(items, list):
                    self.set_downloads(items)

        self.app.push_screen(AddDialog(self.call), done)

    async def action_downloads(self) -> None:
        if self.tab != "media":
            return
        self.app.push_screen(DownloadsScreen(self))

    async def action_edit_item(self) -> None:
        item = self.media_item()
        if item is None:
            return
        info = await self.call("GET", f"/media/item/{item}")
        if not info:
            return

        async def done(fields: dict | None) -> None:
            if not fields:
                return
            r = await self.call("PATCH", f"/media/item/{item}", fields)
            if r is not None:
                self.notify(f"Lagret «{r.get('title', '')}».", markup=False)
                await self.action_reload()

        self.app.push_screen(EditDialog(info), done)

    async def action_cover(self) -> None:
        item = self.media_item()
        if item is None:
            return
        if self.sub == "boker" and (self.data.get("books", {}).get(item) or {}).get("kind") == "ebook":
            self.notify("E-bøker har ikke eget cover.", markup=False)
            return
        row = self.query_one("#media-table", DataTable).get_row_at(self.query_one("#media-table", DataTable).cursor_row)
        what = str(row[1]) if len(row) > 1 else ""

        async def done(res: tuple[str, str] | None) -> None:
            if not res:
                return
            if res[0] == "url":
                r = await self.call("PUT", f"/media/item/{item}/cover", {"url": res[1]})
                if r is not None:
                    self.notify("Nytt cover lagret.", markup=False)
            else:
                self.notify("Leter etter cover …", markup=False)
                r = await self.call("POST", f"/media/item/{item}/cover/fetch")
                if r is not None:
                    self.notify("Fant et nytt cover." if r.get("found") else "Fant ikke noe cover på nettet.", markup=False)
            await self.action_reload()

        self.app.push_screen(CoverDialog(what), done)

    async def action_delete_item(self) -> None:
        item = self.media_item()
        if item is None:
            return
        info = await self.call("GET", f"/media/item/{item}")
        if not info:
            return
        what = {"album": "albumet", "track": "sangen", "video": "filmen/episoden", "book": "boka"}.get(str(info.get("type")), "")
        text = (f"Flytte {what} «{info.get('title')}» til papirkurven?\n{info.get('files')} filer · "
                f"{human_size(info.get('size'))} · {info.get('folder')}\n\nKan hentes tilbake fra papirkurven.")

        async def done(yes: bool | None) -> None:
            if yes:
                r = await self.call("DELETE", f"/media/item/{item}")
                if r is not None:
                    self.notify(f"Slettet {r.get('what', '')} – {r.get('trashed')} filer i papirkurven.", markup=False)
                    await self.action_reload()

        self.app.push_screen(ConfirmDialog("✗ Slette?", text), done)

    async def action_scan(self) -> None:
        if self.tab != "media":
            return
        self.notify("Skanner biblioteket …", markup=False)
        r = await self.call("POST", "/media/scan")
        if r is not None:
            self.notify("Skannet: " + ", ".join(f"{k} {v}" for k, v in r.items()), markup=False)
            await self.action_reload()

    async def action_key_m(self) -> None:
        """m: musikk i media, demp i PC."""
        if self.tab == "media":
            await self.action_sub("musikk")
        elif self.tab == "pc":
            await self.action_mute()

    async def action_new(self) -> None:
        async def done(res: tuple[str, str, str] | None) -> None:
            if res:
                body = {"title": res[0], "start": res[1], **({"repeat": res[2]} if res[2] else {})}
                if (r := await self.call("POST", "/plan", body)) is None:
                    return
                n = r.get("series_count", 1) if isinstance(r, dict) else 1
                self.notify(f"La til «{res[0]}» {fmt_when(res[1])}" + (f" og {n - 1} til (↻)." if n > 1 else "."), markup=False)
                await self.action_reload()

        self.app.push_screen(EventDialog(), done)

    async def player_control(self, action: str) -> None:
        if not self.player.get("current"):
            if action != "next":
                self.notify("Ingenting spilles på PC-en.", markup=False)
            return
        r = await self.call("POST", "/pc/control", {"action": action})
        if r is not None:
            self.set_player(r)

    async def action_prev(self) -> None:
        if self.tab in ("hjem", "media", "pc"):
            await self.player_control("prev")

    async def action_pause(self) -> None:
        if self.tab in ("hjem", "media", "pc") and self.player.get("current"):
            await self.player_control("resume" if self.player.get("paused") else "pause")

    async def action_stop(self) -> None:
        if self.tab in ("hjem", "media", "pc"):
            if self.player.get("current"):
                await self.player_control("stop")
                self.notify("Stoppet.", markup=False)

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = str(event.row_key.value)
        if self.tab == "plan":
            ev = next((e for e in self.data.get("plan", []) if e["id"] == key), None)
            if ev is None:
                return
            when = f"{ev['start'][8:10].lstrip('0')}.{ev['start'][5:7].lstrip('0')}" + (f" {ev['start'][11:16]}" if len(ev["start"]) > 10 else "")

            async def done(res: tuple[str, str, str] | None) -> None:
                if res:
                    if await self.call("PATCH", f"/plan/{key}", {"title": res[0], "start": res[1]}) is None:
                        return
                    self.notify(f"Endret «{res[0]}».", markup=False)
                    await self.action_reload()

            self.app.push_screen(EventDialog(ev["title"], when, "▦ Endre avtale"), done)
        elif self.tab == "media" and self.sub == "musikk":
            kind, _, ident = key.partition(":")
            r = await self.call("POST", "/pc/play", {"playlist": ident} if kind == "p" else {"album": ident})
            if r is not None:
                self.set_player(r)
                await self.action_reload()
        elif self.tab == "media" and self.sub == "film":
            v = self.data.get("videos", {}).get(key)
            if v:
                t = Text()
                t.append(f"{v['title']}\n", style=f"bold {GOLD}")
                t.append(f"{v.get('year') or ''}  ·  {clock(v['duration'])}  ·  {'spilles direkte' if v.get('direct') else 'omkodes ved avspilling'}\n\n",
                         style=MUTED)
                if v["position"] > 5 and v["duration"]:
                    t.append(f"Kommet til {clock(v['position'])}  {bar(v['position'] / v['duration'])}\n\n", style=GOLD)
                t.append("Spill av i Zeus-appen på telefonen (fanen ♪ media › film).", style=MUTED)
                self.app.push_screen(InfoDialog("▶ Film", t))
        elif self.tab == "media" and self.sub == "boker":
            b = self.data.get("books", {}).get(key)
            if b:
                t = Text()
                t.append(f"{b['title']}\n", style=f"bold {GOLD}")
                t.append(f"{b['author']}  ·  {'lydbok, ' + clock(b['duration']) if b['kind'] == 'audio' else 'e-bok (' + b['format'] + ')'}\n\n",
                         style=MUTED)
                t.append("Les eller hør i Zeus-appen på telefonen (fanen ♪ media › bøker) – eller be Zeus oppsummere den.", style=MUTED)
                self.app.push_screen(InfoDialog("§ Bok", t))
        elif self.tab == "pc" and key.startswith("o:"):
            await self.call("POST", "/audio/output", {"sink": key[2:]})
            await self.action_reload()
        elif self.tab == "pc" and key.startswith("b:"):
            await self.action_bluetooth()

    async def action_delete(self) -> None:
        key = self.selected()
        if key is None:
            return
        if self.tab == "plan":
            ev = next((e for e in self.data.get("plan", []) if e["id"] == key), None)

            async def remove(rest: bool | None) -> None:
                if rest is None:
                    return
                if await self.call("DELETE", f"/plan/{key}" + ("?series=rest" if rest else "")) is None:
                    return
                what = f"resten av «{ev['title']}»" if rest and ev else f"«{ev['title'] if ev else ''}»"
                self.notify(f"Slettet {what} – u angrer.", markup=False)
                await self.action_reload()

            if ev and ev.get("series"):
                self.app.push_screen(SeriesDeleteDialog(ev["title"]), remove)
            else:
                await remove(False)
        elif self.tab == "pc" and (t := self.focused_table()) is not None and t.id == "dev-table":
            name = str(t.get_row(key)[0])

            async def done(yes: bool | None) -> None:
                if yes:
                    if await self.call("DELETE", f"/devices/{key}") is None:
                        return
                    self.notify(f"{name} er fjernet og må pares på nytt (olympus pair).", markup=False)
                    await self.action_reload()

            self.app.push_screen(ConfirmDialog("▣ Fjerne enhet?", f"Fjerne {name}?\nDen mister tilgangen med en gang og må pares på nytt."), done)

    async def action_place(self) -> None:
        if self.tab != "hjem":
            return

        async def done(place: str | None) -> None:
            if place:
                r = await self.call("PUT", "/weather/place", {"place": place})
                if r is not None:
                    self.notify(f"Været gjelder nå {(r.get('weather') or {}).get('place') or place}.", markup=False)
                    await self.action_reload()

        self.app.push_screen(PlaceDialog((self.data.get("weather") or {}).get("place") or ""), done)

    async def action_undo(self) -> None:
        if self.tab == "zeus" and getattr(self, "last_undo", None):  # Zeus' siste endring (gjort uten kort)
            seq, text = self.last_undo
            self.last_undo = None
            if await self.call("POST", f"/undo/{seq}") is not None:
                self.notify(f"Angret: {text}", markup=False)
                self.log_line("  ↶", f"angret: {text}", DIM)
            return
        if self.tab != "plan":
            return
        log = self.own_log()
        if log:
            if await self.call("POST", f"/undo/{log[0]['seq']}") is None:
                return
            self.notify(f"Angret: {VERBS.get(log[0]['action'], log[0]['action'])} «{log[0]['title']}».", markup=False)
            await self.action_reload()

    async def action_lock(self) -> None:
        if self.tab != "pc":
            return

        async def done(yes: bool | None) -> None:
            if yes and await self.call("POST", "/pc/action", {"action": "lock"}) is not None:
                self.notify("Skjermen er låst.", markup=False)

        self.app.push_screen(ConfirmDialog("◈ Låse skjermen?", "Låse skjermen på denne PC-en nå?"), done)

    def _selected_output(self) -> dict[str, Any] | None:
        key = self.selected() or ""
        outs = (self.data.get("audio") or {}).get("outputs", [])
        return next((o for o in outs if f"o:{o['name']}" == key), None) or next((o for o in outs if o["default"]), None)

    async def action_volume(self, delta: int) -> None:
        if self.tab != "pc" or (o := self._selected_output()) is None:
            return
        await self.call("POST", "/audio/volume", {"percent": max(0, min(100, o["volume"] + delta)), "sink": o["name"]})
        await self.action_reload()

    async def action_mute(self) -> None:
        if self.tab != "pc" or (o := self._selected_output()) is None:
            return
        await self.call("POST", "/audio/mute", {"muted": not o["muted"], "sink": o["name"]})
        await self.action_reload()

    async def action_bluetooth(self) -> None:
        if self.tab != "pc":
            return
        key = self.selected() or ""
        if not key.startswith("b:"):
            self.notify("Velg en Bluetooth-enhet i lista først.", severity="warning", markup=False)
            return
        bt = next((b for b in self.data["audio"]["bluetooth"] if b["mac"] == key[2:]), None)
        self.notify("Kobler …", markup=False)
        r = await self.call("POST", "/audio/bluetooth", {"mac": key[2:], "connect": not (bt or {}).get("connected")})
        if r is None:
            return
        self.notify("Ferdig." if r.get("ok") else "Fikk ikke koblet – er enheten på og i nærheten?",
                    severity="information" if r.get("ok") else "warning", markup=False)
        await self.action_reload()

