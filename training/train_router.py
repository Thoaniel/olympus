"""Tren velgeren (olympus.router) og mål den ærlig.

1. Holder 20 % av MALENE helt utenfor (ikke bare setninger), så målingen viser om den forstår nye formuleringer.
2. Måler på de 40 oppgavene fra modelltesten – fjernet fra treningsdataene, aldri sett.
3. Trener på nytt på alt (fortsatt uten de 40) og lagrer til ~/.olympus/router/model.npz.

    python training/train_router.py
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from olympus.router import Router, default_path  # noqa: E402
from router_data import generate  # noqa: E402

# De 40 oppgavene i modelltesten (scratchpad/eval/tasks.json) med riktig merkelapp. Aldri i treningen.
EVAL = {
    "hei": "none", "takk": "none", "hva er klokka?": "none", "hvilken dag er det i morgen?": "none",
    "hvor mange dager er det til julaften?": "dato:dager", "hvilken ukedag er 17. mai neste år?": "dato:ukedag",
    "hva skjer i morgen?": "plan:list", "hva skjer denne uka?": "plan:list", "hva skjer på mandag?": "plan:list",
    "har jeg noe i planen på lørdag?": "plan:list", "legg inn ZTEST tannlege fredag kl 10": "plan:add",
    "legg inn ZTEST middag med Ola i morgen kl 18": "plan:add", "flytt treningen på tirsdag til kl 19": "plan:move",
    "slett ZTEST kino": "plan:delete", "skal på ZTEST trening hver mandag kl 20 framover": "plan:add",
    "hvordan har PC-en det?": "pc:info", "hvordan har serveren det?": "pc:info", "hvor varm er GPU-en?": "pc:info",
    "hvor mye diskplass har jeg igjen?": "pc:info", "pågår det noen nedlastinger?": "downloads:list",
    "hva har jeg i musikkbiblioteket?": "media_search", "hvor mange filmer har jeg?": "media_search",
    "har jeg noen bøker?": "media_search", "hva slags musikk liker jeg?": "media:taste",
    "lag en spilleliste med det jeg liker best": "media:mix", "hva spilles nå på PC-en?": "pc:status",
    "hva husker du om meg?": "memory:list", "husk at jeg drikker kaffen svart": "memory:add",
    "hva snakket vi om i går?": "session_search",
    "sjekk denne lenken: https://archive.org/details/his_girl_friday": "media:inspect",
    "finn Peer Gynt som lydbok": "media_find", "bytt til gemini": "model:switch",
    "hvilke modeller kan du bruke?": "model:list", "hva er været?": "pc:weather", "lås PC-en": "pc:remote",
    "hvor ligger instruksene dine?": "none", "skriv en kort bursdagshilsen til mamma": "none",
    "forklar kort hva en magnetlenke er": "none",
    "hva er forskjellen på en film og en serie i biblioteket mitt?": "none",
    "slett albumet blue train": "media:delete",
}


def norm(t: str) -> str:
    return re.sub(r"[^\wæøå ]", "", t.lower()).strip()


def main() -> None:
    eval_norm = {norm(q) for q in EVAL}
    data = [r for r in generate() if norm(r["text"]) not in eval_norm]  # ingen lekkasje fra testen
    para = Path.home() / ".olympus" / "router" / "paraphrases.jsonl"  # 27B sine varianter (router_paraphrase.py)
    llm = [json.loads(l) for l in para.read_text().splitlines()] if para.exists() else []
    llm = [r for r in llm if norm(r["text"]) not in eval_norm]
    for r in llm:  # 27B blandet «sett sted» med vanlige værspørsmål: de er vær, ikke stedsbytte
        if r["label"] == "pc:place" and not re.search(r"\b(sett|bytt|endre|bruk|lokasjon|bor|gjelde|standard|flytt|lagre|husk)\w*", r["text"], re.I):
            r["label"] = "pc:weather"
    for i, r in enumerate(llm):  # del dem i småbiter, så en femtedel kan holdes utenfor
        r["template"] = f"llm:{r['label']}#{i % 5}"
    data += llm
    print(f"{len(data)} eksempler ({len(llm)} fra 27B), {len({r['label'] for r in data})} merkelapper (testsetningene fjernet)")

    # 1) En femtedel av malene og av 27B-variantene holdt helt utenfor
    rnd = random.Random(1)
    templates = sorted({r["template"] for r in data})
    held = {t for t in templates if (t.endswith("#0") if t.startswith("llm:") else rnd.random() < 0.2)}
    train = [(r["text"], r["label"]) for r in data if r["template"] not in held]
    t0 = time.time()
    m = Router.train(train)
    for name, keep in (("håndskrevne maler", lambda t: not t.startswith("llm:")), ("27B-varianter", lambda t: t.startswith("llm:"))):
        test = [(r["text"], r["label"]) for r in data if r["template"] in held and keep(r["template"])]
        if test:
            acc = sum(m.predict(x)[0] == y for x, y in test) / len(test)
            print(f"Aldri sett ({name}): {acc:.1%} riktig av {len(test)}")
    print(f"(trent på {len(train)} på {time.time() - t0:.0f} s)")

    # 2) Endelig modell på alt, målt på de 40 testoppgavene
    final = Router.train([(r["text"], r["label"]) for r in data])
    hits, wrong = 0, []
    t0 = time.perf_counter()
    for q, y in EVAL.items():
        pred, p = final.predict(q)
        hits += pred == y
        if pred != y:
            wrong.append(f"   ✗ «{q}» → {pred} ({p:.0%}), skulle vært {y}")
    ms = (time.perf_counter() - t0) / len(EVAL) * 1000
    print(f"Modelltestens 40 oppgaver: {hits}/40 riktig, {ms:.1f} ms per melding")
    print("\n".join(wrong))
    conf = Counter()
    for q, y in EVAL.items():
        pred, p = final.predict(q)
        conf["sikre (≥55 %) og riktige" if p >= 0.55 and pred == y else "sikre men feil" if p >= 0.55 else "usikre (ingen hint)"] += 1
    print("Hint som ville blitt gitt:", dict(conf))
    final.save(default_path())
    print("Lagret:", default_path())


if __name__ == "__main__":
    main()
