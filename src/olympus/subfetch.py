"""Undertekster fra OpenSubtitles (REST-API v1). Trenger API-nøkkel og konto (nedlasting krever innlogging):
`olympus auth login opensubtitles`. Fila lagres ved siden av filmen som «Film.nb.srt» – biblioteket finner den selv.

Treff velges med filens OpenSubtitles-hash først (eksakt samme utgivelse, synkronisert), ellers tittel/sesong/episode.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

from olympus import netsafe

API = "https://api.opensubtitles.com/api/v1"
UA = "Olympus v1.0"
LANGS = {"no": "nb", "nb": "nb", "en": "en", "sv": "sv", "da": "da"}


def movie_hash(path: Path) -> str:
    """OpenSubtitles-hash: størrelse + summen av første og siste 64 kB som 64-biters tall."""
    size = path.stat().st_size
    if size < 131072:
        raise ValueError("for liten fil")
    h = size
    with path.open("rb") as f:
        for pos in (0, size - 65536):
            f.seek(pos)
            buf = f.read(65536)
            for (n,) in struct.iter_unpack("<Q", buf):
                h = (h + n) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


class NotConfigured(Exception):
    pass


def creds(auth: dict[str, Any]) -> dict[str, str]:
    c = auth.get("opensubtitles") or {}
    if not c.get("key"):
        raise NotConfigured("OpenSubtitles er ikke satt opp: kjør «olympus auth login opensubtitles» (gratis konto).")
    return c


def find_and_save(auth: dict[str, Any], video: dict[str, Any], lang: str = "no") -> Path:
    """Finn beste undertekst på språket og lagre den ved filmen. Returnerer stien."""
    c = creds(auth)
    path = Path(video["path"])
    head = {"Api-Key": c["key"], "User-Agent": UA, "Accept": "application/json"}
    params: dict[str, Any] = {"languages": lang}
    try:
        params["moviehash"] = movie_hash(path)
    except (OSError, ValueError):
        pass
    if video.get("kind") == "episode":
        params.update({"query": video.get("show") or "", "season_number": video.get("season"),
                       "episode_number": video.get("episode")})
    else:
        params["query"] = video.get("title") or path.stem
        if video.get("year"):
            params["year"] = video["year"]
    with netsafe.client(timeout=20) as h:
        r = h.get(f"{API}/subtitles", params=params, headers=head)
        r.raise_for_status()
        data = r.json().get("data") or []
        if not data:
            raise FileNotFoundError("Fant ingen undertekster på det språket.")
        # samme utgivelse (hash) først, så flest nedlastinger
        data.sort(key=lambda d: (not d["attributes"].get("moviehash_match"), -int(d["attributes"].get("download_count") or 0)))
        file_id = data[0]["attributes"]["files"][0]["file_id"]
        tok = h.post(f"{API}/login", json={"username": c.get("username", ""), "password": c.get("password", "")},
                     headers={**head, "Content-Type": "application/json"})
        tok.raise_for_status()
        auth_head = {**head, "Authorization": f"Bearer {tok.json()['token']}", "Content-Type": "application/json"}
        dl = h.post(f"{API}/download", json={"file_id": file_id, "sub_format": "srt"}, headers=auth_head)
        dl.raise_for_status()
        link = dl.json()["link"]
    body, _ = netsafe.fetch_bytes(link, max_bytes=5 * 1024 * 1024)
    out = path.with_name(f"{path.stem}.{LANGS.get(lang, lang)}.srt")
    out.write_bytes(body)
    return out
