"""Wikimedia Commons: lyd og video med fri lisens (klassisk musikk, gamle filmer, opptak)."""

from __future__ import annotations

from olympus.sources import Result

NAME = "Wikimedia Commons"
KINDS = {"music", "film"}


async def search(client, query: str, kind: str | None) -> list[Result]:
    ftype = {"music": "audio", "film": "video"}.get(kind or "", "audio|video")
    r = await client.get("https://commons.wikimedia.org/w/api.php", params={
        "action": "query", "generator": "search", "gsrsearch": f"{query} filetype:{ftype}", "gsrnamespace": 6,
        "gsrlimit": 10, "prop": "imageinfo", "iiprop": "url|size|extmetadata|mediatype", "format": "json"})
    r.raise_for_status()
    out = []
    for p in ((r.json().get("query") or {}).get("pages") or {}).values():
        ii = (p.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata") or {}
        media = ii.get("mediatype", "")
        k = "film" if media == "VIDEO" else "music"
        if kind and k != kind:
            continue
        out.append(Result(title=p.get("title", "").removeprefix("File:").rsplit(".", 1)[0], kind=k, source=NAME,
                          page=ii.get("descriptionurl", ""), direct=ii.get("url", ""), size=int(ii.get("size") or 0),
                          creator=(meta.get("Artist") or {}).get("value", "")[:80],
                          license=(meta.get("LicenseShortName") or {}).get("value", "")))
    return out
