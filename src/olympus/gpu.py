"""VRAM-avlesning: AMD (amdgpu-sysfs, ingen rocm-smi), NVIDIA (nvidia-smi) og Apple Silicon (delt minne – macOS lar
GPU-en bruke ~75 % av RAM). None = ukjent (bare CPU, eller en GPU vi ikke kan lese): Olympus prøver likevel."""

from __future__ import annotations

import functools
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

GiB = 1024**3
DRM = Path("/sys/class/drm")


def gaming() -> bool:
    """Olymparchs spillmodus: flagget finnes nøyaktig mens et spill kjører (eller spillmodus er slått på for hånd).
    Da får spillet GPU-en: den lokale modellen frigjøres og lastes ikke før spillet er ferdig. Skymodeller virker."""
    import os

    run = os.environ.get("XDG_RUNTIME_DIR")  # din egen (0700); aldri /tmp, der andre kunne lagt flagget
    return bool(run) and (Path(run) / "olymparch" / "gaming").exists()


@dataclass(frozen=True)
class VramInfo:
    card: str
    total: int
    used: int

    @property
    def free(self) -> int:
        return self.total - self.used

    @property
    def total_gb(self) -> float:
        return self.total / GiB

    @property
    def used_gb(self) -> float:
        return self.used / GiB

    @property
    def free_gb(self) -> float:
        return self.free / GiB

    def short(self) -> str:
        return f"{self.used_gb:.1f}/{self.total_gb:.1f} GB"


def read_vram(drm_root: Path = DRM) -> VramInfo | None:
    """VRAM for kortet med mest minne: AMD, så NVIDIA, så Apple Silicon. None = ukjent."""
    return _amd(drm_root) or _nvidia() or _apple()


def _nvidia() -> VramInfo | None:
    import shutil

    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout
        rows = [tuple(int(x) for x in line.split(",")) for line in out.strip().splitlines() if "," in line]
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not rows:
        return None
    total, used = max(rows)
    return VramInfo(card="nvidia", total=total * 1024**2, used=used * 1024**2)


def _apple() -> VramInfo | None:
    import platform

    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return None
    import psutil

    vm = psutil.virtual_memory()
    total = int(vm.total * 0.75)
    return VramInfo(card="apple", total=total, used=max(0, min(total, int(vm.total - vm.available))))


def _amd(drm_root: Path = DRM) -> VramInfo | None:
    best: VramInfo | None = None
    for dev in sorted(drm_root.glob("card*/device")):
        total_f, used_f = dev / "mem_info_vram_total", dev / "mem_info_vram_used"
        if not (total_f.exists() and used_f.exists()):
            continue
        try:
            info = VramInfo(
                card=dev.parent.name,
                total=int(total_f.read_text().strip()),
                used=int(used_f.read_text().strip()),
            )
        except (OSError, ValueError):
            continue
        if best is None or info.total > best.total:
            best = info
    return best


@functools.cache
def gpu_name(drm_root: Path = DRM) -> str | None:
    """Markedsnavn fra lspci, f.eks. «Radeon RX 9070/9070 XT/9070 GRE»."""
    info = read_vram(drm_root)
    if info is None:
        return None
    dev = (drm_root / info.card / "device").resolve()
    try:
        out = subprocess.run(
            ["lspci", "-vmm", "-s", dev.name], capture_output=True, text=True, timeout=2, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    for line in out.splitlines():
        if line.startswith("Device:"):
            name = line.split(":", 1)[1].strip()
            m = re.search(r"\[(.+)\]", name)
            return m.group(1) if m else name
    return f"AMD GPU ({info.card})"


def short_gpu_name(drm_root: Path = DRM) -> str | None:
    """Kort navn for akkurat dette kortet, f.eks. «RX 9070 XT · 16 GB».

    lspci gir hele familien («Radeon RX 9070/9070 XT/9070 GRE»). Varianten skilles på VRAM (GRE har 12 GB)
    og standard effektgrense (XT ligger rundt 300 W og høyere, vanlig 9070 rundt 220 W)."""
    name, info = gpu_name(drm_root), read_vram(drm_root)
    if name is None or info is None:
        return name
    name = name.removeprefix("Radeon ")
    if "/" in name:
        variants = [v.strip() for v in name.split("/")]
        base = variants[0]
        prefix = base.rsplit(" ", 1)[0] if " " in base else ""  # «RX» fra «RX 9070»
        full = [v if not prefix or v.startswith(prefix) else f"{prefix} {v}" for v in variants]
        cap = 0
        for f in (drm_root / info.card / "device").glob("hwmon/hwmon*/power1_cap_default"):
            try:
                cap = int(f.read_text()) // 1_000_000
            except (OSError, ValueError):
                pass
        pick = None
        if info.total_gb < 13:
            pick = next((v for v in full if "GRE" in v), None)
        elif cap >= 280:
            pick = next((v for v in full if v.endswith("XT")), None)
        elif cap:
            pick = base
        name = pick or name
    return f"{name} · {round(info.total_gb)} GB"
