"""Natt: velgeren lærer av hvordan du faktisk bruker Zeus.

Fra de lagrede samtalene hentes (meldingen din → verktøyet Zeus brukte), eller «none» når han svarte uten verktøy.
Det blandes med de håndskrevne malene og 27B-variantene, velgeren trenes på nytt (CPU, numpy, noen minutter, Nice 19)
og måles på de 40 testoppgavene (aldri trent på). Den nye tas bare i bruk hvis den treffer minst like mange og ikke
blir sikker-men-feil oftere. Testsamtaler (ZTEST o.l.) tas aldri med.

    uv run python training/nightly_router.py [--dry-run]
Rapport: ~/.olympus/training/router-night.md
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from olympus.router import HINTS, Router, default_path  # noqa: E402
from router_data import generate  # noqa: E402
from train_router import EVAL, norm  # noqa: E402

OLY = Path.home() / ".olympus"
REPORT = OLY / "training" / "router-night.md"
TEST = re.compile(r"ztest|^test\b|^probe", re.I)


def label_for(tools: list[str]) -> str | None:
    """«⚙ plan add «Tannlege» …» → plan:add; «⚙ media_search jazz» → media_search. Ukjent → None."""
    if not tools:
        return "none"
    first = tools[0].removeprefix("⚙").strip().split()
    if not first:
        return None
    name = first[0]
    action = first[1] if len(first) > 1 else ""
    for cand in (f"{name}:{action}", name):
        if cand in HINTS:
            return cand
    return None


def real_pairs(sessions_dir: Path) -> list[tuple[str, str]]:
    out = []
    for f in sorted(sessions_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if TEST.search(d.get("title") or ""):
            continue
        t = d.get("transcript") or []
        for i, e in enumerate(t[:-1]):
            if e.get("kind") != "user" or TEST.search(e.get("text") or ""):
                continue
            nxt = t[i + 1]
            if nxt.get("kind") != "agent" or nxt.get("draft"):
                continue
            if "tools" not in nxt and nxt.get("text", "").strip() == "":
                continue
            lab = label_for(nxt.get("tools") or [])
            text = (e.get("text") or "").strip()
            if lab and 2 <= len(text) <= 300 and norm(text) not in {norm(q) for q in EVAL}:
                out.append((text, lab))
    return out


def score(m: Router) -> tuple[int, int]:
    hits = wrong_sure = 0
    for q, y in EVAL.items():
        pred, p = m.predict(q)
        hits += pred == y
        wrong_sure += pred != y and p >= 0.55
    return hits, wrong_sure


def main() -> None:
    dry = "--dry-run" in sys.argv
    t0 = time.time()
    real = real_pairs(OLY / "sessions")
    eval_norm = {norm(q) for q in EVAL}
    base = [(r["text"], r["label"]) for r in generate() if norm(r["text"]) not in eval_norm]
    para = OLY / "router" / "paraphrases.jsonl"
    llm = [json.loads(x) for x in para.read_text().splitlines()] if para.exists() else []
    llm = [(r["text"], r["label"]) for r in llm if norm(r["text"]) not in eval_norm]
    rows = base + llm + real * 3  # ekte bruk veier tyngre
    random.Random(0).shuffle(rows)
    new = Router.train(rows)
    old = Router.load(default_path())
    n_hits, n_wrong = score(new)
    o_hits, o_wrong = score(old) if old else (0, 99)
    adopt = n_hits >= o_hits and n_wrong <= o_wrong
    if adopt and not dry:
        new.save(default_path())
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        f"# Velgeren – natt {time.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"- Ekte eksempler fra samtalene: {len(real)} (vektet ×3), totalt {len(rows)}\n"
        f"- Testsettet (40): ny {n_hits}/40 ({n_wrong} sikre feil), i bruk {o_hits}/40 ({o_wrong} sikre feil)\n"
        f"- {'Tatt i bruk' if adopt and not dry else 'Ikke tatt i bruk' + (' (prøvekjøring)' if dry else '')}\n"
        f"- Tid: {time.time() - t0:.0f} s\n")
    print(REPORT.read_text())


if __name__ == "__main__":
    main()
