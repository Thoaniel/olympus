"""Finn filer i et svar: kodeblokker merket med filnavn, så de kan lagres med ett klikk.

Gjenkjenner de vanlige måtene modeller merker filer på:
    ```html nettside/index.html          (sti etter språket i infostrengen)
    ```python title="app.py"
    ### `style.css`   /   **script.js**   /   Fil: main.py      (linja rett over blokken)
Bare relative stier uten «..» og uten skjulte deler (.bashrc, .config/…) godtas, så ingenting kan havne utenfor
mappen eller overskrive dotfiler. Eksisterende filer overskrives bare når brukeren bekrefter det særskilt.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

FENCE = re.compile(r"^(```+|~~~+)[ \t]*([^\n]*)\n(.*?)^\1[ \t]*$", re.M | re.S)
NAME = r"[\w.\-/]+\.[A-Za-z0-9]{1,8}"
INFO_PATH = re.compile(rf"""(?:title|file|filename|path)\s*=\s*["']?({NAME})["']?|^\S+\s+({NAME})\s*$|^({NAME})$""")
LABEL = re.compile(rf"""^\s*(?:#+\s*|[-*]\s*|\d+\.\s*)?(?:\*\*|__)?(?:fil|file|filnavn)?\s*:?\s*[`*_]*({NAME})[`*_]*\s*:?\s*(?:\*\*|__)?\s*$""",
                   re.I)


def safe_rel(path: str) -> str | None:
    p = PurePosixPath(path.strip())
    if p.is_absolute() or not p.parts or any(part.startswith(".") for part in p.parts):
        return None  # absolutt, «..», eller skjult fil/mappe (dotfiler som .bashrc)
    return str(p)


def extract_files(markdown: str) -> list[tuple[str, str]]:
    """[(relativ sti, innhold)] i rekkefølgen de står; samme sti flere ganger → siste versjon."""
    found: dict[str, str] = {}
    for m in FENCE.finditer(markdown):
        info, body = m.group(2).strip(), m.group(3)
        name = None
        im = INFO_PATH.search(info)
        if im:
            name = next(g for g in im.groups() if g)
        else:
            before = markdown[: m.start()].rstrip("\n").splitlines()[-2:]
            for line in reversed(before):
                if not line.strip():
                    continue
                lm = LABEL.match(line)
                if lm:
                    name = lm.group(1)
                else:  # «… lagre koden som `index.html`.» – nøyaktig ett filnavn i backticks på linja over
                    ticks = set(re.findall(rf"`({NAME})`", line))
                    if len(ticks) == 1:
                        name = ticks.pop()
                break
        rel = safe_rel(name) if name else None
        if rel:
            found.pop(rel, None)
            found[rel] = body if body.endswith("\n") else body + "\n"
    return list(found.items())


def existing(base: Path, files: list[tuple[str, str]]) -> list[str]:
    """Filene som allerede finnes (og dermed ville blitt overskrevet)."""
    return [rel for rel, _ in files if (base / rel).exists()]


def save_files(base: Path, files: list[tuple[str, str]], overwrite: bool = False) -> list[Path]:
    """Skriv filene under base (lager mapper ved behov). Eksisterende filer hoppes over med mindre overwrite.
    Returnerer stiene som ble skrevet."""
    base = base.resolve()
    written = []
    for rel, content in files:
        if safe_rel(rel) is None:
            continue
        target = (base / rel).resolve()  # resolve følger symlenker: en lenke ut av mappen fanges under
        if base not in target.parents:  # dobbeltsjekk: aldri utenfor base
            continue
        if (base / rel).exists() and not overwrite:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(target)
    return written
