import pytest

from olympus.tui.app import OlympusApp
from olympus.tui.widgets import AgentMessage


async def wait_reply(app, pilot, n=1):
    for _ in range(200):
        await pilot.pause(0.05)
        msgs = app.orch.messages
        if len([m for m in msgs if m["role"] == "assistant"]) >= n and not app._generating:
            return
    raise AssertionError("fikk ikke svar")


@pytest.fixture
def app(fake_config):
    fake_config.data["general"]["startup_animation"] = False
    a = OlympusApp(fake_config)
    yield a
    a.manager.shutdown()


@pytest.mark.asyncio
async def test_tui_zeus_answers_and_shows_model(app):
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.press(*"kan du fikse denne Python-feilen i skriptet mitt", "enter")
        await wait_reply(app, pilot)
        assert app.orch.messages[-1]["content"].startswith("[Zeus@qwen35-9b]")
        assert "Zeus" in str(app.query(AgentMessage).last().header.render())
        assert app.orch.active_agent == "zeus"
        assert "qwen35-9b" in str(app.query_one("#modelbar").render())
        assert "sky" not in str(app.query_one("#modelbar").render())

        await pilot.press(*"/he", "tab", "enter")
        await pilot.pause(0.1)
        assert "/modell" in str(app.query(".sys-msg").last().render())

        await pilot.press(*"/new", "enter")
        await pilot.pause(0.1)
        assert app.orch.messages == [] and len(app.query(AgentMessage)) == 0


@pytest.mark.asyncio
async def test_tui_claimed_action_without_tool_is_redone(app):
    """Strenghet: sier Zeus at noe er gjort uten at et verktøy har skrevet noe, forkastes svaret og gjøres på nytt."""
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.press(*"jeg har lagt inn avtalen i planen", "enter")
        await wait_reply(app, pilot)
        await pilot.pause(0.3)
        msgs = list(app.query(AgentMessage))
        assert len(msgs) == 2
        assert msgs[0].has_class("draft") and "forkastet" in str(msgs[0].header.render())
        assert sum(m["role"] == "assistant" and bool(m.get("content")) for m in app.orch.messages) == 1
        await pilot.click(msgs[0].header)                                     # klikk viser utkastet igjen
        await pilot.pause(0.1)
        assert not msgs[0].has_class("draft")


@pytest.mark.asyncio
async def test_tui_survives_missing_backend(app):
    app.config.data["llamacpp"]["binary"] = "finnes-ikke"
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        assert app.load_error
        assert "pacman" in str(app.query(".err-msg").first().render())


@pytest.mark.asyncio
async def test_tui_model_picker_switches_and_persists(app, fake_config):
    from olympus import config as cfg
    from olympus.tui.model_picker import ModelPicker, ProvidersScreen

    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.press(*"hei", "enter")
        await wait_reply(app, pilot)
        await pilot.press("f7")
        await pilot.pause(0.5)
        assert isinstance(app.screen, ModelPicker)
        await pilot.press(*"qwopus")
        await pilot.pause(0.3)
        await pilot.press("enter")
        await pilot.pause(0.5)
        assert not isinstance(app.screen, ModelPicker)
        assert app.current_model() == "qwopus-coder-9b"
        assert app.orch.messages  # samtalen fortsetter med ny modell (ingen ny økt)
        # lagret i brukerens config.toml: overlever omstart, og blir den lokale «tilbake til»-modellen
        assert cfg.saved_value(fake_config.home, "agents.zeus.model") == "qwopus-coder-9b"
        assert cfg.saved_value(fake_config.home, "general.local_model") == "qwopus-coder-9b"
        assert "qwopus-coder-9b" in str(app.query_one("#modelbar").render())
        # ukjent modell avvises
        await pilot.press(*"/modell finnes/ikke", "enter")
        await pilot.pause(0.2)
        assert app.current_model() == "qwopus-coder-9b"
        assert "Ukjent modell" in str(app.query(".err-msg").last().render())
        # providers-vinduet åpnes og lukkes
        await pilot.press("f3")
        await pilot.pause(0.3)
        assert isinstance(app.screen, ProvidersScreen)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ProvidersScreen)


@pytest.mark.asyncio
async def test_tui_cloud_model_is_marked_and_warned(app, fake_config, monkeypatch):
    from olympus.providers import save_auth

    save_auth(fake_config, "groq", "gsk-test")  # «tilkoblet» uten nett: lista er fra config
    async with app.run_test(size=(160, 50)) as pilot:
        await app.switch_model("groq/openai/gpt-oss-120b")
        await pilot.pause(0.2)
        assert app.current_model() == "groq/openai/gpt-oss-120b"
        bar = str(app.query_one("#modelbar").render())
        assert "☁" in bar and "sky – samtalen sendes ut" in bar
        assert app.query_one("#model").has_class("cloud")
        warn = str(app.query(".sys-msg").last().render())
        assert "Groq" in warn and "minnet" in warn and "/modell lokal" in warn
        await pilot.press(*"/modell lokal", "enter")
        await pilot.pause(0.3)
        assert app.current_model() == "qwen35-9b"
        assert not app.query_one("#model").has_class("cloud")


@pytest.mark.asyncio
async def test_tui_sessions_and_settings(app):
    from olympus.tui.chrome import SettingsScreen

    async with app.run_test(size=(180, 50)) as pilot:
        assert app.query_one("#welcome").display
        assert app.query_one("#sessions").display  # bred nok for øktpanelet

        # Første melding: velkomsten forsvinner, og økten lagres med tittel
        await pilot.press(*"kan du fikse denne Python-feilen i skriptet mitt", "enter")
        await wait_reply(app, pilot)
        await pilot.pause(0.2)
        assert not app.query_one("#welcome").display
        saved = app.sessions.list()
        assert len(saved) == 1 and saved[0].title.startswith("kan du fikse")
        sid = saved[0].id

        # Ny økt, så åpne den gamle igjen: samme meldinger og samme visning
        await pilot.press("ctrl+l")
        await pilot.pause(0.1)
        assert app.orch.messages == [] and app.query_one("#welcome").display
        await app.open_session(sid)
        await pilot.pause(0.1)
        assert len(app.orch.messages) == 2 and len(app.query(AgentMessage)) == 1
        assert app.session_id == sid

        # F2 åpner innstillingene
        await pilot.press("f2")
        await pilot.pause(0.2)
        assert isinstance(app.screen, SettingsScreen)
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_tui_sessions_hidden_when_narrow(app):
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)
        assert not app.query_one("#sessions").display


@pytest.mark.asyncio
async def test_tui_banner_adapts(app):
    async with app.run_test(size=(188, 50)) as pilot:
        await pilot.pause(0.2)
        assert app._banner_mode == "full"           # stor skjerm, ingen samtale: hele tempelet
        await pilot.resize_terminal(80, 24)
        await pilot.pause(0.2)
        assert app._banner_mode == "compact"        # 80×24: én linje, aldri knust grafikk
        await pilot.resize_terminal(188, 50)
        await pilot.pause(0.2)
        await pilot.press(*"kan du fikse denne Python-feilen i skriptet mitt", "enter")
        await wait_reply(app, pilot)
        assert app._banner_mode == "compact"        # samtalen eier skjermen
        assert "tok/s" in str(app.query(AgentMessage).last().header.render()) or app.gen_stats is not None


@pytest.mark.asyncio
async def test_tui_panels_collapse_to_rails_and_remember(app, fake_config):
    async with app.run_test(size=(188, 50)) as pilot:
        await pilot.pause(0.2)
        assert app.query_one("#syspanel").display and not app.query_one("#syspanel-rail").display
        await pilot.press("f4")
        await pilot.pause(0.1)
        assert not app.query_one("#syspanel").display and app.query_one("#syspanel-rail").display
        await pilot.press("ctrl+o")
        await pilot.pause(0.1)
        assert app.query_one("#sessions-rail").display
        await pilot.click("#syspanel-rail")  # klikk på skinnen folder ut igjen
        await pilot.pause(0.1)
        assert app.query_one("#syspanel").display
        app.action_toggle_section("vram")
        assert "vram" in app.query_one("#syspanel").collapsed
    # Ny app med samme config: oppsettet huskes
    again = OlympusApp(fake_config)
    try:
        assert again._sessions_hidden and not again._panel_hidden and "vram" in again._sections_collapsed
    finally:
        again.manager.shutdown()


@pytest.mark.asyncio
async def test_tui_ctrl_c_copies_selection(app, monkeypatch):
    async with app.run_test(size=(120, 40)) as pilot:
        copied = []
        monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
        monkeypatch.setattr(app.screen, "get_selected_text", lambda: "kopier meg")
        await pilot.press(*"utkast")
        await pilot.press("ctrl+c")
        assert copied == ["kopier meg"]
        assert app.query_one("#prompt").text == "utkast"  # markert tekst kopieres, feltet tømmes ikke
        monkeypatch.setattr(app.screen, "get_selected_text", lambda: None)
        await pilot.press("ctrl+c")
        assert app.query_one("#prompt").text == ""  # uten markering: som før (tøm feltet)


@pytest.mark.asyncio
async def test_tui_session_delete_cross(app):
    app.sessions.save("s1", "Første økt", [{"role": "user", "content": "hei"}], [{"kind": "user", "text": "hei"}])
    async with app.run_test(size=(188, 50)) as pilot:
        await pilot.pause(0.2)
        panel = app.query_one("#sessions")
        panel.reload()
        await pilot.pause(0.1)
        ol = app.query_one("#sessions-list")
        # Finn ✕ i den rendrede økt-raden (linje 1: under «+ Ny økt») og klikk på den
        row = "".join(seg.text for seg in ol.render_line(1))
        x = row.index("✕")
        await pilot.click("#sessions-list", offset=(x, 1))
        await pilot.pause(0.1)
        assert panel._armed == "s1" and app.sessions.load("s1") is not None  # første klikk: bare markert
        await pilot.click("#sessions-list", offset=(x, 1))
        await pilot.pause(0.1)
        assert app.sessions.load("s1") is None                                 # andre klikk: slettet
        assert app.session_id is None                                          # og økten ble ikke åpnet


@pytest.mark.asyncio
async def test_tui_session_click_opens_once(app, monkeypatch):
    app.sessions.save("s2", "Andre økt", [{"role": "user", "content": "hei"}], [{"kind": "user", "text": "hei"}])
    async with app.run_test(size=(188, 50)) as pilot:
        await pilot.pause(0.2)
        app.query_one("#sessions").reload()
        await pilot.pause(0.1)
        opened = []

        async def fake_open(sid):
            opened.append(sid)

        monkeypatch.setattr(app, "open_session", fake_open)
        await pilot.click("#sessions-list", offset=(4, 1))
        await pilot.pause(0.2)
        assert opened == ["s2"]  # akkurat én gang


@pytest.mark.asyncio
async def test_tui_offers_to_save_code_files(app, tmp_path):
    from olympus.tui.widgets import SaveFiles

    app.orch.cwd = tmp_path
    (tmp_path / "style.css").write_text("body{}\n")  # allerede skrevet (f.eks. av write_file): hoppes over
    text = "### `index.html`\n```html\n<p>hei</p>\n```\n\n**style.css**\n```css\nbody{}\n```\n"
    async with app.run_test(size=(120, 40)) as pilot:
        msg = AgentMessage("ϟ", "Zeus", "#ffffff")
        await app._log(msg)
        await app._offer_files(msg, {"text": text})
        await pilot.pause(0.1)
        offer = app.query_one(SaveFiles)
        assert [f for f, _ in offer.files] == ["index.html"]
        await pilot.click(SaveFiles)
        await pilot.pause(0.1)
        assert (tmp_path / "index.html").read_text() == "<p>hei</p>\n"
        assert "Lagret" in str(offer.render())
