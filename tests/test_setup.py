"""olympus setup: riktig ferdigbygd llama.cpp for plattformen, og modellforslag etter GPU-minnet."""
from olympus.setup import asset_name, pick

ASSETS = [{"name": n} for n in (
    "llama-b1-bin-ubuntu-vulkan-x64.tar.gz", "llama-b1-bin-ubuntu-x64.tar.gz", "llama-b1-bin-ubuntu-vulkan-arm64.tar.gz",
    "llama-b1-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip", "llama-b1-bin-win-vulkan-x64.zip",
    "llama-b1-bin-win-cpu-x64.zip", "llama-b1-bin-macos-arm64.tar.gz", "llama-b1-bin-macos-x64.tar.gz")]


def test_right_llama_cpp_per_platform():
    want = {("Linux", "x86_64", False): "ubuntu-vulkan-x64", ("Linux", "aarch64", False): "ubuntu-vulkan-arm64",
            ("Windows", "AMD64", True): "win-cuda-12.4-x64", ("Windows", "AMD64", False): "win-vulkan-x64",
            ("Darwin", "arm64", False): "macos-arm64", ("Darwin", "x86_64", False): "macos-x64"}
    for (s, m, nv), part in want.items():
        a = pick(ASSETS, asset_name(s, m, nv))
        assert a and part in a["name"] and a["name"].startswith("llama-"), (s, m, nv, a)


def test_model_suggestion_fits_vram(fake_config, monkeypatch):
    from olympus import setup
    from olympus.gpu import VramInfo

    monkeypatch.setattr("olympus.gpu.read_vram", lambda: VramInfo("x", 16 * 1024**3, 0))
    assert setup.suggest_model(fake_config) in fake_config.get("models")
    monkeypatch.setattr("olympus.gpu.read_vram", lambda: VramInfo("x", 2 * 1024**3, 0))
    assert setup.suggest_model(fake_config) is None


def test_service_always_runs(tmp_path, monkeypatch):
    """The server is a user service that is always running: restarted after any exit, never given up on."""
    from olympus import sysutil

    calls = []
    monkeypatch.setattr(sysutil, "SYSTEM", "Linux")
    monkeypatch.setattr(sysutil.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(sysutil.subprocess, "run", lambda cmd, **_k: calls.append(cmd))
    sysutil.install_service()
    unit = (tmp_path / ".config/systemd/user/olympus-web.service").read_text()
    assert "Restart=always" in unit and "StartLimitIntervalSec=0" in unit
    assert " web" in next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    assert ["systemctl", "--user", "enable", "--now", "olympus-web"] in calls
