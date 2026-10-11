"""ASCII-banner: tempel over «OLYMPUS», begge i «ANSI Shadow»-blokkstil, med diagonal gull→hvit-gradient."""

from __future__ import annotations

from rich.color import Color
from rich.style import Style
from rich.text import Text

# «ANSI Shadow»-bokstaver, 6 linjer høye.
LETTERS = {
    "O": [" ██████╗ ", "██╔═══██╗", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    "L": ["██╗     ", "██║     ", "██║     ", "██║     ", "███████╗", "╚══════╝"],
    "Y": ["██╗   ██╗", "╚██╗ ██╔╝", " ╚████╔╝ ", "  ╚██╔╝  ", "   ██║   ", "   ╚═╝   "],
    "M": ["███╗   ███╗", "████╗ ████║", "██╔████╔██║", "██║╚██╔╝██║", "██║ ╚═╝ ██║", "╚═╝     ╚═╝"],
    "P": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔═══╝ ", "██║     ", "╚═╝     "],
    "U": ["██╗   ██╗", "██║   ██║", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    "S": ["███████╗", "██╔════╝", "███████╗", "╚════██║", "███████║", "╚══════╝"],
}


def word(text: str) -> list[str]:
    return ["".join(LETTERS[ch][row] for ch in text) for row in range(6)]


# Skyggetegnet for en tom celle, ut fra om cellen til venstre, over og over-til-venstre er fylt.
# Samme regel som «ANSI Shadow»-fonten bruker, så tempelet får nøyaktig samme skygge som bokstavene.
SHADOW = {(1, 0, 0): "╗", (1, 0, 1): "║", (0, 0, 1): "╝", (0, 1, 1): "═", (1, 1, 1): "╔", (0, 1, 0): "╚", (1, 1, 0): "╔"}


def shadowed(mask: list[str]) -> list[str]:
    """Maske ("#" = fylt) → █-blokker med ╗║╝═-skygge nede til høyre."""
    h, w = len(mask), max(len(r) for r in mask)

    def filled(r: int, c: int) -> int:
        return int(0 <= r < h and 0 <= c < len(mask[r]) and mask[r][c] == "#")

    rows = []
    for r in range(h + 1):
        rows.append("".join("█" if filled(r, c) else SHADOW.get((filled(r, c - 1), filled(r - 1, c), filled(r - 1, c - 1)), " ")
                            for c in range(w + 1)).rstrip())
    return rows


def temple(width: int, columns: int = 7) -> list[str]:
    """Tempel i samme blokkstil som bokstavene: trappet gavl med hult tympanon, bjelke, søyler med
    kapitél og to trinn, sentrert over en logo som er width tegn bred."""
    w = width
    row = lambda pred: "".join("#" if pred(c) else " " for c in range(w))
    mask = []
    for span, hole in ((6, 0), (18, 0), (30, 10), (42, 22), (54, 34)):  # (gavlbredde, hull i midten)
        x0, h0 = (w - span) // 2, (w - hole) // 2
        mask.append(row(lambda c, x0=x0, span=span, h0=h0, hole=hole:
                        x0 <= c < x0 + span and not (hole and h0 <= c < h0 + hole)))
    mask += [row(lambda c: 0 < c < w - 1), ""]
    cap, shaft = 6, 4
    gap = (w - 2 - columns * cap) / (columns - 1)
    xs = [round(1 + i * (cap + gap)) for i in range(columns)]
    mask.append(row(lambda c: any(x <= c < x + cap for x in xs)))
    mask += [row(lambda c: any(x + 1 <= c < x + 1 + shaft for x in xs))] * 3
    mask += [row(lambda c: 0 < c < w - 1), row(lambda c: True)]
    return shadowed(mask)


COMPACT = "⚡ O L Y M P U S"  # én linje: når samtalen er i gang eller skjermen er liten


def banner_lines(mode: str = "full") -> list[str]:
    """«full» = tempel + logo (velkomst på stor skjerm), «logo» = bare blokkbokstavene, «compact» = én linje."""
    if mode == "compact":
        return [COMPACT]
    logo = word("OLYMPUS")
    width = len(logo[0])
    lines = logo if mode == "logo" else [*temple(width), "", *logo]
    full = max(len(line) for line in lines)
    return [line.ljust(full) for line in lines]  # lik bredde, så sentreringen ikke forskyver linjene


def _lerp(a: Color, b: Color, t: float) -> Color:
    ta, tb = a.get_truecolor(), b.get_truecolor()
    return Color.from_rgb(
        ta.red + (tb.red - ta.red) * t,
        ta.green + (tb.green - ta.green) * t,
        ta.blue + (tb.blue - ta.blue) * t,
    )


def render(lines: list[str], start: str, end: str) -> Text:
    """Farg linjene med diagonal gradient (gull nederst til venstre → hvitt oppe til høyre)."""
    c0, c1 = Color.parse(start), Color.parse(end)
    width = max((len(line) for line in lines), default=1)
    height = max(len(lines), 1)
    # Forhåndsberegn 32 trinn så vi slipper én Style per tegn.
    steps = [Style(color=_lerp(c0, c1, i / 31), bold=True) for i in range(32)]
    text = Text(no_wrap=True, overflow="crop")
    for y, line in enumerate(lines):
        for x, ch in enumerate(line):
            if ch == " ":
                text.append(ch)
                continue
            t = 0.65 * (x / width) + 0.35 * (1 - y / height)
            text.append(ch, steps[min(31, int(t * 31))])
        text.append("\n")
    text.rstrip()
    return text
