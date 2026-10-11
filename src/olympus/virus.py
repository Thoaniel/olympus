"""Viruskontroll for nedlastinger: alt sjekkes før det havner i biblioteket.

To lag:
  1. Programmer forkledd som media: en «film» eller «sang» som egentlig er et Windows-program (MZ), et Linux-program
     (ELF), et skript (#!) eller et Mac-program – den vanligste trojaneren i torrenter. Virker alltid.
  2. ClamAV på alle filene: systemets (clamd/clamscan) hvis den finnes, ellers en utgave pakket ut uten root i
     ~/.local/opt/clamav med databasen i ~/.olympus/clamav (scripts/clamav_local.sh; oppdateres daglig av
     olympus-freshclam.timer). Mangler begge, sier resultatet «ikke sjekket av ClamAV».

Kjøres i en tråd (blokkerer). nice/ionice, så skanningen ikke varmer opp PC-en mer enn nødvendig.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Filtyper som aldri hører hjemme i et mediebibliotek: kjørbare filer og skript.
RISKY_EXT = {".exe", ".scr", ".bat", ".cmd", ".com", ".pif", ".msi", ".lnk", ".vbs", ".vbe", ".js", ".jse", ".wsf",
             ".ps1", ".hta", ".jar", ".apk", ".dll", ".sh", ".run", ".appimage", ".deb", ".rpm", ".dmg", ".app"}
MAGIC = {b"MZ": "Windows-program", b"\x7fELF": "Linux-program", b"#!": "skript", b"\xcf\xfa\xed\xfe": "Mac-program",
         b"\xca\xfe\xba\xbe": "Mac-/Java-program"}


@dataclass
class Result:
    clean: bool = True
    hits: list[tuple[str, str]] = field(default_factory=list)  # (fil, hva)
    clamav: bool = False  # ClamAV faktisk kjørte
    risky_extras: list[str] = field(default_factory=list)  # kjørbare filer i nedlastingen (importeres aldri)

    def summary(self) -> str:
        if not self.clean:
            return "Virus/trojaner funnet: " + "; ".join(f"{Path(f).name} ({what})" for f, what in self.hits[:5])
        note = "ren" if self.clamav else "ren (ikke sjekket av ClamAV – installer clamav)"
        if self.risky_extras:
            note += f"; {len(self.risky_extras)} programfil(er) i nedlastingen ble ikke tatt med"
        return note


def disguised(path: Path) -> str:
    """Hva fila egentlig er hvis den er et program forkledd som media/e-bok; '' ellers."""
    try:
        with path.open("rb") as f:
            head = f.read(4)
    except OSError:
        return ""
    return next((what for magic, what in MAGIC.items() if head.startswith(magic)), "")


LOCAL = Path.home() / ".local" / "opt" / "clamav"  # pakket ut uten root (scripts/clamav_local.sh)
LOCAL_DB = Path.home() / ".olympus" / "clamav" / "db"


def clamav_env() -> dict[str, str]:
    import os

    env = dict(os.environ)
    if (LOCAL / "usr" / "bin" / "clamscan").exists():
        env["LD_LIBRARY_PATH"] = str(LOCAL / "usr" / "lib")
        env["CVD_CERTS_DIR"] = str(LOCAL / "etc" / "clamav" / "certs")
    return env


def clamav_cmd() -> list[str] | None:
    """Systemets clamd (rask, hvis den kjører) → systemets clamscan → den lokale utgaven i ~/.local/opt/clamav."""
    if shutil.which("clamdscan") and Path("/run/clamav/clamd.ctl").exists():
        return ["clamdscan", "--fdpass", "--no-summary", "--infected", "--multiscan"]
    if shutil.which("clamscan"):
        return ["clamscan", "--no-summary", "--infected", "--recursive"]
    local = LOCAL / "usr" / "bin" / "clamscan"
    if local.exists() and any(LOCAL_DB.glob("*.cvd")):
        return [str(local), f"--database={LOCAL_DB}", "--no-summary", "--infected", "--recursive"]
    return None


def _nice(cmd: list[str]) -> list[str]:
    out = ["nice", "-n", "15"] + cmd
    return (["ionice", "-c", "3"] + out) if shutil.which("ionice") else out


def scan(paths: list[Path], media_ext: set[str]) -> Result:
    """Sjekk filene (og mappene) i en nedlasting. media_ext = endelsene som skal inn i biblioteket."""
    res = Result()
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            files += [f for f in p.rglob("*") if f.is_file() and not f.is_symlink()]
        elif p.is_file() and not p.is_symlink():
            files.append(p)
    for f in files:
        ext = f.suffix.lower()
        if ext in RISKY_EXT:
            res.risky_extras.append(str(f))
        if ext in media_ext and (what := disguised(f)):
            res.hits.append((str(f), f"{what} forkledd som {ext}"))
    cmd = clamav_cmd()
    if cmd and files:
        try:
            p = subprocess.run(_nice(cmd + ["--", *[str(x) for x in paths if x.exists()]]), capture_output=True,
                               text=True, timeout=6 * 3600, env=clamav_env())
            if p.returncode in (0, 1):
                res.clamav = True
                for line in p.stdout.splitlines():
                    if line.rstrip().endswith("FOUND") and ": " in line:
                        f, sig = line.rsplit(": ", 1)
                        res.hits.append((f, sig.removesuffix(" FOUND").strip()))
        except (OSError, subprocess.TimeoutExpired):
            pass
    res.clean = not res.hits
    return res
