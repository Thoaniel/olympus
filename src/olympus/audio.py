"""Lyden på PC-en: utganger (PipeWire/PulseAudio via pactl), volum og Bluetooth-høyttaleren (bluetoothctl) på Linux;
volum/demping via osascript på macOS. Windows: ikke støttet ennå (tomme lister). Alt er lokale kommandoer på PC-en.
"""

from __future__ import annotations

import asyncio
import json
import platform
import re
from typing import Any

MACOS = platform.system() == "Darwin"

MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


async def _run(*args: str, timeout: float = 15) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return out.decode(errors="replace")
    except (OSError, TimeoutError):
        return ""


async def outputs() -> list[dict[str, Any]]:
    if MACOS:
        vol = (await _run("osascript", "-e", "output volume of (get volume settings)")).strip()
        muted = (await _run("osascript", "-e", "output muted of (get volume settings)")).strip() == "true"
        return [{"name": "system", "label": "Systemlyd", "volume": int(vol) if vol.isdigit() else 0, "muted": muted,
                 "default": True, "bluetooth": False}] if vol else []
    raw = await _run("pactl", "-f", "json", "list", "sinks")
    default = (await _run("pactl", "get-default-sink")).strip()
    try:
        sinks = json.loads(raw or "[]")
    except ValueError:
        return []
    out = []
    for s in sinks:
        vols = [int(str(v.get("value_percent", "0")).rstrip("%")) for v in (s.get("volume") or {}).values()]
        out.append({"name": s.get("name"), "label": s.get("description") or s.get("name"),
                    "volume": max(vols) if vols else 0, "muted": bool(s.get("mute")), "default": s.get("name") == default,
                    "bluetooth": str(s.get("name", "")).startswith("bluez_")})
    return out


async def set_volume(percent: int, sink: str | None = None) -> None:
    if MACOS:
        await _run("osascript", "-e", f"set volume output volume {max(0, min(100, int(percent)))}")
        return
    await _run("pactl", "set-sink-volume", sink or "@DEFAULT_SINK@", f"{max(0, min(150, int(percent)))}%")


async def set_mute(muted: bool, sink: str | None = None) -> None:
    if MACOS:
        await _run("osascript", "-e", "set volume " + ("with" if muted else "without") + " output muted")
        return
    await _run("pactl", "set-sink-mute", sink or "@DEFAULT_SINK@", "1" if muted else "0")


async def set_default(sink: str) -> bool:
    if sink not in {o["name"] for o in await outputs()}:
        return False
    await _run("pactl", "set-default-sink", sink)
    return True


async def bluetooth() -> list[dict[str, Any]]:
    """Parede Bluetooth-enheter og om de er koblet til."""
    out = []
    for line in (await _run("bluetoothctl", "devices", "Paired")).splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[0] == "Device" and MAC.match(parts[1]):
            info = await _run("bluetoothctl", "info", parts[1])
            out.append({"mac": parts[1], "name": parts[2], "connected": "Connected: yes" in info,
                        "audio": "Audio Sink" in info or "0000110b" in info.lower()})
    return out


async def bt_connect(mac: str, connect: bool = True) -> bool:
    if not MAC.match(mac or ""):
        return False
    out = await _run("bluetoothctl", "connect" if connect else "disconnect", mac, timeout=25)
    return "successful" in out.lower()


async def status() -> dict[str, Any]:
    outs, bt = await asyncio.gather(outputs(), bluetooth())
    return {"outputs": outs, "bluetooth": bt}
