# Søkekilder for Zeus

Her bestemmer du hvor Zeus leter når du ber ham finne musikk, film, serier eller bøker
(verktøyet `media_find`). Hver kilde er én Python-fil.

| Fil | Kilde | Har | Hentes som |
|---|---|---|---|
| `archive.py` | Internet Archive | film, musikk, lydbøker, e-bøker | torrent (Transmission) |
| `librivox.py` | LibriVox | lydbøker | zip (direkte) |
| `gutenberg.py` | Project Gutenberg | e-bøker | EPUB (direkte) |
| `commons.py` | Wikimedia Commons | musikk, film | fil (direkte) |
| `jamendo.py` | Jamendo | musikk (CC) | zip – trenger `jamendo_client_id` i `~/.olympus/sources.toml` |

## Lage en ny kilde

1. Lag `minkilde.py` her:

```python
from olympus.sources import Result

NAME = "Min kilde"
KINDS = {"music", "film"}          # music | film | show | audiobook | ebook

async def search(client, query, kind):
    r = await client.get("https://…/api/search", params={"q": query})
    r.raise_for_status()
    return [Result(title=x["title"], kind="film", source=NAME, page=x["url"],
                   torrent=x["torrent_url"])          # eller direct=… for vanlig nedlasting
            for x in r.json()["items"]]
```

2. Legg navnet til i `SOURCES` i `__init__.py`.
3. Start Olympus på nytt (`systemctl --user restart olympus-web`).

`client` er en `httpx.AsyncClient`. Et treff med `torrent` (URL til .torrent eller `magnet:`) hentes
med Transmission; med `direct` lastes fila ned rett. Når nedlastingen er ferdig, sorteres den inn i
biblioteket (`~/Filmer`, `~/Serier`, `~/Musikk`, `~/Lydbøker`, `~/Bøker`), og cover/plakat hentes.

Test en kilde alene:

```fish
cd ~/dev/olympus; uv run python -c "import asyncio; from olympus.sources import search_all; print(asyncio.run(search_all('metropolis', 'film', ['archive'])))"
```
