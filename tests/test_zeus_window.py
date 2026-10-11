"""`olympus zeus`: godkjenning kan ikke gis for noe brukeren ikke har sett, og modellen vises."""

import pytest

from olympus.tui.zeus_window import FullPreview, ZeusWindow, preview_parts


def test_preview_parts_never_hides_silently():
    assert preview_parts("kort") == ("kort", 0)
    shown, hidden = preview_parts("x" * 1000)
    assert len(shown) == 600 and hidden == 400


@pytest.fixture
def win(monkeypatch):
    w = ZeusWindow("http://127.0.0.1:9", "test")
    sent: list[dict] = []

    async def send(msg):
        sent.append(msg)

    async def listen():  # ingen ekte server i testen
        return None

    monkeypatch.setattr(w, "send", send)
    monkeypatch.setattr(w, "listen", listen)
    w.GRACE = 0  # egen test for ventetiden
    w.sent = sent
    return w


@pytest.mark.asyncio
async def test_long_preview_must_be_viewed_before_approval(win):
    async with win.run_test(size=(140, 45)) as pilot:
        win.on_server({"type": "approval", "id": "a1", "reason": "Skriv fil", "preview": "A" * 590 + "SKJULT-HALE" * 50})
        await pilot.pause(0.1)
        ask = str(win.query_one("#ask").render())
        assert "tegn skjult" in ask and "ctrl+v" in ask
        await pilot.press("ctrl+j")
        await pilot.pause(0.1)
        assert win.sent == []                                  # sperret: brukeren har ikke sett alt
        await pilot.press("ctrl+v")
        await pilot.pause(0.2)
        assert isinstance(win.screen, FullPreview)
        assert "SKJULT-HALE" * 50 in str(win.screen.query_one("VerticalScroll Static").render())
        await pilot.press("escape")
        await pilot.pause(0.1)
        await pilot.press("ctrl+j")
        await pilot.pause(0.1)
        assert win.sent == [{"type": "approve", "id": "a1", "answer": "yes"}]


@pytest.mark.asyncio
async def test_deny_always_allowed_and_short_preview_approves(win):
    async with win.run_test(size=(140, 45)) as pilot:
        win.on_server({"type": "approval", "id": "a2", "reason": "Kjør", "preview": "B" * 5000})
        await pilot.pause(0.1)
        await pilot.press("ctrl+n")
        await pilot.pause(0.1)
        assert win.sent == [{"type": "approve", "id": "a2", "answer": "no"}]
        win.on_server({"type": "approval_done", "id": "a2"})
        win.on_server({"type": "approval", "id": "a3", "reason": "Kjør", "preview": "df -h /"})
        await pilot.pause(0.1)
        await pilot.press("ctrl+j")
        await pilot.pause(0.1)
        assert win.sent[-1] == {"type": "approve", "id": "a3", "answer": "yes"}


@pytest.mark.asyncio
async def test_shows_model_and_cloud_marker(win):
    async with win.run_test(size=(160, 45)) as pilot:
        win.on_server({"type": "model", "model": "groq/openai/gpt-oss-120b", "cloud": True})
        await pilot.pause(0.1)
        top = str(win.query_one("#top").render())
        assert "☁ groq/openai/gpt-oss-120b" in top and "sky – samtalen sendes ut" in top
        win.on_server({"type": "model", "model": "qwen36-27b", "cloud": False})
        await pilot.pause(0.1)
        assert "qwen36-27b · lokal" in str(win.query_one("#top").render())


def test_server_never_cuts_previews_silently():
    from olympus.web.wire import PREVIEW_MAX, wire_preview

    long = "x" * 10_000
    assert wire_preview(long) == long                     # tidligere kuttet til 3000/4000 tegn uten å si fra
    huge = "y" * (PREVIEW_MAX + 5)
    assert wire_preview(huge).endswith("avslå hvis du er usikker.") and "5 tegn til er ikke vist" in wire_preview(huge)


@pytest.mark.asyncio
async def test_ctrl_j_ignored_right_after_a_new_approval(win):
    win.GRACE = 0.7
    async with win.run_test(size=(140, 45)) as pilot:
        win.on_server({"type": "approval", "id": "g1", "tool": "shell", "reason": "Kjør", "preview": "ls"})
        await pilot.press("ctrl+j")
        await pilot.pause(0.05)
        assert win.sent == []                                  # kom akkurat: tastetrykket var ment for noe annet
        await pilot.pause(0.8)
        await pilot.press("ctrl+j")
        await pilot.pause(0.05)
        assert win.sent == [{"type": "approve", "id": "g1", "answer": "yes"}]


@pytest.mark.asyncio
async def test_approves_only_the_displayed_approval(win):
    async with win.run_test(size=(140, 45)) as pilot:
        win.on_server({"type": "approval", "id": "v1", "tool": "shell", "reason": "Første", "preview": "ls"})
        await pilot.pause(0.05)
        assert win.displayed == "v1"
        # Kappløp: v1 er besvart et annet sted og v2 kommer før panelet er tegnet på nytt.
        win.pending = [{"type": "approval", "id": "v2", "tool": "shell", "reason": "Andre", "preview": "rm -rf x"}]
        await win.action_approve(True)
        assert win.sent == []                                  # v2 er ikke vist ennå: ingenting godkjennes
        win.render_all()
        await win.action_approve(True)
        assert win.sent == [{"type": "approve", "id": "v2", "answer": "yes"}]


@pytest.mark.asyncio
async def test_dangerous_call_needs_two_presses(win):
    async with win.run_test(size=(140, 45)) as pilot:
        win.on_server({"type": "approval", "id": "d1", "tool": "shell", "reason": "Slett", "preview": "rm -rf ~/x",
                       "danger": True})
        await pilot.pause(0.05)
        await pilot.press("ctrl+j")
        await pilot.pause(0.05)
        assert win.sent == []
        assert "trykk ctrl+j igjen for å godkjenne – FARLIG" in str(win.query_one("#ask").render())
        await pilot.press("ctrl+j")
        await pilot.pause(0.05)
        assert win.sent == [{"type": "approve", "id": "d1", "answer": "yes"}]
        # for sent andre trykk (over 3 s): må bekrefte på nytt
        win.on_server({"type": "approval_done", "id": "d1"})
        win.on_server({"type": "approval", "id": "d2", "tool": "shell", "reason": "Slett", "preview": "x", "danger": True})
        await pilot.press("ctrl+j")
        win.armed = ("d2", win.armed[1] - 5)
        await pilot.press("ctrl+j")
        await pilot.pause(0.05)
        assert win.sent[-1]["id"] == "d1"                      # d2 ikke godkjent ennå


@pytest.mark.asyncio
async def test_approval_text_is_literal_and_shows_tool(win):
    async with win.run_test(size=(140, 45)) as pilot:
        win.on_server({"type": "approval", "id": "m1", "tool": "write_file", "reason": "[bold]Ufarlig[/bold] [@click=app.quit]x[/]",
                       "preview": "[red]tekst[/red]"})
        await pilot.pause(0.05)
        ask = str(win.query_one("#ask").render())
        assert "write_file" in ask
        assert "[bold]Ufarlig[/bold] [@click=app.quit]x[/]" in ask and "[red]tekst[/red]" in ask  # ingen markup tolket
