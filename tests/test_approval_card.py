"""Godkjenningskortet i hovedvinduet: samme regler som olympus zeus og Lab's."""

import pytest
from textual.app import App

from olympus.tui.widgets import ToolApproval


class Host(App):
    def __init__(self, card: ToolApproval) -> None:
        super().__init__()
        self.card, self.answers = card, []

    def compose(self):
        yield self.card

    def on_tool_approval_answered(self, e: ToolApproval.Answered) -> None:
        self.answers.append(e.answer)


async def run(card: ToolApproval, *keys: str, wait: float = 1.2) -> list[str]:
    app = Host(card)
    async with app.run_test() as pilot:
        await pilot.press("enter")          # Enter rett etter at kortet dukker opp: aldri et ja
        await pilot.press("y")              # innenfor ventetiden: teller ikke
        await pilot.pause(wait)
        for k in keys:
            await pilot.press(k)
            await pilot.pause(0.05)
    return app.answers


@pytest.mark.asyncio
async def test_enter_and_early_yes_never_approve_then_y_does():
    assert await run(ToolApproval("Kjør kommando", "ls", "bash", False, "shell")) == []
    assert await run(ToolApproval("Kjør kommando", "ls", "bash", False, "shell"), "y") == ["yes"]


@pytest.mark.asyncio
async def test_danger_needs_two_presses_and_long_preview_must_be_opened():
    assert await run(ToolApproval("Slett", "rm x", "bash", True, "shell"), "y") == []
    assert await run(ToolApproval("Slett", "rm x", "bash", True, "shell"), "y", "y") == ["yes"]
    long = "\n".join(f"linje {i}" for i in range(60))
    assert await run(ToolApproval("Skriv fil", long, "text", False, "write_file"), "y") == []
    assert await run(ToolApproval("Skriv fil", long, "text", False, "write_file"), "v", "y") == ["yes"]
    assert await run(ToolApproval("Skriv fil", long, "text", False, "write_file"), "n") == ["no"]
