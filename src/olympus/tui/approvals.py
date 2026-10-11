"""Godkjenninger fra Zeus – én felles logikk for `olympus zeus` (zeus_window) og zeus-fanen i Lab's (lab_view),
så reglene aldri kan skli fra hverandre:

  1. Aldri stille avkorting: er forhåndsvisningen for lang for panelet, står det «⚠ N tegn skjult», og godkjenning
     virker først når hele er åpnet (ctrl+v).
  2. Bare kallet som vises, kan godkjennes: id-en lagres når panelet tegnes, aldri «første i køen» ved tastetrykket.
  3. Ventetid (GRACE) etter at en godkjenning dukker opp (eller blir synlig): tastetrykk ment for noe annet teller ikke.
  4. Farlige kall krever to trykk innen CONFIRM sekunder.
  5. Alt fra serveren (verktøy, grunn, forhåndsvisning) vises som ren tekst – aldri markup.
"""

from __future__ import annotations

import time
from typing import Any

from rich.text import Text

GOLD, MUTED, OK, BAD = "#d4a73a", "#8a8696", "#8fbf6f", "#e0605e"
PREVIEW_LIMIT = 600  # tegn av forhåndsvisningen som får plass i panelet; resten vises i ctrl+v


def preview_parts(preview: str, limit: int = PREVIEW_LIMIT) -> tuple[str, int]:
    """(det som vises i panelet, antall tegn som er skjult). Aldri stille avkorting: skjult > 0 må vises og sperrer
    godkjenning til hele forhåndsvisningen er åpnet."""
    return preview[:limit], max(0, len(preview) - limit)


class ApprovalGate:
    GRACE = 0.7
    CONFIRM = 3.0

    def __init__(self) -> None:
        self.pending: list[dict[str, Any]] = []
        self.viewed: set[str] = set()  # godkjenninger der hele forhåndsvisningen er åpnet
        self.arrived: dict[str, float] = {}  # id → da den kom (eller sist ble synlig)
        self.displayed: str | None = None  # id-en panelet viser nå – bare den kan godkjennes
        self.armed: tuple[str, float] | None = None  # farlig kall: første trykk (id, tid)

    # --- hendelser ------------------------------------------------------------------------------------
    def add(self, m: dict[str, Any]) -> None:
        self.arrived[str(m.get("id"))] = time.monotonic()
        self.pending.append(m)

    def done(self, aid: Any) -> None:
        self.pending = [p for p in self.pending if str(p.get("id")) != str(aid)]
        self.viewed.discard(str(aid))
        self.arrived.pop(str(aid), None)

    def clear(self) -> None:
        self.pending = []
        self.displayed, self.armed = None, None

    def shown_again(self) -> None:
        """Panelet ble synlig igjen (f.eks. bytte til zeus-fanen): ventetiden starter på nytt."""
        now = time.monotonic()
        for p in self.pending:
            self.arrived[str(p.get("id"))] = now
        self.armed = None

    # --- regler ---------------------------------------------------------------------------------------
    def must_view(self, p: dict[str, Any]) -> bool:
        return preview_parts(str(p.get("preview") or ""))[1] > 0 and str(p.get("id")) not in self.viewed

    def armed_for(self, aid: str | None) -> bool:
        return bool(self.armed and aid and self.armed[0] == aid and time.monotonic() - self.armed[1] <= self.CONFIRM)

    def decide(self, yes: bool, aid: str | None = None) -> tuple[str, dict[str, Any] | None]:
        """Hva et trykk betyr: ("send", p) svar sendes for akkurat p · ("ignore", None) · ("view", p) må se hele først ·
        ("confirm", p) farlig, trykk igjen."""
        aid = aid or self.displayed
        p = next((x for x in self.pending if str(x.get("id")) == aid), None) if aid else None
        if p is None:
            return "ignore", None
        if yes:
            if time.monotonic() - self.arrived.get(str(aid), 0.0) < self.GRACE:
                return "ignore", None
            if self.must_view(p):
                return "view", p
            if p.get("danger") and not self.armed_for(aid):
                self.armed = (str(aid), time.monotonic())
                return "confirm", p
        self.armed = None
        return "send", p

    def view(self) -> dict[str, Any] | None:
        """ctrl+v: kallet som vises, merket som sett i sin helhet."""
        if self.displayed is None:
            return None
        p = next((x for x in self.pending if str(x.get("id")) == self.displayed), None)
        if p is not None:
            self.viewed.add(str(p.get("id")))
        return p

    # --- visning --------------------------------------------------------------------------------------
    def current(self) -> dict[str, Any] | None:
        """Kallet som skal vises; id-en huskes som den eneste som kan godkjennes."""
        p = self.pending[0] if self.pending else None
        self.displayed = str(p.get("id")) if p else None
        return p

    @staticmethod
    def title(p: dict[str, Any]) -> str:
        return "ϟ ZEUS VIL" + ("  ·  FARLIG" if p.get("danger") else "")  # fast tekst (border_title tolker markup)

    def card(self, p: dict[str, Any]) -> Text:
        t = Text("verktøy: ", style=MUTED)
        t.append(str(p.get("tool") or "?"), style="bold")
        t.append("\n")
        t.append(f"{p.get('reason')}\n", style=f"bold {BAD if p.get('danger') else GOLD}")
        shown, hidden = preview_parts(str(p.get("preview") or ""))
        t.append(shown + "\n")
        if hidden:
            seen = str(p.get("id")) in self.viewed
            t.append(f"⚠ {hidden} tegn skjult – trykk ctrl+v for å se alt" + (" (sett)" if seen else "") + "\n",
                     style=f"bold {OK if seen else BAD}")
        t.append("\n")
        blocked = self.must_view(p)
        armed = p.get("danger") and self.armed_for(str(p.get("id")))
        t.append(" ctrl+j ", style=f"bold #0b0b0f on {MUTED if blocked else GOLD}")
        t.append(" godkjenn (se hele først)   " if blocked else
                 " trykk ctrl+j igjen for å godkjenne – FARLIG   " if armed else
                 " godkjenn (to ganger – FARLIG)   " if p.get("danger") else " godkjenn   ",
                 style=f"bold {BAD}" if armed else MUTED)
        t.append(" ctrl+n ", style=f"bold {GOLD}")
        t.append(" avslå", style=MUTED)
        if hidden:
            t.append("   ")
            t.append(" ctrl+v ", style=f"bold {GOLD}")
            t.append(" se hele", style=MUTED)
        if len(self.pending) > 1:
            t.append(f"   (+{len(self.pending) - 1} til)", style=MUTED)
        return t

    def notice(self, verdict: str) -> str:
        return {"view": "Se hele forhåndsvisningen først (ctrl+v) – noe av den er skjult her.",
                "confirm": "FARLIG: trykk ctrl+j igjen innen 3 s for å godkjenne."}.get(verdict, "")
