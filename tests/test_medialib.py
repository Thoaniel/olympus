"""Olympus som mediaserver: bibliotek, spillelister, bøker, film (direkte/omkodet, undertekster) og framdrift."""

import json
import shutil
import subprocess
import zipfile

import pytest
from aiohttp.test_utils import TestClient, TestServer

from olympus.medialib import Library
from olympus.tools import ToolCall, Toolbox

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="trenger ffmpeg")


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "quiet", "-y", *args], check=True, timeout=60)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("media")
    album = root / "Musikk" / "Satie" / "Gymnopédies"
    album.mkdir(parents=True)
    for n in (1, 2):
        ff("-f", "lavfi", "-i", f"sine=frequency={300 + n * 100}:duration=2", "-metadata", f"title=Gymnopédie No. {n}",
           "-metadata", "artist=Erik Satie", "-metadata", "album=Gymnopédies", "-metadata", f"track={n}",
           "-metadata", "date=1888", str(album / f"0{n}.mp3"))
    films = root / "Filmer"
    films.mkdir()
    ff("-f", "lavfi", "-i", "testsrc=size=320x240:duration=3:rate=10", "-f", "lavfi", "-i", "sine=duration=3",
       "-c:v", "mpeg4", "-c:a", "mp2", "-shortest", str(films / "Metropolis (1927).avi"))
    (films / "Metropolis (1927).no.srt").write_text("1\n00:00:00,500 --> 00:00:02,000\nHei fra Metropolis\n")
    books = root / "Bøker"
    books.mkdir()
    with zipfile.ZipFile(books / "peer.epub", "w") as z:
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="OEBPS/c.opf"/></rootfiles></container>')
        z.writestr("OEBPS/c.opf", '<package><metadata><dc:title>Peer Gynt</dc:title><dc:creator>Henrik Ibsen</dc:creator>'
                   '</metadata><manifest><item id="a" href="a.html"/></manifest><spine><itemref idref="a"/></spine></package>')
        z.writestr("OEBPS/a.html", "<html><body><h1>Første akt</h1><p>Peer, du lyver!</p></body></html>")
    audiobook = root / "Lydbøker" / "Et dukkehjem"
    audiobook.mkdir(parents=True)
    for n in (1, 2):
        ff("-f", "lavfi", "-i", "sine=duration=1", "-metadata", f"title=Del {n}", "-metadata", "album=Et dukkehjem",
           "-metadata", "artist=Henrik Ibsen", str(audiobook / f"del{n}.mp3"))
    lib = Library(root / "media.db", {"music": [root / "Musikk"], "films": [films], "shows": [root / "Serier"],
                                      "ebooks": [books], "audiobooks": [root / "Lydbøker"]}, root / "cache")
    lib.scan()
    return lib


def test_scan_reads_tags_books_and_films(media):
    assert media.stats() == {"tracks": 2, "albums": 1, "books": 2, "films": 1, "episodes": 0, "playlists": 0}
    album = media.albums()[0]
    assert album["title"] == "Gymnopédies" and album["artist"] == "Erik Satie" and album["year"] == 1888
    assert [t["title"] for t in media.album_tracks(album["id"])] == ["Gymnopédie No. 1", "Gymnopédie No. 2"]
    film = media.videos()["films"][0]
    assert film["title"] == "Metropolis" and film["year"] == 1927 and film["direct"] is False   # avi/mpeg4 omkodes
    assert media.video(film["id"])["subtitles"][0]["lang"] == "no"
    books = {b["title"]: b for b in media.books()}
    assert books["Peer Gynt"]["author"] == "Henrik Ibsen" and books["Peer Gynt"]["format"] == "epub"
    assert len(media.book(books["Et dukkehjem"]["id"])["parts"]) == 2
    assert "Peer, du lyver!" in media.book_text(books["Peer Gynt"]["id"])[0][1]
    assert media.scan()["musikk"] == 2                       # ny skanning leser ingenting på nytt


def test_playlists_and_songs_for(media):
    ids = [t["id"] for t in media.album_tracks(media.albums()[0]["id"])]
    p = media.playlist_create("Rolig", [ids[1]])
    media.playlist_add(p["id"], ids)
    assert [t["title"] for t in media.playlist_tracks(p["id"])] == ["Gymnopédie No. 2", "Gymnopédie No. 1"]
    assert [t["id"] for t in media.songs_for("rolig")] == [ids[1], ids[0]]           # spilleliste etter navn
    assert len(media.songs_for("gymnopédies")) == 2                                    # album
    assert media.playlist_delete(p["id"]) and media.playlists() == []


@pytest.mark.asyncio
async def test_media_api_streams_with_range_transcodes_and_tracks_progress(media):
    from aiohttp import web

    from olympus.web.media_api import add_media_routes

    async def changed(_w):
        pass

    app = web.Application()
    add_media_routes(app, media, changed)
    async with TestClient(TestServer(app)) as c:
        track = media.album_tracks(media.albums()[0]["id"])[0]
        r = await c.get(f"/api/v1/media/track/{track['id']}/stream", headers={"Range": "bytes=0-99"})
        assert r.status == 206 and len(await r.read()) == 100                         # spoling virker
        film = media.videos()["films"][0]
        r = await c.get(f"/api/v1/media/video/{film['id']}/transcode?start=1&h=240")
        data = await r.read()
        assert r.status == 200 and b"ftyp" in data[:64] and len(data) > 1000          # fragmentert MP4
        vtt = await (await c.get(f"/api/v1/media/video/{film['id']}/sub/0")).text()
        assert vtt.startswith("WEBVTT") and "Hei fra Metropolis" in vtt
        await c.put(f"/api/v1/media/progress/{film['id']}", json={"position": 1.5, "duration": 3})
        assert (await (await c.get("/api/v1/media/videos")).json())["films"][0]["position"] == 1.5
        r = await c.post("/api/v1/media/playlists", json={"name": "Middag", "tracks": [track["id"]]})
        assert r.status == 201
        pid = (await r.json())["id"]
        r = await c.patch(f"/api/v1/media/playlist/{pid}", json={"add": [t["id"] for t in media.album_tracks(track["album_id"])]})
        assert len(await r.json()) == 2
        media.playlist_delete(pid)


@pytest.mark.asyncio
async def test_zeus_media_tools_read_freely_but_playlist_changes_need_approval(media, tmp_path):
    tb = Toolbox(tmp_path, lib=media)
    out = (await tb.run(ToolCall("1", "media_search", json.dumps({"query": "metropolis"})))).output
    assert "Metropolis (1927)" in out
    create = ToolCall("2", "playlist", json.dumps({"action": "create", "name": "Satie", "query": "gymnopédies"}))
    assert tb.approval(create).needed and "Satie" in tb.approval(create).preview
    assert not tb.approval(ToolCall("3", "playlist", json.dumps({"action": "list"}))).needed
    r = await tb.run(create)
    assert r.ok and "2 spor" in r.output
    for p in media.playlists():
        media.playlist_delete(p["id"])


def test_audio_tool_reads_freely_but_changes_need_approval(tmp_path):
    tb = Toolbox(tmp_path)
    assert not tb.approval(ToolCall("1", "audio", json.dumps({"action": "status"}))).needed
    appr = tb.approval(ToolCall("2", "audio", json.dumps({"action": "volume", "percent": 30})))
    assert appr.needed and "30" in appr.preview


def test_fetch_art_saves_album_cover_and_film_poster_once(media, monkeypatch):
    calls = []
    monkeypatch.setattr("olympus.medialib.musicbrainz_cover", lambda c, artist, album: calls.append(album) or b"\xff\xd8JPEG")
    monkeypatch.setattr("olympus.medialib.wikipedia_image", lambda c, q: calls.append(q) or b"\xff\xd8JPEG")
    monkeypatch.setattr("olympus.medialib.time.sleep", lambda s: None)
    got = media.fetch_art()
    assert got == {"album": 1, "film": 1}
    album = media.albums()[0]
    assert album["cover_track"] and media.cover(album["cover_track"])[0].startswith(b"\xff\xd8")
    film = media.videos()["films"][0]
    assert media.poster(film["id"]).name == "Metropolis (1927)-poster.jpg"     # nedlastet plakat foran stillbilde
    assert any("Metropolis 1927 film" in c for c in calls)
    calls.clear()
    assert media.fetch_art() == {"album": 0, "film": 0} and calls == []        # ikke spurt på nytt


def _lib(root):
    return Library(root / "media.db", {"music": [root / "Musikk"], "films": [root / "Filmer"], "shows": [],
                                       "ebooks": [], "audiobooks": []}, root / "cache")


def test_playlists_and_progress_survive_moves_and_index_rebuild(tmp_path):
    music = tmp_path / "Musikk" / "A"
    music.mkdir(parents=True)
    ff("-f", "lavfi", "-i", "sine=duration=1", "-metadata", "title=Sang", "-metadata", "artist=X", "-metadata", "album=A",
       str(music / "sang.mp3"))
    lib = _lib(tmp_path)
    lib.scan()
    tid = lib.search("sang")["tracks"][0]["id"]
    p = lib.playlist_create("Liste", [tid])
    lib.set_progress(tid, 42)
    (tmp_path / "Musikk" / "Flyttet").mkdir()
    (music / "sang.mp3").rename(tmp_path / "Musikk" / "Flyttet" / "nytt-navn.mp3")   # flytt og gi nytt navn
    lib.scan()
    assert [t["id"] for t in lib.playlist_tracks(p["id"])] == [tid]                    # samme id etter flytting
    assert lib.progress(tid)["position"] == 42
    lib.db.close()
    (tmp_path / "media.db").unlink()                                                     # indeksen bygges på nytt …
    lib = _lib(tmp_path)
    lib.scan()
    assert lib.playlists()[0]["name"] == "Liste" and len(lib.playlist_tracks(p["id"])) == 1   # … dine data står


def test_legacy_media_db_is_migrated(tmp_path):
    import sqlite3

    music = tmp_path / "Musikk"
    music.mkdir()
    ff("-f", "lavfi", "-i", "sine=duration=1", "-metadata", "title=Gammel", "-metadata", "artist=Y", str(music / "g.mp3"))
    old = sqlite3.connect(tmp_path / "media.db")                                          # første utgave: id fra filbanen
    old.executescript("""CREATE TABLE tracks (id TEXT PRIMARY KEY, path TEXT); CREATE TABLE playlists (id TEXT, name TEXT,
        created REAL, updated REAL); CREATE TABLE playlist_items (playlist_id TEXT, pos INTEGER, track_id TEXT);
        CREATE TABLE progress (item_id TEXT PRIMARY KEY, position REAL, duration REAL, updated REAL);""")
    old.execute("INSERT INTO tracks VALUES ('gammel-id', ?)", (str(music / "g.mp3"),))
    old.execute("INSERT INTO playlists VALUES ('p1', 'Favoritter', 0, 0)")
    old.execute("INSERT INTO playlist_items VALUES ('p1', 0, 'gammel-id')")
    old.commit()
    old.close()
    lib = _lib(tmp_path)
    assert lib.playlists()[0]["name"] == "Favoritter"
    assert lib.playlist_tracks("p1")[0]["title"] == "Gammel"                             # koblet til ny, stabil id
    assert (tmp_path / "media.db.v0").exists()                                           # den gamle er tatt vare på
