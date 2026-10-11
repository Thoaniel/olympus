"""Varig minne for Zeus, etter mønster fra Hermes Agent.

To små, kuraterte filer i ~/.olympus/memory/:
  MEMORY.md – Zeus' egne notater: miljø, konvensjoner, ting de har lært (standard maks 2 200 tegn)
  USER.md   – hvem brukeren er: preferanser, stil, faste opplysninger (standard maks 1 375 tegn)

Oppføringer skilles med «§». Begge leses inn i systemprompten som et frosset øyeblikksbilde når økten starter
(så modellens prefiks-cache holder hele økten); endringer underveis lagres med en gang, men vises først i neste
økt. Ingenting komprimeres av seg selv: blir en fil full, får agenten en feil og må slå sammen eller fjerne.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

SEP = "\n§\n"
TARGETS = {"memory": "MEMORY.md", "user": "USER.md"}
TITLES = {"memory": "MINNE (dine egne notater)", "user": "BRUKEREN"}
# Mønstre som ikke hører hjemme i et minne som havner i systemprompten (injeksjon / lekkasje).
SUSPICIOUS = re.compile(
    r"ignore (all |the )?(previous|above)|glem (alle )?(tidligere|instruksjonene)|system ?prompt|"
    r"you are now|du er nå en|<\s*/?\s*(system|assistant|tool)\s*>|api[_ -]?key|password|passord\s*[:=]",
    re.I,
)


@dataclass
class MemoryResult:
    ok: bool
    message: str


class MemoryStore:
    def __init__(self, root: Path, limits: dict[str, int] | None = None) -> None:
        self.root = root
        self.limits = {"memory": 2200, "user": 1375, **(limits or {})}

    def path(self, target: str) -> Path:
        return self.root / TARGETS[target]

    def entries(self, target: str) -> list[str]:
        p = self.path(target)
        if not p.exists():
            return []
        return [e.strip() for e in p.read_text(encoding="utf-8").split("§") if e.strip()]

    def _write(self, target: str, entries: list[str]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        text = SEP.join(entries) + ("\n" if entries else "")
        fd = os.open(self.path(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)

    def size(self, target: str) -> int:
        return len(SEP.join(self.entries(target)))

    @staticmethod
    def _check(content: str) -> str | None:
        if any(unicodedata.category(ch) in ("Cf", "Co") for ch in content):
            return "Inneholder usynlige tegn – ikke lagret."
        if SUSPICIOUS.search(content):
            return "Ser ut som instruksjoner eller hemmeligheter, ikke et minne – ikke lagret."
        return None

    @staticmethod
    def entry_id(text: str) -> str:
        """Stabil id for en oppføring (fra teksten): appen og Zeus peker på samme oppføring uten å gjengi den."""
        return hashlib.sha1(text.encode()).hexdigest()[:10]

    def items(self, target: str) -> list[dict[str, str]]:
        return [{"id": self.entry_id(e), "text": e} for e in self.entries(target)]

    def find(self, entry_id: str) -> tuple[str, str] | None:
        """(mål, tekst) for en id, eller None."""
        for target in TARGETS:
            for e in self.entries(target):
                if self.entry_id(e) == entry_id:
                    return target, e
        return None

    def limits_info(self) -> dict[str, dict[str, int]]:
        return {t: {"used": self.size(t), "max": self.limits[t]} for t in TARGETS}

    def apply(self, action: str, target: str, content: str = "", old_text: str = "", old_id: str = "") -> MemoryResult:
        if target not in TARGETS:
            return MemoryResult(False, f"Ukjent mål {target!r}: bruk memory eller user.")
        if old_id and not old_text:
            hit = self.find(old_id)
            if hit is None or hit[0] != target:
                return MemoryResult(False, f"Fant ingen oppføring med id {old_id!r}.")
            old_text = hit[1]
        entries = self.entries(target)
        content = content.strip()
        if action in ("add", "replace"):
            if not content:
                return MemoryResult(False, "Tomt innhold.")
            if (err := self._check(content)) is not None:
                return MemoryResult(False, err)
        if action == "add":
            if content in entries:
                return MemoryResult(False, "Finnes allerede.")
            new = [*entries, content]
        elif action in ("replace", "remove"):
            hits = ([i for i, e in enumerate(entries) if e == old_text] if old_id else []) or \
                   [i for i, e in enumerate(entries) if old_text and old_text in e]
            if len(hits) != 1:
                return MemoryResult(False, f"old_text må treffe nøyaktig én oppføring (traff {len(hits)}).")
            new = list(entries)
            if action == "replace":
                new[hits[0]] = content
            else:
                new.pop(hits[0])
        else:
            return MemoryResult(False, f"Ukjent handling {action!r}: bruk add, replace eller remove.")
        size, limit = len(SEP.join(new)), self.limits[target]
        if size > limit:
            return MemoryResult(False, f"{TARGETS[target]} blir {size} av maks {limit} tegn. Slå sammen eller fjern "
                                       "oppføringer først (replace/remove), og prøv igjen.")
        self._write(target, new)
        return MemoryResult(True, f"{TARGETS[target]}: {len(new)} oppføringer, {size}/{limit} tegn "
                                  f"({size * 100 // limit} %). Vises i systemprompten fra neste økt.")

    def snapshot(self) -> str:
        """Blokken som legges i systemprompten (fryses ved øktstart)."""
        parts = []
        for target, title in TITLES.items():
            entries = self.entries(target)
            if not entries:
                continue
            size, limit = self.size(target), self.limits[target]
            body = "\n".join(f"- {e}" for e in entries)
            parts.append(f"### {title} – {size}/{limit} tegn ({size * 100 // limit} %)\n{body}")
        if not parts:
            return ""
        return ("## Varig minne\nDette har du lagret fra tidligere økter. Bruk det, og hold det oppdatert med "
                "memory-verktøyet (fakta om brukeren → user, lærdom om miljø og arbeid → memory).\n\n"
                + "\n\n".join(parts))
