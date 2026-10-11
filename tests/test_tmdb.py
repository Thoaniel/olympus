"""TMDb: oppslag (norsk, engelsk som reserve), info ved filmen (Film.tmdb.json) og plakat."""
import json

import httpx

from olympus import tmdb


def fake_tmdb():
    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/3/search/movie":
            assert req.headers["authorization"] == "Bearer eyJtest"
            return httpx.Response(200, json={"results": [{"id": 438631}]})
        if p == "/3/movie/438631" and req.url.params.get("language") == "nb-NO":
            return httpx.Response(200, json={"title": "Dune", "overview": "", "genres": [{"name": "Sci-fi"}],
                                             "vote_average": 7.84, "release_date": "2021-09-15", "poster_path": "/p.jpg",
                                             "credits": {"cast": [{"name": "Timothée Chalamet"}, {"name": "Zendaya"}]}})
        if p == "/3/movie/438631":
            return httpx.Response(200, json={"overview": "Paul Atreides …"})
        if req.url.host == "image.tmdb.org":
            return httpx.Response(200, content=b"\xff\xd8bilde", headers={"content-type": "image/jpeg"})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_lookup_norwegian_with_english_fallback():
    info = tmdb.lookup(fake_tmdb(), "eyJtest", "film", "Dune", 2021)
    assert info["title"] == "Dune" and info["overview"] == "Paul Atreides …" and info["rating"] == 7.8
    assert info["cast"] == ["Timothée Chalamet", "Zendaya"] and info["year"] == 2021 and info["poster"].endswith("/p.jpg")


def test_meta_file_next_to_film_or_in_show_folder(tmp_path):
    film = tmp_path / "Dune (2021).mkv"
    assert tmdb.meta_file(film, None).name == "Dune (2021).tmdb.json"
    assert tmdb.meta_file(tmp_path / "S01" / "e1.mkv", "Serie").name == "tmdb.json"
    tmdb.meta_file(film, None).write_text(json.dumps({"title": "Dune"}))
    assert tmdb.read_meta(film, None) == {"title": "Dune"}
