"""Project Gutenberg (via Gutendex): over 70 000 e-bøker i public domain, som EPUB."""

from __future__ import annotations

from olympus.sources import Result

NAME = "Project Gutenberg"
KINDS = {"ebook"}
TIMEOUT = 40  # Gutendex er treg


async def search(client, query: str, kind: str | None) -> list[Result]:
    r = await client.get("https://gutendex.com/books/", params={"search": query})
    r.raise_for_status()
    out = []
    for b in r.json().get("results", [])[:10]:
        fmts = b.get("formats", {})
        epub = fmts.get("application/epub+zip", "")
        if not epub:
            continue
        authors = ", ".join(a.get("name", "") for a in b.get("authors", []))
        out.append(Result(title=b.get("title", ""), kind="ebook", source=NAME,
                          page=f"https://www.gutenberg.org/ebooks/{b.get('id')}", direct=epub, creator=authors,
                          license="public domain", extra={"languages": b.get("languages", [])}))
    return out
