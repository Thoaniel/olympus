"""Jamendo: uavhengig musikk med Creative Commons-lisens. Krever en gratis client_id fra
https://devportal.jamendo.com – legg den i ~/.olympus/sources.toml som `jamendo_client_id = "…"`. Uten den hoppes
kilden over."""

from __future__ import annotations

import tomllib
from pathlib import Path

from olympus.sources import Result

NAME = "Jamendo"
KINDS = {"music"}


def _client_id() -> str:
    try:
        return tomllib.loads((Path.home() / ".olympus" / "sources.toml").read_text()).get("jamendo_client_id", "")
    except (OSError, tomllib.TOMLDecodeError):
        return ""


async def search(client, query: str, kind: str | None) -> list[Result]:
    cid = _client_id()
    if not cid:
        return []
    r = await client.get("https://api.jamendo.com/v3.0/albums/", params={
        "client_id": cid, "format": "json", "limit": 10, "namesearch": query, "include": "licenses"})
    r.raise_for_status()
    out = []
    for a in r.json().get("results", []):
        out.append(Result(title=a.get("name", ""), kind="music", source=NAME, page=a.get("shareurl", ""),
                          direct=a.get("zip", ""), creator=a.get("artist_name", ""), license="Creative Commons",
                          year=int(a["releasedate"][:4]) if a.get("releasedate") else None))
    return out
