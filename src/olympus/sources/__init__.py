"""Søkekilder for Zeus: hvor han leter etter musikk, film, serier og bøker å laste ned.

Hver kilde er én fil i denne mappen med
    NAME = "visningsnavn"
    KINDS = {"music", "film", "show", "audiobook", "ebook"}   # hva kilden kan ha
    async def search(client: httpx.AsyncClient, query: str, kind: str | None) -> list[Result]
Kildene listes i SOURCES under; `search_all` spør alle samtidig og samler svarene. Se README.md.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict, dataclass, field
from importlib import import_module
from typing import Any

import httpx

KINDS = ("music", "film", "show", "audiobook", "ebook")

# Rekkefølgen her er rekkefølgen i svaret. Legg til egne kilder ved å lage en fil og skrive navnet her.
SOURCES = ["archive", "librivox", "gutenberg", "commons", "jamendo"]


@dataclass
class Result:
    title: str
    kind: str                      # music | film | show | audiobook | ebook
    source: str                    # kildens NAME
    page: str = ""                 # side å vise brukeren (for å se hva det er)
    torrent: str = ""              # .torrent-URL eller magnet:-lenke (hentes med Transmission)
    direct: str = ""               # direkte nedlastingslenke (hentes uten torrent)
    size: int = 0                  # byte, 0 = ukjent
    year: int | None = None
    creator: str = ""
    license: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.source}|{self.torrent or self.direct or self.page}".encode()).hexdigest()[:10]

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "id": self.id}


def _load(name: str):
    return import_module(f"olympus.sources.{name}")


async def search_all(query: str, kind: str | None = None, sources: list[str] | None = None,
                     limit: int = 8, timeout: float = 15) -> tuple[list[Result], list[str]]:
    """Spør alle kildene samtidig. Returnerer (treff, feil per kilde). En treg eller nede kilde stopper ingen andre."""
    mods = []
    errors: list[str] = []
    for name in sources or SOURCES:
        try:
            mod = _load(name)
        except ImportError as e:
            errors.append(f"{name}: {e}")
            continue
        if kind is None or kind in getattr(mod, "KINDS", KINDS):
            mods.append(mod)
    headers = {"User-Agent": "OlympusLab/0.3 (personlig mediabibliotek)"}
    async with httpx.AsyncClient(timeout=max([timeout, *(getattr(m, "TIMEOUT", 0) for m in mods)]), headers=headers,
                                 follow_redirects=True) as client:
        results = await asyncio.gather(
            *(asyncio.wait_for(m.search(client, query, kind), getattr(m, "TIMEOUT", timeout)) for m in mods),
            return_exceptions=True)
    hits: list[Result] = []
    for mod, r in zip(mods, results, strict=True):
        if isinstance(r, BaseException):
            errors.append(f"{getattr(mod, 'NAME', mod.__name__)}: {type(r).__name__}")
        else:
            hits += r[:limit]
    return hits, errors
