"""Overopphetingsvern: les temperaturer fra hwmon og vent/stopp ved tunge jobber.

Grensene ligger godt under det sensorene selv kaller kritisk (RX 9070 XT: junction
110 °C, minne 108 °C; i7-14700F: 100 °C). Ved pause fortsettes det først når alt er
`hysteresis` grader under grensen, så viftene får tid til å ta igjen varmen.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from olympus.config import Config

HWMON = Path("/sys/class/hwmon")

# (hwmon-navn, etikett) → nøkkel
SENSORS = {
    ("amdgpu", "edge"): "gpu_edge",
    ("amdgpu", "junction"): "gpu_junction",
    ("amdgpu", "mem"): "gpu_mem",
    ("coretemp", "Package id 0"): "cpu",
    ("k10temp", "Tctl"): "cpu",
    ("nvme", "Composite"): "nvme",
}
NAMES = {"gpu_junction": "GPU", "gpu_mem": "GPU-minne", "gpu_edge": "GPU-kant", "cpu": "CPU", "nvme": "NVMe"}


@dataclass
class Reading:
    temps: dict[str, float]
    gpu_watts: float | None = None


_SENSOR_FILES: dict[Path, tuple[dict[str, Path], Path | None]] = {}


def _sensor_files(root: Path) -> tuple[dict[str, Path], Path | None]:
    """Hvilke filer som har temperaturene/effekten vi bryr oss om. Letingen (mange små filer) gjøres én gang."""
    if root in _SENSOR_FILES:
        return _SENSOR_FILES[root]
    temps: dict[str, Path] = {}
    power = None
    for h in sorted(root.glob("hwmon*")):
        try:
            name = (h / "name").read_text().strip()
        except OSError:
            continue
        for t in sorted(h.glob("temp*_input")):
            label_f = t.with_name(t.name.replace("_input", "_label"))
            label = label_f.read_text().strip() if label_f.exists() else ""
            key = SENSORS.get((name, label))
            if key and key not in temps:
                temps[key] = t
        if name == "amdgpu" and power is None and (h / "power1_average").exists():
            power = h / "power1_average"
    _SENSOR_FILES[root] = (temps, power)
    return temps, power


def _nvidia_temp() -> float | None:
    import shutil
    import subprocess

    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout.split()
        return float(out[0]) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def read(root: Path = HWMON) -> Reading:
    """Temperaturer fra Linux' hwmon (AMD-GPU, CPU); NVIDIA via nvidia-smi når hwmon ikke har GPU-en. På macOS/Windows
    finnes ofte ingen åpen kilde – da er lesningen tom, og varmevernet slipper alt gjennom."""
    reading = _read_hwmon(root)
    if "gpu_junction" not in reading.temps:
        t = _nvidia_temp()
        if t is not None:
            reading.temps["gpu_junction"] = t
    return reading


def _read_hwmon(root: Path = HWMON) -> Reading:
    files, power = _sensor_files(root) if root.is_dir() else ({}, None)
    temps: dict[str, float] = {}
    for key, f in files.items():
        try:
            temps[key] = int(f.read_text()) / 1000
        except (OSError, ValueError):
            _SENSOR_FILES.pop(root, None)  # sensorene er nummerert om (f.eks. etter hvilemodus): let på nytt neste gang
    watts = None
    if power is not None:
        try:
            watts = int(power.read_text()) / 1e6
        except (OSError, ValueError):
            _SENSOR_FILES.pop(root, None)
    return Reading(temps, watts)


class Thermal:
    def __init__(self, config: Config, reader: Callable[[], Reading] = read) -> None:
        self.config = config
        self.reader = reader

    def limits(self) -> dict[str, float]:
        c = self.config
        return {
            "gpu_junction": float(c.get("thermal.gpu_junction_max", 95)),
            "gpu_mem": float(c.get("thermal.gpu_mem_max", 94)),
            "cpu": float(c.get("thermal.cpu_max", 90)),
            "nvme": float(c.get("thermal.nvme_max", 75)),
        }

    def too_hot(self, reading: Reading | None = None, margin: float = 0.0) -> list[str]:
        """Sensorer over grensen (minus `margin`), som lesbar tekst."""
        r = reading or self.reader()
        out = []
        for key, limit in self.limits().items():
            v = r.temps.get(key)
            if v is not None and v >= limit - margin:
                out.append(f"{NAMES[key]} {v:.0f} °C (grense {limit:.0f})")
        return out

    def critical(self, reading: Reading | None = None) -> list[str]:
        """Over grensen + 5 °C: tunge jobber skal avbrytes."""
        return self.too_hot(reading, margin=-5.0)

    async def wait_cool(self, on_status: Callable[[str], None] | None = None, timeout: float = 900.0) -> bool:
        """Vent til alt er under grensen minus hysterese. False ved tidsavbrudd."""
        if not self.config.get("thermal.enabled", True):
            return True
        hot = self.too_hot()
        if not hot:
            return True
        hyst = float(self.config.get("thermal.hysteresis", 10))
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            hot = self.too_hot(margin=hyst)
            if not hot:
                return True
            if on_status:
                on_status(f"▲ kjøler ned før vi fortsetter: {', '.join(hot)}")
            await asyncio.sleep(5)
        return False

    def short(self, reading: Reading | None = None) -> tuple[str, str]:
        """(tekst, nivå) for statuslinjen. nivå: ok | varm | het."""
        r = reading or self.reader()
        parts = []
        if "gpu_junction" in r.temps:
            parts.append(f"GPU {r.temps['gpu_junction']:.0f}°")
        if "cpu" in r.temps:
            parts.append(f"CPU {r.temps['cpu']:.0f}°")
        level = "het" if self.too_hot(r) else "varm" if self.too_hot(r, margin=10) else "ok"
        return " ".join(parts), level
