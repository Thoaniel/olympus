"""Systemprompt og prosjektkontekst."""

from __future__ import annotations

import os
import platform
from datetime import datetime
from pathlib import Path

from olympus.config import Config

PROJECT_FILES = ("OLYMPUS.md", "AGENTS.md")
PROJECT_CONTEXT_LIMIT = 8000  # tegn


def project_context(cwd: Path) -> str | None:
    for name in PROJECT_FILES:
        p = cwd / name
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
            if len(text) > PROJECT_CONTEXT_LIMIT:
                text = text[:PROJECT_CONTEXT_LIMIT] + "\n…(avkortet)"
            return f"## Prosjektkontekst fra {name}\n\n{text}"
    return None


def identity(config: Config, agent: str) -> str:
    """Hvem assistenten er, så den svarer med riktig navn (og vet at hjernen er på PC-en uansett modell)."""
    name = config.agent(agent).get("display", agent)
    return (f"## Hvem du er\nDu heter {name} og er brukerens personlige assistent i Olympus. Spør brukeren hvem du er "
            f"eller hva du heter, svarer du at du er {name}, ikke «Olympus» og ikke navnet på språkmodellen.")


DAYS = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]


def today_line(now: datetime | None = None) -> str:
    """«2026-10-10 (lørdag, uke 41)» + de neste 7 dagene – modellen regner ellers feil på ukedager."""
    from datetime import timedelta

    now = now or datetime.now()
    tomorrow = now + timedelta(days=1)
    week = [f"{DAYS[(now + timedelta(days=i)).weekday()]} {(now + timedelta(days=i)):%d.%m}" for i in range(2, 8)]
    return (f"I dag er {DAYS[now.weekday()]} {now:%d.%m.%Y} (uke {now.isocalendar().week}). "
            f"I morgen er {DAYS[tomorrow.weekday()]} {tomorrow:%d.%m}. Deretter: {', '.join(week)}")


def system_prompt(config: Config, cwd: Path, agent: str | None = None) -> str:
    """SOUL.md + Zeus' egen prompt + miljø + prosjektkontekst."""
    parts = [config.soul().strip()]
    if agent:
        role = config.agent_prompt(agent).strip()
        if role:
            parts.append(role)
        parts.append(identity(config, agent))
    parts.append(
        "## Miljø\n"
        f"- Dato: {today_line()}\n"  # bare dato: klokkeslett endret prompten hvert minutt (ødela prefiks-cachen)
        f"- Arbeidsmappe: {cwd}\n"
        f"- Bruker: {os.environ.get('USER', '?')} på {platform.node()}"
    )
    if config.get("general.read_project_context", True):
        ctx = project_context(cwd)
        if ctx:
            parts.append(ctx)
    return "\n\n".join(parts)
