"""Kommandolinje: `olympus`, `olympus "spørsmål"`, `olympus config|models|doctor|setup`."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys

from olympus import __version__
from olympus import config as cfg

SUBCOMMANDS = {"config", "models", "modell", "doctor", "setup", "service", "auth", "library", "web", "pair", "status", "zeus"}


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [j/N] ").strip().lower() in ("j", "ja", "y", "yes")
    except EOFError:
        return False


# --- engangskjøring -------------------------------------------------------
async def _ask(config: cfg.Config, question: str) -> int:
    """Engangskjøring. Svaret går til stdout; status til stderr."""
    from olympus.agents.orchestrator import Orchestrator
    from olympus.backends.llamacpp import BackendError
    from olympus.model_manager import ModelManager, VramError

    def err(msg: str) -> None:
        print(f"\033[2m{msg}\033[0m", file=sys.stderr)

    from olympus import procs

    procs.reap_orphans()
    manager = ModelManager(config, on_status=lambda m: err(f"[{m}]"))
    orch = Orchestrator(config, manager)
    try:
        async for ev in orch.run(question):
            if ev.kind == "notice":
                err(f"[{ev.text}]")
            elif ev.kind == "agent_start" and orch.backends.is_cloud(ev.model):
                err(f"[☁ {ev.model}: sendes til {orch.backends.provider_label(ev.model)}]")
            elif ev.kind == "chunk" and ev.chunk and ev.chunk.kind == "content":
                sys.stdout.write(ev.chunk.text)
                sys.stdout.flush()
            elif ev.kind == "agent_end":
                sys.stdout.write("\n")
        return 0
    except (BackendError, VramError, KeyError, ValueError) as e:
        print(f"olympus: {e}", file=sys.stderr)
        return 1
    finally:
        manager.shutdown()


# --- config ---------------------------------------------------------------
def cmd_config(config: cfg.Config, args: argparse.Namespace) -> int:
    if args.action == "get":
        if not args.key:
            import tomlkit

            print(tomlkit.dumps(config.data), end="")
            return 0
        value = config.get(args.key)
        if value is None:
            print(f"Ukjent nøkkel: {args.key}", file=sys.stderr)
            return 1
        print(value)
        return 0
    if args.action == "set":
        if not args.key or args.value is None:
            print("Bruk: olympus config set <nøkkel> <verdi>", file=sys.stderr)
            return 2
        value = cfg.set_value(args.key, args.value)
        print(f"{args.key} = {value!r}")
        return 0
    if args.action == "path":
        print(config.home / "config.toml")
        return 0
    return 2


# --- models ---------------------------------------------------------------
def cmd_models(config: cfg.Config, args: argparse.Namespace) -> int:
    from olympus import models

    if args.action == "list":
        print(f"{'ID':<16} {'TYPE':<6} {'STØRRELSE':>9}  {'LISENS':<12} STATUS")
        for row in models.status_rows(config):
            print(f"{row[0]:<16} {row[1]:<6} {row[2]:>9}  {row[3]:<12} {row[4]}")
        print(f"\nModellmappe: {config.path('models_dir')}")
        return 0
    if not args.model:
        print(f"Bruk: olympus models {args.action} <id>", file=sys.stderr)
        return 2
    try:
        m = config.model(args.model)
    except KeyError as e:
        print(e, file=sys.stderr)
        return 1
    if args.action == "pull":
        path = config.model_path(args.model)
        if path.exists():
            print(f"Finnes allerede: {path}")
            return 0
        free = shutil.disk_usage(config.path("models_dir")).free / 1e9
        print(f"Modell:  {args.model}\nRepo:    {m['repo']}\nFil:     {m['file']}")
        print(f"Lisens:  {m.get('license', '?')}\nStørrelse: {m.get('size_gb', '?')} GB "
              f"(ledig på disk: {free:.0f} GB)\nMål:     {path}")
        if not args.yes and not _confirm("Laste ned?"):
            print("Avbrutt.")
            return 1
        print(models.pull(config, args.model))
        return 0
    if args.action == "rm":
        if not args.yes and not _confirm(f"Slette {config.model_path(args.model)}?"):
            return 1
        print("Slettet." if models.remove(config, args.model) else "Var ikke lastet ned.")
        return 0
    return 2


# --- modell / auth -----------------------------------------------------------
def cmd_modell(config: cfg.Config, args: argparse.Namespace) -> int:
    """`olympus modell` viser valgene; `olympus modell <id|lokal>` bytter (lagres i config.toml)."""
    from olympus.model_choices import (
        ModelCatalog,
        cloud_warning,
        current,
        resolve_choice,
        set_model,
    )
    from olympus.model_manager import ModelManager
    from olympus.providers import Backends

    backends = Backends(config, ModelManager(config))
    catalog = ModelCatalog(config, backends)
    if not args.model:
        cur = current(config)
        for ch in catalog.choices():
            mark = "●" if ch["id"] == cur else "○"
            print(f"{mark} {'☁ ' if ch['cloud'] else '  '}{ch['id']:<34} {ch.get('note', '')}")
        print("\nBytt med `olympus modell <id>`, tilbake til lokalt med `olympus modell lokal`. I TUI-en: /modell eller F7.")
        return 0
    ref = resolve_choice(catalog, args.model)
    if ref is None:
        print(f"Ukjent modell: {args.model}. `olympus modell` viser valgene.", file=sys.stderr)
        return 1
    set_model(config, ref)
    print(f"Zeus svarer nå med {ref}.")
    warn = cloud_warning(config, backends, ref)
    if warn:
        print(warn)
    print("(Kjører olympus-web, gjelder det fra neste melding.)")
    return 0


def _media_keys(config: cfg.Config, args: argparse.Namespace) -> int:
    """Nøkler til undertekster (OpenSubtitles: API-nøkkel + bruker) og filminfo (TMDb: API-nøkkel). Lagres i
    auth.json (kun lesbar for deg)."""
    import getpass
    import json
    import os

    from olympus.providers import auth_path, load_auth

    data = load_auth(config)
    prov = args.provider
    if args.action == "logout":
        data.pop(prov, None)
    elif args.action == "status":
        print(f"{prov}: {'nøkkel lagret' if prov in data else 'ingen nøkkel'}")
        return 0
    elif prov == "opensubtitles":
        print("Gratis konto og API-nøkkel: https://www.opensubtitles.com/en/consumers (nedlasting krever innlogging).")
        data[prov] = {"key": getpass.getpass("API-nøkkel (vises ikke): ").strip(),
                      "username": input("Brukernavn: ").strip(), "password": getpass.getpass("Passord (vises ikke): ")}
    else:
        print("Gratis nøkkel: https://www.themoviedb.org/settings/api (API Read Access Token eller API-nøkkel v3).")
        data[prov] = {"key": getpass.getpass("TMDb-nøkkel (vises ikke): ").strip()}
    p = auth_path(config)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2))
    print("Fjernet." if args.action == "logout" else f"Lagret i {p} (kun lesbar for deg). Start Olympus på nytt: "
          "systemctl --user restart olympus-web")
    return 0


def cmd_auth(config: cfg.Config, args: argparse.Namespace) -> int:
    import getpass

    from olympus.providers import auth_path, load_auth, save_auth

    if args.provider in ("opensubtitles", "tmdb"):
        return _media_keys(config, args)
    if args.provider != "anthropic":
        print("Nøkler: anthropic, opensubtitles, tmdb. Claude Code bruker innloggingen i `claude`.", file=sys.stderr)
        return 2
    if args.action == "login":
        print("Lag en nøkkel på https://console.anthropic.com/settings/keys (betaling per token).")
        key = getpass.getpass("Anthropic API-nøkkel (vises ikke): ").strip()
        if not key.startswith("sk-ant-"):
            print("Det ser ikke ut som en Anthropic-nøkkel (sk-ant-…). Ingenting lagret.", file=sys.stderr)
            return 1
        save_auth(config, "anthropic", key)
        print(f"Lagret i {auth_path(config)} (kun lesbar for deg).")
        return 0
    if args.action == "logout":
        save_auth(config, "anthropic", None)
        print("Nøkkelen er fjernet.")
        return 0
    env = config.get("providers.anthropic.api_key_env", "ANTHROPIC_API_KEY")
    import os

    if os.environ.get(env):
        print(f"anthropic: nøkkel fra ${env}")
    elif load_auth(config).get("anthropic"):
        print(f"anthropic: nøkkel i {auth_path(config)}")
    else:
        print("anthropic: ingen nøkkel. `olympus auth login anthropic`")
    claude = shutil.which(config.get("providers.claude-code.binary", "claude"))
    print(f"claude-code: {claude or 'ikke installert'}")
    return 0


def cmd_library(config: cfg.Config, args: argparse.Namespace) -> int:
    from olympus.library import Library

    lib = Library(config)
    if args.action == "index":
        counts = lib.build(progress=lambda s, i, n: print(f"  {s}: {i}/{n}", file=sys.stderr))
        for src, n in counts.items():
            print(f"{src}: {n} dokumenter")
        return 0
    if args.action == "sources":
        for name, path in lib.sources().items():
            print(f"{name:<12} {path}")
        return 0
    query = " ".join(args.query)
    if not query:
        print("Bruk: olympus library search <ord>", file=sys.stderr)
        return 2
    for h in lib.search(query, 10):
        print(f"[{h.id}] {h.title} § {h.section or 'innledning'}\n    {h.snippet[:160]}")
    return 0


# --- doctor (enkel versjon; utvides i milepæl 5) ----------------------------
def cmd_doctor(config: cfg.Config) -> int:
    from olympus.gpu import gpu_name, read_vram
    from olympus.model_manager import ModelManager, VramError

    problems = 0

    def line(ok: bool | None, text: str, hint: str = "") -> None:
        nonlocal problems
        mark = {True: "\033[32m✓\033[0m", False: "\033[31m✗\033[0m", None: "\033[33m!\033[0m"}[ok]
        print(f" {mark} {text}")
        if hint:
            print(f"     → {hint}")
        if ok is False:
            problems += 1

    print(f"Olympus {__version__} · config {config.home / 'config.toml'}\n")
    vram = read_vram()
    if vram:
        line(True, f"GPU: {gpu_name()} · VRAM {vram.used_gb:.1f}/{vram.total_gb:.1f} GB brukt")
        if vram.used_gb > 3:
            line(None, f"{vram.used_gb:.1f} GB VRAM er allerede i bruk av andre programmer",
                 "Sjekk `ollama ps` og frigjør med `ollama stop <modell>`.")
    else:
        line(False, "Fant ingen amdgpu-VRAM i /sys/class/drm")

    if shutil.which("ollama") and subprocess.run(
        ["systemctl", "is-active", "--quiet", "ollama"], check=False
    ).returncode == 0:
        ps = subprocess.run(["ollama", "ps"], capture_output=True, text=True, check=False).stdout
        loaded = [l.split()[0] for l in ps.splitlines()[1:] if l.strip()]
        if loaded:
            line(None, f"Ollama holder modeller i VRAM: {', '.join(loaded)}",
                 f"`ollama stop {loaded[0]}` (eller sett OLLAMA_KEEP_ALIVE kortere)")

    binary = config.get("llamacpp.binary")
    found = shutil.which(binary)
    if found:
        devs = subprocess.run(
            [found, "--list-devices"], capture_output=True, text=True, check=False, timeout=30
        )
        line(True, f"llama-server: {found}")
        devices = [l.strip() for l in devs.stdout.splitlines() if l.startswith("  ")]
        for d in devices:
            print(f"     {d}")
        if not devices:
            line(False, "llama-server finner ingen GPU (`llama-server --list-devices`)",
                 "Sjekk at ggml-vulkan (eller ggml-hip) er installert.")
    else:
        line(False, f"`{binary}` mangler", "sudo pacman -S llama-cpp ggml-vulkan")

    manager = ModelManager(config)
    for mid, m in config.get("models", {}).items():
        if m.get("kind") != "llm":
            continue
        path = config.model_path(mid)
        if not path.exists():
            line(None, f"Modell {mid} er ikke lastet ned", f"olympus models pull {mid}")
            continue
        try:
            manager.check_fit(mid)
            line(True, f"Modell {mid} ({manager.footprint_gb(mid):.1f} GB med overhead) får plass")
        except VramError as e:
            line(False, f"Modell {mid} får ikke plass nå", str(e).replace("\n", "\n       "))

    for key in ("models_dir", "logs_dir"):
        p = config.path(key)
        p.mkdir(parents=True, exist_ok=True)
        line(shutil.disk_usage(p).free > 0, f"{key}: {p}")

    print("\nAlt ser bra ut." if problems == 0 else f"\n{problems} problem(er) funnet.")
    return 0 if problems == 0 else 1


# --- inngang --------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="olympus", description="Lokal AI-assistent i terminalen.")
    p.add_argument("--version", action="version", version=f"olympus {__version__}")
    sub = p.add_subparsers(dest="command")
    c = sub.add_parser("config", help="les/skriv config")
    c.add_argument("action", choices=["get", "set", "path"])
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="?")
    m = sub.add_parser("models", help="list/last ned/slett modeller")
    m.add_argument("action", choices=["list", "pull", "rm"])
    m.add_argument("model", nargs="?")
    m.add_argument("-y", "--yes", action="store_true", help="ikke spør om bekreftelse")
    sub.add_parser("doctor", help="diagnose GPU, backends og modeller")
    mo = sub.add_parser("modell", help="vis eller bytt modellen Zeus svarer med (lokal eller sky)")
    mo.add_argument("model", nargs="?", help="modell-id fra lista, eller «lokal»")
    li = sub.add_parser("library", help="bibliotek: Arch Wiki + egne dokumenter")
    li.add_argument("action", choices=["index", "search", "sources"])
    li.add_argument("query", nargs="*")
    au = sub.add_parser("auth", help="API-nøkkel for skyproviders")
    au.add_argument("action", choices=["login", "logout", "status"])
    au.add_argument("provider", nargs="?", default="anthropic")
    sub.add_parser("setup", help="sett opp en ny maskin: llama.cpp, lokal modell og autostart")
    sv = sub.add_parser("service", help="start Olympus-serveren automatisk ved innlogging (systemd/launchd/Windows)")
    sv.add_argument("action", choices=["install"])
    sub.add_parser("web", help="Olympus på telefonen: mobil-app over Tailscale (skriver ut lenke og QR-kode)",
                   description="Starter en webserver med en mobil-app. Lytter bare på localhost og Tailscale-adressen, "
                               "og krever en hemmelig nøkkel (~/.olympus/web-token). Installer Tailscale på PC og "
                               "telefon først: sudo pacman -S tailscale && sudo systemctl enable --now tailscaled && "
                               "sudo tailscale up")
    sub.add_parser("pair", help="Par Zeus-appen på telefonen (viser en QR-kode som gjelder i 10 minutter)")
    sub.add_parser("zeus", help="eget vindu som viser hva Zeus gjør og lar deg styre ham (godkjenn, stopp, AI av/på)")
    sub.add_parser("status", help="levende statusvindu: tjenester, Zeus, maskinen, plan, nedlastinger og telefoner")
    web_p = sub.choices["web"]
    web_p.add_argument("--link", action="store_true", help="bare vis lenken og QR-koden (når tjenesten allerede kjører)")
    return p


def main(argv: list[str] | None = None) -> int:

    argv = list(sys.argv[1:] if argv is None else argv)
    config = cfg.load()

    # `olympus "spørsmål"` — alt som ikke er en underkommando eller et flagg.
    if argv and argv[0] not in SUBCOMMANDS and not argv[0].startswith("-"):
        return asyncio.run(_ask(config, " ".join(argv)))

    args = build_parser().parse_args(argv)
    match args.command:
        case None:
            from olympus.tui.app import run

            run(config)
            return 0
        case "config":
            return cmd_config(config, args)
        case "models":
            return cmd_models(config, args)
        case "doctor":
            return cmd_doctor(config)
        case "modell":
            return cmd_modell(config, args)
        case "auth":
            return cmd_auth(config, args)
        case "library":
            return cmd_library(config, args)
        case "zeus":
            from olympus.tui.zeus_window import run as zeus_run

            return zeus_run(config)
        case "status":
            from olympus.statusview import run as status_run

            return status_run(config)
        case "pair":
            from olympus.web.server import show_pair

            show_pair(config)
            return 0
        case "web":
            from olympus.web.server import run, show_link

            if args.link:
                show_link(config)
            else:
                run(config)
            return 0
        case "setup":
            from olympus.setup import run as setup_run

            return setup_run(config)
        case "service":
            from olympus.sysutil import install_service

            print(install_service())
            return 0
    return 2
