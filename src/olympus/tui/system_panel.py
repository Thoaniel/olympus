"""Sidepanel med systemstatus: temperaturer, termovakt, VRAM per modell, og kontekst/kostnad for økta.

Vises når terminalen er minst MIN_WIDTH kolonner bred (skjules automatisk under det) og kan slås av/på med
F4. Ren tekst, oppdateres hvert annet sekund og når et svar er ferdig. Hver tilstand vises én gang: tall
og en kort merkelapp, med farge som forsterkning (aldri farge alene).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static

from olympus.gpu import read_vram
from olympus.thermal import NAMES, Thermal, read
from olympus.tui.navigation import click, cmd_action

if TYPE_CHECKING:
    from olympus.agents.orchestrator import Orchestrator
    from olympus.config import Config
    from olympus.model_manager import ModelManager

MIN_WIDTH = 120  # under dette tar panelet for mye plass fra samtalen, og det skjules


def bar(frac: float, width: int) -> str:
    """Enkel stolpe med blokktegn, f.eks. ▕████░░░░▏."""
    width = max(4, width)
    n = round(max(0.0, min(1.0, frac)) * width)
    return "▕" + "█" * n + "░" * (width - n) + "▏"


def fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


SECTIONS = ("maskin", "vram", "fart", "kontekst", "kostnad")  # rekkefølgen i panelet (og tastene 1–5)


class SystemPanel(VerticalScroll):
    """Systemstatus for Olympus: maskin, VRAM, fart, kontekst og kostnad.

    Hver seksjon kan legges sammen (klikk på overskriften, eller 1–5 når panelet har fokus); sammenlagt viser den
    et sammendrag på én linje. Hele panelet legges sammen til en smal skinne med » øverst eller F4."""

    BINDINGS: ClassVar = [Binding(str(i + 1), f"toggle_section({i})", show=False) for i in range(len(SECTIONS))]

    def __init__(self, config: Config, manager: ModelManager, orch: Orchestrator,
                 collapsed: set[str] | None = None, **kw) -> None:
        super().__init__(**kw)
        self.config, self.manager, self.orch = config, manager, orch
        self.thermal = Thermal(config)
        self.collapsed: set[str] = set(collapsed or ())

    def compose(self):
        yield Static(id="sys-body")

    def on_mount(self) -> None:
        self.set_interval(2.0, self.refresh_panel)
        self.refresh_panel()

    def toggle(self, key: str) -> None:
        self.collapsed ^= {key}
        self.refresh_panel()
        saver = getattr(self.app, "save_ui_state", None)
        if saver:
            saver()

    def action_toggle_section(self, i: int) -> None:
        if 0 <= i < len(SECTIONS):
            self.toggle(SECTIONS[i])

    def refresh_panel(self) -> None:
        if not self.display:
            return
        ui = self.config.theme["ui"]
        muted, accent, err, ok = ui["muted"], ui["accent"], ui["error"], ui.get("success", "green")
        inner = max(12, self.size.width - 3)
        sections: list[tuple[str, str, Text, Text]] = []  # (nøkkel, navn, sammendrag, innhold)

        def row(t: Text, label: str, value: str, style: str = "") -> None:
            t.append(f"{label:<11}", style=muted)
            t.append(f"{value}\n", style=style)

        # --- maskin ---
        t, r = Text(), read()
        limits = self.thermal.limits()
        for key in ("gpu_junction", "gpu_mem", "cpu", "nvme"):
            if key in r.temps:
                v = r.temps[key]
                lim = limits.get(key)
                if key == "nvme" and not (lim and v >= lim - 10):
                    continue                                   # NVMe bare når den nærmer seg grensen
                style = err if lim and v >= lim else accent if lim and v >= lim - 10 else ""
                t.append(f"{NAMES.get(key, key):<11}", style=muted)
                t.append(f"{v:.0f}°", style=style)
                t.append(f" / {lim:.0f}°\n" if lim else "\n", style=muted)  # grensen termovakten bruker
        if r.gpu_watts is not None and r.gpu_watts > 200:
            row(t, "GPU-effekt", f"{r.gpu_watts:.0f} W", accent)  # bare når kortet jobber hardt
        hot = self.thermal.too_hot(r)
        row(t, "Termovakt", "kjøler ned" if hot else "ok", f"bold {err}" if hot else ok)
        gpu = r.temps.get("gpu_junction")
        summ = Text(f"{gpu:.0f}° " if gpu is not None else "", style="")
        summ.append("kjøler ned" if hot else "ok", style=f"bold {err}" if hot else ok)
        sections.append(("maskin", "MASKIN", summ, t))

        # --- vram ---
        t = Text()
        vram = read_vram()
        if vram:
            t.append(f"{vram.used_gb:.1f} / {vram.total_gb:.1f} GiB\n")
            frac = vram.used_gb / vram.total_gb if vram.total_gb else 0
            t.append(bar(frac, inner - 2) + "\n", style=err if frac > 0.92 else accent if frac > 0.8 else muted)
        else:
            t.append("ukjent\n", style=muted)
        warm = set(self.config.get("vram.keep_warm", []))
        usage = self.manager.usage()
        if usage:
            for model, size, measured in usage:
                # 0 GiB = serveren eies av en annen Olympus-instans og deles; vis anslått størrelse og «delt».
                shared = size <= 0
                if shared:
                    size, measured = self.manager.footprint_gb(model), False
                label = f"{model} (delt)" if shared else model
                name = label if len(label) <= inner - 8 else label[: inner - 9] + "…"
                t.append(f" {name:<{inner - 7}}", style=click(cmd_action(f"/unload {model}")))
                t.append(f"{size:4.1f}{'' if measured else '~'}", style=muted)
                t.append(" ♨\n" if model in warm else "\n", style=accent)
        else:
            t.append(" ingen modell lastet\n", style=muted)
        loading = getattr(self.app, "loading", None)
        if loading:
            t.append(f" laster {loading} …\n", style=accent)
        summ = Text(f"{vram.used_gb:.1f}/{vram.total_gb:.1f} GiB" if vram else "ukjent")
        sections.append(("vram", "VRAM", summ, t))

        # --- fart (den viktigste målingen for lokale modeller); vises når det finnes et tall ---
        stats = getattr(self.app, "gen_stats", {}) or {}
        if stats.get("tps"):
            t = Text()
            live = stats.get("live")
            t.append(f"{stats['tps']:.0f} tok/s", style=f"bold {accent}" if live else "")
            t.append("  nå\n" if live else "  siste svar\n", style=muted)
            if stats.get("ttft") is not None:
                t.append(f"{stats['ttft']:.1f} s", style="")
                t.append(" til første token\n", style=muted)
            sections.append(("fart", "FART", Text(f"{stats['tps']:.0f} tok/s", style=f"bold {accent}" if live else ""), t))

        # --- kontekst ---
        t = Text()
        used, ctx = self.orch.context_used()
        if ctx:
            frac = used / ctx
            t.append(f"{fmt_tokens(used)} / {fmt_tokens(ctx)}  {frac * 100:.0f} %\n")
            t.append(bar(frac, inner - 2) + "\n", style=err if frac > 0.9 else accent if frac > 0.7 else muted)
            summ = Text(f"{frac * 100:.0f} %", style=err if frac > 0.9 else accent if frac > 0.7 else "")
        else:
            t.append(fmt_tokens(used) + "\n")
            summ = Text(fmt_tokens(used))
        mem = getattr(self.orch, "memory", None)
        if mem is not None:
            parts = [f"{label} {mem.size(tg) * 100 // max(1, mem.limits[tg])} %" for tg, label in (("user", "deg"), ("memory", "notater"))]
            t.append(f"{'Minne':<11}", style=muted)
            t.append(" · ".join(parts), style=click(cmd_action("/minne")))
            t.append("\n")
        n = getattr(self.orch, "compressions", 0)
        if n:
            t.append(f"{'Komprimert':<11}", style=muted)
            t.append(f"{n}×\n")
        sections.append(("kontekst", "KONTEKST", summ, t))

        # --- kostnad ---
        t = Text()
        last = getattr(self.orch, "last_model", None)
        summ = Text("–", style=muted)
        if last:
            cost = self.orch.backends.cost(last)
            where = "sky" if self.orch.backends.is_cloud(last) else "lokal"
            style = err if cost == "betalt" else accent if cost == "abonnement" else ok
            t.append(f"{'Siste svar':<11}", style=muted)
            t.append(where)
            t.append(" · ", style=muted)
            t.append(f"{cost}\n", style=style)
            summ = Text(cost, style=style)
        else:
            t.append(" ingen svar ennå\n", style=muted)
        tally = getattr(self.orch, "session_tokens", {})
        if set(tally) - {"gratis"}:  # bare lokalt/gratis? da sier KONTEKST og «gratis» over alt som trengs
            t.append("\nØkta\n", style=muted)
            for kind in ("gratis", "gratisnivå", "abonnement", "betalt"):
                if tally.get(kind):
                    t.append(f" {kind:<11}{fmt_tokens(tally[kind]):>7} tokens\n", style=err if kind == "betalt" else "")
        sections.append(("kostnad", "KOSTNAD", summ, t))

        # --- sett sammen: overskrift (klikk = fold), innhold eller sammendrag ---
        out = Text()
        for i, (key, name, summary, body) in enumerate(sections):
            folded = key in self.collapsed
            extra = 2 if i == 0 else 0  # plass til » på første linje
            toggle = click(f"app.toggle_section({key!r})", f"bold {accent}")
            out.append(name, style=toggle)
            if folded:
                out.append(" ")
                out.append_text(summary)
                pad = inner - len(name) - 1 - summary.cell_len - 2 - extra
                out.append(" " + "─" * max(1, pad), style=muted)
                out.append(" ▸", style=click(f"app.toggle_section({key!r})", muted))
            else:
                out.append(" " + "─" * max(1, inner - len(name) - 3 - extra), style=muted)
                out.append(" ▾", style=click(f"app.toggle_section({key!r})", muted))
            if i == 0:  # hele panelet legges sammen med » helt øverst til høyre
                out.append(" »", style=click("app.toggle_panel", f"bold {accent}"))
            out.append("\n")
            if not folded:
                out.append_text(body)
                out.append("\n")
        self.query_one("#sys-body", Static).update(out)
