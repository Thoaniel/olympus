"""Velgeren: en liten, trent klassifiserer som gjetter hvilket verktøy/handling en melding trenger («plan:delete»,
«memory:add», «none» …). Brukes som et HINT til Zeus – han beholder alle verktøyene og kan velge annerledes –
så et feilgjett koster lite, mens et riktig gjett hjelper ham å treffe (kino → planen, ikke filmene).

Tekstbiter (bokstav-n-gram 2–5 og ord/ordpar) hashes til faste trekk; multinomisk logistisk regresjon i numpy.
Ingen GPU, ingen ekstra avhengigheter, ~1 ms per melding. Trenes med training/train_router.py; modellen ligger i
~/.olympus/router/model.npz.
"""

from __future__ import annotations

import json
import re
import zlib
from pathlib import Path

import numpy as np

DIM = 1 << 17
WORD = re.compile(r"[\wæøå:/.\-]+", re.I)

# Hva hintet sier til Zeus for hver merkelapp (verktøy + handling, eller ingen).
HINTS = {
    "none": "trolig ingen verktøy (svar direkte)",
    "dato:dager": "dato (action=dager)", "dato:ukedag": "dato (action=ukedag)", "dato:pluss": "dato (action=pluss)",
    "plan:list": "plan (action=list)", "plan:add": "plan (action=add; repeat hvis det gjentas)",
    "plan:move": "plan (action=move/edit)", "plan:delete": "plan (action=delete) – en avtale i planen",
    "pc:info": "pc (action=info)", "pc:status": "pc (action=status)", "pc:play": "pc (action=play)",
    "pc:control": "pc (next/pause/resume/stop/prev)", "pc:remote": "pc (lock/screen_off/screen_on/sleep/open_olympus)",
    "pc:handoff": "pc (action=handoff)", "pc:weather": "pc (action=weather)", "pc:place": "pc (action=place)",
    "audio": "audio", "downloads:list": "downloads (list)", "downloads:cancel": "downloads (cancel)",
    "media_search": "media_search (søk i brukerens eget bibliotek)", "media:taste": "media (action=taste)",
    "media:mix": "media (action=mix) eller playlist", "media:like": "media (action=like)",
    "media:inspect": "media (action=inspect) på lenken", "media:add": "media (action=add) med lenken",
    "media:delete": "media_search for id, så media (action=delete)", "media:edit": "media (action=edit)",
    "media:cover": "media (cover_fetch/cover_set)", "media:scan": "media (action=scan)",
    "media_find": "media_find (søk i åpne kilder etter noe nytt)", "playlist": "playlist",
    "memory:list": "memory (action=list)", "memory:add": "memory (action=add)", "memory:remove": "memory (action=remove)",
    "session_search": "session_search", "model:switch": "model (action=switch)", "model:list": "model (action=list)",
    "files": "read_file/list_dir/search_files", "shell": "shell", "snl": "snl_search/snl_read",
}


def features(text: str) -> dict[int, float]:
    """Hashede tekstbiter: bokstav-n-gram (tåler bøyning og skrivefeil) + ord og ordpar."""
    t = " " + re.sub(r"\s+", " ", text.lower().strip()) + " "
    out: dict[int, float] = {}

    def add(key: str, w: float) -> None:
        h = zlib.crc32(key.encode()) & (DIM - 1)
        out[h] = out.get(h, 0.0) + w

    for n in (2, 3, 4, 5):
        for i in range(len(t) - n + 1):
            add("c" + t[i:i + n], 1.0)
    words = WORD.findall(t)
    for w in words:
        add("w" + ("<url>" if "://" in w or w.startswith("magnet:") else w), 2.0)
    for a, b in zip(words, words[1:]):
        add("b" + a + " " + b, 1.5)
    norm = sum(v * v for v in out.values()) ** 0.5 or 1.0
    return {k: v / norm for k, v in out.items()}


class Router:
    def __init__(self, W: np.ndarray, b: np.ndarray, labels: list[str]) -> None:
        self.W, self.b, self.labels = W, b, labels

    @classmethod
    def load(cls, path: Path) -> Router | None:
        try:
            z = np.load(path)
            labels = json.loads(path.with_suffix(".json").read_text())
            return cls(z["W"], z["b"], labels)
        except (OSError, ValueError, KeyError):
            return None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, W=self.W, b=self.b)
        path.with_suffix(".json").write_text(json.dumps(self.labels, ensure_ascii=False))

    def probs(self, text: str) -> np.ndarray:
        f = features(text)
        idx = np.fromiter(f.keys(), dtype=np.int64)
        val = np.fromiter(f.values(), dtype=np.float32)
        z = val @ self.W[idx] + self.b
        z = np.exp(z - z.max())
        return z / z.sum()

    def predict(self, text: str) -> tuple[str, float]:
        p = self.probs(text)
        i = int(p.argmax())
        return self.labels[i], float(p[i])

    @classmethod
    def train(cls, rows: list[tuple[str, str]], epochs: int = 30, lr: float = 0.5, l2: float = 1e-5,
              seed: int = 0) -> Router:
        labels = sorted({y for _, y in rows})
        li = {y: i for i, y in enumerate(labels)}
        X = []
        for text, _ in rows:
            f = features(text)
            X.append((np.fromiter(f.keys(), dtype=np.int64), np.fromiter(f.values(), dtype=np.float32)))
        Y = np.array([li[y] for _, y in rows])
        W = np.zeros((DIM, len(labels)), dtype=np.float32)
        b = np.zeros(len(labels), dtype=np.float32)
        r = np.random.default_rng(seed)
        for ep in range(epochs):
            step = lr / (1 + ep * 0.15)
            for i in r.permutation(len(rows)):
                idx, val = X[i]
                z = val @ W[idx] + b
                z = np.exp(z - z.max())
                p = z / z.sum()
                p[Y[i]] -= 1.0  # gradient av kryssentropi
                W[idx] -= step * (np.outer(val, p) + l2 * W[idx])
                b -= step * p
        return cls(W, b, labels)


_cached: tuple[float, Router | None] = (-1.0, None)


def default_path() -> Path:
    return Path.home() / ".olympus" / "router" / "model.npz"


def load_default() -> Router | None:
    """Den trente velgeren (lastes på nytt hvis fila er byttet ut). None hvis den ikke er trent ennå."""
    global _cached
    p = default_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    if _cached[0] != mtime:
        _cached = (mtime, Router.load(p))
    return _cached[1]


def hint(text: str, threshold: float = 0.55) -> str:
    """Hintet som legges på brukermeldingen til modellen, eller '' når velgeren er usikker/ikke trent."""
    r = load_default()
    if r is None or len(text.split()) < 1:
        return ""
    # TV-casting er nyere enn treningsdataene: «spill Dune på TV-en» skal ikke få hintet pc (play)
    if re.search(r"\b(tv(-en|en)?|chromecast|cast|caste|stua|storskjerm)\b", text, re.I):
        return "(Velgeren tror dette er: tv (list/play/pause/resume/stop/seek) – film eller serie på TV-en. Bruk det hvis det stemmer.)"
    if re.search(r"\b(søk på nett\w*|google\w*|nyhet\w*|på nettet)\b", text, re.I):
        return "(Velgeren tror dette er: web (search, så read) – oppgi kildene. Bruk det hvis det stemmer.)"
    if re.search(r"\b(kapittel|kapitlet|bok-id|boka|boken|e-boka)\b", text, re.I):
        return "(Velgeren tror dette er: media (action=read) – les teksten i boka før du svarer. Bruk det hvis det stemmer.)"
    label, p = r.predict(text)
    if p < threshold or label not in HINTS:
        return ""
    return f"(Velgeren tror dette er: {HINTS[label]} – sikkerhet {p:.0%}. Bruk det hvis det stemmer.)"
