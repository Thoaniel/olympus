#!/usr/bin/env python3
"""Kontroll før publisering: ingenting personlig eller hemmelig i filene som skal ut.

Sjekker hver fil (også bilder – tekstfelter/metadata leses som bytes) mot
  - en lokal sperreliste (~/.olympus/release-blocklist.txt, én frase per linje – publiseres aldri),
  - mønstre for hemmeligheter (API-nøkler, private nøkler, tokens),
  - e-postadresser, Tailscale-adresser (100.64.0.0/10), MAC-adresser og hjemmemapper (/home/<navn>).

    python scripts/release_check.py [mappe]      (standard: filene git sporer)
Avslutter med kode 1 og lister funnene hvis noe er galt.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
    "privat nøkkel": rb"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----",
    "API-nøkkel": rb"\b(?:sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|ghp_[A-Za-z0-9]{30,}|gho_[A-Za-z0-9]{30,}|"
                  rb"AIza[0-9A-Za-z_-]{35}|gsk_[A-Za-z0-9]{40,}|xox[baprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{40,}\.[A-Za-z0-9_-]{20,})",
    "e-post": rb"\b[A-Za-z0-9._%+-]+@(?:gmail|hotmail|outlook|yahoo|icloud|proton(?:mail)?|live|online)\.[a-z]{2,}\b",
    "Tailscale-adresse": rb"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b",
    "MAC-adresse": rb"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b",
    "hjemmemappe": rb"/home/(?!user\b|USER\b|you\b|olympus\b)[a-z][a-z0-9_-]{1,30}/",
}
# Kjente, ufarlige treff (eksempler i tester/dokumentasjon)
ALLOW = [rb"100\.64\.\d\.\d{1,3}", rb"00:00:00:00:00:00", rb"AA:BB:CC:DD:EE:FF", rb"aa:bb:cc:dd:ee:ff"]


def files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
        return [root / f for f in out.decode().split("\0") if f]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    bl = Path.home() / ".olympus" / "release-blocklist.txt"
    words = [w.strip() for w in bl.read_text().splitlines() if w.strip() and not w.startswith("#")] if bl.exists() else []
    if not words:
        print("⚠ Ingen sperreliste (~/.olympus/release-blocklist.txt) – bare de generelle mønstrene sjekkes.")
    hits = []
    for f in files(root):
        try:
            data = f.read_bytes()
        except OSError:
            continue
        low = data.lower()
        rel = f.relative_to(root)
        for w in words:
            if w.lower().encode() in low:
                hits.append(f"{rel}: sperreord «{w}»")
        for name, pat in PATTERNS.items():
            for m in re.finditer(pat, data):
                if not any(re.fullmatch(a, m.group(0)) for a in ALLOW):
                    hits.append(f"{rel}: {name} «{m.group(0)[:40].decode(errors='replace')}»")
    if hits:
        print("✗ Fant ting som ikke skal publiseres:")
        print("\n".join(f"  {h}" for h in sorted(set(hits))))
        return 1
    print(f"✓ {len(files(root))} filer sjekket – ingenting personlig eller hemmelig funnet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
