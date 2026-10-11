"""Én spiller for PC-en (pause, forrige, posisjon, overlevering til telefonen) og PC-fanens API (status og
fjernkontroll). ffmpeg og systemkommandoer kjøres aldri for ekte her."""

import asyncio
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from olympus.medialib import SAFE_INPUT, Library
from olympus.mediaserver import Player
from olympus.web.server import get_token, make_app


def add_tracks(lib: Library, n: int = 3) -> None:
    for i in range(n):
        lib.db.execute("INSERT OR REPLACE INTO tracks (id, path, title, artist, album, album_artist, album_id, duration, "
                       "track_no, mtime) VALUES (?, ?, ?, 'Artist', 'Album', 'Artist', 'a1', 200, ?, 0)",
                       (f"t{i}", f"/musikk/{i}.ogg", f"Sang {i}", i))
    lib.db.commit()


async def fake_spawn(self, path, start):  # sover i stedet for å spille
    self.spawned = [*getattr(self, "spawned", []), (path, start)]
    return await asyncio.create_subprocess_exec("sleep", "30")


async def until(cond, timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "tidsfrist"
        await asyncio.sleep(0.02)


@pytest.fixture
def lib(tmp_path):
    lb = Library(tmp_path / "media.db", {"music": [tmp_path / "Musikk"]}, tmp_path / "cache", tmp_path / "lab.db")
    add_tracks(lb)
    return lb


def test_ffmpeg_starts_at_position_before_input_and_keeps_safe_input():
    args = Player()._spawn_args("/x.ogg", 42.5)
    i = args.index("-i")
    assert args[args.index("-ss") + 1] == "42.50" and args.index("-ss") < i
    assert args[i - len(SAFE_INPUT):i] == list(SAFE_INPUT)
    assert "-ss" not in Player()._spawn_args("/x.ogg", 0)


@pytest.mark.asyncio
async def test_player_pause_resume_next_prev_stop(lib, monkeypatch):
    monkeypatch.setattr(Player, "_spawn", fake_spawn)
    plays = []
    monkeypatch.setattr(lib, "record_play", lambda tid, listened, dur, src: plays.append((tid, listened, src)) or True)
    events = []

    async def listen(st):
        events.append(st)

    p = Player(listeners=[listen])
    await p.play(lib, lib.album_tracks("a1"), index=1, position=30)
    await until(lambda: p.current is not None)
    st = p.status()
    assert st["current"]["id"] == "t1" and st["index"] == 1 and st["playing"] and not st["paused"]
    assert 30 <= st["position"] < 31 and st["duration"] == 200 and [t["id"] for t in st["queue"]] == ["t0", "t1", "t2"]
    assert "path" not in st["current"] and p.spawned[-1] == ("/musikk/1.ogg", 30)

    assert await p.pause()
    frozen = p.position()
    await asyncio.sleep(0.3)
    assert p.position() == frozen and p.status()["paused"] and not p.status()["playing"]
    assert await p.resume() and not p.paused
    assert p.position() - frozen < 0.1  # pausen telles ikke

    await p.next()
    await until(lambda: p.current is not None and p.current["id"] == "t2")
    assert plays[-1][0] == "t1" and plays[-1][1] < 2 and plays[-1][2] == "pc"  # hørte bare litt (uten pausen)

    await p.prev()  # under 5 s inn: forrige sang
    await until(lambda: p.current is not None and p.current["id"] == "t1")
    p._offset = 10  # «10 s inn»: forrige starter sangen på nytt
    await p.prev()
    await until(lambda: p.spawned[-1] == ("/musikk/1.ogg", 0) and len(p.spawned) == 4)
    assert p.current["id"] == "t1"

    await p.stop()
    assert p.current is None and not p.status()["playing"] and p.status()["queue"] == []
    await asyncio.sleep(0.3)
    assert events and events[-1]["current"] is None  # siste melding: stoppet


@pytest.mark.asyncio
async def test_prev_goes_back_into_the_previous_queue(lib, monkeypatch):
    monkeypatch.setattr(Player, "_spawn", fake_spawn)
    p = Player()
    await p.play(lib, lib.album_tracks("a1")[:1])
    await until(lambda: p.current is not None)
    await p.play(lib, lib.album_tracks("a1")[2:])
    await until(lambda: p.current is not None and p.current["id"] == "t2")
    await p.prev()
    await until(lambda: p.current is not None and p.current["id"] == "t0")
    assert [t["id"] for t in p.tracks] == ["t0", "t2"]
    await p.stop()


@pytest.fixture
def app_lib(fake_config, monkeypatch):
    monkeypatch.setattr(Player, "_spawn", fake_spawn)
    monkeypatch.setattr(Library, "scan", lambda self: {})
    monkeypatch.setattr(Library, "fetch_art", lambda self: {})
    from olympus.medialib import open_library

    lb = open_library(fake_config)
    add_tracks(lb)
    return lb


@pytest.mark.asyncio
async def test_pc_api_play_control_handoff_and_broadcast(fake_config, app_lib):
    from olympus.mediaserver import PLAYER

    app = make_app(fake_config)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/api/v1/pc")).status == 401
        await client.get(f"/?t={get_token(fake_config)}")
        st = await (await client.get("/api/v1/pc")).json()
        assert st["current"] is None and st["playing"] is False and st["queue"] == [] and st["position"] == 0
        assert (await client.post("/api/v1/pc/handoff")).status == 409

        ws = await client.ws_connect("/api/v1/ws", origin=str(client.make_url("/")).rstrip("/"))
        first = [(await ws.receive_json(timeout=5))["type"] for _ in range(4)]
        assert first[-1] == "player"
        for bad in ({"tracks": "t0"}, {"tracks": [1]}, {"album": "a1", "index": -1}, {"album": "a1", "index": "1"},
                    {"album": "a1", "index": 7}, {"album": "a1", "position": -3}, {"album": "a1", "position": "x"},
                    {"album": "a1", "index": True}):
            assert (await client.post("/api/v1/pc/play", json=bad)).status == 400, bad
        r = await client.post("/api/v1/pc/play", json={"tracks": ["t0", "t1", "t2"], "index": 1, "position": 12.5})
        st = await r.json()
        assert r.status == 200 and st["current"]["id"] == "t1" and st["position"] >= 12.5 and st["index"] == 1
        msg = await ws.receive_json(timeout=5)
        while msg["type"] != "player":
            msg = await ws.receive_json(timeout=5)
        assert msg["current"]["id"] == "t1" and {"position", "duration", "paused", "queue", "index"} <= set(msg)

        for action, check in (("pause", lambda s: s["paused"] and not s["playing"]), ("resume", lambda s: s["playing"]),
                              ("next", lambda s: s["current"]["id"] == "t2"), ("prev", lambda s: s["current"]["id"] == "t1")):
            s = await (await client.post("/api/v1/pc/control", json={"action": action})).json()
            assert check(s), (action, s)
        assert (await client.post("/api/v1/pc/control", json={"action": "dans"})).status == 400

        h = await (await client.post("/api/v1/pc/handoff")).json()
        assert [t["id"] for t in h["tracks"]] == ["t1", "t2"] and h["index"] == 0 and h["position"] >= 0
        assert PLAYER.current is None and (await (await client.get("/api/v1/pc")).json())["playing"] is False

        assert (await client.get("/api/v1/media/mix?limit=500")).status == 400   # utenfor 1–100
        mix = await (await client.get("/api/v1/media/mix?limit=100")).json()
        assert {t["id"] for t in mix["items"]} <= {"t0", "t1", "t2"} and mix["items"] and "path" not in mix["items"][0]
        assert (await client.get("/api/v1/media/mix?limit=x")).status == 400
        albums = await (await client.get("/api/v1/media/albums")).json()
        assert albums[0]["like"] == 0
        await ws.close()


@pytest.mark.asyncio
async def test_pc_actions_use_fixed_commands_and_sleep_needs_confirm(fake_config, app_lib, monkeypatch):
    from olympus import pcctl

    ran: list[list[str]] = []

    def fake_run(argv, timeout=10):
        ran.append(argv)
        if argv[0] == "loginctl" and argv[1] == "show-user":
            return 0, "4\n"
        if argv[0] == "pgrep":
            return 1, ""
        return 0, "ok"

    monkeypatch.setattr(pcctl, "run_argv", fake_run)
    monkeypatch.setattr(pcctl, "desktop", lambda: "hyprland")
    app = make_app(fake_config)
    async with TestClient(TestServer(app)) as client:
        assert (await client.post("/api/v1/pc/action", json={"action": "lock"})).status == 401
        await client.get(f"/?t={get_token(fake_config)}")
        for bad in ({"action": "rm -rf"}, {"action": ["lock"]}, {}):
            assert (await client.post("/api/v1/pc/action", json=bad)).status == 400
        r = await client.post("/api/v1/pc/action", json={"action": "sleep"})
        assert r.status == 400 and "bekreftes" in (await r.json())["error"]
        assert (await client.post("/api/v1/pc/action", json={"action": "sleep", "confirm": "ja"})).status == 400
        assert not any(a[0] == "systemctl" for a in ran)
        assert (await client.post("/api/v1/pc/action", json={"action": "lock"})).status == 200
        assert ran[-1] == ["loginctl", "lock-session", "4"]
        await client.post("/api/v1/pc/action", json={"action": "screen_off"})
        assert ran[-1] == ["hyprctl", "dispatch", "hl.dsp.dpms({ action = 'off' })"]
        await client.post("/api/v1/pc/action", json={"action": "screen_on"})
        assert ran[-1][-1] == "hl.dsp.dpms({ action = 'on' })"
        ran.clear()
        await client.post("/api/v1/pc/action", json={"action": "open_olympus"})
        assert ran[1][2].startswith("hl.dsp.exec_cmd('") and ran[1][2].endswith("scripts/ws2.sh')")
        r = await client.post("/api/v1/pc/action", json={"action": "sleep", "confirm": True})
        assert r.status == 200
        await asyncio.sleep(1.3)
        assert ran[-1] == ["systemctl", "suspend"]  # (falsk – ingenting sov)

        monkeypatch.setattr(pcctl, "run_argv", lambda argv, timeout=10: (1, "nei"))
        assert (await client.post("/api/v1/pc/action", json={"action": "screen_on"})).status == 502

        monkeypatch.setattr("olympus.statusview._active", lambda unit: unit != "transmission")
        info = await (await client.get("/api/v1/pc/info")).json()
        assert {"host", "gpu", "cpu", "disk_pct", "ai", "model", "services", "backup", "devices"} <= set(info)
        assert info["model"]["id"] == "qwen35-9b" and info["model"]["cloud"] is False and info["devices"] == 0
        assert info["services"] == {"olympus-web": True, "transmission": False, "tailscaled": True}



def test_pc_commands_for_every_platform(monkeypatch):
    from olympus import pcctl

    for d, lock, off in (("windows", ["rundll32.exe", "user32.dll,LockWorkStation"], "powershell"),
                         ("macos", ["osascript", "-e", 'tell application "System Events" to keystroke "q" using {control down, command down}'], "pmset"), ("sway", None, "swaymsg")):
        monkeypatch.setattr(pcctl, "desktop", lambda d=d: d)
        monkeypatch.setattr(pcctl, "session_id", lambda: None)
        if lock:
            assert pcctl.commands("lock") == [lock]
        assert pcctl.commands("screen_off")[0][0] == off
        assert pcctl.commands("open_olympus") and pcctl.commands("sleep")
