"""Nedlastinger: import inn i biblioteket (riktig mappe/navn, ett lydformat, trygg utpakking) og Zeus' godkjenning."""

import json
import zipfile

import pytest

from olympus.downloads import Downloads, _unzip_all
from olympus.lab import Lab
from olympus.sources import Result
from olympus.tools import ToolCall, Toolbox


class FakeTransmission:
    def __init__(self):
        self.added, self.removed = [], []

    def add(self, torrent, folder):
        folder.mkdir(parents=True, exist_ok=True)
        self.added.append((torrent, folder))
        return "hash1"

    def info(self, hashes):
        return {"hash1": {"percentDone": 1.0, "isFinished": True, "error": 0}}

    def remove(self, h, delete_data):
        self.removed.append(h)


class FakeLib:
    def scan(self):
        return {}

    def fetch_art(self, limit=10):
        return {}


@pytest.fixture
def dl(tmp_path, monkeypatch):
    monkeypatch.setattr("olympus.downloads.STAGING", tmp_path / "Nedlastinger")
    d = Downloads(Lab(tmp_path / "lab.db"), FakeLib(), {k: tmp_path / v for k, v in
                  {"music": "Musikk", "film": "Filmer", "show": "Serier", "audiobook": "Lydbøker", "ebook": "Bøker"}.items()})
    d.tr = FakeTransmission()
    return d


@pytest.mark.asyncio
async def test_torrent_film_is_imported_with_subtitles_and_cleaned_after_seeding(dl, tmp_path):
    row = dl.start("film", "Metropolis", torrent="https://archive.org/x.torrent", year=1927, source="Internet Archive")
    folder = dl.tr.added[0][1]
    (folder / "Metropolis_512kb.mp4").write_bytes(b"x" * 10)
    (folder / "Metropolis.mkv").write_bytes(b"x" * 100)                       # størst = hovedfilen
    (folder / "Metropolis.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhei\n")
    told = []

    async def notify(t):
        told.append(t)

    dl.notify = notify
    assert await dl.poll() == ["Metropolis"]
    dest = tmp_path / "Filmer" / "Metropolis (1927)"
    assert (dest / "Metropolis (1927).mkv").stat().st_size == 100
    assert (dest / "Metropolis (1927).en.srt").exists()
    assert (dest / "Metropolis (1927).mkv").stat().st_nlink == 2           # hardlenke: torrenten deler videre
    assert told == ["↓ «Metropolis» er klar i biblioteket."]
    assert dl.get(row["id"])["status"] == "deler"
    await dl.poll()                                                         # delt ferdig → rydd
    assert dl.get(row["id"])["status"] == "ferdig" and dl.tr.removed == ["hash1"] and not folder.exists()
    assert (dest / "Metropolis (1927).mkv").exists()                        # biblioteket beholder sin kopi


def test_music_keeps_only_best_format(dl, tmp_path):
    row = dl.start("music", "Live 1977", torrent="magnet:?xt=urn:btih:abc", creator="Grateful Dead")
    folder = dl.tr.added[0][1]
    for n in (1, 2):
        (folder / f"d1t0{n}.flac").write_bytes(b"f")
        (folder / f"d1t0{n}.mp3").write_bytes(b"m")
    dl._set(row["id"], status="lastet ned")
    dest = dl.import_item(row["id"])
    assert sorted(p.name for p in dest.iterdir()) == ["d1t01.flac", "d1t02.flac"]
    assert dest == tmp_path / "Musikk" / "Grateful Dead" / "Live 1977"


def test_zip_extraction_never_escapes_folder(tmp_path):
    folder = tmp_path / "dl"
    folder.mkdir()
    with zipfile.ZipFile(folder / "bok.zip", "w") as z:
        z.writestr("kapittel1.mp3", b"ok")
        z.writestr("../../ute.txt", b"ondsinnet")
        z.writestr("/etc/absolutt.txt", b"ondsinnet")
    _unzip_all(folder)
    assert (folder / "bok" / "kapittel1.mp3").exists()
    assert not (tmp_path / "ute.txt").exists() and not list(tmp_path.rglob("absolutt.txt"))


@pytest.mark.asyncio
async def test_zeus_needs_approval_to_download_and_sees_license(dl, tmp_path):
    tb = Toolbox(tmp_path, dl=dl)
    res = Result(title="Nosferatu", kind="film", source="Internet Archive", torrent="https://archive.org/n.torrent",
                 year=1922, license="public domain", size=2_500_000_000)
    tb.found[res.id] = res
    call = ToolCall("1", "media_get", json.dumps({"id": res.id}))
    appr = tb.approval(call)
    assert appr.needed and "public domain" in appr.preview and "2.5 GB" in appr.preview and "torrent" in appr.preview
    assert not tb.approval(ToolCall("2", "media_find", json.dumps({"query": "x"}))).needed
    r = await tb.run(call)
    assert r.ok and dl.list()[0]["title"] == "Nosferatu"


def test_import_ignores_symlinks_pointing_outside(dl, tmp_path):
    secret = tmp_path / "hemmelig.txt"
    secret.write_text("privat")
    row = dl.start("ebook", "Bok", direct="https://example.org/bok.epub")
    folder = tmp_path / "Nedlastinger" / "direkte" / row["id"]
    folder.mkdir(parents=True)
    (folder / "bok.epub").symlink_to(secret)
    dl._set(row["id"], status="lastet ned")
    with pytest.raises(ValueError):
        dl.import_item(row["id"])              # symlenken ut av mappa tas ikke med
    assert not list((tmp_path / "Bøker").glob("*")) if (tmp_path / "Bøker").exists() else True


@pytest.mark.asyncio
async def test_approved_hit_cannot_be_swapped_for_other_title(dl, tmp_path):
    tb = Toolbox(tmp_path, dl=dl)
    res = Result(title="Nosferatu", kind="film", source="Internet Archive", torrent="https://archive.org/n.torrent", year=1922)
    tb.found[res.id] = res
    await tb.run(ToolCall("1", "media_get", json.dumps({"id": res.id, "title": "Noe annet", "kind": "music"})))
    d = dl.list()[0]
    assert d["title"] == "Nosferatu" and d["kind"] == "film"
    assert not (await tb.run(ToolCall("2", "media_get", json.dumps({"id": "finnes-ikke"})))).ok


def test_cancelled_ytdlp_leaves_no_half_files(tmp_path):
    from olympus.downloads import _rm_partials

    root = tmp_path / "Musikk"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "gammel.mp3").write_bytes(b"x")
    (album / "sang.f123.mp3").write_bytes(b"x")               # ekte fil med «rart» navn: røres ikke
    (album / "ny.webm.part").write_bytes(b"x")
    (album / "ny.webm.ytdl").write_bytes(b"x")
    (album / "annen.webm.part").write_bytes(b"x")             # en annen nedlasting som pågår: røres ikke
    half = root / "Ny artist" / "Singler"
    half.mkdir(parents=True)
    (half / "sang.m4a.part-Frag3").write_bytes(b"x")
    _rm_partials(root, [str(album / "ny.webm"), str(half / "sang.m4a"), "/etc/passwd"])
    assert (album / "gammel.mp3").exists() and (album / "sang.f123.mp3").exists() and (album / "annen.webm.part").exists()
    assert not (album / "ny.webm.part").exists() and not (album / "ny.webm.ytdl").exists()
    assert not (root / "Ny artist").exists()


@pytest.mark.asyncio
async def test_names_you_typed_become_overrides_and_files_stay_untouched(dl, tmp_path):
    seen = {}
    dl.lib.set_override = lambda f, fields: seen.__setitem__(f.name, fields)
    row = dl.start("music", "ZTEST rart navn", torrent="magnet:?xt=urn:btih:abc", creator="Ukjent")
    dl.set_names(row["id"], {"title": "ZTEST Album", "artist": "ZTEST Artist", "junk": "x"})
    folder = dl.tr.added[0][1]
    (folder / "01.flac").write_bytes(b"f")
    (folder / "02.flac").write_bytes(b"f")
    await dl.poll()
    assert seen == {n: {"album": "ZTEST Album", "album_artist": "ZTEST Artist"} for n in ("01.flac", "02.flac")}
    assert (folder / "01.flac").read_bytes() == b"f"                       # fila (delt via torrent) er urørt
    assert "paths" not in dl.get(row["id"]) and dl.get(row["id"])["named"]["title"] == "ZTEST Album"


@pytest.mark.asyncio
async def test_ytdlp_names_only_touch_its_own_files(dl, tmp_path):
    seen = []
    dl.lib.set_override = lambda f, fields: seen.append(f.name)
    shared = tmp_path / "Musikk" / "ZTEST"
    shared.mkdir(parents=True)
    (shared / "annet.mp3").write_bytes(b"m")                                # en annen nedlastings fil i samme mappe
    (shared / "min.mp3").write_bytes(b"m")
    row = dl.start_ytdlp("music", "https://example.org/x", "ZTEST")
    dl.set_names(row["id"], {"artist": "ZTEST Artist"})
    dl._set(row["id"], status="lastet ned", meta=json.dumps({**json.loads(dl._row(row["id"])["meta"]),
                                                              "path": str(shared), "paths": [str(shared / "min.mp3")]}))
    await dl.poll()
    assert seen == ["min.mp3"]


def test_magnet_trackers_inside_your_network_are_removed(monkeypatch):
    from olympus import netsafe

    monkeypatch.setattr(netsafe, "is_internal_host", lambda h: h in ("router.lan", "finnesikke.invalid"))
    url = ("magnet:?xt=urn:btih:ABCDEF0123&dn=ZTEST%20Film&tr=udp%3A%2F%2Ftracker.example.org%3A1337"
           "&tr=http%3A%2F%2F192.168.1.1%2Fannounce&tr=http%3A%2F%2Frouter.lan%2Fa&tr=udp%3A%2F%2F127.0.0.1%3A80"
           "&x.pe=192.168.50.5:6881&x.pe=8.8.8.8:6881&ws=http%3A%2F%2Ffinnesikke.invalid%2Ff")
    out, dropped = netsafe.safe_magnet(url)
    assert "tracker.example.org" in out and "8.8.8.8" in out and "xt=urn:btih:ABCDEF0123" in out and "dn=ZTEST" in out
    for bad in ("192.168", "router.lan", "127.0.0.1", "192.168.50.5", "finnesikke"):
        assert bad not in out and any(bad in d for d in dropped)


def test_magnet_unknown_keys_and_odd_tracker_forms_are_dropped(monkeypatch):
    from olympus import netsafe

    monkeypatch.setattr(netsafe, "is_internal_host", lambda h: False)
    url = ("magnet:?xt=urn:btih:ABC&TR=http%3A%2F%2Fok.example%2Fa&tr.1=udp%3A%2F%2Fok2.example%3A1"
           "&tr=http%3A%2F%2Fevil.example%40127.0.0.1%2F&tr=file%3A%2F%2F%2Fetc%2Fpasswd&foo=bar")
    out, dropped = netsafe.safe_magnet(url)
    assert "ok.example" in out and "ok2.example" in out and "foo" not in out
    assert "127.0.0.1" not in out and "passwd" not in out and len(dropped) == 3
