"""Ripping: gjenkjenning av plate (lyd-CD/DVD) fra cd-info, MusicBrainz plate-ID og trygge filnavn."""
from olympus import rip

CD_INFO = """Disc mode is listed as: CD-DA
CD-ROM Track List (1 - 3)
  #: MSF       LSN    Type   Green? Copy? Channels Premphasis?
  1: 00:02:00  000000 audio  false  no    2        no
  2: 03:30:52  015652 audio  false  no    2        no
  3: 07:01:10  031435 audio  false  no    2        no
170: 10:44:20  048170 leadout (108 MB raw, 108 MB formatted)
"""


def test_audio_cd_is_recognised(monkeypatch):
    monkeypatch.setattr(rip, "_run", lambda args, timeout=30: CD_INFO if args[0] == "cd-info" else "")
    info = rip.disc_info("/dev/sr0")
    assert info["kind"] == "audio" and info["tracks"] == [(1, 0), (2, 15652), (3, 31435)] and info["leadout"] == 48170


def test_dvd_and_empty(monkeypatch):
    monkeypatch.setattr(rip, "_run", lambda a, timeout=30: "Disc mode is listed as: DVD-ROM\n" if a[0] == "cd-info" else "FILM\n")
    assert rip.disc_info("/dev/sr0") == {"kind": "dvd", "label": "FILM"}
    monkeypatch.setattr(rip, "_run", lambda a, timeout=30: "")
    assert rip.disc_info("/dev/sr0") == {"kind": "empty"}


def test_discid_and_safe_names():
    d = rip.musicbrainz_discid([(1, 0), (2, 15652), (3, 31435)], 48170)
    assert len(d) == 28 and d.endswith("-") and "/" not in d and "+" not in d
    assert d == rip.musicbrainz_discid([(1, 0), (2, 15652), (3, 31435)], 48170)
    assert rip.safe('AC/DC: "Back in Black"') == "ACDC Back in Black"
