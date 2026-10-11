import sys
from pathlib import Path

import pytest

FAKE = Path(__file__).parent / "fake_llama_server.py"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("OLYMPUS_HOME", str(tmp_path / "olympus"))
    # Testene skal aldri se ekte llama-servere på maskinen (standardport 8770).
    (tmp_path / "olympus").mkdir()
    # … og aldri åpne TV-castingens strømmeport på hjemmenettet
    (tmp_path / "olympus" / "config.toml").write_text("[llamacpp]\nport = 18970\n\n[cast]\nenabled = false\n")
    return tmp_path / "olympus"


@pytest.fixture
def fake_config(home, tmp_path, monkeypatch):
    """Config som peker på den falske serveren og en tom modellfil."""
    from olympus import config as cfg

    wrapper = tmp_path / "llama-server"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    wrapper.chmod(0o755)
    cfg.ensure_home()
    c = cfg.load()
    c.data["llamacpp"]["binary"] = str(wrapper)
    c.data["llamacpp"]["port"] = 18770
    c.data["llamacpp"]["startup_timeout"] = 15
    c.data["llamacpp"]["extra_args"] = []
    c.data["paths"]["models_dir"] = str(tmp_path / "models")
    c.data["paths"]["logs_dir"] = str(tmp_path / "logs")
    c.data["paths"]["sessions_dir"] = str(tmp_path / "sessions")
    # Faste testmodeller (små 9B-er), uavhengig av hvilke modeller Olympus leveres med. Qwen og Qwopus er «lastet ned»,
    # NeoHorse ikke (tester fallback).
    c.data["models"] = {
        mid: {"description": f"{mid} testmodell. Til tester.", "repo": f"test/{mid}", "file": f"{mid}.gguf",
              "kind": "llm", "size_gb": size, "license": "apache-2.0", "thinking": False}
        for mid, size in (("qwen35-9b", 6.58), ("qwopus-coder-9b", 6.47), ("neohorse-9b", 6.47))
    }
    c.data["agents"]["zeus"]["model"] = "qwen35-9b"
    c.data["general"]["local_model"] = "qwen35-9b"
    c.data["general"]["brain"] = "local"  # testene kobler seg aldri til den ekte hjernen på maskinen
    c.data["general"]["open_labs"] = False  # og åpner ikke Lab's (som snakker med den ekte serveren)
    c.data["vram"]["keep_warm"] = ["qwen35-9b"]
    c.data["library"]["dir"] = str(tmp_path / "library")
    for mid in ("qwen35-9b", "qwopus-coder-9b"):
        p = c.model_path(mid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"GGUF")
    # Ingen ekte GPU-sjekk i tester.
    monkeypatch.setattr("olympus.model_manager.read_vram", lambda: None)
    return c
