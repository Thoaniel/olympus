"""LibriVox: lydbøker lest inn av frivillige, alle i public domain. Hentes som zip (direkte)."""

from __future__ import annotations

from olympus.sources import Result

NAME = "LibriVox"
KINDS = {"audiobook"}


async def search(client, query: str, kind: str | None) -> list[Result]:
    r = await client.get("https://librivox.org/api/feed/audiobooks", params={
        "title": f"^{query}", "format": "json", "limit": 10, "extended": 1})
    if r.status_code == 404:
        return []
    r.raise_for_status()
    out = []
    for b in r.json().get("books", []):
        authors = ", ".join(f"{a.get('first_name', '')} {a.get('last_name', '')}".strip() for a in b.get("authors", []))
        out.append(Result(title=b.get("title", ""), kind="audiobook", source=NAME, page=b.get("url_librivox", ""),
                          direct=b.get("url_zip_file", ""), creator=authors, license="public domain",
                          year=int(b["copyright_year"]) if str(b.get("copyright_year", "")).isdigit() else None,
                          extra={"language": b.get("language", ""), "time": b.get("totaltime", "")}))
    return out
