"""Treningsdata for velgeren (olympus.router): norske meldinger → hvilket verktøy/handling Zeus bør bruke.

Hver hensikt har håndskrevne formuleringer med plassholdere ({dag}, {tid}, {hva} …) som fylles med varianter, pluss
litt «støy» (små bokstaver, skrivefeil, dialekt). De 40 oppgavene i modelltesten er IKKE med her – de brukes bare til
å måle (se train_router.py), så tallene er ærlige.

    python training/router_data.py > ~/.olympus/router/data.jsonl
"""

from __future__ import annotations

import json
import random

DAG = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag", "i morgen", "i dag", "i overmorgen",
       "neste fredag", "på torsdag", "førstkommende mandag", "den 24.", "12.11", "3. desember", "neste uke"]
TID = ["kl 10", "kl 18", "klokka 20", "kl. 14.30", "halv åtte", "19:00", "kl 9", "om kvelden", "kl 07", "08:15", "kl 17"]
HVA = ["tannlege", "kino", "middag med Ola", "trening", "frisør", "møte med sjefen", "bursdag til Kari", "fotball",
       "legetime", "konsert", "vaksine", "foreldremøte", "pizza med gutta", "yoga", "eksamen", "bilservice", "date"]
ARTIST = ["Satie", "Daft Punk", "Kygo", "Metallica", "Taylor Swift", "Erik Satie", "Röyksopp", "Aurora", "Drake",
          "Pink Floyd", "Juice WRLD", "Chopin"]
FILM = ["Metropolis", "Nosferatu", "Big Buck Bunny", "Ringenes herre", "Interstellar", "His Girl Friday", "Dune"]
BOK = ["Peer Gynt", "Sult", "Et dukkehjem", "Kristin Lavransdatter", "Dracula", "Moby Dick", "Pride and Prejudice"]
URL = ["https://archive.org/details/metropolis1927", "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
       "https://soundcloud.com/artist/track", "https://example.org/fil.mp3", "magnet:?xt=urn:btih:abc123&dn=Film.2001",
       "https://archive.org/details/nosferatu", "https://bandcamp.com/album/x"]
MODELL = ["gemini", "claude", "opus", "den lokale", "qwen", "35b", "den raske modellen", "sky-modellen"]
STED = ["Oslo", "Bergen", "Majorstuen", "Lillehammer", "Tromsø"]

I: dict[str, list[str]] = {
    "none": [
        "hei", "hallo", "heisann", "god morgen", "god kveld", "takk", "tusen takk", "takk skal du ha", "ok", "flott",
        "supert", "nice", "kult", "ja takk", "nei takk", "hade", "snakkes", "hvordan går det med deg?", "hvem er du?",
        "hva kan du hjelpe meg med?", "fortell en vits", "skriv et dikt om høsten", "skriv en kort tekst til {hva}",
        "lag en handleliste for taco", "forklar hva en GPU er", "hva betyr ordet melankoli?", "oversett hei til spansk",
        "skriv en melding til sjefen om at jeg er syk", "gi meg tips til trening", "hva er hovedstaden i Frankrike?",
        "forklar kort hva DNS er", "skriv en takkemelding til Kari", "hvordan koker man egg?", "hva er 15 prosent av 300?",
        "skriv en kort hilsen til pappa", "fortell noe interessant", "hva synes du om {artist}?", "jeg er sliten",
        "hva er forskjellen på RAM og VRAM?", "forklar torrent enkelt", "lag et navn til katten min", "god natt",
        "hvilken dag er det i dag?", "hva er klokka nå?", "hvilken ukedag er det i morgen?", "hva er datoen i dag?",
        "hvor ligger systemprompten din?", "hvor er reglene dine?", "hva heter du?", "kan du snakke nynorsk?",
    ],
    "dato:dager": [
        "hvor mange dager er det til {dag}?", "hvor lenge er det til jul?", "hvor mange dager til 17. mai?",
        "hvor mange dager er det til nyttårsaften?", "når er det julaften, hvor mange dager?", "dager igjen til påske?",
        "hvor lenge til bursdagen min 3. mars?", "hvor mange dager er det mellom 1. mai og 17. mai?",
        "telle dager til sommerferien 20. juni", "hvor mange dager siden 1. januar?", "kor mange dager e det te jul",
        "hvor mange uker er det til jul?",
    ],
    "dato:ukedag": [
        "hvilken ukedag er {dag} neste år?", "hvilken dag er julaften i år?", "hvilken ukedag faller 17. mai på?",
        "hva slags dag er 3. mars 2027?", "er nyttårsaften en fredag?", "hvilken ukedag blir bursdagen min 12.11?",
        "hvilken dag i uka er 24.12?", "hvilken ukedag var 1. januar?", "ka dag e 17. mai",
    ],
    "dato:pluss": [
        "hvilken dato er det om 40 dager?", "hva er datoen om tre uker?", "hvilken dag er det om 100 dager?",
        "hva blir datoen 14 dager etter 1. desember?", "om 90 dager, hvilken dato?",
    ],
    "plan:list": [
        "hva skjer {dag}?", "hva har jeg {dag}?", "har jeg noe {dag}?", "har jeg noe i planen {dag}?",
        "hva står i kalenderen min?", "hva skjer neste uke?", "vis planen min", "hva er planen i dag?",
        "er jeg ledig {dag} {tid}?", "når er {hva}?", "når skal jeg til {hva}?", "har jeg {hva} denne uka?",
        "hva har jeg på timeplanen?", "ka skjer i morra", "noe på gang i helga?", "hvilke avtaler har jeg?",
        "hva skjer resten av uka?", "har jeg tid til {hva} {dag}?",
    ],
    "plan:add": [
        "legg inn {hva} {dag} {tid}", "sett opp {hva} {dag} {tid}", "minn meg på {hva} {dag} {tid}",
        "jeg skal på {hva} {dag} {tid}", "book {hva} {dag} {tid}", "legg til {hva} i planen {dag}",
        "ny avtale: {hva} {dag} {tid}", "skriv inn {hva} {dag}", "jeg har {hva} {dag} {tid}, legg det inn",
        "{hva} {dag} {tid}", "kan du legge inn {hva} {dag}?", "legg inn {hva} hver {dag} {tid}",
        "skal på {hva} hver uke {tid}", "{hva} hver dag {tid} framover", "sett opp {hva} hverdager {tid}",
        "legg inn {hva} annenhver uke", "planlegg {hva} {dag}", "husk {hva} {dag} {tid} i kalenderen",
    ],
    "plan:move": [
        "flytt {hva} til {dag}", "flytt {hva} til {tid}", "endre {hva} til {dag} {tid}", "utsett {hva} til {dag}",
        "{hva} er flyttet til {dag} {tid}", "kan du flytte {hva} en time?", "bytt tidspunkt for {hva} til {tid}",
        "endre navnet på {hva} til {hva}", "{hva} {dag} blir {tid} i stedet", "skyv {hva} til neste uke",
    ],
    "plan:delete": [
        "slett {hva}", "fjern {hva} fra planen", "avlys {hva}", "{hva} er avlyst", "ta bort {hva} {dag}",
        "slett avtalen {dag}", "jeg skal ikke på {hva} likevel", "stryk {hva}", "slett alle {hva}-avtalene",
        "fjern {hva} {dag} {tid}", "kanseller {hva}", "slett {hva} og {hva}",
    ],
    "pc:info": [
        "hvordan har PC-en det?", "hvordan går det med serveren?", "hvor varm er PC-en?", "hva er temperaturen på GPU?",
        "hvor mye diskplass er det igjen?", "er disken full?", "hvor mye VRAM brukes?", "kjører alle tjenestene?",
        "hvordan står det til med serveren?", "status på maskinen", "er PC-en på?", "hvor varm er CPU-en?",
        "er backupen tatt?", "hvilken modell er lastet?", "hvor mye minne bruker modellen?", "kossen går det med pc-en",
        "går tailscale?", "er GPUen varm nå?",
    ],
    "pc:status": [
        "hva spilles nå?", "hva spilles på PC-en?", "hvilken sang er det som går?", "hva hører jeg på nå?",
        "hva er det som spilles på høyttaleren?", "hva heter denne sangen?", "hvilken låt er dette?",
        "hva er neste sang i køen?", "hvor langt har vi kommet i sangen?",
    ],
    "pc:play": [
        "spill {artist}", "sett på {artist}", "spill noe jeg liker", "sett på musikk", "spill musikk på PC-en",
        "spill noe chill", "sett på noe rolig", "spill albumet til {artist}", "spill spillelisten min",
        "kan du spille noe?", "sett på litt musikk på høyttaleren", "spill {artist} på pc-en", "start musikken",
    ],
    "pc:control": [
        "neste sang", "hopp over", "pause", "pause musikken", "stopp musikken", "fortsett", "spill videre",
        "forrige sang", "skru av musikken", "hopp til neste", "ta en pause i musikken", "start igjen",
    ],
    "pc:remote": [
        "lås PC-en", "lås skjermen", "slå av skjermen", "skru av skjermen", "sett PC-en i dvale", "legg PC-en i hvilemodus",
        "skru på skjermen", "åpne olympus på pc-en", "lås maskinen", "få pc-en til å sove",
    ],
    "pc:handoff": [
        "flytt musikken til telefonen", "ta med musikken til mobilen", "fortsett på telefonen", "spill videre på mobilen",
        "send musikken til telefonen min",
    ],
    "pc:weather": [
        "hva er været?", "hvordan blir været i dag?", "regner det?", "hvor kaldt er det ute?", "trenger jeg paraply?",
        "hva er temperaturen ute?", "blir det sol i dag?", "er det glatt ute?", "hvordan er været i {sted}?",
    ],
    "pc:place": [
        "sett stedet til {sted}", "jeg bor i {sted}", "bruk {sted} for været", "endre sted til {sted}",
        "været skal gjelde {sted}",
    ],
    "audio": [
        "skru opp volumet", "skru ned lyden", "sett volumet til 30", "demp lyden", "skru av lyden", "høyere",
        "lavere lyd", "bytt lydutgang til hodetelefonene", "koble til bluetooth-høyttaleren", "hvilken lydutgang brukes?",
        "mute", "litt høyere takk",
    ],
    "downloads:list": [
        "pågår det noen nedlastinger?", "hvor langt har nedlastingen kommet?", "er filmen ferdig lastet ned?",
        "hva laster ned nå?", "vis nedlastingene", "hvor mye er igjen av nedlastingen?", "har {film} lastet ned ferdig?",
        "går nedlastingen?", "hvordan går det med torrenten?", "status på nedlastingene",
    ],
    "downloads:cancel": [
        "avbryt nedlastingen", "stopp nedlastingen av {film}", "kanseller torrenten", "slett nedlastingen",
        "ikke last ned {film} likevel",
    ],
    "media_search": [
        "hva har jeg i musikkbiblioteket?", "har jeg noe av {artist}?", "hvor mange sanger har jeg?",
        "hvor mange filmer har jeg?", "har jeg {film}?", "har jeg noen bøker?", "hvilke album har jeg?",
        "finn {artist} i biblioteket mitt", "søk etter {film} i samlingen", "hvilke serier har jeg?",
        "har jeg lydboka {bok}?", "vis musikken min", "hva slags filmer har jeg?", "ligger {film} på PC-en?",
        "hvor mange album er det i biblioteket?", "har jeg noen lydbøker?",
    ],
    "media:taste": [
        "hva slags musikk liker jeg?", "hva er musikksmaken min?", "hvilke artister hører jeg mest på?",
        "hva hører jeg mest på?", "hvilken sjanger liker jeg best?", "beskriv smaken min", "hva er favorittlåtene mine?",
        "hvilke sanger hopper jeg over?",
    ],
    "media:mix": [
        "lag en miks av det jeg liker", "spill en miks etter smaken min", "lag en spilleliste med det jeg liker best",
        "sett sammen favorittene mine", "lag en playlist med favorittsangene mine", "gi meg en miks for kvelden",
    ],
    "media:like": [
        "jeg liker denne sangen", "lik denne", "denne er bra, merk den", "jeg liker ikke denne", "dislike denne låta",
        "merk {artist} som favoritt", "jeg digger {artist}", "aldri spill denne igjen",
    ],
    "media:inspect": [
        "sjekk denne lenken: {url}", "hva er dette: {url}", "undersøk {url}", "hva inneholder {url}?",
        "er denne lenken en film? {url}", "hvor stor er {url}?", "se på denne: {url}",
    ],
    "media:add": [
        "last ned {url}", "last ned denne: {url}", "legg til {url} i biblioteket", "hent denne filmen: {url}",
        "last ned filmen: {url}", "{url} last ned", "kan du laste ned {url}?", "legg denne inn som serie: {url}",
        "last ned som musikk: {url}",
    ],
    "media:delete": [
        "slett albumet {film}", "slett filmen {film}", "fjern {artist} fra biblioteket", "slett boka {bok}",
        "kast {film}", "slett den sangen", "fjern serien fra biblioteket", "slett albumet til {artist}",
    ],
    "media:edit": [
        "endre artisten på albumet til {artist}", "rett tittelen på {film}", "feil år på {film}, det er 1927",
        "bytt navn på albumet til {film}", "endre sjangeren til jazz", "artisten heter {artist}, rett det",
    ],
    "media:cover": [
        "bytt cover på {film}", "finn nytt cover til albumet", "coveret er feil på {film}", "hent cover på nytt",
    ],
    "media:scan": [
        "skann biblioteket", "les inn musikken på nytt", "oppdater biblioteket", "jeg har lagt inn nye filer, skann",
        "finn de nye filmene jeg la inn",
    ],
    "media_find": [
        "finn {bok} som lydbok", "finn {film} på nett", "søk etter {bok} som e-bok", "finnes {film} gratis?",
        "finn klassisk musikk av {artist} i de åpne kildene", "let etter {film} på internet archive",
        "finn en lydbok av Ibsen", "finn gamle stumfilmer", "kan du finne {bok} til meg?", "søk etter {bok} på librivox",
    ],
    "playlist": [
        "lag en spilleliste som heter trening", "vis spillelistene mine", "legg denne sangen til i spillelisten chill",
        "slett spillelisten fest", "hva er i spillelisten trening?", "lag en ny playlist", "gi nytt navn til spillelisten",
        "fjern sangen fra spillelisten",
    ],
    "memory:list": [
        "hva husker du om meg?", "hva vet du om meg?", "hva har du lagret om meg?", "vis minnet ditt",
        "hva husker du?", "hva har jeg fortalt deg om meg selv?", "hva står i notatene dine om meg?",
    ],
    "memory:add": [
        "husk at jeg drikker kaffen svart", "husk at jeg heter {artist}", "noter deg at jeg bor i {sted}",
        "husk at jeg liker {artist}", "lagre at jeg trener tre ganger i uka", "husk at mamma har bursdag 3. mars",
        "skriv deg bak øret at jeg ikke liker jazz", "ikke glem at jeg er allergisk mot nøtter", "husk dette: {hva}",
    ],
    "memory:remove": [
        "glem at jeg bor i {sted}", "slett det du husker om kaffen", "fjern det fra minnet ditt", "glem det jeg sa om {artist}",
        "du kan glemme det med jazz",
    ],
    "session_search": [
        "hva snakket vi om i går?", "hva sa jeg om {hva} sist?", "finn samtalen der vi snakket om {film}",
        "hva ble vi enige om forrige uke?", "husker du da vi snakket om {artist}?", "hva var det vi diskuterte i morges?",
        "søk i samtalene etter {hva}",
    ],
    "model:switch": [
        "bytt til {modell}", "bruk {modell}", "skift modell til {modell}", "kan du bruke {modell} i stedet?",
        "gå over til {modell}", "svar med {modell} fra nå",
    ],
    "model:list": [
        "hvilke modeller kan du bruke?", "hvilken modell bruker du?", "hvilke modeller har jeg?", "list modellene",
        "hvilken AI svarer nå?", "hvilke skymodeller finnes?",
    ],
    "files": [
        "les fila ~/notater.txt", "hva står i config.toml?", "vis innholdet i mappa Nedlastinger", "finn filer som heter rapport",
        "hva ligger i ~/dev?", "åpne README-filen", "hvor ligger fila med passord?", "list filene i hjemmemappa",
    ],
    "shell": [
        "kjør ls i hjemmemappa", "hvor mye plass bruker mappa Nedlastinger?", "hvilke prosesser bruker mest CPU?",
        "start tjenesten på nytt", "hva er IP-adressen til PC-en?", "kjør en oppdatering av systemet",
    ],
    "snl": [
        "slå opp Henrik Ibsen på snl", "hva sier store norske leksikon om fotosyntese?", "søk i leksikonet etter Bergen",
        "les snl-artikkelen om vikingtiden",
    ],
}

TYPO = {"hva": ["ka", "va", "hv"], "jeg": ["je", "eg"], "har": ["ha"], "det": ["de"], "ikke": ["ikkje", "ikk"],
        "hvordan": ["koss", "hvorden"], "morgen": ["morra"], "skal": ["ska"]}


def fill(t: str, r: random.Random) -> str:
    for key, pool in (("{dag}", DAG), ("{tid}", TID), ("{hva}", HVA), ("{artist}", ARTIST), ("{film}", FILM),
                      ("{bok}", BOK), ("{url}", URL), ("{modell}", MODELL), ("{sted}", STED)):
        while key in t:
            t = t.replace(key, r.choice(pool), 1)
    return t


def noise(t: str, r: random.Random) -> str:
    if r.random() < 0.3:
        t = t.lower()
    if r.random() < 0.2:
        t = t.rstrip("?!.")
    if r.random() < 0.15:
        words = t.split()
        for i, w in enumerate(words):
            if w in TYPO and r.random() < 0.6:
                words[i] = r.choice(TYPO[w])
        t = " ".join(words)
    if r.random() < 0.1:
        t = t.capitalize() + r.choice(["", " takk", " :)", " pls"])
    return t


def generate(seed: int = 7, per_template: int = 6) -> list[dict]:
    r = random.Random(seed)
    out, seen = [], set()
    for label, templates in I.items():
        for ti, t in enumerate(templates):
            n = per_template if "{" in t else 2
            for _ in range(n):
                text = noise(fill(t, r), r)
                if text not in seen:
                    seen.add(text)
                    out.append({"text": text, "label": label, "template": f"{label}#{ti}"})
    r.shuffle(out)
    return out


if __name__ == "__main__":
    for row in generate():
        print(json.dumps(row, ensure_ascii=False))
