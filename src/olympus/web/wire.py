"""Hendelser over ledningen: orkestratorens Event → JSON og tilbake, så TUI-en kan vise en tur hjernen kjører
nøyaktig som om den kjørte lokalt (verktøylinjer, godkjenning, forkastede svar …)."""

from __future__ import annotations

from typing import Any

from olympus.agents.orchestrator import Event
from olympus.backends.llamacpp import Chunk
from olympus.tools import Approval, ToolCall, ToolResult


def call_to_wire(c: ToolCall) -> dict[str, Any]:
    return {"id": c.id, "name": c.name, "arguments": c.arguments}


def call_from_wire(d: dict[str, Any]) -> ToolCall:
    return ToolCall(d.get("id", ""), d.get("name", ""), d.get("arguments", ""))


PREVIEW_MAX = 200_000  # tegn; mer enn dette sendes ikke, men det står alltid hvor mye som mangler


def wire_preview(preview: str) -> str:
    """Forhåndsvisningen av et verktøykall til skjermene. Aldri stille avkortet: det brukeren godkjenner, skal hun ha
    kunnet se. Bare ekstremt lange (over PREVIEW_MAX) kuttes, og da med en tydelig linje om hvor mye som mangler."""
    if len(preview) <= PREVIEW_MAX:
        return preview
    return preview[:PREVIEW_MAX] + f"\n\n⚠ {len(preview) - PREVIEW_MAX} tegn til er ikke vist her – avslå hvis du er usikker."


def approval_to_wire(a: Approval) -> dict[str, Any]:
    return {"needed": a.needed, "reason": a.reason, "danger": a.danger, "preview": wire_preview(a.preview),
            "preview_lang": a.preview_lang}


def approval_from_wire(d: dict[str, Any]) -> Approval:
    return Approval(bool(d.get("needed", True)), d.get("reason", ""), bool(d.get("danger")), d.get("preview", ""),
                    d.get("preview_lang", "text"))


def to_wire(ev: Event) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": ev.kind, "agent": ev.agent, "text": ev.text, "model": ev.model,
                           "value": ev.value, "total": ev.total}
    if ev.chunk is not None:
        out["chunk"] = {"kind": ev.chunk.kind, "text": ev.chunk.text, "usage": ev.chunk.usage or {}}
    if ev.call is not None:
        out["call"] = call_to_wire(ev.call)
    if ev.result is not None:
        r = ev.result
        out["result"] = {"ok": r.ok, "output": r.output[:20000], "summary": r.summary, "denied": r.denied}
    return out


def from_wire(d: dict[str, Any]) -> Event:
    ev = Event(d.get("kind", ""), agent=d.get("agent", ""), text=d.get("text", ""), model=d.get("model", ""),
               value=int(d.get("value", 0)), total=int(d.get("total", 0)))
    if c := d.get("chunk"):
        ev.chunk = Chunk(c.get("kind", "content"), c.get("text", ""), usage=c.get("usage") or {})
    if c := d.get("call"):
        ev.call = call_from_wire(c)
    if r := d.get("result"):
        ev.result = ToolResult(bool(r.get("ok")), r.get("output", ""), r.get("summary", ""), bool(r.get("denied")))
    return ev
