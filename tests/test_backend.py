import pytest

from olympus.chat import system_prompt
from olympus.model_manager import ModelManager


@pytest.mark.asyncio
async def test_complete_and_stream_through_fake_server(fake_config):
    manager = ModelManager(fake_config)
    try:
        server = await manager.ensure("qwen35-9b")
        assert "Forbedret" in await server.complete([{"role": "user", "content": "x"}])
        text = "".join([c.text async for c in server.stream_chat([
            {"role": "system", "content": "SOUL\n\nDu er Calliope"},
            {"role": "user", "content": "hei"}]) if c.kind == "content"])
        assert text.startswith("[Calliope@qwen35-9b]")
        assert await manager.ensure("qwen35-9b") is server  # gjenbruk
    finally:
        manager.shutdown()
    assert not server.running()


@pytest.mark.asyncio
async def test_missing_binary_gives_clear_error(fake_config):
    from olympus.backends.llamacpp import BackendError

    fake_config.data["llamacpp"]["binary"] = "finnes-ikke-llama-server"
    with pytest.raises(BackendError, match="pacman"):
        await ModelManager(fake_config).ensure("qwen35-9b")


@pytest.mark.asyncio
async def test_lru_evicts_specialist_but_keeps_warm_model(fake_config):
    p = fake_config.model_path("neohorse-9b")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"GGUF")
    fake_config.data["vram"]["budget_gb"] = 14.0  # plass til to modeller, ikke tre
    manager = ModelManager(fake_config)
    try:
        await manager.ensure("qwen35-9b")
        await manager.ensure("qwopus-coder-9b")
        await manager.ensure("qwen35-9b")  # qwen nylig brukt …
        await manager.ensure("qwopus-coder-9b")  # … men nå er qwen LRU
        await manager.ensure("neohorse-9b")
        assert manager.loaded() == ["qwen35-9b", "neohorse-9b"]  # varm modell spart
    finally:
        manager.shutdown()


def test_system_prompt_has_agent_role_and_project_context(fake_config, tmp_path):
    (tmp_path / "AGENTS.md").write_text("Bruk tabs, ikke spaces.")
    prompt = system_prompt(fake_config, tmp_path, "zeus")
    assert "Bruk tabs" in prompt and "Du er Zeus" in prompt and "# Olympus" in prompt
    assert "Athena" not in prompt and "Apollo" not in prompt  # ingen andre guder igjen


@pytest.mark.asyncio
async def test_second_instance_reuses_running_server(fake_config):
    first, second = ModelManager(fake_config), ModelManager(fake_config)
    try:
        server = await first.ensure("qwen35-9b")
        shared = await second.ensure("qwen35-9b")
        assert shared.external and shared.port == server.port
        # en annen modell får en ny, ledig port
        other = await second.ensure("qwopus-coder-9b")
        assert other.port != server.port and not other.external
        second.shutdown()
        assert server.running()  # den andre instansen stopper ikke den første sin server
    finally:
        first.shutdown()
        second.shutdown()


def test_reap_orphans_stops_servers_from_dead_owner(home, tmp_path):
    import json
    import subprocess
    import sys
    import time

    from olympus import procs

    # en «server» med llama-server i kommandolinjen, i egen prosessgruppe
    script = tmp_path / "llama-server-fake.py"
    script.write_text("import time\nwhile True: time.sleep(1)\n")
    p = subprocess.Popen([sys.executable, str(script)], start_new_session=True)
    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner.wait()
    (procs._dir() / "llama-9999.json").write_text(json.dumps({"pid": p.pid, "owner": dead_owner.pid}))
    assert procs.reap_orphans() == ["llama-9999"]
    time.sleep(0.3)
    assert p.poll() is not None


def test_lora_args(fake_config, tmp_path):
    m = ModelManager(fake_config)
    assert m._lora_args("qwen35-9b") == []
    lora = tmp_path / "atlas.gguf"
    lora.write_bytes(b"x")
    fake_config.data["models"]["qwen35-9b"]["lora"] = str(lora)
    assert m._lora_args("qwen35-9b") == ["--lora", str(lora)]
    fake_config.data["models"]["qwen35-9b"]["lora_enabled"] = False
    assert m._lora_args("qwen35-9b") == []
