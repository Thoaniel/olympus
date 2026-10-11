"""Olympus Lab's: planen (med angring og synk), paring av Zeus-appen, API-et og AI-bryteren."""

import asyncio
import json
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from olympus.lab import Lab
from olympus.tools import ToolCall, Toolbox
from olympus.web.server import make_app


def test_plan_add_move_delete_undo_and_sync(tmp_path):
    lab = Lab(tmp_path / "lab.db")
    seen = []
    lab.listeners.append(seen.append)
    ev = lab.add("Trening", "2026-10-10 12:00")
    assert ev["start"] == "2026-10-10T12:00" and ev["source"] == "du"
    lab.update(ev["id"], start="2026-10-10T14:00", who="zeus")
    assert lab.day("2026-10-10")[0]["start"] == "2026-10-10T14:00"
    seq_before_delete = lab.changes_since(0)["seq"]
    lab.delete(ev["id"])
    assert lab.day("2026-10-10") == []
    changed = lab.changes_since(seq_before_delete)
    assert changed["events"][0]["deleted"]                 # en klient som var borte, ser slettingen
    lab.undo(changed["seq"])                               # angre slettingen
    assert lab.day("2026-10-10")[0]["title"] == "Trening"
    assert len(seen) == 4                                   # hver endring varsler (add, update, delete, undo)
    assert lab.add("Hele dagen", "2026-10-11")["all_day"]
    with pytest.raises(ValueError):
        lab.add("", "2026-10-10T10:00")
    with pytest.raises(ValueError):
        lab.add("Feil", "i morgen kanskje")


def test_pair_code_is_single_use_and_expires(tmp_path, monkeypatch):
    lab = Lab(tmp_path / "lab.db")
    code = lab.new_pair_code()
    device_id, token = lab.pair(code, "Pixel 9")
    assert lab.device_for(token)["name"] == "Pixel 9"
    assert lab.pair(code, "igjen") is None                 # bare én gang
    old = lab.new_pair_code()
    monkeypatch.setattr("olympus.lab.time.time", lambda: time.time_ns() / 1e9 + 3600)
    assert lab.pair(old, "for sent") is None               # utløpt
    monkeypatch.undo()
    assert lab.revoke(device_id) and lab.device_for(token) is None
    assert "token" not in json.dumps(lab.devices())        # nøkkelen lagres aldri i klartekst


@pytest.mark.asyncio
async def test_app_api_pairs_edits_plan_and_respects_ai_switch(fake_config):
    app = make_app(fake_config)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/api/v1/plan")).status == 401
        from olympus.lab import open_lab

        code = open_lab(fake_config.home).new_pair_code()
        r = await client.post("/api/v1/pair", json={"code": code, "name": "Pixel 9"})
        assert r.status == 200
        auth = {"Authorization": "Bearer " + (await r.json())["token"]}
        assert (await client.post("/api/v1/pair", json={"code": code})).status == 403
        r = await client.post("/api/v1/plan", json={"title": "Tannlege", "start": "2026-10-13T10:00"}, headers=auth)
        ev = await r.json()
        assert r.status == 201 and ev["title"] == "Tannlege"
        r = await client.patch(f"/api/v1/plan/{ev['id']}", json={"start": "2026-10-14T10:00"}, headers=auth)
        assert (await r.json())["start"] == "2026-10-14T10:00"
        plan = await (await client.get("/api/v1/plan", headers=auth)).json()
        assert [e["title"] for e in plan["events"]] == ["Tannlege"] and plan["seq"] == 2
        log = await (await client.get("/api/v1/log", headers=auth)).json()
        assert log[0]["who"] == "du (Pixel 9)"

        ws = await client.ws_connect("/api/v1/ws", headers=auth)  # appen: nøkkel i stedet for opphav/cookie
        assert (await ws.receive_json())["type"] == "hello"
        await client.delete(f"/api/v1/plan/{ev['id']}", headers=auth)
        kinds = []
        for _ in range(6):
            kinds.append((await ws.receive_json(timeout=5))["type"])
            if "plan_changed" in kinds:
                break
        assert "plan_changed" in kinds                     # alle skjermer får beskjed med en gang

        assert (await (await client.post("/api/v1/ai", json={"on": False}, headers=auth)).json())["ai"] is False
        await ws.send_json({"type": "send", "text": "hei"})
        notes = []
        for _ in range(6):
            m = await ws.receive_json(timeout=5)
            if m["type"] == "notice":
                notes.append(m["text"])
                break
        assert "Zeus er av" in notes[0]                     # appen virker videre, Zeus svarer ikke
        brief = await (await client.get("/api/v1/brief", headers=auth)).json()
        assert brief["server"]["ai"] is False and "events" in brief

        devices = await (await client.get("/api/v1/devices", headers=auth)).json()
        await client.delete(f"/api/v1/devices/{devices[0]['id']}", headers=auth)
        assert (await client.get("/api/v1/plan", headers=auth)).status == 401   # fjernet enhet er ute
        await ws.close()


@pytest.mark.asyncio
async def test_zeus_plan_tool_reads_freely_but_changes_need_approval(tmp_path):
    lab = Lab(tmp_path / "lab.db")
    tb = Toolbox(tmp_path, lab=lab, undo_instead=False)  # med tools.undo_instead = false: kort for alt
    add = ToolCall("1", "plan", json.dumps({"action": "add", "title": "Trening", "start": "2026-10-10T14:00"}))
    appr = tb.approval(add)
    assert appr.needed and "Trening" in appr.preview
    r = await tb.run(add)
    assert r.ok and lab.events()[0]["source"] == "zeus"
    ls = ToolCall("2", "plan", json.dumps({"action": "list", "from": "2026-10-01", "to": "2026-10-31"}))
    assert not tb.approval(ls).needed
    out = (await tb.run(ls)).output
    eid = lab.events()[0]["id"]
    assert eid in out and "Trening" in out
    delete = ToolCall("3", "plan", json.dumps({"action": "delete", "id": eid}))
    assert tb.approval(delete).danger                      # sletting er rød i godkjenningskortet
    assert (await tb.run(ToolCall("4", "plan", json.dumps({"action": "delete", "id": "finnes-ikke"})))).ok is False



@pytest.mark.asyncio
async def test_plan_list_understands_norwegian_dates(tmp_path):
    """Modellen skriver «lørdag 17.10» eller «17.10.2026»; før krevde list ISO, og den brukte flere kall på å gjette."""
    from datetime import date, timedelta

    lab = Lab(tmp_path / "lab.db")
    day = date.today() + timedelta(days=3)
    lab.add("Kino", f"{day.isoformat()}T19:00")
    tb = Toolbox(tmp_path, lab=lab, undo_instead=False)
    for to in (f"{day:%d.%m.%Y}", f"{day:%d.%m}", day.isoformat()):
        r = await tb.run(ToolCall("1", "plan", json.dumps({"action": "list", "from": f"{day:%d.%m}", "to": to})))
        assert r.ok and "Kino" in r.output and f"{day:%d.%m}" in r.output.splitlines()[0], (to, r.output)
    bad = await tb.run(ToolCall("2", "plan", json.dumps({"action": "list", "to": "en gang"})))
    assert not bad.ok and "Forstår ikke" in bad.output

def test_plan_approval_shows_every_change_and_guards_cloud(tmp_path):
    lab = Lab(tmp_path / "lab.db")
    ev = lab.add("Trening", "2026-10-10T14:00")
    tb = Toolbox(tmp_path, lab=lab, undo_instead=False)
    edit = ToolCall("1", "plan", json.dumps({"action": "edit", "id": ev["id"], "title": "Avlyst", "note": "slett meg"}))
    preview = tb.approval(edit).preview
    assert "Trening" in preview and "title: Avlyst" in preview and "note: slett meg" in preview
    cloud = Toolbox(tmp_path, lab=lab, cloud=True)
    assert cloud.approval(ToolCall("2", "plan", json.dumps({"action": "list"}))).needed


def test_nightly_backup_copies_your_data_and_keeps_14_days(tmp_path):
    from datetime import datetime

    from olympus import backup

    home = tmp_path / "home"
    lab = Lab(home / "lab.db")
    lab.add("Tannlege", "2026-10-13T10:00")
    (home / "memory").mkdir()
    (home / "memory" / "MEMORY.md").write_text("husk dette")
    dest = tmp_path / "backup"
    for d in range(20):                                  # gamle dager som skal ryddes
        (dest / f"2026-09-{d + 1:02}").mkdir(parents=True)
    info = backup.run_backup(home, dest)
    assert info["ok"] and info["kept"] == 14
    today = dest / info["date"]
    copy = Lab(today / "lab.db")
    assert copy.events()[0]["title"] == "Tannlege" and (today / "memory" / "MEMORY.md").read_text() == "husk dette"
    assert backup.due(None, datetime(2026, 10, 11, 3, 5)) and not backup.due(None, datetime(2026, 10, 11, 2, 0))
    assert not backup.due(info, datetime.fromisoformat(info["date"] + "T04:00"))


def test_plan_rejects_bad_input_on_add_and_edit(tmp_path):
    lab = Lab(tmp_path / "lab.db")
    ev = lab.add("Tannlege", "2026-10-13T10:00", all_day="false")
    assert ev["all_day"] is False
    for bad in ({"title": ""}, {"title": "   "}, {"title": ["x"]}, {"note": {"a": 1}}, {"end": "2026-10-12T10:00"}):
        with pytest.raises(ValueError):
            lab.update(ev["id"], **bad)
    assert lab.update(ev["id"], title="x" * 5000)["title"] == "x" * 200
    with pytest.raises(ValueError):
        lab.add("Møte", "2026-10-13T10:00", end="2026-10-13T09:00")
    with pytest.raises(ValueError):
        lab.add(["liste"], "2026-10-13T10:00")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_api_turns_bad_input_into_400_not_500(fake_config):
    app = make_app(fake_config)
    async with TestClient(TestServer(app)) as client:
        from olympus.lab import open_lab

        code = open_lab(fake_config.home).new_pair_code()
        r = await client.post("/api/v1/pair", json={"code": code, "name": None})
        ck = {"Authorization": "Bearer " + (await r.json())["token"]}
        assert (await (await client.get("/api/v1/devices", headers=ck)).json())[0]["name"] == "telefon"
        assert (await client.get("/api/v1/plan?from=tull", headers=ck)).status == 400
        r = await client.post("/api/v1/plan", json={"title": "A", "start": "2026-10-13T10:00"}, headers=ck)
        eid = (await r.json())["id"]
        assert (await client.patch(f"/api/v1/plan/{eid}", json={"note": {"a": 1}}, headers=ck)).status == 400
        assert (await client.patch(f"/api/v1/plan/{eid}", json={"title": ""}, headers=ck)).status == 400
        assert (await client.get("/api/v1/media/search?q=a&limit=abc", headers=ck)).status == 400
        assert (await client.post("/api/v1/media/playlists", json={"name": "P", "tracks": 5}, headers=ck)).status == 400
        assert (await client.put("/api/v1/media/progress/x", json={"position": float("inf")}, headers=ck)).status == 400
        assert (await client.post("/api/v1/pc/control", json={"action": "dans"}, headers=ck)).status == 400
        assert (await client.get("/api/v1/media/playlist/finnesikke", headers=ck)).status == 404


def test_claims_and_weekdays_and_quoted_pipes():
    from datetime import datetime

    from olympus.agents.orchestrator import claims_action
    from olympus.chat import today_line
    from olympus.tools import _segments

    assert claims_action("Ferdig – lagt inn middag søndag kl 18.") and claims_action("Slettet «middag».")
    assert not claims_action("Skal jeg legge inn middag?") and not claims_action("Du har ingen avtaler i dag.")
    assert claims_action("Jeg legger til «kino» lørdag 17.10 kl. 20:00 – vent på godkjenning.")
    assert today_line(datetime(2026, 10, 10)).startswith("I dag er lørdag 10.10.2026 (uke 41). I morgen er søndag 11.10.")
    assert _segments("grep -E 'a|b' f") == [["grep", "-E", "a|b", "f"]]
    assert _segments("ls | wc -l") == [["ls"], ["wc", "-l"]]
    assert _segments("cat a#b; rm x") == [["cat", "a#b"], ["rm", "x"]]  # # skjuler ikke kommandoer
    assert ["rm", "-rf", "~"] in (_segments("ls # ; rm -rf ~") or [["rm", "-rf", "~"]])


@pytest.mark.asyncio
async def test_plan_list_to_is_inclusive(tmp_path):
    lab = Lab(tmp_path / "lab.db")
    lab.add("Tannlege", "2026-10-16T10:00")
    box = Toolbox(tmp_path)
    box.lab = lab
    r = await box.run(ToolCall("1", "plan", json.dumps({"action": "list", "from": "2026-10-12", "to": "2026-10-16"})))
    assert "Tannlege" in r.output


def test_plan_understands_norwegian_dates(tmp_path):
    from datetime import date, timedelta

    lab = Lab(tmp_path / "lab.db")
    ev = lab.add("Middag", "i morgen 18")
    assert ev["start"] == f"{date.today() + timedelta(days=1)}T18:00"
    with pytest.raises(ValueError):
        lab.add("Tull", "en eller annen gang")


def test_history_keeps_real_tool_calls_and_cuts_cleanly(fake_config, tmp_path):
    from olympus.agents.orchestrator import Orchestrator, text_only
    from olympus.model_manager import ModelManager
    from olympus.tools import ToolCall

    o = Orchestrator(fake_config, ModelManager(fake_config), cwd=tmp_path)
    o.messages.append({"role": "user", "content": "legg inn middag i morgen 18"})
    o.turn_tools = []
    o._log_tool(ToolCall("c1", "plan", '{"action": "add", "title": "Middag", "start": "i morgen 18"}'), "ok: la til Middag")
    o._record([("zeus", "Lagt inn.")])
    roles = [m["role"] for m in o.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert o.messages[2]["content"] == "ok: la til Middag" and "Systemnotat" not in o.messages[0]["content"]
    assert [m["role"] for m in text_only(o.messages)] == ["user", "assistant"]
    o.messages.append({"role": "user", "content": "takk"})
    o.turn_tools = []
    o._record([("zeus", "Bare hyggelig.")])
    assert "ingen verktøy ble kjørt" in o.messages[-2]["content"]
    o.turn_tools = []
    o._log_tool(ToolCall("c2", "shell", '{"command": "curl -H \'Authorization: Bearer hemmelig\' x"}'), "ok: kjørt")
    assert "hemmelig" not in o.turn_tools[0]["args"] and "curl" in o.turn_tools[0]["args"]  # kommandoen, uten nøkkelen
    o._record([("zeus", "Ferdig.")])
    from olympus.agents.orchestrator import for_model

    local = for_model(o.messages, cloud=False)
    sky = for_model(o.messages, cloud=True)
    assert any("curl" in str(m.get("tool_calls", "")) for m in local)        # lokalt: Zeus ser hva han kjørte
    assert not any("curl" in str(m.get("tool_calls", "")) for m in sky)      # skyen får bare kortversjonen
    assert all(not k.startswith("_") for m in local + sky for k in m)


def test_undo_of_undo_deletes_again(tmp_path):
    lab = Lab(tmp_path / "lab.db")
    ev = lab.add("Tannlege", "2026-10-16T10:00")
    lab.delete(ev["id"])
    del_seq = lab.changes_since(0)["seq"]
    lab.undo(del_seq)
    assert not lab.get(ev["id"])["deleted"]
    lab.undo(lab.changes_since(0)["seq"])
    assert lab.get(ev["id"])["deleted"]


def test_lab_table_and_dates_are_safe():
    import datetime as dt

    from olympus.dates import parse_when

    T = dt.date(2026, 10, 10)
    assert parse_when("i morgen kl. 18", T) == "2026-10-11T18:00" and parse_when("12.10. 20", T) == "2026-10-12T20:00"
    assert parse_when("fredag 16.10 11:00", T) == "2026-10-16T11:00"
    assert parse_when("torsk 18", T) is None and parse_when("2026-13-45", T) is None
    assert parse_when("2026-12-24 25:99", T) is None and parse_when("29.2", dt.date(2028, 3, 1)) is None


@pytest.mark.asyncio
async def test_plan_tool_finds_events_by_title_and_previews_dates(tmp_path):
    lab = Lab(tmp_path / "lab.db")
    lab.add("ZTEST tannlege", "fredag 10")
    tb = Toolbox(tmp_path, lab=lab, undo_instead=False)
    call = ToolCall("1", "plan", json.dumps({"action": "move", "match": "tannlege", "start": "fredag 11"}))
    ap = tb.approval(call)
    assert ap.needed and "tannlege" in ap.preview and "fre" in ap.preview and "11:00" in ap.preview
    r = await tb.run(call)
    assert r.ok and "11:00" in r.output
    missing = ToolCall("2", "plan", json.dumps({"action": "delete", "match": "finnes ikke"}))
    assert tb.approval(missing).reject and not (await tb.run(missing)).ok
    ap = tb.approval(ToolCall("3", "plan", json.dumps({"action": "delete", "match": "tannlege"})))
    assert ap.needed and ap.bind["id"]  # godkjenningen låses til avtalen som ble vist på kortet


def test_bound_id_never_falls_back_to_title(tmp_path):
    lab = Lab(tmp_path / "lab.db")
    gone = lab.add("Tannlege", "fredag 10")
    lab.delete(gone["id"])
    lab.add(f"Notat {gone['id']}", "fredag 12")  # tittel som inneholder den slettede id-en
    tb = Toolbox(tmp_path, lab=lab, undo_instead=False)
    assert tb._plan_ref(gone["id"]) == ""


@pytest.mark.asyncio
async def test_app_update_is_served_to_paired_phones(fake_config):
    app = make_app(fake_config)
    async with TestClient(TestServer(app)) as client:
        from olympus.lab import open_lab

        code = open_lab(fake_config.home).new_pair_code()
        r = await client.post("/api/v1/pair", json={"code": code, "name": "Pixel 9"})
        auth = {"Authorization": "Bearer " + (await r.json())["token"]}
        assert (await client.get("/api/v1/app", headers=auth)).status == 404
        d = fake_config.home / "app"
        d.mkdir()
        (d / "zeus.apk").write_bytes(b"PK\x03\x04apk")
        (d / "version.json").write_text('{"code": 5, "name": "0.5"}')
        assert (await client.get("/api/v1/app")).status == 401  # bare parede telefoner
        info = await (await client.get("/api/v1/app", headers=auth)).json()
        assert info == {"code": 5, "name": "0.5", "size": 7}
        r = await client.get("/api/v1/app/apk", headers=auth)
        assert r.status == 200 and r.headers["Content-Type"] == "application/vnd.android.package-archive"


@pytest.mark.asyncio
async def test_media_delete_trashes_album_files_and_rescans(tmp_path, monkeypatch):
    from olympus.medialib import Library

    music = tmp_path / "Musikk" / "Artist" / "Album"
    music.mkdir(parents=True)
    lib = Library(tmp_path / "media.db", {"music": [tmp_path / "Musikk"]}, tmp_path / "cache", tmp_path / "lab.db")
    monkeypatch.setattr("shutil.which", lambda name: None)  # ikke gio i testen: flytt til en papirkurv under tmp
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    f = music / "spor.ogg"
    f.write_bytes(b"x")
    (music / "cover.jpg").write_bytes(b"jpg")  # rest som skal følge med
    lib.db.execute("INSERT INTO tracks (id, path, title, artist, album, album_artist, album_id, mtime) VALUES "
                   "('t1', ?, 'Spor', 'Artist', 'Album', 'Artist', 'a1', 0)", (str(f),))
    lib.db.commit()
    tb = Toolbox(tmp_path, lab=Lab(tmp_path / "lab.db"))
    tb.lib = lib
    ap = tb.approval(ToolCall("1", "media", '{"action": "delete", "id": "a1"}'))
    assert ap.needed and ap.danger and "spor.ogg" in ap.preview
    r = await tb.run(ToolCall("1", "media", '{"action": "delete", "id": "a1"}'))
    assert r.ok and not f.exists() and any((tmp_path / ".local/share/Trash/files").iterdir())
    assert not (tmp_path / "Musikk" / "Artist").exists()  # tomme mapper ryddet
    assert tb.approval(ToolCall("2", "media", '{"action": "delete", "id": "finnes-ikke"}')).reject


def test_music_taste_learns_from_plays_and_likes(tmp_path):
    from olympus.medialib import Library

    lib = Library(tmp_path / "media.db", {"music": [tmp_path / "Musikk"]}, tmp_path / "cache", tmp_path / "lab.db")
    rows = [("j1", "Kind of Blue", "Miles Davis", "jazz", "a1"), ("j2", "So What", "Miles Davis", "jazz", "a1"),
            ("j3", "Blue in Green", "Miles Davis", "jazz", "a1"), ("m1", "Metall", "Bråk", "metal", "a2"),
            ("p1", "Pop", "Popstjerne", "pop", "a3")]
    for i, (tid, title, artist, genre, album) in enumerate(rows):
        lib.db.execute("INSERT INTO tracks (id, path, title, artist, album, album_artist, album_id, duration, genre, mtime) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, 200, ?, 0)", (tid, f"/x/{i}.ogg", title, artist, album, artist, album, genre))
    lib.db.commit()
    for _ in range(3):
        assert lib.record_play("j1", 200, 200, "telefon")
    lib.record_play("j2", 190, 200, "pc")
    lib.record_play("m1", 5, 200, "telefon")   # hoppet over
    lib.record_play("m1", 8, 200, "telefon")
    lib.set_like("p1", -1)
    assert not lib.record_play("finnes-ikke", 10)
    t = lib.taste()
    assert t["artists"][0] == "Miles Davis" and t["genres"][0] == "jazz"
    assert "Metall – Bråk" in t["often_skipped"] and "Pop – Popstjerne" in t["disliked"]
    mix = lib.songs_for_taste(10, seed="x")
    ids = [s["id"] for s in mix]
    assert "p1" not in ids and "j3" in ids          # aldri ✕, men uspilt fra en artist du liker
    assert lib.songs_for("spill noe jeg liker")[0]["id"] in {"j1", "j2", "j3", "m1"}


@pytest.mark.asyncio
async def test_dato_tool_counts_days_and_weekdays(tmp_path):
    from datetime import date

    tb = Toolbox(tmp_path)
    r = await tb.run(ToolCall("1", "dato", json.dumps({"action": "dager", "fra": "2026-10-10", "til": "2026-12-24"})))
    assert r.ok and r.output.startswith("75 dager")
    r = await tb.run(ToolCall("2", "dato", json.dumps({"action": "ukedag", "dato": "2026-12-24"})))
    assert "torsdag" in r.output
    r = await tb.run(ToolCall("3", "dato", json.dumps({"action": "pluss", "dato": "2026-10-10", "dager": 40})))
    assert "19.11.2026" in r.output
    r = await tb.run(ToolCall("4", "dato", json.dumps({"action": "dager", "til": "julaften"})))
    assert r.ok and date.today().year >= 2026
    assert not tb.approval(ToolCall("5", "dato", '{"action": "ukedag", "dato": "fredag"}')).needed


def test_dates_understand_norwegian_month_names():
    import datetime as dt

    from olympus.dates import parse_when

    T = dt.date(2026, 10, 10)
    assert parse_when("17. mai", T) == "2027-05-17" and parse_when("3. mars 2027 18:00", T) == "2027-03-03T18:00"
    assert parse_when("24. desember", T) == "2026-12-24"


@pytest.mark.asyncio
async def test_denied_tool_is_not_asked_again_in_the_same_turn(fake_config, tmp_path):
    from olympus.agents.orchestrator import Orchestrator
    from olympus.model_manager import ModelManager

    o = Orchestrator(fake_config, ModelManager(fake_config), cwd=tmp_path)
    o.toolbox.lab = Lab(tmp_path / "lab.db")
    o.toolbox.undo_instead = False  # testen trenger et kall som krever ja
    asked = []

    async def approve(call, approval):
        asked.append(call.name)
        return "no"

    # Første kall: brukeren sier nei. Samme verktøy igjen i samme runde: spørres ikke, avvises med beskjed.
    o.turn_tools = []
    r1 = await o._run_tool(ToolCall("1", "plan", json.dumps({"action": "add", "title": "A", "start": "i morgen 18"})), approve)
    assert r1.denied and asked == ["plan"]


def test_claims_guard_understands_negations():
    from olympus.agents.orchestrator import claims_action

    assert claims_action("Ferdig – lagt inn middag søndag kl 18.")
    for no in ("Greit, jeg starter ikke nedlastingen.", "Jeg bytter ikke modell.", "Ingenting er lagt inn.",
               "Ok, da lar jeg være."):
        assert not claims_action(no), no


def test_edit_moves_playlists_likes_plays_and_progress_to_the_new_id(tmp_path):
    from olympus.medialib import Library
    from olympus.mediaops import _carry_over

    lib = Library(tmp_path / "media.db", {"music": [tmp_path / "Musikk"]}, tmp_path / "cache", tmp_path / "lab.db")
    p = lib.playlist_create("Liste")
    lib.db.execute("INSERT INTO tracks (id, path, title, artist, album, album_artist, album_id, mtime) "
                   "VALUES ('old', '/x.ogg', 'T', 'A', 'Al', 'A', 'alb', 0)")
    lib.db.commit()
    lib.playlist_set(p["id"], ["old"])
    lib.set_like("old", 1)
    lib.set_like("album:alb", 1)
    lib.record_play("old", 100, 120)
    lib.set_progress("old", 33)
    lib.db.execute("UPDATE tracks SET id = 'new', album_id = 'alb2' WHERE id = 'old'")  # som etter en redigering + skann
    lib.db.commit()
    _carry_over(lib, {"/x.ogg": ("old", "alb")}, {"/x.ogg": ("new", "alb2")}, "alb")
    assert [t["id"] for t in lib.playlist_tracks(p["id"])] == ["new"]
    assert lib.like("new") == 1 and lib.like("album:alb2") == 1 and lib.progress("new")["position"] == 33
    assert lib.db.execute("SELECT count(*) FROM user.plays WHERE track_id = 'new'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_cookie_writes_from_other_sites_are_refused(fake_config):
    from olympus.web.server import get_token

    app = make_app(fake_config)
    async with TestClient(TestServer(app)) as client:
        from olympus.lab import open_lab

        open_lab(fake_config.home)
        ck = {"Cookie": f"olympus_web={get_token(fake_config)}"}
        here = str(client.make_url("")).rstrip("/")
        body = '{"title": "CSRF", "start": "2026-10-13T10:00"}'
        bad = await client.post("/api/v1/plan", data=body, headers={**ck, "Origin": "http://evil.example", "Content-Type": "text/plain"})
        assert bad.status == 403
        port = await client.post("/api/v1/plan", data=body, headers={**ck, "Origin": "http://127.0.0.1:9999"})
        assert port.status == 403                                    # annen port på samme maskin
        site = await client.post("/api/v1/plan", data=body, headers={**ck, "Sec-Fetch-Site": "same-site"})
        assert site.status == 403
        ok = await client.post("/api/v1/plan", data=body, headers={**ck, "Origin": here, "Content-Type": "application/json"})
        assert ok.status == 201                                      # Olympus' egen side
        tui = await client.post("/api/v1/plan", data=body, headers={**ck, "Content-Type": "application/json"})
        assert tui.status == 201                                     # Lab's/TUI (ingen nettleser, ingen Origin)
        assert (await client.get("/api/v1/plan", headers={**ck, "Origin": "http://evil.example"})).status == 200  # lesing er ufarlig
        assert (await client.get("/api/v1/media/search?q=a&limit=99999999999999999999", headers=ck)).status in (200, 400)
        assert (await client.put("/api/v1/media/progress/finnes-ikke", json={"position": 3}, headers={**ck, "Origin": here})).status == 404


def test_edit_never_rewrites_a_file_a_torrent_still_shares(tmp_path):
    import os
    import shutil
    import subprocess

    from olympus.medialib import Library
    from olympus.mediaops import edit_item

    if not shutil.which("ffmpeg"):
        pytest.skip("trenger ffmpeg")
    music = tmp_path / "Musikk" / "ZTEST" / "Album"
    music.mkdir(parents=True)
    f = music / "01.flac"
    subprocess.run(["ffmpeg", "-loglevel", "quiet", "-f", "lavfi", "-i", "anullsrc=d=1", "-metadata", "title=Gammel",
                    "-metadata", "album=Album", str(f)], check=True)
    os.link(f, tmp_path / "seed.flac")                                     # Transmission deler samme fil
    before = f.read_bytes()
    lib = Library(tmp_path / "media.db", {"music": [tmp_path / "Musikk"]}, tmp_path / "cache", tmp_path / "lab.db")
    lib.scan()
    tid = lib.db.execute("SELECT id FROM tracks").fetchone()[0]
    out = edit_item(lib, tid, {"title": "ZTEST Ny"})
    assert out["title"] == "ZTEST Ny" and f.read_bytes() == before == (tmp_path / "seed.flac").read_bytes()


@pytest.mark.asyncio
async def test_repeating_events_are_one_series_zeus_and_app_can_add_and_end(tmp_path, fake_config):
    lab = Lab(tmp_path / "lab.db")
    tb = Toolbox(tmp_path, lab=lab, undo_instead=False)
    add = ToolCall("1", "plan", json.dumps({"action": "add", "title": "ZTEST Trening", "start": "2026-10-12T20:00",
                                            "end": "2026-10-12T21:00", "repeat": "weekly", "count": 4}))
    appr = tb.approval(add)
    assert appr.needed and "gjentas: hver uke – 4 avtaler, første man 12.10 20:00, siste man 2.11 20:00" in appr.preview
    r = await tb.run(add)
    evs = lab.events()
    assert r.ok and "4 avtaler" in r.output and [e["start"] for e in evs] == [
        "2026-10-12T20:00", "2026-10-19T20:00", "2026-10-26T20:00", "2026-11-02T20:00"]
    assert len({e["series"] for e in evs}) == 1 and evs[2]["end"] == "2026-10-26T21:00"
    assert tb.approval(ToolCall("x", "plan", json.dumps({"action": "add", "title": "x", "start": "2026-10-12",
                                                         "repeat": "hvert år"}))).reject
    rest = ToolCall("2", "plan", json.dumps({"action": "delete", "id": evs[1]["id"], "series": True}))
    assert "3 avtaler i alt" in tb.approval(rest).preview
    assert (await tb.run(rest)).ok and [e["start"][:10] for e in lab.events()] == ["2026-10-12"]

    app = make_app(fake_config)
    async with TestClient(TestServer(app)) as client:
        from olympus.lab import open_lab

        open_lab(fake_config.home)
        code = open_lab(fake_config.home).new_pair_code()
        auth = {"Authorization": "Bearer " + (await (await client.post("/api/v1/pair", json={"code": code, "name": "ZTEST"})).json())["token"]}
        r = await client.post("/api/v1/plan", headers=auth, json={"title": "ZTEST Jobb", "start": "2026-10-12T08:00",
                                                                  "repeat": "weekdays", "until": "2026-10-18"})
        first = await r.json()
        assert r.status == 201 and first["series_count"] == 5 and first["series"]
        bad = await client.post("/api/v1/plan", headers=auth, json={"title": "x", "start": "2026-10-12", "repeat": "weekly",
                                                                    "count": "mange"})
        assert bad.status == 400
        ch = await (await client.get("/api/v1/plan/changes?since=0", headers=auth)).json()
        assert sum(e["title"] == "ZTEST Jobb" for e in ch["events"]) == 5      # telefonen får alle via synken
        later = sorted((e for e in ch["events"] if e["title"] == "ZTEST Jobb"), key=lambda e: e["start"])[2]
        gone = await (await client.delete(f"/api/v1/plan/{later['id']}?series=rest", headers=auth)).json()
        assert gone["series_deleted"] == 3


def test_undo_takes_back_a_whole_series_in_one_step(tmp_path):
    lab = Lab(tmp_path / "lab.db")
    lab.add("ZTEST Annet", "2026-10-11T09:00")
    lab.add_series("ZTEST Kor", "2026-10-12T18:00", repeat="weekly", count=5)
    lab.undo(lab.log(1)[0]["seq"])
    assert [e["title"] for e in lab.events()] == ["ZTEST Annet"]           # alle 5 borte, den andre står
    evs = lab.add_series("ZTEST Kor", "2026-10-12T18:00", repeat="weekly", count=5)
    lab.delete_series(evs[2]["id"])
    assert len(lab.events()) == 3
    lab.undo(lab.log(1)[0]["seq"])
    assert len(lab.events()) == 6                                           # alle tre tilbake



def test_series_edge_cases_from_review(tmp_path):
    from olympus.lab import series_dates

    lab = Lab(tmp_path / "lab.db")
    with pytest.raises(ValueError):                                     # hverdager lør–søn: ingen forekomster
        lab.add_series("ZTEST", "2026-10-17T10:00", repeat="weekdays", until="2026-10-18")
    assert len(series_dates("2026-10-12", None, "monthly")) == 12      # månedlig uten slutt: ett år
    assert len(series_dates("2026-10-12", None, "monthly", count=36)) == 24   # taket er to år
    assert series_dates("2026-10-17T08:00", None, "weekdays", count=1)[0][0] == "2026-10-19T08:00"  # lør → man
    # angre tar bare sin egen slengen, ikke en enkelt sletting rett etter
    evs = lab.add_series("ZTEST", "2026-10-12T18:00", repeat="weekly", count=6)
    lab.delete_series(evs[3]["id"])
    lab.delete(evs[0]["id"])
    lab.undo(lab.log(1)[0]["seq"])
    assert len(lab.events()) == 3
    # angre midt i en slengen tar hele slengen, én gang; angre angringen setter den tilbake
    seq_mid = [r for r in lab.log(20) if r["action"] == "delete"][1]["seq"]
    lab.undo(seq_mid)
    assert len(lab.events()) == 6
    lab.undo(seq_mid)                                                   # samme slengen igjen: ingenting skjer
    assert len(lab.events()) == 6
    lab.undo(lab.log(1)[0]["seq"])                                      # angre angringen
    assert len(lab.events()) == 3


def test_retry_with_same_client_id_never_duplicates(tmp_path):
    lab = Lab(tmp_path / "lab.db")
    a = lab.add_series("ZTEST", "2026-10-12T18:00", repeat="weekly", count=4, client="tmp-1")
    b = lab.add_series("ZTEST", "2026-10-12T18:00", repeat="weekly", count=4, client="tmp-1")
    assert [e["id"] for e in a] == [e["id"] for e in b] and len(lab.events()) == 4
    one = lab.add("ZTEST én", "2026-10-13T10:00", client="tmp-2")
    assert lab.add("ZTEST én", "2026-10-13T10:00", client="tmp-2")["id"] == one["id"] and len(lab.events()) == 5


def test_release_tags_are_stripped_from_show_and_episode_names():
    from olympus.medialib import strip_release
    from olympus.mediaops import clean_title

    assert strip_release("Big Buck Bunny S01 2160p AMZN") == "Big Buck Bunny"
    assert strip_release("Sintel 2160p 10bit WEB-DL DDP5 1 HEVC-Grp") == "Sintel"
    assert strip_release("Mad Max Fury Road 1080p BluRay x264") == "Mad Max Fury Road"
    for keep in ("Blade Runner 2049", "Webster", "Season of the Witch", "The Office US"):
        assert strip_release(keep) == keep
    assert clean_title("Show.Name.S02.1080p.WEB-DL.x265") == ("Show Name", None)


def test_release_strip_is_fast_on_hostile_names():
    import time

    from olympus.medialib import strip_release

    t = time.monotonic()
    strip_release("a" + " " * 20000 + "x")
    assert time.monotonic() - t < 0.5


@pytest.mark.asyncio
async def test_zeus_warm_endpoint_answers_at_once_and_respects_ai_switch(fake_config, monkeypatch):
    from olympus.agents.orchestrator import Orchestrator

    calls = []

    async def fake_warm(self, agent="zeus"):
        calls.append(agent)
        return True

    monkeypatch.setattr(Orchestrator, "warm", fake_warm)
    app = make_app(fake_config)
    async with TestClient(TestServer(app)) as client:
        from olympus.lab import open_lab

        lab = open_lab(fake_config.home)
        auth = {"Authorization": "Bearer " + (await (await client.post("/api/v1/pair", json={"code": lab.new_pair_code(), "name": "ZTEST"})).json())["token"]}
        r = await client.post("/api/v1/zeus/warm", headers=auth)
        assert r.status == 202 and (await r.json())["warming"]
        await asyncio.sleep(0.05)
        assert calls == ["zeus"]
        lab.set_ai(False)
        r = await client.post("/api/v1/zeus/warm", headers=auth)
        assert (await r.json())["warming"] is False


@pytest.mark.asyncio
async def test_warming_counts_as_use_so_the_idle_unloader_keeps_the_model(fake_config, monkeypatch):
    import time as _t

    from olympus.agents.orchestrator import Orchestrator

    async def fake_warm(self, agent="zeus"):
        return True

    monkeypatch.setattr(Orchestrator, "warm", fake_warm)
    app = make_app(fake_config)
    async with TestClient(TestServer(app)) as client:
        from olympus.lab import open_lab

        lab = open_lab(fake_config.home)
        auth = {"Authorization": "Bearer " + (await (await client.post("/api/v1/pair", json={"code": lab.new_pair_code(), "name": "ZTEST"})).json())["token"]}
        from olympus.web.server import HUB

        hub = app[HUB]
        before = _t.monotonic()
        await client.post("/api/v1/zeus/warm", headers=auth)
        assert hub.last_used >= before



@pytest.mark.asyncio
async def test_always_allow_covers_only_that_action_and_never_danger(tmp_path):
    """«Godkjenn alltid i denne samtalen» (appen/Lab's): plan add slipper kortet etterpå, plan delete (rød) gjør aldri det."""
    lab = Lab(tmp_path / "lab.db")
    tb = Toolbox(tmp_path, lab=lab, undo_instead=False)
    add = ToolCall("1", "plan", json.dumps({"action": "add", "title": "Trening", "start": "2026-10-10T14:00"}))
    assert tb.approval(add).needed
    tb.session_allow.update({"plan", "plan:add"})
    assert not tb.approval(add).needed
    ev = lab.add("Kino", "2026-10-11T19:00")
    delete = ToolCall("2", "plan", json.dumps({"action": "delete", "id": ev["id"]}))
    tb.session_allow.add("plan:delete")
    assert tb.approval(delete).needed  # rødt: spør hver gang



def test_plan_exports_as_ics(tmp_path):
    from olympus.lab import to_ics

    lab = Lab(tmp_path / "lab.db")
    lab.add("Tannlege, kontroll", "2026-10-16T10:00", note="ta med kort")
    lab.add("Bursdag", "2026-10-20")
    ics = to_ics(lab.events())
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert "SUMMARY:Tannlege\\, kontroll" in ics and "DTSTART;TZID=Europe/Oslo:20261016T100000" in ics
    assert "DTSTART;VALUE=DATE:20261020" in ics and "DTEND;VALUE=DATE:20261021" in ics
    assert ics.count("BEGIN:VEVENT") == 2 and "UID:" in ics


def test_ics_title_cannot_inject_lines():
    from olympus.lab import to_ics

    ics = to_ics([{"id": "a", "title": "Møte\r\nATTENDEE:evil", "start": "2026-10-16T10:00", "end": None,
                   "all_day": False, "note": "x\ry"}])
    assert "\r\nATTENDEE" not in ics and "SUMMARY:Møte\\nATTENDEE:evil" in ics and "DESCRIPTION:x\\ny" in ics



@pytest.mark.asyncio
async def test_single_plan_changes_happen_at_once_and_can_be_undone(tmp_path):
    """Angre i stedet for å spørre: én avtale lagt til/flyttet skjer med en gang (med angre); serier og sletting spør."""
    lab = Lab(tmp_path / "lab.db")
    tb = Toolbox(tmp_path, lab=lab)
    add = ToolCall("1", "plan", json.dumps({"action": "add", "title": "Tannlege", "start": "2026-10-16T10:00"}))
    assert not tb.approval(add).needed
    r = await tb.run(add)
    assert r.ok and r.undo and lab.events()[0]["title"] == "Tannlege"
    lab.undo(r.undo)
    assert lab.events() == []                                       # «↶ angre»
    series = ToolCall("2", "plan", json.dumps({"action": "add", "title": "Trening", "start": "2026-10-12T20:00",
                                               "repeat": "weekly", "count": 4}))
    assert tb.approval(series).needed                               # serier spør
    ev = lab.add("Kino", "2026-10-17T19:00")
    assert tb.approval(ToolCall("3", "plan", json.dumps({"action": "delete", "id": ev["id"]}))).needed  # sletting spør


@pytest.mark.asyncio
async def test_several_approvals_in_one_round_become_one_card(fake_config, tmp_path):
    from olympus.agents.orchestrator import Orchestrator
    from olympus.model_manager import ModelManager

    o = Orchestrator(fake_config, ModelManager(fake_config), cwd=tmp_path)
    o.toolbox.lab = Lab(tmp_path / "lab.db")
    o.turn_denied, o.turn_denials = set(), 0
    calls = [ToolCall("a", "write_file", json.dumps({"path": "x.txt", "content": "1"})),
             ToolCall("b", "write_file", json.dumps({"path": "y.txt", "content": "2"})),
             ToolCall("c", "dato", json.dumps({"action": "ukedag", "dato": "17.05.2027"}))]  # trenger ikke ja
    cards = []

    async def yes(call, appr):
        cards.append(appr)
        return "yes"

    await o._batch_approve(calls, yes)
    assert len(cards) == 1 and cards[0].reason == "Zeus vil gjøre 2 ting" and "x.txt" in cards[0].preview
    assert set(o._batch_yes) == {id(calls[0]), id(calls[1])}
    r = await o._run_tool(calls[0], yes)
    assert r.ok and len(cards) == 1 and (tmp_path / "x.txt").read_text() == "1"  # ikke spurt på nytt

    async def no(call, appr):
        return "no"

    await o._batch_approve(calls, no)
    assert o._batch_no == {id(calls[0]), id(calls[1])} and o.turn_denials == 1



def test_undo_instead_only_with_local_model(tmp_path):
    lab = Lab(tmp_path / "lab.db")
    tb = Toolbox(tmp_path, lab=lab)
    add = ToolCall("1", "plan", json.dumps({"action": "add", "title": "Tannlege", "start": "2026-10-16T10:00"}))
    assert not tb.approval(add).needed
    tb.cloud = True  # skymodell: kortet kommer tilbake
    assert tb.approval(add).needed


@pytest.mark.asyncio
async def test_batch_approval_is_asked_again_if_the_call_changed(fake_config, tmp_path):
    from olympus.agents.orchestrator import Orchestrator
    from olympus.model_manager import ModelManager

    o = Orchestrator(fake_config, ModelManager(fake_config), cwd=tmp_path)
    o.turn_denied, o.turn_denials = set(), 0
    calls = [ToolCall("same", "write_file", json.dumps({"path": "x.txt", "content": "1"})),
             ToolCall("same", "write_file", json.dumps({"path": "y.txt", "content": "2"}))]
    asked = []

    async def yes(call, appr):
        asked.append(appr.reason)
        return "yes"

    await o._batch_approve(calls, yes)
    assert len(asked) == 1 and len(o._batch_yes) == 2  # to kall med samme modell-id holdes fra hverandre
    o._batch_yes[id(calls[1])] = "noe annet enn det som vises nå"  # det godkjente kortet viste noe annet
    await o._run_tool(calls[1], yes)
    assert len(asked) == 2  # spurt på nytt
