"""`olympus status`: et levende statusvindu for workspace 2 – tjenestene, Zeus, maskinen, dagens plan,
nedlastinger, telefoner og siste endringer. Leser bare fra Olympus-serveren (samme API som appen) hvert 5. sekund."""

from __future__ import annotations

import asyncio
import datetime as dt
import shutil
import subprocess
from typing import Any

import httpx
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

GOLD, MUTED, OK, BAD, LINE = "#d4a73a", "#8a8696", "#8fbf6f", "#e0605e", "#34344a"
SERVICES = [("olympus-web", "server"), ("transmission", "nedlasting"), ("tailscaled", "Tailscale")]


def _active(unit: str) -> bool:
    from olympus.sysutil import service_active

    return service_active(unit)


def _ago(ts: float | None) -> str:
    if not ts:
        return "aldri"
    s = int(dt.datetime.now().timestamp() - ts)
    if s < 90:
        return "nå"
    if s < 3600:
        return f"{s // 60} min siden"
    if s < 86400:
        return f"{s // 3600} t siden"
    return f"{s // 86400} d siden"


def _meter(pct: float, width: int = 20) -> Text:
    n = round(max(0.0, min(100.0, pct)) / 100 * width)
    color = BAD if pct >= 90 else GOLD
    return Text("▮" * n, style=color) + Text("▯" * (width - n), style=LINE)


def _panel(title: str, body: Any) -> Panel:
    if isinstance(body, Text):
        body.rstrip()  # ingen tom linje nederst i rammen
    return Panel(body, title=Text(title, style=f"bold {GOLD}"), title_align="left", border_style=LINE, padding=(0, 1))


async def _fetch(c: httpx.AsyncClient, path: str) -> Any:
    try:
        r = await c.get(path)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


def render(d: dict[str, Any], services: dict[str, bool]) -> Group:
    now = dt.datetime.now()
    st, dash = d.get("status"), d.get("dashboard") or {}
    head = Text("▲ OLYMPUS · STATUS", style=f"bold {GOLD}")
    head.append(f"   {now:%H:%M:%S}", style=MUTED)
    if st is None:
        head.append("   ● ingen kontakt med serveren", style=BAD)

    svc = Text()
    for unit, label in SERVICES:
        up = services.get(unit, False)
        svc.append("● " if up else "○ ", style=OK if up else BAD)
        svc.append(f"{label}   ", style="" if up else BAD)

    zeus = Text()
    if st:
        zeus.append("● " if st.get("ai") else "○ ", style=OK if st.get("ai") else MUTED)
        zeus.append(f"Zeus {'på' if st.get('ai') else 'av'} · svarer med ")
        zeus.append(f"☁ {st.get('model')} · sky – samtalen sendes ut\n" if st.get("cloud") else f"{st.get('model')}\n",
                    style=GOLD if st.get("cloud") else "")
        models = ", ".join(st.get("models") or []) or "ingen lastet (lastes ved behov)"
        zeus.append(f"i GPU-en: {models}\n", style=MUTED)
        zeus.append("svarer nå …" if st.get("busy") else "ledig", style=GOLD if st.get("busy") else MUTED)

    s = dash.get("server") or {}
    machine = Table.grid(padding=(0, 1))
    machine.add_column(style=MUTED, width=6)
    machine.add_column()
    machine.add_column(style=MUTED)
    gpu, cpu = s.get("gpu"), s.get("cpu")
    if gpu is not None:
        machine.add_row("GPU", _meter(gpu), f"{gpu:.0f}°")
    if cpu is not None:
        machine.add_row("CPU", _meter(cpu), f"{cpu:.0f}°")
    if s.get("disk_pct") is not None:
        machine.add_row("disk", _meter(s["disk_pct"]), f"{s['disk_pct']} %")
    b = dash.get("backup") or {}
    if b:
        machine.add_row("backup", Text("✓ " + str(b.get("date", "")) if b.get("ok") else "✗ feilet",
                                      style=OK if b.get("ok") else BAD), "")

    plan = Text()
    for e in (dash.get("events") or [])[:6]:
        plan.append(f"{e['start'][11:16] or 'hele dagen':<11}", style=MUTED)
        plan.append(f"{e['title']}{' ϟ' if e.get('source') == 'zeus' else ''}\n")
    nxt = [e for e in dash.get("next_events") or [] if e["start"][:10] > now.date().isoformat()]
    if not plan:
        plan.append("ingenting i dag\n", style=MUTED)
    if nxt:
        plan.append(f"neste: {nxt[0]['start'][:10]} {nxt[0]['start'][11:16]} {nxt[0]['title']}", style=MUTED)

    dl = Text()
    for x in (d.get("downloads") or [])[:5]:
        dl.append(f"{x.get('title', '?')[:34]:<35}", style="")
        dl.append(f"{x.get('status', '')}  {round(100 * float(x.get('progress') or 0))} %\n", style=MUTED)
    if not dl:
        dl.append("ingen", style=MUTED)

    dev = Text()
    for x in d.get("devices") or []:
        name = x.get("name", "?")
        dev.append("● " if (x.get("last_seen") or 0) > now.timestamp() - 600 else "○ ",
                   style=OK if (x.get("last_seen") or 0) > now.timestamp() - 600 else MUTED)
        dev.append(f"{'emulator' if name.startswith('sdk_') else 'telefon'} ({name[:10]})  ")
        dev.append(_ago(x.get("last_seen")) + "\n", style=MUTED)
    if not dev:
        dev.append("ingen telefoner paret – kjør olympus pair", style=MUTED)

    log = Text()
    verbs = {"add": "la til", "update": "endret", "delete": "slettet", "undo": "angret"}
    for row in (d.get("log") or [])[:4]:
        log.append(f"{dt.datetime.fromtimestamp(row['ts']):%H:%M}  ", style=MUTED)
        log.append(f"{verbs.get(row['action'], row['action']):<8}", style=GOLD if row["who"] == "zeus" else "")
        log.append(f"{row['title'][:30]}  ")
        log.append(("ϟ Zeus" if row["who"] == "zeus" else row["who"]) + "\n", style=MUTED)
    if not log:
        log.append("ingen endringer ennå", style=MUTED)

    # To kolonner: vinduet på workspace 2 er bare en kvart skjerm høyt.
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_row(Group(_panel("◈ TJENESTER", svc), _panel("ϟ ZEUS", zeus), _panel("▣ MASKINEN", machine)),
                 Group(_panel("▦ I DAG", plan), _panel("↓ NEDLASTINGER", dl), _panel("⚿ TELEFONER", dev)))
    return Group(head, grid, _panel("≡ SISTE ENDRINGER", log))


async def watch(base: str, token: str, every: float = 5.0) -> None:
    async with httpx.AsyncClient(base_url=base.rstrip("/") + "/api/v1", timeout=4, cookies={"olympus_web": token}) as c:
        with Live(Text("henter …", style=MUTED), refresh_per_second=2, screen=True) as live:
            while True:
                keys = ("status", "dashboard", "downloads", "devices", "log")
                vals = await asyncio.gather(*(_fetch(c, "/" + k) for k in keys))
                services = {u: await asyncio.to_thread(_active, u) for u, _ in SERVICES}
                live.update(render(dict(zip(keys, vals, strict=True)), services))
                await asyncio.sleep(every)


def run(config: Any) -> int:
    from olympus.web.server import get_token

    port = int(config.get("web.port", 8787))
    try:
        asyncio.run(watch(f"http://127.0.0.1:{port}", get_token(config)))
    except KeyboardInterrupt:
        pass
    return 0
