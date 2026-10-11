"""Filminfo fra TMDb (beskrivelse, sjanger, skuespillere, vurdering, plakat). Trenger gratis nøkkel:
`olympus auth login tmdb` (API Read Access Token «eyJ…» eller v3-nøkkel). Norsk tekst når TMDb har den, ellers engelsk.

Infoen lagres ved filmen som «Film.tmdb.json» (serier: «<serie>/tmdb.json»), så den overlever at biblioteket bygges
på nytt og forsvinner med filmen.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w500"


def meta_file(video_path: Path, show: str | None) -> Path:
    return video_path.parent / "tmdb.json" if show else video_path.with_name(video_path.stem + ".tmdb.json")


def read_meta(video_path: Path, show: str | None) -> dict[str, Any] | None:
    try:
        return json.loads(meta_file(video_path, show).read_text())
    except (OSError, ValueError):
        return None


def _auth(key: str) -> tuple[dict[str, str], dict[str, str]]:
    """(headere, parametre): v4-token som Bearer, v3-nøkkel som api_key."""
    if key.startswith("eyJ"):
        return {"Authorization": f"Bearer {key}", "Accept": "application/json"}, {}
    return {"Accept": "application/json"}, {"api_key": key}


def lookup(c: Any, key: str, kind: str, title: str, year: int | None = None) -> dict[str, Any] | None:
    """kind: film | episode (da er title serienavnet). None = ikke funnet."""
    head, base = _auth(key)
    tv = kind == "episode"
    params = {**base, "query": title, "language": "nb-NO", "include_adult": "false"}
    if year and not tv:
        params["year"] = str(year)
    r = c.get(f"{API}/search/{'tv' if tv else 'movie'}", params=params, headers=head)
    r.raise_for_status()
    hits = r.json().get("results") or []
    if not hits and year and not tv:  # årstallet i filnavnet kan være feil
        params.pop("year")
        r = c.get(f"{API}/search/movie", params=params, headers=head)
        r.raise_for_status()
        hits = r.json().get("results") or []
    if not hits:
        return None
    tid = hits[0]["id"]
    path = f"{API}/{'tv' if tv else 'movie'}/{tid}"
    d = c.get(path, params={**base, "language": "nb-NO", "append_to_response": "credits"}, headers=head)
    d.raise_for_status()
    det = d.json()
    overview = det.get("overview") or ""
    if not overview:  # ingen norsk beskrivelse: engelsk
        en = c.get(path, params={**base, "language": "en-US"}, headers=head)
        if en.status_code == 200:
            overview = en.json().get("overview") or ""
    cast = [p.get("name") for p in (det.get("credits") or {}).get("cast", [])[:6] if p.get("name")]
    date = det.get("release_date") or det.get("first_air_date") or ""
    return {"tmdb_id": tid, "title": det.get("title") or det.get("name") or title, "overview": overview[:2000],
            "genres": [g["name"] for g in det.get("genres", [])][:5], "cast": cast,
            "rating": round(float(det.get("vote_average") or 0), 1), "year": int(date[:4]) if date[:4].isdigit() else None,
            "poster": f"{IMG}{det['poster_path']}" if det.get("poster_path") else ""}
