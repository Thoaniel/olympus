"""`olympus setup`: alt Olympus trenger på en ny maskin (Linux, macOS, Windows).

1. llama.cpp (llama-server): finnes det på PATH, brukes det; ellers lastes riktig ferdigbygd utgave ned fra
   github.com/ggml-org/llama.cpp (Vulkan på Linux/Windows – virker med AMD, NVIDIA og Intel; CUDA på Windows med
   NVIDIA; Metal på Mac), kontrollsummen sjekkes, og stien skrives i ~/.olympus/config.toml.
2. En lokal modell som passer GPU-minnet (eller ingen – du kan bruke skymodeller).
3. Autostart av serveren (systemd / launchd / Oppgaveplanlegging).
"""

from __future__ import annotations

import hashlib
import io
import platform
import shutil
import tarfile
import zipfile
from pathlib import Path
from typing import Any

from olympus import config as cfg

REPO = "ggml-org/llama.cpp"


def asset_name(system: str, machine: str, nvidia: bool) -> list[str]:
    """Nøkkelord for filen som passer (i prioritert rekkefølge)."""
    arm = machine.lower() in ("arm64", "aarch64")
    if system == "Darwin":
        return ["bin-macos-arm64"] if arm else ["bin-macos-x64"]
    if system == "Windows":
        if arm:
            return ["bin-win-vulkan-arm64", "bin-win-cpu-arm64"]
        return (["bin-win-cuda-12.4-x64"] if nvidia else []) + ["bin-win-vulkan-x64", "bin-win-cpu-x64"]
    return ["bin-ubuntu-vulkan-arm64", "bin-ubuntu-arm64"] if arm else ["bin-ubuntu-vulkan-x64", "bin-ubuntu-x64"]


def pick(assets: list[dict[str, Any]], wanted: list[str]) -> dict[str, Any] | None:
    for key in wanted:
        for a in assets:
            n = a["name"]
            if key in n and n.startswith("llama-") and n.endswith((".zip", ".tar.gz")):
                return a
    return None


def latest_assets(c: Any) -> tuple[str, list[dict[str, Any]]]:
    """Nyeste utgave med ferdigbygde filer (de nummererte bNNNN-utgavene)."""
    r = c.get(f"https://api.github.com/repos/{REPO}/releases", params={"per_page": 10})
    r.raise_for_status()
    for rel in r.json():
        if len(rel.get("assets", [])) > 5:
            return rel["tag_name"], rel["assets"]
    raise RuntimeError("Fant ingen llama.cpp-utgave med ferdigbygde filer.")


def install_llama(c: Any, dest: Path) -> Path:
    import shutil as sh

    sysname, machine = platform.system(), platform.machine()
    tag, assets = latest_assets(c)
    a = pick(assets, asset_name(sysname, machine, bool(sh.which("nvidia-smi"))))
    if a is None:
        raise RuntimeError(f"Ingen ferdigbygd llama.cpp for {sysname}/{machine}. Bygg selv: https://github.com/{REPO}")
    print(f"Laster ned {a['name']} ({a['size'] // 1_000_000} MB) …")
    data = c.get(a["browser_download_url"], follow_redirects=True, timeout=600).content
    digest = (a.get("digest") or "").removeprefix("sha256:")
    if digest and hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError("Kontrollsummen stemmer ikke – nedlastingen er avbrutt.")
    target = dest / tag
    target.mkdir(parents=True, exist_ok=True)
    extras = []
    if "win-cuda" in a["name"]:  # CUDA på Windows: runtime-bibliotekene ligger i en egen fil
        cv = a["name"].split("win-cuda-")[1].split("-")[0]
        extras = [x for x in assets if x["name"] == f"cudart-llama-bin-win-cuda-{cv}-x64.zip"]
    for x in extras:
        print(f"Laster ned {x['name']} ({x['size'] // 1_000_000} MB) …")
        xdata = c.get(x["browser_download_url"], follow_redirects=True, timeout=600).content
        if (x.get("digest") or "").removeprefix("sha256:") not in ("", hashlib.sha256(xdata).hexdigest()):
            raise RuntimeError("Kontrollsummen stemmer ikke – nedlastingen er avbrutt.")
        _unzip(xdata, target)
    if a["name"].endswith(".zip"):
        _unzip(data, target)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            t.extractall(target, filter="data")
    exe = "llama-server.exe" if sysname == "Windows" else "llama-server"
    found = next(target.rglob(exe), None)
    if found is None:
        raise RuntimeError("Fant ikke llama-server i arkivet.")
    found.chmod(0o755)
    return found


def _unzip(data: bytes, target: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for m in z.namelist():  # ingen stier ut av mappa
            if m.startswith("/") or ".." in Path(m).parts:
                raise RuntimeError("Uventet filsti i arkivet.")
        z.extractall(target)


def suggest_model(config: cfg.Config) -> str | None:
    """Største modell i katalogen som får plass i GPU-minnet (Apple: delt minne)."""
    from olympus.gpu import read_vram

    v = read_vram()
    free = (v.total_gb - 0.8) if v else 0
    llms = sorted(((float(m.get("size_gb", 0)), mid) for mid, m in config.get("models", {}).items()
                   if m.get("kind") == "llm"), reverse=True)
    return next((mid for size, mid in llms if size * 1e9 / 1024**3 + 1.0 <= free), None)


def run(config: cfg.Config) -> int:
    import httpx

    from olympus.sysutil import install_service

    home = config.home
    print("ϟ Olympus – oppsett\n")
    binary = shutil.which(str(config.get("llamacpp.binary") or "llama-server"))
    if binary:
        print(f"✓ llama.cpp: {binary}")
    else:
        with httpx.Client(timeout=30, headers={"User-Agent": "olympus-setup"}) as c:
            binary = str(install_llama(c, home / "llama.cpp"))
        cfg.set_values({"llamacpp.binary": binary}, home)
        print(f"✓ llama.cpp installert: {binary}")
    mid = suggest_model(config)
    if mid and not config.is_downloaded(mid):
        size = config.model(mid).get("size_gb")
        if input(f"Last ned den lokale modellen {mid} ({size} GB)? [J/n] ").strip().lower() in ("", "j", "y", "ja", "yes"):
            from olympus.models import pull

            pull(config, mid)
            cfg.set_values({"agents.zeus.model": mid, "general.local_model": mid}, home)
            print(f"✓ Zeus bruker {mid}")
    elif not mid:
        print("· Ingen modell får plass i GPU-minnet – bruk en skymodell («olympus modell») eller en mindre modell.")
    if input("Start Olympus automatisk ved innlogging? [J/n] ").strip().lower() in ("", "j", "y", "ja", "yes"):
        print("✓ " + install_service())
    print("\nFerdig. Start: «olympus» (terminal) eller «olympus web» (server + app). Par telefonen: «olympus pair».")
    return 0
