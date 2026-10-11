import pytest

from olympus import config as cfg
from olympus.backends.llamacpp import BackendError, parse_sse_line
from olympus.gpu import GiB, read_vram
from olympus.model_manager import ModelManager, VramError
from olympus.tui.banner import banner_lines, word


def test_ensure_home_creates_defaults(home):
    (home / "config.toml").unlink()
    cfg.ensure_home()
    assert "# Dine Olympus-innstillinger" in (home / "config.toml").read_text()
    for name in ("config.toml", "theme.toml", "SOUL.md"):
        assert (home / name).exists()
    assert (home / "memory").is_dir() and (home / "skills").is_dir()


def test_user_config_overrides_but_keeps_defaults(home):
    cfg.ensure_home()
    (home / "config.toml").write_text('[vram]\nbudget_gb = 12.0\n')
    c = cfg.load()
    assert c.get("vram.budget_gb") == 12.0
    assert c.get("vram.overhead_gb") == 0.8  # fra standardverdiene
    assert c.agent("zeus")["icon"] == "ϟ" and list(c.get("agents")) == ["zeus"]  # bare Zeus
    assert "presets" not in c.data and "judge" not in c.data and "router" not in c.data


def test_set_value_preserves_comments(home):
    cfg.ensure_home()
    path = home / "config.toml"
    path.write_text("# Min kommentar\n" + path.read_text())
    cfg.set_value("vram.budget_gb", "13.5")
    cfg.set_value("general.startup_animation", "false")
    text = (home / "config.toml").read_text()
    assert "# Min kommentar" in text
    c = cfg.load()
    assert c.get("vram.budget_gb") == 13.5 and c.get("general.startup_animation") is False


def test_parse_sse():
    assert parse_sse_line("") == []
    assert parse_sse_line("data: [DONE]") is None
    chunks = parse_sse_line('data: {"choices":[{"delta":{"reasoning_content":"hm","content":"hei"}}]}')
    assert [(c.kind, c.text) for c in chunks] == [("reasoning", "hm"), ("content", "hei")]
    usage = parse_sse_line('data: {"choices":[],"usage":{"total_tokens":42}}')
    assert usage[0].kind == "usage" and usage[0].usage["total_tokens"] == 42
    with pytest.raises(BackendError):
        parse_sse_line('data: {"error":{"message":"boom"}}')


def test_read_vram_picks_largest_card(tmp_path):
    for card, total, used in (("card0", 512 * 1024**2, 1), ("card1", 16 * GiB, 4 * GiB)):
        d = tmp_path / card / "device"
        d.mkdir(parents=True)
        (d / "mem_info_vram_total").write_text(str(total))
        (d / "mem_info_vram_used").write_text(str(used))
    info = read_vram(tmp_path)
    assert info.card == "card1" and info.free_gb == pytest.approx(12.0)
    assert read_vram(tmp_path / "nope") is None


def test_check_fit_reports_foreign_usage_and_smaller_quant(home, monkeypatch):
    from olympus.gpu import VramInfo

    monkeypatch.setattr(
        "olympus.model_manager.read_vram", lambda: VramInfo("card1", 16 * GiB, 12 * GiB)
    )
    c = cfg.load()
    c.data["models"]["mini-q5"] = {"repo": "x/y", "file": "a.gguf", "kind": "llm", "size_gb": 6.5}
    c.data["models"]["mini-q4"] = {"repo": "x/y", "file": "b.gguf", "kind": "llm", "size_gb": 3.0}
    m = ModelManager(c)
    with pytest.raises(VramError) as e:
        m.check_fit("mini-q5")
    assert "andre programmer" in str(e.value) and "mini-q4" in str(e.value)


def test_banner_shape():
    logo = word("OLYMPUS")
    assert len(logo) == 6 and len({len(r) for r in logo}) == 1
    assert len(banner_lines()) > 10


@pytest.mark.asyncio
async def test_wait_free_waits_for_driver_to_release(home, monkeypatch):
    from olympus.gpu import VramInfo

    readings = iter([12, 12, 4, 4])  # GiB brukt: frigjøres etter to avlesninger
    monkeypatch.setattr(
        "olympus.model_manager.read_vram",
        lambda: VramInfo("card1", 16 * GiB, next(readings) * GiB),
    )
    m = ModelManager(cfg.load())
    assert await m._wait_free(7.0, timeout=3)


def test_model_info_descriptions(home):
    from olympus.model_info import describe

    c = cfg.load()
    assert "MoE" in describe(c, "qwen36-35b-a3b").description
    assert not describe(c, "groq/whisper-large-v3").chat
    assert not describe(c, "groq/meta-llama/llama-guard-4-12b").chat
    assert describe(c, "groq/llama-3.1-8b-instant").chat
    assert "Ukjent" in describe(c, "openrouter/noe-helt-nytt:free").description


def test_detects_model_spilled_to_cpu(fake_config, tmp_path):
    from olympus.gpu import VramInfo

    m = ModelManager(fake_config)
    path = fake_config.model_path("qwen35-9b")
    with path.open("wb") as f:  # «4 GiB» modellfil (sparse)
        f.truncate(4 * GiB)
    before = VramInfo("card1", 16 * GiB, 1 * GiB)
    assert m._spilled("qwen35-9b", before, VramInfo("card1", 16 * GiB, 5 * GiB)) == 0.0   # alt på GPU-en
    assert m._spilled("qwen35-9b", before, VramInfo("card1", 16 * GiB, 2 * GiB)) == 1.0   # bare 1 av 4 GiB


def test_media_dirs_follow_existing_folders_then_language(home, monkeypatch, tmp_path):
    from olympus.config import media_dir

    monkeypatch.setenv("HOME", str(tmp_path))
    c = cfg.load()
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert media_dir(c, "music") == tmp_path / "Music"
    monkeypatch.setenv("LANG", "nb_NO.UTF-8")
    assert media_dir(c, "music") == tmp_path / "Musikk"
    (tmp_path / "Music").mkdir()
    assert media_dir(c, "music") == tmp_path / "Music"  # finnes: brukes uansett språk
    c.data.setdefault("media_server", {})["music_dir"] = "/data/musikk"
    assert str(media_dir(c, "music")) == "/data/musikk"
