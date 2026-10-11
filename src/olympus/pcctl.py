"""Telefonen som fjernkontroll for PC-en: lås skjermen, skjerm av/på, hvilemodus og «åpne Olympus».

Bare faste kommandoer (argv-lister, aldri et skall, aldri tekst fra klienten). Én gren per plattform:
Linux (Hyprland, Sway, KDE, GNOME/andre via loginctl/xset), macOS (pmset/osascript) og Windows (rundll32/PowerShell).
Hyprland med Lua-oppsett: dispatchere skrives som hl.dsp.…(…) til `hyprctl dispatch`.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # olympus-mappa (src/olympus/pcctl.py → …/olympus)
WS2 = ROOT / "scripts" / "ws2.sh"
ACTIONS = ("lock", "screen_off", "screen_on", "sleep", "open_olympus")


def run_argv(argv: list[str], timeout: float = 10) -> tuple[int, str]:
    """Kjør én fast kommando. Testene bytter ut denne – ingenting her skal kjøre for ekte i en test."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=hypr_env())
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)


def hypr_env() -> dict[str, str]:
    """Miljøet med en gyldig HYPRLAND_INSTANCE_SIGNATURE: tjenesten kan ha arvet en gammel etter at Hyprland
    startet på nytt – da brukes den nyeste instansen under $XDG_RUNTIME_DIR/hypr."""
    env = dict(os.environ)
    run = Path(env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "hypr"
    sig = env.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    if not sig or not (run / sig).is_dir():
        with_sock = [d for d in run.glob("*") if (d / ".socket.sock").exists()] if run.is_dir() else []
        if with_sock:
            env["HYPRLAND_INSTANCE_SIGNATURE"] = max(with_sock, key=lambda d: d.stat().st_mtime).name
    return env


def session_id() -> str | None:
    """Den grafiske økta til brukeren (loginctl lock-session trenger den – tjenesten er ikke selv i en økt)."""
    rc, out = run_argv(["loginctl", "show-user", str(os.getuid()), "-p", "Display", "--value"])
    sid = out.strip().splitlines()[0] if rc == 0 and out.strip() else ""
    return sid if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", sid) else None


def ws2_running() -> bool:
    rc, _ = run_argv(["pgrep", "-f", str(WS2)])
    return rc == 0


def desktop() -> str:
    """windows | macos | hyprland | sway | kde | gnome | x11 | linux (ukjent Wayland/annet)."""
    sysname = platform.system()
    if sysname == "Windows":
        return "windows"
    if sysname == "Darwin":
        return "macos"
    cur = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or "hyprland" in cur:
        return "hyprland"
    if os.environ.get("SWAYSOCK") or "sway" in cur:
        return "sway"
    if "kde" in cur:
        return "kde"
    if "gnome" in cur:
        return "gnome"
    return "x11" if os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY") else "linux"


WEB_URL = "http://127.0.0.1:8787/"
_WIN_SCREEN_OFF = ("(Add-Type '[DllImport(\"user32.dll\")]public static extern int SendMessage(int h,int m,int w,int l);' "
                   "-Name a -Pas)::SendMessage(-1,0x0112,0xF170,2)")


def commands(action: str) -> list[list[str]]:
    """Kommandoene for en handling (tom liste = ukjent eller ikke støttet her)."""
    d = desktop()
    if d == "windows":
        return {"lock": [["rundll32.exe", "user32.dll,LockWorkStation"]],
                "screen_off": [["powershell", "-NoProfile", "-Command", _WIN_SCREEN_OFF]],
                "screen_on": [["powershell", "-NoProfile", "-Command", "(New-Object -ComObject WScript.Shell).SendKeys('+')"]],
                "sleep": [["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]],
                "open_olympus": [["cmd", "/c", "start", "", WEB_URL]]}.get(action, [])
    if d == "macos":
        # Ekte låsing (Ctrl+Cmd+Q, som låseskjermen i menyen) – ikke bare skjerm av, som låser bare hvis «krev passord»
        # er på. Krever at Olympus har fått lov til å styre maskinen (Tilgjengelighet); ellers feiler den, og det meldes.
        return {"lock": [["osascript", "-e", 'tell application "System Events" to keystroke "q" using {control down, command down}']],
                "screen_off": [["pmset", "displaysleepnow"]],
                "screen_on": [["caffeinate", "-u", "-t", "2"]],
                "sleep": [["pmset", "sleepnow"]],
                "open_olympus": [["open", WEB_URL]]}.get(action, [])
    if action == "lock":
        sid = session_id()
        return [["loginctl", "lock-session", *([sid] if sid else [])]]
    if action in ("screen_off", "screen_on"):
        on = action == "screen_on"
        if d == "hyprland":
            return [["hyprctl", "dispatch", f"hl.dsp.dpms({{ action = '{action[7:]}' }})"]]
        if d == "sway":
            return [["swaymsg", "output * dpms " + ("on" if on else "off")]]
        if d == "kde" and shutil.which("kscreen-doctor"):
            return [["kscreen-doctor", "--dpms", "on" if on else "off"]]
        if shutil.which("xset"):
            return [["xset", "dpms", "force", "on" if on else "off"]]
        return []
    if action == "sleep":
        return [["systemctl", "suspend"]]
    if action == "open_olympus":
        if d == "hyprland" and WS2.exists():  # eget oppsett: Lab's på workspace 2
            start = [] if ws2_running() else [["hyprctl", "dispatch", f"hl.dsp.exec_cmd('{WS2}')"]]
            return [*start, ["hyprctl", "dispatch", "hl.dsp.focus({ workspace = 2 })"]]
        return [["xdg-open", WEB_URL]]
    return []


def run_action(action: str) -> tuple[bool, str]:
    cmds = commands(action)
    if not cmds:
        return False, "ukjent handling" if action not in ACTIONS else f"ikke støttet på denne maskinen ({desktop()})"
    for argv in cmds:
        rc, out = run_argv(argv)
        if rc != 0 or out.startswith("error"):
            return False, out[:200] or f"{argv[0]} feilet ({rc})"
    return True, ""
