"""Internet Archive: filmer, musikk (også Live Music Archive), lydbøker og tekster. Nesten alt har en torrent.
Bare innhold med fri lisens eller i public domain tas med (licenseurl satt, eller samlinger som er fri)."""

from __future__ import annotations

from olympus.sources import Result

NAME = "Internet Archive"
KINDS = {"music", "film", "show", "audiobook", "ebook"}

MEDIATYPE = {"music": "audio", "film": "movies", "show": "movies", "audiobook": "audio", "ebook": "texts"}
# Samlinger der innholdet er fritt å laste ned selv uten licenseurl på hvert element.
FREE_COLLECTIONS = {"etree", "librivoxaudio", "feature_films", "prelinger", "opensource_movies", "gutenberg",
                    "classic_tv", "publicdomainmovies", "audio_bookspoetry", "78rpm"}


async def search(client, query: str, kind: str | None) -> list[Result]:
    flt = f" AND mediatype:{MEDIATYPE[kind]}" if kind else " AND mediatype:(audio OR movies OR texts)"
    if kind == "audiobook":
        flt += " AND collection:(librivoxaudio OR audio_bookspoetry)"
    docs: list[dict] = []
    # Først treff i tittelen (mest brukte først), så fritekst hvis det gir for lite.
    for q in (f"title:({query}){flt}", f"({query}){flt}"):
        r = await client.get("https://archive.org/advancedsearch.php", params={
            "q": q, "fl[]": ["identifier", "title", "mediatype", "year", "creator", "licenseurl", "item_size", "collection"],
            "rows": 15, "sort[]": "downloads desc", "output": "json"})
        r.raise_for_status()
        seen = {d["identifier"] for d in docs}
        docs += [d for d in r.json().get("response", {}).get("docs", []) if d["identifier"] not in seen]
        if len(docs) >= 5:
            break
    out = []
    for d in docs:
        cols = set(d.get("collection") or [])
        lic = d.get("licenseurl") or ""
        if not lic and not cols & FREE_COLLECTIONS:
            continue  # ukjent lisens: ikke vis
        ident = d["identifier"]
        mt = d.get("mediatype")
        k = kind or ("film" if mt == "movies" else "ebook" if mt == "texts" else "audiobook" if "librivoxaudio" in cols else "music")
        creator = d.get("creator") or ""
        out.append(Result(
            title=str(d.get("title") or ident), kind=k, source=NAME, page=f"https://archive.org/details/{ident}",
            torrent=f"https://archive.org/download/{ident}/{ident}_archive.torrent",
            size=int(d.get("item_size") or 0), year=int(str(d.get("year"))[:4]) if str(d.get("year") or "")[:4].isdigit() else None,
            creator=creator if isinstance(creator, str) else ", ".join(creator), license=lic or "public domain / fri samling",
            extra={"identifier": ident}))
    return out
