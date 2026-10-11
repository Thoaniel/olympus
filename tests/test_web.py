"""Olympus på telefonen: nøkkel kreves, og en melding over websocket gir svar fra Zeus."""

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from olympus.web.server import COOKIE, get_token, hosts, make_app


@pytest.mark.asyncio
async def test_requires_token_and_sets_cookie(fake_config):
    app = make_app(fake_config)
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/")
        assert r.status == 401
        r = await client.get("/api/state")
        assert r.status == 401
        r = await client.get(f"/?t=wrong")
        assert r.status == 401
        r = await client.get(f"/?t={get_token(fake_config)}", allow_redirects=False)
        assert r.status == 302 and COOKIE in r.cookies
        r = await client.get("/api/state")       # cookien er satt nå
        st = await r.json()
        assert r.status == 200 and st["model"] == "qwen35-9b" and st["cloud"] is False and "preset" not in st
        assert (await client.get("/manifest.json")).status == 200  # PWA-manifestet trenger ikke nøkkel


@pytest.mark.asyncio
async def test_chat_over_websocket(fake_config):
    app = make_app(fake_config)
    async with TestClient(TestServer(app)) as client:
        await client.get(f"/?t={get_token(fake_config)}")
        assert (await client.get("/ws", headers={"Origin": "https://ondsinnet.example"})).status == 403
        ws = await client.ws_connect("/ws", origin=str(client.make_url("/")).rstrip("/"))
        assert (await ws.receive_json())["type"] == "hello"
        assert (await ws.receive_json())["type"] == "state"
        await ws.send_json({"type": "send", "text": "kan du fikse denne Python-feilen i skriptet mitt"})
        seen, text = [], ""
        while True:
            m = await asyncio.wait_for(ws.receive_json(), 30)
            seen.append(m["type"])
            if m["type"] == "chunk":
                text += m["text"]
            if m["type"] == "done":
                break
        assert "agent_start" in seen and "[Zeus@qwen35-9b]" in text
        r = await client.get("/api/sessions")
        assert len(await r.json()) == 1          # økten lagres som i TUI-en
        await ws.close()


@pytest.mark.asyncio
async def test_models_api_lists_validates_switches_and_broadcasts(fake_config):
    from olympus import config as cfg

    app = make_app(fake_config)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/api/v1/models")).status == 401  # nøkkel kreves
        assert (await client.post("/api/v1/models", json={"model": "qwopus-coder-9b"})).status == 401
        await client.get(f"/?t={get_token(fake_config)}")
        r = await client.get("/api/v1/models")
        data = await r.json()
        assert data["current"] == "qwen35-9b" and not data["cloud"]
        ids = [c["id"] for c in data["choices"]]
        assert ids[:2] == ["qwen35-9b", "qwopus-coder-9b"] and "neohorse-9b" not in ids  # bare nedlastede
        assert all({"id", "label", "cloud"} <= set(c) for c in data["choices"])
        ws = await client.ws_connect("/ws", origin=str(client.make_url("/")).rstrip("/"))
        for _ in range(4):  # hello, state, stats, player
            await ws.receive_json()
        for bad in ({"model": "finnes/ikke"}, {"model": 42}, {"model": ""}, {"model": "x" * 500}, {}):
            assert (await client.post("/api/v1/models", json=bad)).status == 400
        assert (await client.post("/api/v1/models", data="ikke json")).status == 400
        r = await client.post("/api/v1/models", json={"model": "qwopus-coder-9b"})
        assert r.status == 200 and (await r.json())["current"] == "qwopus-coder-9b"
        got = [await asyncio.wait_for(ws.receive_json(), 5) for _ in range(3)]
        assert got[0] == {"type": "model", "model": "qwopus-coder-9b", "cloud": False, "provider": "lokal", "warning": ""}
        assert {"state", "stats"} <= {m["type"] for m in got} and got[2]["stats"]["model"] == "qwopus-coder-9b"
        assert cfg.saved_value(fake_config.home, "agents.zeus.model") == "qwopus-coder-9b"
        assert (await (await client.get("/api/v1/status")).json())["model"] == "qwopus-coder-9b"
        # ws-kommandoen «model» gjør det samme; «lokal» er ett steg tilbake
        await ws.send_json({"type": "command", "name": "model", "model": "lokal"})
        m = await asyncio.wait_for(ws.receive_json(), 5)
        assert m["type"] == "model" and m["model"] == "qwopus-coder-9b"  # sist valgte lokale modell
        await ws.close()


@pytest.mark.asyncio
async def test_switching_to_cloud_warns_everyone(fake_config):
    from olympus.providers import save_auth

    save_auth(fake_config, "groq", "gsk-test")
    app = make_app(fake_config)
    async with TestClient(TestServer(app)) as client:
        await client.get(f"/?t={get_token(fake_config)}")
        ws = await client.ws_connect("/ws", origin=str(client.make_url("/")).rstrip("/"))
        for _ in range(4):
            await ws.receive_json()
        r = await client.post("/api/v1/models", json={"model": "groq/openai/gpt-oss-120b"})
        body = await r.json()
        assert body["cloud"] and "Groq" in body["warning"]
        m = await asyncio.wait_for(ws.receive_json(), 5)
        assert m["type"] == "model" and m["cloud"] and m["provider"] == "Groq"
        n = await asyncio.wait_for(ws.receive_json(), 5)
        assert n["type"] == "notice" and "sendes nå til Groq" in n["text"]
        st = await (await client.get("/api/v1/status")).json()
        assert st["cloud"] is True and st["model"] == "groq/openai/gpt-oss-120b"
        await ws.close()


def test_never_listens_on_all_interfaces(fake_config, monkeypatch):
    monkeypatch.setattr("olympus.web.server.tailscale_ip", lambda: "100.64.0.7")
    assert hosts(fake_config) == ["127.0.0.1", "100.64.0.7"]
    monkeypatch.setattr("olympus.web.server.tailscale_ip", lambda: None)
    assert hosts(fake_config) == ["127.0.0.1"]


@pytest.mark.asyncio
async def test_websocket_rejects_foreign_host_even_if_origin_matches(fake_config):
    app = make_app(fake_config)
    async with TestClient(TestServer(app)) as client:
        await client.get(f"/?t={get_token(fake_config)}")
        port = client.make_url("/").port
        r = await client.get("/ws", headers={"Origin": f"http://ondsinnet.example:{port}", "Host": f"ondsinnet.example:{port}"})
        assert r.status == 403  # DNS-rebinding: fremmed vertsnavn avvises


@pytest.mark.asyncio
async def test_frees_the_gpu_when_idle(fake_config):
    from olympus.web.server import Hub

    fake_config.data["web"]["idle_unload"] = 0
    hub = Hub(fake_config)
    try:
        await hub.manager.ensure("qwen35-9b")
        assert hub.manager.loaded() == ["qwen35-9b"]
        task = asyncio.create_task(hub.idle_unloader(interval=0.05))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if not hub.manager.loaded():
                break
        task.cancel()
        assert hub.manager.loaded() == []   # ingen modell i GPU-en når telefonen ikke brukes
    finally:
        hub.manager.shutdown()


@pytest.mark.asyncio
async def test_one_brain_for_tui_and_phone(fake_config):
    """TUI-en og telefonen er samme hjerne: TUI-ens svar kjøres i hjernen, og en tur fra telefonen vises i TUI-en."""
    import copy

    from olympus.tui.app import OlympusApp
    from olympus.tui.widgets import AgentMessage, UserMessage

    brain = make_app(fake_config)
    async with TestClient(TestServer(brain)) as phone:
        tui_cfg = copy.copy(fake_config)
        tui_cfg.data = copy.deepcopy(fake_config.data)
        tui_cfg.data["general"]["brain"] = "auto"
        tui_cfg.data["web"]["brain_url"] = str(phone.make_url("/")).rstrip("/")
        app = OlympusApp(tui_cfg)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause(0.5)
            assert app.remote, "TUI-en skulle koblet seg til hjernen"
            # 1) melding fra TUI-en: svaret kommer fra hjernen
            await pilot.press(*"kan du fikse denne Python-feilen i skriptet mitt", "enter")
            for _ in range(200):
                await pilot.pause(0.05)
                if not app._generating and app.query(AgentMessage):
                    break
            assert "[Zeus@" in app.query(AgentMessage).last().body._markdown
            assert len(app.sessions.list()) == 1          # hjernen lagret økten
            # 2) melding fra telefonen: dukker opp i TUI-en
            await phone.get(f"/?t={get_token(fake_config)}")
            ws = await phone.ws_connect("/ws", origin=str(phone.make_url("/")).rstrip("/"))
            await ws.send_json({"type": "send", "text": "skriv en e-post til sjefen om ferie"})
            for _ in range(200):
                await pilot.pause(0.05)
                users = [u for u in app.query(UserMessage)]
                if len(users) == 2 and not app._generating and len(app.query(AgentMessage)) == 2:
                    break
            assert len(app.query(UserMessage)) == 2
            assert "[Zeus@" in app.query(AgentMessage).last().body._markdown
            # 3) modellbytte i TUI-en går via hjernen og lagres der; alle skjermer får det
            await app.switch_model("qwopus-coder-9b")
            for _ in range(100):
                await pilot.pause(0.05)
                if app.current_model() == "qwopus-coder-9b":
                    break
            assert app.current_model() == "qwopus-coder-9b" and fake_config.agent("zeus")["model"] == "qwopus-coder-9b"
            await ws.close()


@pytest.mark.asyncio
async def test_status_polling_does_not_keep_the_gpu_busy(fake_config):
    from olympus.web.server import Hub

    hub = Hub(fake_config)
    try:
        await hub.handle({"type": "stats"})
        assert hub.last_used == 0.0      # TUI-ens statusspørsmål hvert par sekund teller ikke som bruk
        await hub.handle({"type": "new"})
        assert hub.last_used > 0
    finally:
        hub.manager.shutdown()
