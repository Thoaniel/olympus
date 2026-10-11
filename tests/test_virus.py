"""Viruskontrollen: programmer forkledd som media stoppes, ClamAV-treff stoppes, rene filer slipper gjennom."""

import json

import pytest

from olympus import virus
from olympus.downloads import Downloads
from olympus.lab import Lab

MEDIA = {".mkv", ".mp3", ".epub"}


def test_disguised_programs_are_caught_without_clamav(tmp_path, monkeypatch):
    monkeypatch.setattr(virus, "clamav_cmd", lambda: None)
    (tmp_path / "Film.mkv").write_bytes(b"MZ\x90\x00 dette er en exe")
    (tmp_path / "sang.mp3").write_bytes(b"ID3\x03 ekte mp3")
    (tmp_path / "Codec-installer.exe").write_bytes(b"MZ")
    r = virus.scan([tmp_path], MEDIA)
    assert not r.clean and "Windows-program forkledd som .mkv" in r.summary()
    assert any(f.endswith(".exe") for f in r.risky_extras)
    (tmp_path / "Film.mkv").write_bytes(b"\x1aE\xdf\xa3 ekte mkv")
    ok = virus.scan([tmp_path], MEDIA)
    assert ok.clean and "ikke sjekket av ClamAV" in ok.summary() and "programfil" in ok.summary()


def test_clamav_hits_are_reported(tmp_path, monkeypatch):
    fake = tmp_path / "clamscan"
    fake.write_text("#!/bin/sh\nfor a in \"$@\"; do last=$a; done\necho \"$last/sang.mp3: Eicar-Test-Signature FOUND\"\nexit 1\n")
    fake.chmod(0o755)
    d = tmp_path / "dl"
    d.mkdir()
    (d / "sang.mp3").write_bytes(b"ID3")
    monkeypatch.setattr(virus, "clamav_cmd", lambda: [str(fake)])
    monkeypatch.setattr(virus, "_nice", lambda cmd: cmd)
    r = virus.scan([d], MEDIA)
    assert not r.clean and r.clamav and "Eicar-Test-Signature" in r.summary()


class FakeTr:
    def __init__(self):
        self.removed = []

    def add(self, torrent, folder):
        folder.mkdir(parents=True, exist_ok=True)
        return "h1"

    def info(self, hashes):
        return {"h1": {"percentDone": 1.0, "isFinished": False, "error": 0}}

    def remove(self, h, delete_data):
        self.removed.append((h, delete_data))


class FakeLib:
    def scan(self):
        return {}

    def fetch_art(self, n=10):
        return {}


@pytest.mark.asyncio
async def test_infected_torrent_is_deleted_and_never_imported(tmp_path, monkeypatch):
    monkeypatch.setattr("olympus.downloads.STAGING", tmp_path / "Nedlastinger")
    monkeypatch.setattr(virus, "clamav_cmd", lambda: None)
    dl = Downloads(Lab(tmp_path / "lab.db"), FakeLib(), {k: tmp_path / v for k, v in
                   {"music": "Musikk", "film": "Filmer", "show": "Serier", "audiobook": "Lydbøker", "ebook": "Bøker"}.items()})
    dl.tr = FakeTr()
    told = []

    async def notify(t):
        told.append(t)

    dl.notify = notify
    row = dl.start("film", "ZTEST Film", torrent="magnet:?xt=urn:btih:abc")
    folder = tmp_path / "Nedlastinger" / "torrent" / row["id"]
    (folder / "ZTEST.Film.mkv").write_bytes(b"MZ trojaner")
    assert await dl.poll() == []
    got = dl.get(row["id"])
    assert got["status"] == "virus" and "forkledd" in got["error"]
    assert dl.tr.removed == [("h1", True)] and not folder.exists() and not (tmp_path / "Filmer").exists()
    assert told and told[0].startswith("⚠ «ZTEST Film»")


def test_real_clamav_finds_eicar_if_installed(tmp_path):
    if virus.clamav_cmd() is None:
        pytest.skip("ClamAV er ikke installert")
    (tmp_path / "sang.mp3").write_text(r"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*")
    r = virus.scan([tmp_path], MEDIA)
    assert r.clamav and not r.clean and "Eicar" in r.summary()
