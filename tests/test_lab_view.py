"""Lab's på PC-en: samme fem faner som appen, samme samtale med Zeus (med de trygge godkjenningsreglene), den ene
spilleren på PC-en og media med undervalg. Serveren er falsk her – ingenting kjøres for ekte."""

import io
import time
from typing import Any

import pytest
from rich.console import Console
from textual.app import App

from olympus.tui.lab_view import TABS, LabScreen

TRACKS = [{"id": f"t{i}", "title": f"Sang {i}", "artist": "Satie", "album": "Gymnopédies", "album_id": "a1",
           "duration": 180} for i in range(3)]


def playing(paused: bool = False, idx: int = 0) -> dict:
    return {"current": TRACKS[idx], "position": 30.0, "duration": 180.0, "playing": not paused, "paused": paused,
            "queue": TRACKS, "index": idx}


IDLE = {"current": None, "position": 0, "duration": 0, "playing": False, "paused": False, "queue": [], "index": 0}


class FakeApi:
    base = "http://127.0.0.1:9/api/v1"  # ingen server: websocketen får nei med en gang
    cookies = {"olympus_web": "x"}

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.pc = dict(IDLE)
        self.downloads = [{"id": "d1", "title": "Nocturnes", "kind": "music", "method": "torrent", "status": "laster ned",
                           "progress": 0.4, "error": ""},
                          {"id": "d2", "title": "Metropolis", "kind": "film", "method": "direkte", "status": "feilet",
                           "progress": 0.1, "error": "HTTPError"}]

    async def get(self, path: str) -> Any:
        return await self.call("GET", path)

    async def call(self, method: str, path: str, body: dict | None = None) -> Any:
        self.calls.append((method, path, body))
        p = path.split("?")[0]
        if p == "/pc/control":
            act = body["action"]
            self.pc = (IDLE if act == "stop" else playing(paused=act == "pause", idx=1 if act == "next" else 0))
            return self.pc
        if p == "/pc/play":
            self.pc = playing()
            return self.pc
        if p == "/media/inspect":
            return {"url": body["url"], "kind": "music", "method": "ytdlp", "title": "Gymnopédie [bold]1[/]", "artist": "Satie",
                    "year": 1888, "size": 5_000_000, "duration": 200.0, "source": "youtube.com", "legal": False,
                    "warnings": ["Lenken kommer ikke fra en lovlig kilde – last bare ned det du har rett til"],
                    "thumbnail": None, "count": None}
        if p == "/media/add":
            return {"download": {"id": "d9", "title": body.get("title") or "?", "kind": body["kind"], "method": "ytdlp",
                                 "status": "laster ned", "progress": 0, "error": ""}}
        if p == "/downloads" and method == "GET":
            return self.downloads
        if p.startswith("/media/item/") and method == "GET":
            return {"id": "a1", "type": "album", "title": "Gymnopédies", "artist": "Satie", "album": "Gymnopédies",
                    "year": 1888, "genre": "", "files": 3, "size": 9_000_000, "folder": "~/Musikk/Satie/Gymnopédies"}
        if p.startswith("/media/item/") and method == "PATCH":
            return {"id": "a2", "type": "album", **body}
        if p.startswith("/media/item/") and method == "DELETE":
            return {"ok": True, "trashed": 3, "what": "albumet «Gymnopédies»"}
        if p.endswith("/cover/fetch"):
            return {"ok": True, "found": True}
        if p == "/media/scan":
            return {"musikk": 3, "bøker": 0, "video": 0}
        if p.startswith("/downloads/"):
            return {"ok": True}
        return {
            "/pc": self.pc, "/status": {"ai": True, "model": "qwen35-9b", "cloud": False, "models": []},
            "/dashboard": {"events": [], "next_events": [], "continue": [], "server": {"disk_pct": 50, "gpu": 40, "cpu": 30,
                           "ai": True}, "library": {"tracks": 3}, "backup": None, "pc": self.pc},
            "/plan": {"events": [], "seq": 0}, "/log": [],
            "/media/albums": [{"id": "a1", "title": "Gymnopédies [/] [bold]", "artist": "Satie", "tracks": 3, "like": 1}],
            "/media/playlists": [], "/media/videos": {"films": [], "shows": []}, "/media/books": [],
            "/audio": {"outputs": [{"name": "s1", "label": "Høyttaler", "default": True, "muted": False, "volume": 40}],
                       "bluetooth": []},
            "/pc/info": {"host": "olympus", "gpu": {"temp": 40}, "cpu": {"temp": 30}, "disk_pct": 50, "ai": True,
                         "model": {"id": "qwen35-9b", "cloud": False, "loaded": []}, "services": {"olympus-web": True},
                         "backup": None, "devices": 1},
            "/devices": [{"id": "d1", "name": "Pixel 9", "created": time.time(), "last_seen": time.time()}],
        }.get(p, {})


class FakeWs:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, m: dict) -> None:
        self.sent.append(m)


class Host(App):
    def __init__(self, api: FakeApi) -> None:
        super().__init__()
        self.api = api

    def on_mount(self) -> None:
        self.push_screen(LabScreen(self.api))  # type: ignore[arg-type]


def plain(widget) -> str:
    content = getattr(widget, "content", None) or widget.render()
    c = Console(width=200, record=True, file=io.StringIO())
    c.print(content)
    return c.export_text()


def feed_text(s: LabScreen) -> str:
    return "\n".join(plain(w) for w in s.query_one("#zeus-feed").children)


@pytest.mark.asyncio
async def test_lab_has_the_same_five_tabs_and_a_calm_top_bar():
    assert [n for n, _ in TABS] == ["hjem", "zeus", "plan", "media", "pc"]
    api = FakeApi()
    app = Host(api)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        s = app.screen
        head = plain(s.query_one("#lab-head"))
        assert head.startswith("ϟ ZEUS") and "● tilkoblet" in head and "qwen" not in head
        for i, name in enumerate(["hjem", "zeus", "plan", "media", "pc"]):
            await pilot.press(str(i + 1))
            await pilot.pause(0.1)
            if name == "zeus":
                await pilot.press("escape")  # ut av skrivefeltet
            assert s.tab == name
        assert "esc til hjem" in plain(s.query_one("#lab-keys"))
        await pilot.press("escape")
        assert s.tab == "hjem"


@pytest.mark.asyncio
async def test_zeus_tab_shows_the_shared_conversation_live_and_sends():
    api = FakeApi()
    app = Host(api)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        s = app.screen
        ws = FakeWs()
        s.ws = ws
        s.on_server({"type": "hello", "id": "me"})
        s.on_server({"type": "history", "items": [{"kind": "user", "text": "gammel [/] melding"},
                                                  {"kind": "agent", "text": "gammelt svar"}]})
        await pilot.press("2")
        s.on_server({"type": "user", "text": "fra telefonen", "origin": "phone"})
        s.on_server({"type": "chunk", "text": "Hei "})
        s.on_server({"type": "chunk", "text": "der."})
        s.on_server({"type": "done"})
        await pilot.pause(0.2)
        text = feed_text(s)
        assert "gammel [/] melding" in text and "gammelt svar" in text and "fra telefonen" in text
        assert "Hei der." in text and "telefonen / en annen skjerm" in text
        for ch in "hei":
            await pilot.press(ch)
        await pilot.press("enter")
        assert ws.sent[-1] == {"type": "send", "text": "hei"}
        assert s.query_one("#zeus-say").value == ""
        await pilot.press("3")  # tomt felt: tallet bytter fane
        assert s.tab == "plan"


def test_approval_gate_rules_are_shared_with_olympus_zeus():
    from olympus.tui import zeus_window
    from olympus.tui.approvals import ApprovalGate, preview_parts

    assert zeus_window.preview_parts is preview_parts  # én kilde for reglene
    g = ApprovalGate()
    g.add({"id": "a", "tool": "shell", "reason": "[bold]x[/bold]", "preview": "y" * 700, "danger": True})
    p = g.current()
    assert g.displayed == "a" and "⚠ 100 tegn skjult" in g.card(p).plain and "[bold]x[/bold]" in g.card(p).plain
    assert g.decide(True) == ("ignore", None)  # ventetid
    g.arrived["a"] -= 1
    assert g.decide(True)[0] == "view"  # må se hele først
    assert g.view() is p
    assert g.decide(True)[0] == "confirm"  # farlig: første trykk
    g.armed = ("a", g.armed[1] - 5)  # for lenge siden: må bekrefte på nytt
    assert g.decide(True)[0] == "confirm"
    assert g.decide(True) == ("send", p)
    g.shown_again()
    assert g.decide(True) == ("ignore", None)
    assert g.decide(False) == ("send", p)  # avslå går alltid


@pytest.mark.asyncio
async def test_lab_approvals_follow_the_safe_rules():
    api = FakeApi()
    app = Host(api)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        s = app.screen
        ws = FakeWs()
        s.ws = ws
        s.on_server({"type": "approval", "id": "a1", "tool": "shell", "reason": "[bold]kjøre[/bold] [@click=app.quit]x[/]",
                     "preview": "ls [red]", "danger": False})
        s.gate.arrived["a1"] -= 5
        await pilot.press("ctrl+j")  # i en annen fane: ingenting godkjennes uten at du ser det
        assert not ws.sent and "‖" in plain(s.query_one("#lab-tabs"))
        await pilot.press("2")
        await pilot.pause(0.1)
        assert "[bold]kjøre[/bold] [@click=app.quit]x[/]" in plain(s.query_one("#zeus-ask"))  # ren tekst
        await pilot.press("ctrl+j")  # fanen ble nettopp synlig: ventetiden starter på nytt
        assert not ws.sent
        s.gate.arrived["a1"] -= 1
        await pilot.press("enter")  # Enter, mellomrom og bokstaver godkjenner aldri
        await pilot.press("space")
        await pilot.press("escape")
        await pilot.press("enter")
        await pilot.press("j")
        assert not any(m["type"] == "approve" for m in ws.sent)
        await pilot.press("2")
        s.gate.arrived["a1"] -= 1
        await pilot.press("ctrl+j")
        assert ws.sent[-1] == {"type": "approve", "id": "a1", "answer": "yes"}
        s.on_server({"type": "approval_done", "id": "a1"})
        # bare kallet som vises: byttes køen bak panelet, godkjennes ikke det nye
        s.on_server({"type": "approval", "id": "v1", "tool": "shell", "reason": "første", "preview": "ls"})
        s.gate.pending[:] = [{"type": "approval", "id": "v2", "tool": "shell", "reason": "andre", "preview": "rm -rf x"}]
        s.gate.arrived["v2"] = 0
        await pilot.press("ctrl+j")
        assert ws.sent[-1]["id"] == "a1"
        s.gate.pending.clear()
        s.render_zeus()
        # lang forhåndsvisning: står hvor mye som er skjult, og må åpnes først
        s.on_server({"type": "approval", "id": "a2", "tool": "write_file", "reason": "skrive", "preview": "x" * 2000,
                     "danger": False})
        s.gate.arrived["a2"] -= 1
        assert "⚠ 1400 tegn skjult" in plain(s.query_one("#zeus-ask"))
        await pilot.press("ctrl+j")
        assert ws.sent[-1]["id"] == "a1"
        await pilot.press("ctrl+v")
        await pilot.pause(0.1)
        await pilot.press("escape")
        await pilot.press("ctrl+j")
        assert ws.sent[-1] == {"type": "approve", "id": "a2", "answer": "yes"}
        s.on_server({"type": "approval_done", "id": "a2"})
        # farlig: to trykk
        s.on_server({"type": "approval", "id": "a3", "tool": "shell", "reason": "rm", "preview": "rm x", "danger": True})
        s.gate.arrived["a3"] -= 1
        await pilot.press("ctrl+j")
        assert ws.sent[-1]["id"] == "a2"
        await pilot.press("ctrl+j")
        assert ws.sent[-1] == {"type": "approve", "id": "a3", "answer": "yes"}
        s.on_server({"type": "approval_done", "id": "a3"})
        s.on_server({"type": "approval", "id": "a4", "tool": "shell", "reason": "x", "preview": "x", "danger": False})
        await pilot.press("ctrl+n")  # avslå virker alltid
        assert ws.sent[-1] == {"type": "approve", "id": "a4", "answer": "no"}


@pytest.mark.asyncio
async def test_media_subswitch_plays_on_pc_and_player_keys():
    api = FakeApi()
    app = Host(api)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        s = app.screen
        await pilot.press("4")
        await pilot.pause(0.2)
        assert s.sub == "musikk" and s.query_one("#media-table").row_count == 1  # tittel med «[/]» krasjer ikke
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert ("POST", "/pc/play", {"album": "a1"}) in api.calls and s.player["current"]["id"] == "t0"
        assert s.query_one("#lab-now").display
        await pilot.press("space")
        await pilot.pause(0.1)
        assert ("POST", "/pc/control", {"action": "pause"}) in api.calls
        assert s.player["paused"]
        await pilot.press("f")
        await pilot.pause(0.2)
        assert s.sub == "film" and any(c[1] == "/media/videos" for c in api.calls)
        await pilot.press("b")
        await pilot.pause(0.2)
        assert s.sub == "boker"
        await pilot.press("5")
        await pilot.pause(0.3)
        await pilot.press("n")
        await pilot.pause(0.1)
        assert ("POST", "/pc/control", {"action": "next"}) in api.calls
        assert "Sang 1" in plain(s.query_one("#pc-now"))
        # en spillerhendelse fra websocketen oppdaterer uten ny innlasting
        n = len(api.calls)
        s.on_server({"type": "player", **IDLE})
        assert len(api.calls) == n and "Ingenting spilles" in plain(s.query_one("#pc-now"))
        await pilot.press("tab")  # til telefonlista: d spør først
        await pilot.press("d")
        await pilot.pause(0.1)
        assert type(app.screen).__name__ == "ConfirmDialog"
        await pilot.press("n")
        assert not any(c[0] == "DELETE" for c in api.calls)


@pytest.mark.asyncio
async def test_media_tab_add_downloads_edit_cover_delete_scan():
    api = FakeApi()
    app = Host(api)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause(0.3)
        s = app.screen
        await pilot.press("4")
        await pilot.pause(0.2)
        keys = plain(s.query_one("#lab-keys"))
        for k in ("a legg til", "e rediger", "c cover", "x slett", "D nedlastinger", "S skann"):
            assert k in keys, k

        # a: lenke → undersøk (forhåndsvisning som ren tekst) → start
        await pilot.press("a")
        await pilot.pause(0.1)
        assert type(app.screen).__name__ == "AddDialog"
        for ch in "https://youtu.be/x":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert ("POST", "/media/inspect", {"url": "https://youtu.be/x"}) in api.calls
        card = plain(app.screen.query_one("#card"))
        assert "Gymnopédie [bold]1[/]" in card and "lovlig kilde" in card
        assert app.screen.query_one("#kind").value == "music"
        await pilot.press("enter")  # i tittelfeltet: start
        await pilot.pause(0.2)
        add = next(c for c in api.calls if c[1] == "/media/add")
        assert add[2]["url"] == "https://youtu.be/x" and add[2]["kind"] == "music" and add[2]["method"] == "ytdlp"
        assert type(app.screen).__name__ == "LabScreen"

        # D: nedlastingene, live fra websocketen; tastene etter status
        await pilot.press("D")
        await pilot.pause(0.2)
        dls = app.screen
        assert type(dls).__name__ == "DownloadsScreen" and dls.query_one("#dl-table").row_count == 2
        assert "c avbryt" in plain(dls.query_one("#dl-keys")) and "r prøv igjen" not in plain(dls.query_one("#dl-keys"))
        await pilot.press("r")  # pågår: ingen «prøv igjen»
        assert not any(c[1].endswith("/retry") for c in api.calls)
        await pilot.press("c")
        await pilot.pause(0.1)
        assert ("DELETE", "/downloads/d1", None) in api.calls
        await pilot.press("down")
        await pilot.press("r")
        await pilot.pause(0.1)
        assert ("POST", "/downloads/d2/retry", None) in api.calls
        await pilot.press("x")
        await pilot.pause(0.1)
        assert ("DELETE", "/downloads/d2?forget=1", None) in api.calls
        s.on_server({"type": "downloads", "items": [{"id": "d3", "title": "[/] ny", "kind": "music", "status": "laster ned",
                                                    "progress": 0.5, "error": ""}]})
        await pilot.pause(0.1)
        assert dls.query_one("#dl-table").row_count == 1 and s.active_downloads() == 1
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert "nedlastinger (1)" in plain(s.query_one("#lab-keys"))

        # e: rediger (bare endrede felt sendes)
        await pilot.press("e")
        await pilot.pause(0.2)
        assert type(app.screen).__name__ == "EditDialog" and app.screen.query_one("#f-title").value == "Gymnopédies"
        app.screen.query_one("#f-title").value = "Gymnopédies (komplett)"
        app.screen.query_one("#f-title").focus()
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert ("PATCH", "/media/item/a1", {"title": "Gymnopédies (komplett)"}) in api.calls

        # c: cover – tomt felt leter på nettet på nytt
        await pilot.press("c")
        await pilot.pause(0.1)
        assert type(app.screen).__name__ == "CoverDialog"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert ("POST", "/media/item/a1/cover/fetch", None) in api.calls

        # x: slett spør først
        await pilot.press("x")
        await pilot.pause(0.2)
        assert type(app.screen).__name__ == "ConfirmDialog"
        await pilot.press("n")
        await pilot.pause(0.1)
        assert not any(c[0] == "DELETE" and c[1].startswith("/media/item") for c in api.calls)
        await pilot.press("x")
        await pilot.pause(0.2)
        await pilot.press("j")
        await pilot.pause(0.2)
        assert ("DELETE", "/media/item/a1", None) in api.calls

        await pilot.press("S")
        await pilot.pause(0.2)
        assert ("POST", "/media/scan", None) in api.calls


@pytest.mark.asyncio
async def test_plan_can_repeat_and_end_a_series_from_lab():
    api = FakeApi()
    series = [{"id": f"e{i}", "title": "ZTEST Kor", "start": f"2099-01-{i + 1:02d}T18:00", "end": None, "all_day": False,
               "note": "", "source": "du", "series": "s1"} for i in range(3)]
    orig = api.call

    async def call(method, path, body=None):
        if path.startswith("/plan?"):
            api.calls.append((method, path, body))
            return {"events": series, "seq": 1}
        if path == "/plan" and method == "POST":
            api.calls.append((method, path, body))
            return {**series[0], "series_count": 13}
        return await orig(method, path, body)

    api.call = call
    app = Host(api)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("3")
        await pilot.pause(0.2)
        await pilot.press("n")
        await pilot.pause(0.2)
        await pilot.press(*"ZTEST Kor", "enter", *"fre 18")
        await pilot.press("tab", "enter", "down", "down", "down", "enter")   # Gjentas → hver uke
        await pilot.pause(0.1)
        assert "hver uke" in plain(app.screen.query_one("#preview"))
        await pilot.press("shift+tab", "enter")
        await pilot.pause(0.2)
        post = next(c for c in api.calls if c[0] == "POST" and c[1] == "/plan")
        assert post[2]["repeat"] == "weekly" and post[2]["title"] == "ZTEST Kor"
        s = app.screen
        s.query_one("#plan-table").focus()
        s.query_one("#plan-table").move_cursor(row=1)
        await pilot.press("d")
        await pilot.pause(0.2)
        assert "Slette avtale som gjentas" in str(app.screen.query_one("#dlg").border_title)
        await pilot.press("r")
        await pilot.pause(0.2)
        assert ("DELETE", "/plan/e1?series=rest", None) in api.calls


@pytest.mark.asyncio
async def test_home_shows_weather_and_v_sets_the_place():
    api = FakeApi()
    orig = api.call

    async def call(method, path, body=None):
        if path == "/weather/place":
            api.calls.append((method, path, body))
            return {"place": "ZTEST", "weather": {"place": "ZTEST, Kommune", "temp": 3.6, "symbol": "lightrain", "rain": 1.2}}
        r = await orig(method, path, body)
        if path == "/dashboard":
            r = {**r, "weather": {"place": "Bergen", "temp": 3.6, "symbol": "partlycloudy_night", "rain": 0}}
        return r

    api.call = call
    app = Host(api)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.4)
        card = plain(app.screen.query_one("#card-plan"))
        assert "☁ 4° delvis skyet · Bergen" in card
        await pilot.press("v")
        await pilot.pause(0.2)
        await pilot.press(*"ZTEST", "enter")              # tomt felt, nåværende sted som hint
        await pilot.pause(0.3)
        assert ("PUT", "/weather/place", {"place": "ZTEST"}) in api.calls



@pytest.mark.asyncio
async def test_opening_the_zeus_tab_warms_the_model_once():
    api = FakeApi()
    app = Host(api)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("2")
        await pilot.pause(0.2)
        await pilot.press("h", "e", "i")
        await pilot.pause(0.2)
        assert [c for c in api.calls if c[1] == "/zeus/warm"] == [("POST", "/zeus/warm", None)]  # én gang, ikke per tast
