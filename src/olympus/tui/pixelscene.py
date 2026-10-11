"""Pixel art-scenen i sidepanelet: bokstavene i OLYMPUS faller ned som klosser og lander med lynnedslag.

Marmorklosser med innhogde gullbokstaver faller én etter én og stables til et tårn (S nederst, O øverst)
mellom to greske søyler. Hver kloss treffer med sprett og et rykk i bildet; et lyn slår ned i den og kjeder
seg videre ned gjennom klossene under. Til slutt treffer et enormt lyn toppen, tårnet gløder hvitt og
klossene sprenges ut til sidene før loopen begynner på nytt. Bak går
et stjernebilde over himmelen om gangen, og nederst glir en gresk frise.

Scenen tegnes i «kunstpiksler», låses til én felles palett (assets/palette.json) og skaleres med nærmeste
nabo til panelets nøyaktige pikselstørrelse. Den vises med Kittys grafikkprotokoll (textual-image).

Forhåndsvisning:  uv run python -m olympus.tui.pixelscene
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import ClassVar

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# --- palett: én felles palett for hele scenen (art/palette.json) -----------------------------
PAL = json.loads((Path(__file__).parent / "assets" / "palette.json").read_text())


def _c(name: str, i: int) -> tuple[int, int, int]:
    return tuple(PAL[name][i])


NIGHT = [np.array(c, np.float32) for c in PAL["natt"]]
SPARK = _c("glød", 1)
SPARK_GOLD = _c("gull", 4)
GOLD = [tuple(c) for c in PAL["gull"]]  # mørk → lys (6 trinn)
OUTLINE = _c("kontur", 0)
MARBLE = _c("hvit", 3)
MARBLE_SHADE = _c("hvit", 2)
BOLT_SKY = np.array((26, 30, 60), np.float32)  # himmelen lyser opp når lynet slår

LEDGE_FOOT = 12  # raden i gesims-flisen der føttene står (toppflaten er rad 1–18)

BAYER4 = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]], np.float32)


def _palette_lut() -> np.ndarray:
    """Oppslagstabell 32×32×32 (5 bit per kanal) → nærmeste palettfarge."""
    cols = np.array([c for v in PAL.values() for c in v], np.float32)
    g = np.arange(32, dtype=np.float32) * 8 + 4
    cube = np.stack(np.meshgrid(g, g, g, indexing="ij"), -1).reshape(-1, 3)
    wgt = np.array((0.30, 0.59, 0.11), np.float32) ** 0.5  # vekt etter øyets følsomhet
    d = (((cube[:, None, :] - cols[None, :, :]) * wgt) ** 2).sum(-1)
    return cols[d.argmin(1)].reshape(32, 32, 32, 3).astype(np.uint8)


# --- pikselfont for bokstavene (5 × 7) ---------------------------------------------
GLYPHS = {
    "O": [".###.", "##.##", "#...#", "#...#", "#...#", "##.##", ".###."],
    "L": ["##...", "##...", "##...", "##...", "##...", "##...", "#####"],
    "Y": ["##.##", "##.##", ".###.", "..#..", "..#..", "..#..", ".###."],
    "M": ["#...#", "##.##", "#####", "#.#.#", "#.#.#", "#...#", "#...#"],
    "P": ["####.", "##.##", "##.##", "####.", "##...", "##...", "##..."],
    "U": ["##.##", "##.##", "##.##", "##.##", "##.##", "##.##", ".###."],
    "S": [".####", "##...", "##...", ".###.", "...##", "...##", "####."],
}
WORD = "OLYMPUS"


# --- svartfigur-frisen (12 rader; K = svart, i = innrisset strek, F = flamme) -------
FRIEZE = {
    "hoplite": [
        "....KKK.....",
        "...KK.KK....",
        "....KKK.....",
        "....KiKK....",
        "...KKKK...K.",
        "..KKKKKK.K..",
        ".KKiiKKKK...",
        "KKiKKiKK....",
        "KKiKKiKK....",
        ".KKiiKK.....",
        "...KK.KK....",
        "..KK...KK...",
    ],
    "amphora": [
        "..KKKKK..",
        "...KKK...",
        ".KKKKKKK.",
        "K.KKKKK.K",
        "K.KKKKK.K",
        ".KKKKKKK.",
        "KKKKKKKKK",
        "KiiiiiiiK",
        "KKKKKKKKK",
        ".KKKKKKK.",
        "..KKKKK..",
        "..KKKKK..",
    ],
    "lyre": [
        "K.......K",
        "KK.....KK",
        ".K.....K.",
        ".KKKKKKK.",
        ".K.K.K.K.",
        ".K.K.K.K.",
        ".K.K.K.K.",
        ".K.K.K.K.",
        "KKKKKKKKK",
        "KKiiiiiKK",
        ".KKKKKKK.",
        "..KKKKK..",
    ],
    "trident": [
        "K..K..K",
        "K..K..K",
        "K..K..K",
        "KKKKKKK",
        ".KKKKK.",
        "...K...",
        "...K...",
        "...K...",
        "...K...",
        "...K...",
        "...K...",
        "...K...",
    ],
    "keraunos": [
        ".....KK",
        "....KK.",
        "...KK..",
        "..KK...",
        ".KKKKKK",
        "....KK.",
        "...KK..",
        "..KK...",
        ".KKKKK.",
        "...KK..",
        "..KK...",
        ".K.....",
    ],
    "anvil": [
        "KKKK......",
        "KKKK......",
        "..K.......",
        "..K.......",
        "..K.......",
        "..K.......",
        "KKKKKKKKKK",
        ".KKKKKKKK.",
        "...KKKK...",
        "...KKKK...",
        "..KKKKKK..",
        ".KKKKKKKK.",
    ],
    "torch": [
        "..F..",
        ".FFF.",
        "FFfFF",
        ".FfF.",
        ".KKK.",
        "..K..",
        "..K..",
        "..K..",
        "..K..",
        "..K..",
        "..K..",
        ".KKK.",
    ],
}
FRIEZE_ORDER = ["hoplite", "amphora", "trident", "lyre", "hoplite<", "keraunos", "amphora", "anvil", "torch"]
TERRA = _c("terrakotta", 1)
TERRA_DARK = _c("terrakotta", 0)
FIG_BLACK = _c("kontur", 0)
BAND_NAVY = _c("natt", 1)
GOLD_LINE = _c("gull", 3)
GOLD_DIM = _c("gull", 1)

# --- stjernebilder som driver forbi (normaliserte koordinater, kanter) -----------
CONSTELLATIONS = [
    ("ΩΡΙΩΝ", "Orion, jegeren",
     [(.45, 0), (.2, .15), (.68, .2), (.36, .5), (.46, .53), (.56, .56), (.28, .88), (.78, .82)],
     [(0, 1), (0, 2), (1, 3), (2, 5), (3, 4), (4, 5), (3, 6), (5, 7)]),
    ("ΣΚΟΡΠΙΟΣ", "Skorpionen",
     [(0, .05), (.06, .2), (.12, .34), (.26, .36), (.4, .46), (.5, .62), (.55, .8), (.66, .93), (.8, .96),
      (.9, .86), (.84, .72), (0, .32)],
     [(0, 1), (1, 2), (11, 1), (2, 3), (3, 4), (4, 5), (5, 6), (6, 7), (7, 8), (8, 9), (9, 10)]),
    ("ΛΕΩΝ", "Løven fra Nemea",
     [(.12, .12), (.22, .02), (.32, .1), (.3, .28), (.26, .46), (.2, .7), (.62, .42), (.96, .56), (.62, .66)],
     [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (4, 6), (6, 7), (7, 8), (8, 5)]),
    ("ΑΡΚΤΟΣ", "Store Bjørn · Kallisto",
     [(0, .2), (.18, .26), (.33, .36), (.5, .46), (.56, .72), (.88, .76), (.86, .46)],
     [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6), (6, 3)]),
    ("ΤΑΥΡΟΣ", "Tyren · Zevs i forkledning",
     [(.5, .66), (.4, .5), (.62, .5), (.04, .12), (.24, 0), (.88, .18), (.92, .24), (.86, .26), (.9, .14)],
     [(0, 1), (0, 2), (1, 3), (2, 4)]),
    ("ΚΥΚΝΟΣ", "Svanen · Zevs i forkledning",
     [(.5, 0), (.5, .34), (.5, 1), (.08, .22), (.92, .46), (.3, .28), (.7, .4)],
     [(0, 1), (1, 2), (3, 5), (5, 1), (1, 6), (6, 4)]),
    ("ΔΙΟΣΚΟΥΡΟΙ", "Tvillingene Kastor og Polydevkes",
     [(.2, 0), (.25, .32), (.3, .62), (.36, .96), (.48, .05), (.52, .36), (.58, .66), (.64, 1)],
     [(0, 1), (1, 2), (2, 3), (4, 5), (5, 6), (6, 7), (0, 4), (1, 5)]),
    ("ΚΑΣΣΙΟΠΕΙΑ", "Kassiopeia, dronningen",
     [(0, .3), (.25, .72), (.5, .42), (.75, .78), (1, .22)], [(0, 1), (1, 2), (2, 3), (3, 4)]),
]
CONST_PERIOD = 46.0



FONTS = {  # Cinzel (OFL, assets/fonts): tempelinnskrift-stilen man forbinder med greske sagn
    "cinzel": (Path(__file__).parent / "assets" / "fonts" / "Cinzel.ttf", "Black"),
}


def _word_layout(word: str, h: int, condense: float, font: str = "cinzel") -> tuple[list[np.ndarray], list[int], int]:
    """Bokstavene som binære masker med felles grunnlinje og høyde h, plassert etter fontens egen
    avstand og kerning (så ordet står jevnt som én innskrift). → (masker, x per bokstav, total bredde)."""
    from PIL import ImageFont

    path, var = FONTS[font]
    size = h * 6
    try:
        f = ImageFont.truetype(str(path), size)
        if var:
            f.set_variation_by_name(var)
    except OSError:  # reserve: den enkle pikselfonten, jevnt fordelt
        w1 = max(2, int(h * 0.6 * condense))
        g = [np.array([[c == "#" for c in row] for row in GLYPHS[ch]]) for ch in word]
        ms = [np.asarray(Image.fromarray(m.astype(np.uint8) * 255).resize((w1, h), Image.NEAREST)) > 0 for m in g]
        return ms, [i * (w1 + 3) for i in range(len(word))], len(word) * (w1 + 3) - 3
    W = int(f.getlength(word)) + size
    canv, boxes = [], []
    for i, ch in enumerate(word):
        c = Image.new("L", (W, size * 2))
        ImageDraw.Draw(c).text((size // 4 + f.getlength(word[:i]), size // 4), ch, font=f, fill=255)
        canv.append(c)
        boxes.append(c.getbbox())
    top, bot = min(b[1] for b in boxes), max(b[3] for b in boxes)
    left, right = min(b[0] for b in boxes), max(b[2] for b in boxes)
    _ky, kx = h / (bot - top), h / (bot - top) * condense
    masks, xs = [], []
    for c, (x0, _, x1, _) in zip(canv, boxes):
        g = c.crop((x0, top, x1, bot))
        masks.append(np.asarray(g.resize((max(2, round((x1 - x0) * kx)), h), Image.LANCZOS)) > 120)
        xs.append(round((x0 - left) * kx))
    return masks, xs, round((right - left) * kx)


def _gold_letter(mask: np.ndarray) -> np.ndarray:
    """RGBA-bokstav i gull: mørk kontur, fas på kantene, harde gullbånd ovenfra og en
    innrisset rille midt i de tykke strekene, som en hogd tempelinnskrift."""
    h, w = mask.shape
    m = np.zeros((h + 2, w + 2), bool)
    m[1:-1, 1:-1] = mask
    H, W = m.shape

    def shift(a, dy, dx):
        p = np.pad(a, 2)
        return p[2 + dy:2 + dy + H, 2 + dx:2 + dx + W]

    dil = m | shift(m, -1, 0) | shift(m, 1, 0) | shift(m, 0, -1) | shift(m, 0, 1)
    out = np.zeros((H, W, 4), np.uint8)
    g = np.array(GOLD, np.uint8)
    bayer = np.tile(BAYER4, (H // 4 + 1, W // 4 + 1))[:H, :W] / 16
    band = np.arange(H)[:, None] / H * 3 + (bayer - 0.5) * 0.35  # tre bånd med kort dither i overgangene
    level = np.clip(4 - band.astype(int), 2, 4)
    shine = ((np.arange(W)[None, :] + np.arange(H)[:, None] * 0.45) % 26) < 3  # skrå glansstriper
    level = np.where(shine, np.minimum(level + 1, 5), level)
    out[m, :3] = g[level[m]]
    lit = m & (~shift(m, 1, 0) | ~shift(m, 0, 1))      # naboen over/til venstre er utenfor → lys kant
    shade1 = m & (~shift(m, -1, 0) | ~shift(m, 0, -1))  # naboen under/til høyre er utenfor → mørk kant
    shade2 = m & ~shade1 & (~shift(m, -2, 0) | ~shift(m, 0, -2))
    # Rille: piksler minst 4 inn fra kanten, men ikke 5 (en tynn innrisset linje langs strekene).
    inner = m.copy()
    for _ in range(4):
        inner = inner & shift(inner, 1, 0) & shift(inner, -1, 0) & shift(inner, 0, 1) & shift(inner, 0, -1)
    deeper = inner & shift(inner, 1, 0) & shift(inner, -1, 0) & shift(inner, 0, 1) & shift(inner, 0, -1)
    groove = inner & ~deeper
    out[groove, :3] = g[1]
    out[shade2, :3] = g[1]
    out[shade1, :3] = g[0]
    out[lit & ~shade1, :3] = g[5]
    out[dil & ~m, :3] = OUTLINE
    out[dil, 3] = 255
    return out


def _zigzag(rng: random.Random, a: tuple[float, float], b: tuple[float, float], seg: float, amp: float) -> list:
    """Sikksakk-lyn fra a til b: punkter langs linja med vekselvis sideutslag."""
    (ax, ay), (bx, by) = a, b
    dist = math.hypot(bx - ax, by - ay) or 1
    n = max(3, int(dist / seg))
    px, py = -(by - ay) / dist, (bx - ax) / dist
    side = rng.choice((-1, 1))
    pts = [(ax, ay)]
    for i in range(1, n):
        k = i / n + rng.uniform(-0.25, 0.25) / n
        off = side * rng.uniform(amp * 0.5, amp)
        side = -side
        pts.append((ax + (bx - ax) * k + px * off, ay + (by - ay) * k + py * off))
    pts.append((bx, by))
    return pts


class PixelScene:
    # Klossene stables: S slippes først og lander nederst, O sist på toppen, så tårnet leses ovenfra og ned.
    STEP, FALL = 0.45, 0.35               # sekunder mellom klossene, falltid
    ORDER = tuple(range(6, -1, -1))        # slipperekkefølge: S, U, P, M, Y, L, O
    DROP: ClassVar[dict[int, float]] = {i: 0.3 + n * 0.45 for n, i in enumerate(range(6, -1, -1))}
    LAND = tuple(0.3 + (6 - i) * 0.45 + 0.35 for i in range(7))
    CHAIN = 0.05                          # sekunder per kloss lynet bruker på vei ned gjennom tårnet
    FINALE = max(LAND) + 0.45             # stort lynnedslag i toppen
    GLOW = FINALE + 0.12                  # hele tårnet gløder hvitt
    BLAST = FINALE + 0.55                 # klossene sprenges ut til sidene
    LOOP = BLAST + 1.4

    def __init__(self, width: int, height: int, seed: int = 7, font: str = "cinzel", out_scale: int = 1) -> None:
        """width/height er bakgrunnens kunstpiksler; out_scale er hvor mange skjermpiksler hver av dem blir.
        Klossene, lynene og smellene tegnes i skjermoppløsning (ikke forstørret), så de blir glatte og realistiske."""
        self.w, self.h = max(120, width), max(160, height)
        self.K = max(1, out_scale)
        self.font = font
        w, h = self.w, self.h
        self.rng = random.Random(seed)
        self.nrng = np.random.default_rng(seed)
        self.border, self.border_period = self._border_strip()
        self.fb = h - self.border.shape[0]
        self.bayer = (np.tile(BAYER4, (h // 4 + 1, w // 4 + 1))[:h, :w] + 0.5) / 16
        self.sky = self._sky()
        self.lut = _palette_lut()
        self.qdither = ((self.bayer - 0.5) * 14)[..., None].astype(np.float32)
        n = max(10, w * h // 2600)
        self.stars = np.stack([self.nrng.integers(0, w, n), self.nrng.integers(0, self.fb, n),
                               self.nrng.random(n) * 40, self.nrng.random(n) * 0.5 + 0.3], 1)
        self.constellation: tuple[str, str] | None = None
        # Oppsett: tårnet av sju marmorklosser står på gesimsen nederst, mellom to greske søyler, under taket.
        ledge_h = 44
        self.floor = self.fb - 34
        self.ledge_y = self.floor - LEDGE_FOOT
        self.ledge = self._ledge(ledge_h)
        self.roof_h = 46
        self.lh = bh = max(18, (self.floor - self.roof_h - 6) // 7)
        self.bw = bw = min(int(bh * 1.3), w - 2 * 44)
        self.lws = [bw] * len(WORD)
        self.slots = [((w - bw) // 2, self.floor - (len(WORD) - i) * bh) for i in range(len(WORD))]
        K = self.K
        letter_h = int((bh - 14) * K)
        masks, _, _ = _word_layout(WORD, letter_h, 1.0, self.font)
        widest = max(m.shape[1] for m in masks)
        if widest > (bw - 16) * K:
            masks, _, _ = _word_layout(WORD, letter_h, (bw - 16) * K / widest, self.font)
        self.blocks = [self._real_block(m, bw * K, bh * K, seed=i) for i, m in enumerate(masks)]
        self.glow_blocks = [self._glow(b) for b in self.blocks]
        self.block_imgs = [Image.fromarray(b) for b in self.blocks]
        self.glow_imgs = [Image.fromarray(b) for b in self.glow_blocks]
        r = random.Random(seed)
        self.blast = [(r.choice((-1, 1)) * r.uniform(70, 170), -r.uniform(60, 180), r.choice((-1, 1)) * r.uniform(200, 520))
                      for _ in range(len(WORD))]
        self.columns = self._temple()
        n = 34
        self.motes = np.stack([self.nrng.uniform(0, w, n), self.nrng.uniform(4, 11, n),
                               self.nrng.random(n), self.nrng.uniform(0.3, 0.8, n)], 1)

    # --- klossene og søylene ----------------------------------------------------------
    @staticmethod
    def _noise(h: int, w: int, rng: np.random.Generator, octaves: int = 5, base: int = 4) -> np.ndarray:
        """Fraktalt verdistøy (bikubisk forstørret tilfeldighet i flere oktaver), 0–1."""
        out = np.zeros((h, w), np.float32)
        amp, tot = 1.0, 0.0
        for o in range(octaves):
            gh, gw = max(2, base * 2 ** o * h // max(h, w) + 2), max(2, base * 2 ** o * w // max(h, w) + 2)
            g = Image.fromarray((rng.random((gh, gw)) * 255).astype(np.uint8))
            out += np.asarray(g.resize((w, h), Image.BICUBIC), np.float32) / 255 * amp
            tot += amp
            amp *= 0.5
        return out / tot

    def _real_block(self, letter_mask: np.ndarray, BW: int, BH: int, seed: int) -> np.ndarray:
        """Realistisk marmorkloss: polert hvit marmor med grå årer, avrundede kanter som fanger lyset fra øvre
        venstre, og en innhogd bokstav forgylt med bladgull (dybde fra et høydekart, metallisk glans)."""
        rng = np.random.default_rng(900 + seed)
        K = self.K
        yy, xx = np.mgrid[0:BH, 0:BW].astype(np.float32)
        # Marmor: turbulens + årer langs en skrå retning.
        turb = self._noise(BH, BW, rng, 5, 3)
        fine = self._noise(BH, BW, rng, 3, 16)
        # Få, tynne og uregelmessige årer (ikke jevne bånd): skarp topp av en turbulent sinus, pluss svake sideårer.
        t1 = (xx * 0.8 + yy * 1.5) / (BW * 1.4) + turb * 1.8 + rng.uniform(0, 3)
        t2 = (xx * 1.6 - yy * 0.5) / (BW * 1.1) + self._noise(BH, BW, rng, 4, 4) * 2.2
        vein = (1 - np.abs(np.sin(t1 * math.pi))) ** 14 * 0.85 + (1 - np.abs(np.sin(t2 * math.pi))) ** 22 * 0.45
        vein = np.clip(vein, 0, 1)
        cloud = self._noise(BH, BW, rng, 4, 2)               # svak, skyaktig grå marmorering
        marble = np.array((240, 238, 233), np.float32)
        vein_col = np.array((118, 122, 134), np.float32)
        col = marble[None, None] - (cloud[..., None] - 0.4).clip(0) * 34 + (fine[..., None] - 0.5) * 8
        col = col * (1 - vein[..., None] * 0.6) + vein_col * vein[..., None] * 0.6
        # Høydekart: avfaset kant (rundt hele klossen) og bokstaven hogd inn.
        e = np.minimum(np.minimum(xx, BW - 1 - xx), np.minimum(yy, BH - 1 - yy))
        bev = 5.0 * K
        hgt = np.clip(e / bev, 0, 1)
        hgt = np.sin(hgt * math.pi / 2) * bev * 0.8
        lm = np.zeros((BH, BW), np.float32)
        lh, lw = letter_mask.shape
        oy, ox = (BH - lh) // 2, (BW - lw) // 2
        lm[oy:oy + lh, ox:ox + lw] = letter_mask
        soft = np.asarray(Image.fromarray((lm * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.6 * K)),
                          np.float32) / 255
        depth = 3.2 * K
        hgt = hgt - soft * depth
        gy, gx = np.gradient(hgt)
        nrm = np.dstack([-gx, -gy, np.ones_like(hgt)])
        nrm /= np.linalg.norm(nrm, axis=2, keepdims=True)
        L = np.array((-0.5, -0.62, 0.6), np.float32); L /= np.linalg.norm(L)
        V = np.array((0, 0, 1), np.float32)
        Hh = (L + V) / np.linalg.norm(L + V)
        diff = np.clip(nrm @ L, 0, 1)
        spec = np.clip(nrm @ Hh, 0, 1)
        ao = 1 - 0.35 * np.clip(soft * (1 - lm), 0, 1)    # mørkere innerst i fordypningen rundt bokstaven
        stone = col * (0.42 + 0.68 * diff[..., None]) * ao[..., None] + 70 * (spec ** 28)[..., None]
        # Bladgull i bokstaven: metallisk, sterkt høylys, mørkt der veggene vender bort fra lyset.
        gold_base = np.array((214, 160, 58), np.float32)
        gold = gold_base * (0.25 + 0.95 * diff[..., None]) + np.array((255, 236, 170), np.float32) * (spec ** 18)[..., None] * 0.9
        gold += (fine[..., None] - 0.5) * 14
        a = lm[..., None]
        out = stone * (1 - a) + gold * a
        # Tynn mørk fuge rundt klossen.
        edge = (e < 1 * K)
        out[edge] = out[edge] * 0.45
        res = np.zeros((BH, BW, 4), np.uint8)
        res[..., :3] = np.clip(out, 0, 255)
        res[..., 3] = 255
        return res

    @staticmethod
    def _glow(block: np.ndarray) -> np.ndarray:
        """Samme kloss, hvitglødende (når lynet går gjennom den)."""
        g = block.copy()
        g[..., :3] = (g[..., :3].astype(np.float32) * 0.35 + np.array((255, 250, 235), np.float32) * 0.65).astype(np.uint8)
        return g

    def _temple(self) -> Image.Image:
        """Tempelfasade: to joniske søyler på gesimsen og et tak over dem med bjelke, triglyf-frise, gesims og
        trekantgavl med gullpynt øverst."""
        W, H = self.w, self.h
        hv = [tuple(c) + (255,) for c in PAL["hvit"]]
        g = [tuple(c) + (255,) for c in PAL["gull"]]
        k = tuple(PAL["kontur"][0]) + (255,)
        img = Image.new("RGBA", (W, H))
        d = ImageDraw.Draw(img)
        roof = self.roof_h
        bot = self.floor
        cw = max(16, W // 12)
        # Søyler
        for x0 in (6, W - 6 - cw):
            d.rectangle((x0, roof, x0 + cw - 1, bot - 1), fill=hv[3], outline=k)
            d.rectangle((x0 + 1, roof + 1, x0 + 2, bot - 2), fill=hv[4])
            d.rectangle((x0 + cw - 4, roof + 1, x0 + cw - 2, bot - 2), fill=hv[1])
            for x in range(x0 + 5, x0 + cw - 4, 3):                  # kannelering
                d.line((x, roof + 8, x, bot - 9), fill=hv[2] if x < x0 + cw * 0.6 else hv[1])
            d.rectangle((x0 - 4, roof, x0 + cw + 3, roof + 5), fill=hv[4], outline=k)   # kapitél
            for cx in (x0 - 2, x0 + cw + 1):                          # voluter
                d.ellipse((cx - 3, roof + 2, cx + 3, roof + 8), fill=hv[3], outline=k)
                d.point((cx, roof + 5), fill=g[3])
            d.line((x0, roof + 6, x0 + cw - 1, roof + 6), fill=g[2])
            d.rectangle((x0 - 4, bot - 6, x0 + cw + 3, bot - 1), fill=hv[3], outline=k)  # base
            d.line((x0 - 3, bot - 5, x0 + cw + 2, bot - 5), fill=hv[4])
        # Tak: bjelke, triglyf-frise, gesims, gavl
        y = roof - 1
        d.rectangle((0, y - 7, W - 1, y), fill=hv[3], outline=k)                      # bjelke (arkitrav)
        d.line((1, y - 6, W - 2, y - 6), fill=hv[4]); d.line((1, y - 3, W - 2, y - 3), fill=hv[2])
        y -= 8
        d.rectangle((0, y - 9, W - 1, y), fill=hv[2], outline=k)                      # frise
        for x in range(8, W - 8, 16):                                                # triglyfer og metoper
            d.rectangle((x, y - 8, x + 7, y - 1), fill=hv[1])
            for gx in (x + 2, x + 5):
                d.line((gx, y - 8, gx, y - 1), fill=hv[0])
            d.point((x + 11, y - 5), fill=g[3])
        y -= 10
        d.rectangle((0, y - 3, W - 1, y), fill=hv[4], outline=k)                      # gesims
        d.line((1, y - 1, W - 2, y - 1), fill=hv[3])
        y -= 4
        apex = 1
        d.polygon([(2, y), (W // 2, apex), (W - 3, y)], fill=hv[3], outline=k)       # gavl
        d.polygon([(14, y - 2), (W // 2, apex + 6), (W - 15, y - 2)], fill=hv[2])    # tympanon
        d.line((2, y, W // 2, apex), fill=hv[4])
        d.ellipse((W // 2 - 4, apex + 7, W // 2 + 4, apex + 15), fill=g[3], outline=k) # gullpynt
        d.point([(W // 2 - 2, apex + 9), (W // 2 - 1, apex + 9)], fill=g[5])
        return img

    # --- statisk -----------------------------------------------------------------
    def _sky(self) -> np.ndarray:
        ramp = np.array(NIGHT[:5], np.float32)  # natt0 øverst → natt4 nede mot frisen
        k = np.clip(np.arange(self.h, dtype=np.float32)[:, None] / self.fb, 0, 1) ** 1.3 * (len(ramp) - 1)
        k = np.repeat(k, self.w, 1)
        lo = np.floor(k).astype(int)
        idx = np.where((k - lo) > self.bayer, np.minimum(lo + 1, len(ramp) - 1), lo)  # ordnet dithering
        return ramp[idx]

    def _ledge(self, lh: int) -> Image.Image:
        """Gesimsen fra kant til kant: Aseprite-flisen (art/ledge.aseprite) gjentatt, med en myk skygge under."""
        tile = Image.open(Path(__file__).parent / "assets" / "ledge.png").convert("RGBA")
        img = Image.new("RGBA", (self.w, lh), (0, 0, 0, 0))
        for x in range(0, self.w, tile.width):
            img.alpha_composite(tile, (x, 0))
        a = np.asarray(img).copy()
        img = Image.fromarray(a)
        d = ImageDraw.Draw(img)
        for i, al in enumerate((110, 60)):
            d.line((0, tile.height + i, self.w, tile.height + i), fill=(4, 4, 14, al))
        return img

    def _border_strip(self) -> tuple[np.ndarray, int]:
        """Frise i to bånd, sømløs så den kan gli: meander · svartfigurfrise på terrakotta."""
        gap = 8
        figs = [FRIEZE[k.rstrip("<")] for k in FRIEZE_ORDER]
        figs = [[r[::-1] for r in f] if k.endswith("<") else f for k, f in zip(FRIEZE_ORDER, figs)]
        period = sum(len(f[0]) + gap for f in figs)
        period += (-period) % 8
        W = period * (self.w // period + 2)
        H = 26
        img = Image.new("RGB", (W, H), BAND_NAVY)
        d = ImageDraw.Draw(img)
        for y, c in ((0, GOLD_LINE), (9, GOLD_DIM), (10, GOLD_LINE), (25, GOLD_LINE)):
            d.line((0, y, W, y), fill=c)
        # Meander (gresk nøkkel), periode 8.
        y0 = 2
        for x in range(0, W, 8):
            for seg in (((0, 6), (0, 0)), ((0, 0), (6, 0)), ((6, 0), (6, 4)), ((6, 4), (2, 4)), ((2, 4), (2, 2)),
                        ((2, 2), (4, 2)), ((0, 6), (7, 6))):
                (ax, ay), (bx, by) = seg
                d.line((x + ax, y0 + ay, x + bx, y0 + by), fill=GOLD_LINE)
        # Svartfigurfrise på terrakotta, som på en gresk vase.
        d.rectangle((0, 11, W, 24), fill=TERRA)
        d.line((0, 24, W, 24), fill=FIG_BLACK)
        px = img.load()
        colors = {"K": FIG_BLACK, "i": TERRA_DARK, "F": GOLD_LINE, "f": (255, 246, 200)}
        for base in range(0, W, period):
            x = base + gap // 2
            for f in figs:
                for fy, row in enumerate(f):
                    for fx, ch in enumerate(row):
                        if ch != "." and x + fx < W:
                            px[x + fx, 12 + fy] = colors[ch]
                x += len(f[0]) + gap
        return np.asarray(img, np.float32), period

    # --- tidslinje ---------------------------------------------------------------
    def letter_y(self, i: int, tau: float) -> float | None:
        """Klossens øvre kant nå: faller med tyngdekraft fra over himmelen, spretter litt og legger seg."""
        t0 = self.DROP[i]
        y = self.slots[i][1]
        if tau < t0:
            return None
        e = tau - t0
        if e < self.FALL:
            k = e / self.FALL
            return y - (1 - k * k) * (y + self.lh + 12)
        b = e - self.FALL
        if b < 0.22:
            return y - math.sin(b / 0.22 * math.pi) * max(2, self.lh * 0.05)
        return y

    def slot_center(self, i: int) -> tuple[float, float]:
        x, y = self.slots[i]
        return x + (self.lws[i] + 2) / 2, y + (self.lh + 2) / 2

    # --- per bilde ---------------------------------------------------------------
    def render(self, t: float) -> Image.Image:
        """Bakgrunnen tegnes i kunstpiksler og forstørres; klosser, lyn, smell og gnister tegnes oppå i
        skjermoppløsning."""
        K = self.K
        tau = t % self.LOOP
        cycle = int(t // self.LOOP)
        landed_set = [i for i in self.ORDER if tau >= self.LAND[i]]
        blasting = tau >= self.BLAST
        f = self.sky.copy()
        self._draw_stars(f, t)
        self._draw_constellation(f, t)
        bolts: list[tuple[list, float, int]] = []
        sparks: list[tuple[float, float, tuple]] = []
        bangs: list[tuple[tuple[float, float], float, int]] = []
        flash = 0.0
        glowing: dict[int, float] = {}
        flick = [(0.05, 1.0), (0.1, 0.25), (0.2, 1.0), (0.26, 0.2), (0.36, 0.8), (0.5, 0.0)]
        for i in landed_set:
            e = tau - self.LAND[i]
            if 0 <= e < 0.5 and tau < self.FINALE:
                br = random.Random(cycle * 100 + i * 7 + int(e * 14))
                cx, cy = self.slot_center(i)
                level = next((v for edge, v in flick if e <= edge), 0.0)
                path = _zigzag(br, (cx + br.uniform(-30, 30), -6), (cx, cy - self.lh * 0.3), max(7, self.h / 26), max(5, self.w / 28))
                bolts.append((path, level, 3))
                j = br.randrange(1, max(2, len(path) - 1))
                bolts.append((_zigzag(br, path[j], (path[j][0] + br.uniform(-26, 26), path[j][1] + br.uniform(8, 26)), 5, 3), level * 0.6, 1))
                flash = max(flash, level * 0.5)
                for n, k in enumerate(range(i + 1, len(WORD))):           # lynet kjeder seg ned gjennom tårnet
                    ek = e - (n + 1) * self.CHAIN
                    if 0 <= ek < 0.3:
                        a, b = self.slot_center(k - 1), self.slot_center(k)
                        lv = 1.0 if ek < 0.12 else max(0.0, 1 - (ek - 0.12) / 0.18)
                        bolts.append((_zigzag(br, (a[0] + br.uniform(-6, 6), a[1]), (b[0] + br.uniform(-6, 6), b[1]), 6, 6), lv, 2))
                        glowing[k] = max(glowing.get(k, 0), lv * 0.8)
                if e < 0.3:
                    glowing[i] = max(glowing.get(i, 0), 1 - e / 0.3)
            if 0 <= e < 0.6 and tau < self.FINALE:
                bangs.append((self.slot_center(i), e, cycle * 53 + i))
        if self.FINALE <= tau < self.BLAST:                          # finalen
            e = tau - self.FINALE
            br = random.Random(cycle * 977 + int(e * 16))
            cx, cy = self.slot_center(0)
            level = 1.0 if e < 0.35 else max(0.0, 1 - (e - 0.35) / 0.2)
            for k in range(3):
                path = _zigzag(br, (cx + br.uniform(-60, 60), -8), (cx + br.uniform(-4, 4), cy - self.lh * 0.4), 9, 9)
                bolts.append((path, level * (1 if k == 0 else 0.7), 3 if k == 0 else 2))
            flash = max(flash, level * 0.9)
            if tau >= self.GLOW:
                gk = min(1.0, (tau - self.GLOW) / 0.25)
                for i in range(len(WORD)):
                    glowing[i] = max(glowing.get(i, 0), gk)
        far, far_level = self._heat_lightning(t)
        flash = max(flash, far_level * 0.25)
        if flash:
            f += (BOLT_SKY * flash * (1 - np.arange(self.h)[:, None, None] / self.h)).astype(np.float32)
        self._draw_motes(f, t)
        img = Image.fromarray(np.clip(f, 0, 255).astype(np.uint8)).convert("RGBA")
        if far:
            self._draw_bolts(img, far)
        img.alpha_composite(self.ledge, (0, self.ledge_y))
        img.alpha_composite(self.columns)
        rgb = np.asarray(img.convert("RGB"), np.float32).copy()
        off = int(t * 3) % self.border_period
        rgb[self.fb:] = self.border[:, off:off + self.w]
        q = np.clip(rgb + self.qdither, 0, 255).astype(np.uint8) >> 3
        bg = Image.fromarray(self.lut[q[..., 0], q[..., 1], q[..., 2]])
        hi = (bg.resize((self.w * K, self.h * K), Image.NEAREST) if K > 1 else bg).convert("RGBA")
        # --- skjermoppløsning herfra: klosser, lyn, smell, gnister ---
        d = ImageDraw.Draw(hi)
        if not blasting:
            for i in [j for j in self.ORDER if j not in landed_set]:
                y = self.letter_y(i, tau)
                if y is None:
                    continue
                x = self.slots[i][0]
                for k in range(1, 4):                                   # fartsstriper
                    yy = (y - k * self.lh * 0.2) * K
                    for fx in (0.25, 0.75):
                        d.line(((x + self.bw * fx) * K, yy, (x + self.bw * fx) * K, yy - self.lh * 0.12 * K),
                               fill=(200, 205, 215, 140), width=max(1, K))
                hi.alpha_composite(self.block_imgs[i], (x * K, int(y * K)))
            for i in landed_set:
                gl = glowing.get(i, 0)
                im = self.block_imgs[i]
                if gl > 0:
                    im = Image.blend(self.block_imgs[i], self.glow_imgs[i], min(1.0, gl))
                hi.alpha_composite(im, (self.slots[i][0] * K, int(self.letter_y(i, tau) * K)))
                age = tau - self.LAND[i]
                if age < 0.6:
                    cx = self.slots[i][0] + self.bw / 2
                    by = self.slots[i][1] + self.lh
                    sr = random.Random(cycle * 31 + i)
                    for _ in range(18):
                        side = sr.choice((-1, 1))
                        vx, vy = side * sr.uniform(30, 110), -sr.uniform(20, 90)
                        sparks.append((cx + side * self.bw * 0.45 + vx * age, by + vy * age + 180 * age * age,
                                       (236, 234, 228) if sr.random() < 0.6 else SPARK_GOLD))
        else:
            e = tau - self.BLAST
            for i in range(len(WORD)):
                vx, vy, spin = self.blast[i]
                src = self.glow_imgs[i] if e < 0.08 else self.block_imgs[i]
                blk = src.rotate(spin * e, Image.BICUBIC, expand=True)
                x0, y0 = self.slots[i]
                x = (x0 + self.bw / 2 + vx * e) * K - blk.width / 2
                y = (y0 + self.lh / 2 + vy * e + 320 * e * e) * K - blk.height / 2
                hi.paste(blk, (int(x), int(y)), blk)
            if e < 0.3:
                for i in range(len(WORD)):
                    bangs.append((self.slot_center(i), e * 2, cycle * 53 + i))
        for c, age, seed in bangs:
            self._bang(hi, c, age, seed, bolts)
        self._draw_bolts(hi, [([(x * K, y * K) for x, y in pts], lv, wd) for pts, lv, wd in bolts], K)
        for x, y, c in sparks:
            d.rectangle((x * K, y * K, x * K + K - 1, y * K + K - 1), fill=c)
        # Risting: et lite rykk idet en kloss lander, et større i finalen (frisen nederst står stille).
        hit = [tau - tl for tl in self.LAND if 0 <= tau - tl < 0.12]
        if (hit and tau < self.FINALE) or self.BLAST <= tau < self.BLAST + 0.25:
            amp = 2 if hit and tau < self.FINALE else 4
            ph = hit[0] if hit and tau < self.FINALE else tau - self.BLAST
            dy = round(math.sin(ph / 0.12 * math.pi * 3) * amp) * K
            a = np.asarray(hi).copy()
            a[: self.fb * K] = np.roll(a[: self.fb * K], dy, axis=0)
            hi = Image.fromarray(a)
        return hi.convert("RGB")

    def _bang(self, img: Image.Image, center: tuple[float, float], age: float, seed: int, bolts: list) -> None:
        """Stort smell der lynet treffer: lysstjerne og lyn som sprenger ut i alle retninger."""
        k = age / 0.6
        K = self.K
        cx, cy = center
        d = ImageDraw.Draw(img)
        if age < 0.12:  # glimtet: lysstjerne med takker, gull ytterst og hvit kjerne
            q = 1 - age / 0.12
            for scale, col in ((1.0, SPARK_GOLD), (0.6, SPARK)):
                pts = []
                for j in range(16):
                    a = j / 16 * math.tau + age * 4
                    rr = self.lh * scale * (0.9 if j % 2 == 0 else 0.28) * (0.6 + 0.6 * q)
                    pts.append(((cx + math.cos(a) * rr) * K, (cy + math.sin(a) * rr * 0.8) * K))
                d.polygon(pts, fill=col)
        # Lyn som sprenger ut fra treffpunktet, nye greiner hver ~1/14 s, kortere og svakere mot slutten.
        if k < 0.75:
            r = random.Random(seed * 997 + int(age * 14))
            level = 1.0 if age < 0.18 else max(0.0, 1 - (k - 0.3) / 0.45)
            for j in range(10):
                a = j / 10 * math.tau + r.uniform(-0.25, 0.25)
                ln = self.lh * (0.55 + 0.75 * min(1, age / 0.12)) * r.uniform(0.7, 1.15)
                end = (cx + math.cos(a) * ln, cy + math.sin(a) * ln * 0.75)
                bolts.append((_zigzag(r, (cx, cy), end, 6, 5), level * (1.0 if j % 2 else 0.7), 3 if j % 2 == 0 else 1))

    def _draw_motes(self, f: np.ndarray, t: float) -> None:
        """Gylne støvkorn som stiger sakte og blinker."""
        x0, speed, ph, br = self.motes.T
        y = self.fb - 2 - ((t * speed + ph * 97) % (self.fb - 4))
        x = (x0 + np.sin(t * 0.7 + ph) * 5).astype(np.int32) % self.w
        k = ((np.sin(t * 2.3 + ph * 9) + 1) / 2 * br)[:, None]
        f[y.astype(np.int32), x] = f[y.astype(np.int32), x] * (1 - k) + np.array(SPARK_GOLD, np.float32) * k

    def _heat_lightning(self, t: float) -> tuple[list, float]:
        """Fjernt lyn høyt på himmelen innimellom, svakt og flimrende."""
        slot = int(t / 9)
        r = random.Random(slot * 7919)
        start = slot * 9 + r.uniform(1, 7)
        e = t - start
        if not 0 <= e < 0.45:
            return [], 0.0
        level = (0.55, 0.15, 0.45, 0.1, 0.3, 0.0)[min(5, int(e / 0.075))]
        x = r.uniform(self.w * 0.1, self.w * 0.9)
        a, b = (x, -4), (x + r.uniform(-40, 40), r.uniform(self.h * 0.12, self.h * 0.25))
        path = _zigzag(r, a, b, 7, 6)
        j = len(path) // 2
        branch = _zigzag(r, path[j], (path[j][0] + r.uniform(-30, 30), path[j][1] + r.uniform(10, 30)), 5, 4)
        return [(path, level * 0.7, 1), (branch, level * 0.5, 1)], level

    def _draw_bolts(self, img: Image.Image, bolts: list, k: int = 1) -> None:
        if not bolts:
            return
        glow = Image.new("L", img.size)
        gd = ImageDraw.Draw(glow)
        for pts, level, width in bolts:
            if level > 0 and len(pts) >= 2:
                gd.line(pts, fill=int(200 * level), width=(width + 2) * k)
        bloom = np.asarray(glow.filter(ImageFilter.GaussianBlur(3 * k)), np.float32)[..., None] / 255
        arr = np.asarray(img, np.float32).copy()
        arr[..., :3] += bloom * np.array(SPARK_GOLD, np.float32) * 1.5
        out = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        d = ImageDraw.Draw(out)
        for pts, level, width in bolts:
            if level > 0.05 and len(pts) >= 2:
                if width >= 3:
                    d.line(pts, fill=(255, 226, 140, 255), width=3 * k)
                d.line(pts, fill=(*SPARK, 255), width=max(1, k))
        img.paste(out)

    def _draw_stars(self, f: np.ndarray, t: float) -> None:
        x, y, p, b = self.stars.T
        k = (np.sin(t * 1.7 + p) + 1) / 2 * b
        f[y.astype(int), x.astype(int)] = (np.array((60, 64, 110)) + np.outer(k, (170, 170, 140))).astype(np.float32)

    def _draw_constellation(self, f: np.ndarray, t: float) -> None:
        i = int(t / CONST_PERIOD) % len(CONSTELLATIONS)
        greek, name, stars, edges = CONSTELLATIONS[i]
        p = (t % CONST_PERIOD) / CONST_PERIOD
        size = self.w * 0.42
        x0 = self.w + 4 - p * (self.w + size + 8)
        y0 = self.h * 0.03 + (1 - math.sin(p * math.pi)) * self.h * 0.08
        fade = min(1.0, math.sin(p * math.pi) * 2.2)
        self.constellation = (greek, name) if 0.12 < p < 0.88 else None
        pts = [(x0 + sx * size, y0 + sy * size * 0.9) for sx, sy in stars]
        line = np.array((70, 92, 150), np.float32) * fade
        for a, b in edges:  # svake prikkede linjer mellom stjernene
            (ax, ay), (bx, by) = pts[a], pts[b]
            n = int(max(abs(bx - ax), abs(by - ay)))
            for j in range(3, n - 2, 2):
                xx, yy = int(ax + (bx - ax) * j / n), int(ay + (by - ay) * j / n)
                if 0 <= xx < self.w and 0 <= yy < self.fb:
                    f[yy, xx] = np.maximum(f[yy, xx], line)
        for j, (sx, sy) in enumerate(pts):
            xx, yy = int(sx), int(sy)
            tw = 0.75 + 0.25 * math.sin(t * 3 + j * 1.7)
            col = np.array((235, 238, 255), np.float32) * fade * tw
            for dx, dy, k in ((0, 0, 1.0), (1, 0, .45), (-1, 0, .45), (0, 1, .45), (0, -1, .45)):
                if 0 <= xx + dx < self.w and 0 <= yy + dy < self.fb:
                    f[yy + dy, xx + dx] = np.maximum(f[yy + dy, xx + dx], col * k)


# Olympus er Zeus: tekstlinja under scenen viser bare ham.
GODS = [("ΖΕΥΣ", "Zevs · himmelens og lynets herre")]


def pixel_panel_cls():
    from rich.text import Text
    from textual.containers import Vertical
    from textual.widgets import Static
    from textual_image.renderable import (
        Image as _probe,  # noqa: F401  (finner terminalens grafikkstøtte)
    )
    from textual_image.widget import Image as ImageWidget

    class OlympusPixels(Vertical):
        """Sidepanel med pixel art-scenen (Kittys grafikkprotokoll), ~15 bilder per sekund."""

        DEFAULT_CSS = """
        OlympusPixels { background: #06081a; height: 1fr; }
        OlympusPixels > .caption { height: 1; color: #ffe9a8; text-style: bold; padding: 0 1; }
        OlympusPixels > .stars { height: 1; color: #8fa2d8; padding: 0 1; }
        OlympusPixels > ImageWidget, OlympusPixels > Image { width: 1fr; height: 1fr; }
        """

        def compose(self):
            yield Static("", classes="caption")
            yield Static("", classes="stars")
            yield ImageWidget()

        def on_mount(self) -> None:
            import time

            from textual_image import _terminal

            self._t0 = time.monotonic()
            self._scene: PixelScene | None = None
            cell = _terminal.get_cell_size()
            self._cell = (cell.width, cell.height) if cell else (10, 20)
            self.set_interval(1 / 15, self._tick)

        def _tick(self) -> None:
            import time

            img_w = self.query_one(ImageWidget)
            cols, rows = img_w.size.width, img_w.size.height
            if cols < 8 or rows < 6:
                return
            px_w, px_h = cols * self._cell[0], rows * self._cell[1]
            scale = max(1, round(px_h / 460))  # scenen er tegnet for ~450 kunstpiksler i høyden
            if self._scene is None or (self._scene.w, self._scene.h) != (px_w // scale, px_h // scale):
                self._scene = PixelScene(px_w // scale, px_h // scale, out_scale=scale)
            t = time.monotonic() - self._t0
            frame = self._scene.render(t)
            if frame.size != (px_w, px_h):
                frame = frame.resize((px_w, px_h), Image.NEAREST)
            img_w.image = frame
            i = int(t / 7) % len(GODS)
            greek, norsk = GODS[i]
            shown = int(((t % 7) / 7) * 2.5 * len(norsk))
            self.query_one(".caption", Static).update(Text(f"{greek} · {norsk[:shown]}"))
            c = self._scene.constellation
            self.query_one(".stars", Static).update(Text(f"✦ {c[0]} · {c[1]}" if c else ""))

    return OlympusPixels


def main() -> None:
    from textual.app import App
    from textual.containers import Horizontal
    from textual.widgets import Static

    OlympusPixels = pixel_panel_cls()

    class Preview(App):
        CSS = """
        Screen { background: #0d0d12; }
        Horizontal { height: 1fr; }
        #tui { width: 4fr; height: 1fr; border: round #d4a73a 40%; color: #7a7686; content-align: center middle; }
        OlympusPixels { width: 1fr; height: 1fr; border: round #d4a73a; }
        """
        BINDINGS: ClassVar = [("q", "quit", "Avslutt")]

        def compose(self):
            with Horizontal():
                yield Static("Olympus-TUI (plassholder)\n\nq avslutter forhåndsvisningen", id="tui")
                yield OlympusPixels()

    Preview().run()


if __name__ == "__main__":
    main()
