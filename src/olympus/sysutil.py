"""Det lille som er ulikt fra plattform til plattform: utklippstavle, «åpne fil», om en tjeneste kjører, og autostart
av Olympus-serveren (systemd på Linux, launchd på macOS, Oppgaveplanlegging på Windows)."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

SYSTEM = platform.system()  # Linux | Darwin | Windows


def copy_text(text: str) -> bool:
    """Legg tekst på utklippstavla (i tillegg til OSC 52 i terminalen)."""
    for argv in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"], ["pbcopy"], ["clip"]):
        if shutil.which(argv[0]):
            try:
                subprocess.run(argv, input=text.encode("utf-16le" if argv[0] == "clip" else "utf-8"), timeout=2, check=False)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


def open_path(target: str | Path) -> None:
    """Åpne en fil/lenke med standardprogrammet."""
    t = str(target)
    if SYSTEM == "Windows":
        os.startfile(t)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["open" if SYSTEM == "Darwin" else "xdg-open", t], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# tjeneste → prosessnavn (når systemd ikke finnes eller ikke kjenner den)
PROCESS = {"olympus-web": ("olympus",), "transmission": ("transmission-daemon", "transmission-daemon.exe"),
           "tailscaled": ("tailscaled", "tailscaled.exe", "tailscale-ipn.exe", "IPNExtension")}


def service_active(unit: str) -> bool:
    if shutil.which("systemctl"):
        for scope in (["--user"], []):
            r = subprocess.run(["systemctl", *scope, "is-active", unit], capture_output=True, text=True)
            if r.stdout.strip() == "active":
                return True
    import psutil

    names = PROCESS.get(unit, (unit,))
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            name = p.info["name"] or ""
            cmd = " ".join(p.info["cmdline"] or [])
        except (psutil.Error, TypeError):
            continue
        if name in names or (unit == "olympus-web" and "olympus" in cmd and " web" in cmd):
            return True
    return False


def _olympus_cmd() -> list[str]:
    exe = shutil.which("olympus")
    return [exe, "web"] if exe else [sys.executable, "-m", "olympus", "web"]


def install_service() -> str:
    """Start Olympus-serveren automatisk ved innlogging. Returnerer en kort beskrivelse av hva som ble gjort."""
    cmd = _olympus_cmd()
    if SYSTEM == "Linux":
        unit = Path.home() / ".config" / "systemd" / "user" / "olympus-web.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        # Always running: the desktop (Olymparch's bar, the phone) talks to it and never starts it by itself.
        # Restarted after any exit, also a clean one, and never given up on (no start limit).
        unit.write_text("[Unit]\nDescription=Olympus (Zeus)\nAfter=network-online.target\nStartLimitIntervalSec=0\n\n"
                        f"[Service]\nExecStart={' '.join(cmd)}\nRestart=always\nRestartSec=3\n\n"
                        "[Install]\nWantedBy=default.target\n")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", "olympus-web"], check=False)
        return f"systemd-tjeneste: {unit} (systemctl --user status olympus-web)"
    if SYSTEM == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "no.olympus.web.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        args = "".join(f"<string>{a}</string>" for a in cmd)
        plist.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
                         "<key>Label</key><string>no.olympus.web</string>"
                         f"<key>ProgramArguments</key><array>{args}</array>"
                         "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>\n")
        subprocess.run(["launchctl", "load", "-w", str(plist)], check=False)
        return f"launchd-agent: {plist}"
    if SYSTEM == "Windows":
        line = subprocess.list2cmdline(cmd)
        subprocess.run(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", "Olympus", "/TR", line], check=False)
        return "Oppgaveplanlegging: «Olympus» starter ved pålogging"
    return "Ukjent plattform – start «olympus web» selv."
