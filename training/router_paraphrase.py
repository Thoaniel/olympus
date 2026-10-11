"""Flere og mer varierte treningseksempler til velgeren: den lokale modellen (27B) skriver mange ulike norske
formuleringer per hensikt. Resultatet legges i ~/.olympus/router/paraphrases.jsonl og brukes av train_router.py.

Kjøres mot Olympus-serveren som allerede går (modellen lastes via forvarmingen og holdes i live).

    python training/router_paraphrase.py [antall per hensikt]
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from olympus import config  # noqa: E402
from olympus.web.server import get_token  # noqa: E402

# Hva hver merkelapp betyr – beskrevet slik en bruker ville tenkt det, med typiske forvekslinger nevnt.
INTENTS = {
    "none": "vanlig prat uten at assistenten trenger å slå opp noe: hilsener, takk, spørsmål om kunnskap, be om å "
            "skrive/oversette/forklare noe, spørre hva dagen/klokka er nå",
    "dato:dager": "hvor mange dager det er til eller siden en dato eller høytid",
    "dato:ukedag": "hvilken ukedag en bestemt dato faller på",
    "dato:pluss": "hvilken dato det blir om et antall dager/uker",
    "plan:list": "hva som står i kalenderen/planen, om man er ledig, når en avtale er",
    "plan:add": "legge inn en ny avtale i kalenderen (også gjentatte: hver uke, hver dag)",
    "plan:move": "flytte eller endre en avtale i kalenderen (tidspunkt, dag, navn)",
    "plan:delete": "slette/avlyse en avtale i kalenderen (f.eks. kino, tannlege, trening er AVTALER, ikke filmer)",
    "pc:info": "hvordan PC-en/serveren har det: temperatur, disk/diskplass, minne, tjenester, backup",
    "pc:status": "hvilken sang som spilles nå på PC-en/høyttaleren",
    "pc:play": "starte musikk på PC-en/høyttaleren (en artist, et album, noe de liker)",
    "pc:control": "styre avspillingen: neste, forrige, pause, fortsett, stopp",
    "pc:remote": "låse PC-en, skru av/på skjermen, sette den i dvale/hvilemodus",
    "pc:handoff": "flytte musikken fra PC-en til telefonen",
    "pc:weather": "været ute nå (temperatur, regn, sol)",
    "pc:place": "sette hvilket sted værmeldingen skal gjelde",
    "audio": "volum, demping, lydutgang og bluetooth-høyttaler",
    "downloads:list": "status på nedlastinger/torrenter som pågår",
    "downloads:cancel": "avbryte en nedlasting",
    "media_search": "hva som finnes i brukerens eget mediebibliotek (musikk, filmer, serier, bøker, lydbøker), antall",
    "media:taste": "brukerens musikksmak: hva de liker, hører mest på, favoritter",
    "media:mix": "lage en miks/spilleliste ut fra det brukeren liker",
    "media:like": "markere en sang/artist som likt eller ikke likt",
    "media:inspect": "undersøke hva en lenke (URL eller magnet) inneholder før nedlasting",
    "media:add": "laste ned en lenke (URL eller magnet) inn i biblioteket",
    "media:delete": "slette et album, en film, en sang eller en bok fra biblioteket",
    "media:edit": "rette tittel/artist/år/sjanger på noe i biblioteket",
    "media:cover": "bytte eller hente cover/plakat",
    "media:scan": "skanne biblioteket på nytt etter nye filer",
    "media_find": "finne noe NYTT å laste ned (film, lydbok, e-bok, musikk) i åpne kilder på nett",
    "playlist": "lage, vise, endre eller slette navngitte spillelister",
    "memory:list": "hva assistenten husker/vet om brukeren",
    "memory:add": "be assistenten huske/notere seg noe om brukeren for senere",
    "memory:remove": "be assistenten glemme noe den husker",
    "session_search": "hva man snakket om i tidligere samtaler",
    "model:switch": "bytte hvilken AI-modell assistenten bruker",
    "model:list": "hvilke AI-modeller som finnes/brukes",
    "files": "lese en fil eller se innholdet i en mappe på PC-en",
    "shell": "kjøre en kommando på PC-en",
    "snl": "slå opp i Store norske leksikon",
}

PROMPT = """Du lager treningsdata. Skriv {n} ULIKE meldinger en norsk bruker kunne sendt til den personlige assistenten sin (Zeus) når de vil: {desc}.

Krav:
- norsk bokmål som folk faktisk skriver på mobilen: korte og lange, uformelle og høflige, noen med små bokstaver, noen med skrivefeil eller dialekt (ka/koss/ikkje/eg), noen uten spørsmålstegn
- varier ordvalg, setningsbygning og konkrete detaljer (navn, steder, dager, klokkeslett, artister, titler)
- bare meldingen, én per linje, ingen nummerering, ingen forklaring"""


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    c = config.load()
    base = "http://127.0.0.1:8787/api/v1"
    h = {"Cookie": f"olympus_web={get_token(c)}"}
    out = Path.home() / ".olympus" / "router" / "paraphrases.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    done = {json.loads(l)["label"] for l in out.read_text().splitlines()} if out.exists() else set()
    httpx.post(f"{base}/zeus/warm", headers=h)
    port = None
    for _ in range(90):  # vent til modellen er lastet
        ps = __import__("subprocess").run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
        m = re.search(r"llama-server .*--port (\d+)", ps)
        if m:
            port = m.group(1)
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
        time.sleep(2)
    if port is None:
        sys.exit("fant ingen modellserver")
    last_keep = time.time()
    with out.open("a") as f:
        for label, desc in INTENTS.items():
            if label in done:
                continue
            if time.time() - last_keep > 60:  # hold modellen i live (ellers frigjøres den etter 5 min)
                httpx.post(f"{base}/zeus/warm", headers=h)
                last_keep = time.time()
            r = httpx.post(f"http://127.0.0.1:{port}/v1/chat/completions", timeout=300, json={
                "messages": [{"role": "user", "content": PROMPT.format(n=n, desc=desc)}],
                "temperature": 0.9, "max_tokens": 2500, "chat_template_kwargs": {"enable_thinking": False}})
            text = r.json()["choices"][0]["message"]["content"]
            lines = [re.sub(r"^\s*([-*•]|\d+[.)])\s*", "", l).strip().strip('"«»') for l in text.splitlines()]
            lines = [l for l in lines if 2 <= len(l) <= 200]
            for l in dict.fromkeys(lines):
                f.write(json.dumps({"text": l, "label": label, "template": f"llm:{label}"}, ensure_ascii=False) + "\n")
            f.flush()
            print(f"{label:18} {len(lines)}", flush=True)


if __name__ == "__main__":
    main()
