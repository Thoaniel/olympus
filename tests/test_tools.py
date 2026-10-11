import json
from pathlib import Path

import pytest

from olympus.agents.orchestrator import Orchestrator
from olympus.model_manager import ModelManager
from olympus.tools import ToolCall, Toolbox, is_readonly


def call(name, **args):
    return ToolCall("c1", name, json.dumps(args))


@pytest.mark.parametrize("cmd,ok", [
    ("ls -la", True), ("git status && git diff", False), ("journalctl -u waybar -n 50 | tail -20", False),
    ("pacman -Qo /usr/bin/ls", True), ("systemctl --user status waybar", False),
    ("systemctl status waybar", True), ("echo hei > fil", False), ("find . -delete", False),
    ("cat $(echo /etc/passwd)", False), ("hyprctl dispatch exec kitty", False), ("rm -rf x", False),
])
def test_is_readonly(cmd, ok):
    assert is_readonly(cmd) is ok


def test_approval_rules(tmp_path):
    tb = Toolbox(tmp_path)
    assert not tb.approval(call("read_file", path="x.py")).needed
    assert not tb.approval(call("shell", command="ls")).needed
    a = tb.approval(call("shell", command="sudo pacman -Syu"))
    assert a.needed and a.danger
    assert tb.approval(call("shell", command="pacman -S htop")).danger
    assert tb.approval(call("shell", command="rm fil")).danger
    w = tb.approval(call("write_file", path="ny.txt", content="hei\n"))
    assert w.needed and not w.danger and "+hei" in w.preview
    assert tb.approval(call("write_file", path="/etc/hosts", content="x")).danger
    # skymodell: lesing utenfor arbeidsmappen må godkjennes
    assert not tb.approval(call("read_file", path="/etc/hostname")).needed
    tb.cloud = True
    assert tb.approval(call("read_file", path="/etc/hostname")).needed
    # «ja for økten» gjelder ikke farlige kommandoer
    tb.session_allow.add("shell")
    assert not tb.approval(call("shell", command="make")).needed
    assert tb.approval(call("shell", command="sudo make install")).needed


@pytest.mark.asyncio
async def test_tools_run(tmp_path):
    tb = Toolbox(tmp_path)
    r = await tb.run(call("write_file", path="a/b.txt", content="en\nto\ntre\n"))
    assert r.ok and (tmp_path / "a/b.txt").read_text() == "en\nto\ntre\n"
    r = await tb.run(call("read_file", path="a/b.txt", offset=2, limit=1))
    assert r.ok and "    2  to" in r.output and "    1  en" not in r.output
    r = await tb.run(call("edit_file", path="a/b.txt", old_text="to", new_text="TO"))
    assert r.ok and "TO" in (tmp_path / "a/b.txt").read_text()
    r = await tb.run(call("edit_file", path="a/b.txt", old_text="finnes ikke", new_text="x"))
    assert not r.ok and "0 ganger" in r.summary
    r = await tb.run(call("search_files", pattern="TO"))
    assert r.ok and "b.txt" in r.output
    r = await tb.run(call("shell", command="echo hei && exit 3"))
    assert not r.ok and "hei" in r.output and "exit 3" in r.summary
    r = await tb.run(ToolCall("c", "read_file", "ikke json"))
    assert not r.ok


@pytest.fixture
def orch(fake_config, tmp_path):
    work = tmp_path / "prosjekt"
    work.mkdir()
    (work / "notat.txt").write_text("hemmelig innhold 42\n")
    manager = ModelManager(fake_config)
    o = Orchestrator(fake_config, manager, cwd=work)
    o.toolbox.cwd = work
    yield o
    manager.shutdown()


@pytest.mark.asyncio
async def test_agent_reads_file_without_asking(orch):
    asked = []

    async def approve(c, a):
        asked.append(c.name)
        return "yes"

    evs = [e async for e in orch.run('VERKTØY read_file {"path": "notat.txt"}', approve=approve)]
    kinds = [e.kind for e in evs]
    assert "tool_call" in kinds and "tool_result" in kinds and "round" in kinds
    assert asked == []  # lesing i arbeidsmappen spør ikke
    res = next(e.result for e in evs if e.kind == "tool_result")
    assert res.ok and "hemmelig innhold 42" in res.output
    text = "".join(e.chunk.text for e in evs if e.kind == "chunk" and e.chunk.kind == "content")
    assert "verktøy sa:" in text and "hemmelig innhold 42" in text


@pytest.mark.asyncio
async def test_agent_write_needs_approval_and_denial_is_respected(orch):
    async def deny(c, a):
        assert a.needed and "notat.txt" in a.reason
        return "no"

    evs = [e async for e in orch.run(
        'VERKTØY write_file {"path": "notat.txt", "content": "overskrevet"}', approve=deny)]
    res = next(e.result for e in evs if e.kind == "tool_result")
    assert res.denied and (orch.cwd / "notat.txt").read_text() == "hemmelig innhold 42\n"

    async def allow(c, a):
        return "yes"

    evs = [e async for e in orch.run(
        'VERKTØY write_file {"path": "notat.txt", "content": "overskrevet"}', approve=allow)]
    assert (orch.cwd / "notat.txt").read_text() == "overskrevet"


@pytest.mark.asyncio
async def test_zeus_tools_depend_on_model(orch):
    assert {"shell", "write_file", "plan", "snl_search"} <= set(orch.tool_names("zeus", "qwen35-9b"))
    assert "plan" in orch.tool_names("zeus", "claude-code/opus")  # Claude Code får verktøyene via MCP
    assert "shell" in orch.tool_names("zeus", "groq/openai/gpt-oss-120b")  # OpenAI-kompatibel sky: ja, med skyregler


@pytest.mark.asyncio
async def test_library_tools(fake_config, tmp_path):
    from olympus.library import Library

    lib = Library(fake_config)
    docs = lib.root / "docs"
    docs.mkdir(parents=True)
    (docs / "notater.md").write_text("# Mine notater\n\n## Waybar\n\nWaybar startes fra hyprland.lua med exec-once.\n\n## Annet\n\nTekst.\n")
    assert lib.build()["docs"] == 1
    tb = Toolbox(tmp_path)
    tb.library = lib
    assert not tb.approval(call("library_search", query="waybar")).needed
    r = await tb.run(call("library_search", query="waybar exec-once"))
    assert r.ok and "Mine notater" in r.output and "Waybar" in r.output
    hit_id = r.output.split("]")[0].strip("[")
    r = await tb.run(call("library_read", ref=hit_id))
    assert r.ok and "hyprland.lua" in r.output
    r = await tb.run(call("library_read", ref="Mine notater", section="Waybar"))
    assert r.ok and "exec-once" in r.output and "Tekst." not in r.output


@pytest.mark.parametrize("cmd", [
    "ls\nrm -rf ~", "ls & rm -rf x", "env rm -rf x", "uv pip install evil", "git branch -D main",
    "sort -o /etc/passwd x", "git diff --output=/tmp/x", "find . -fprint /tmp/x", "ip link set eth0 down",
    "date -s 2020-01-01", "ls; $(rm x)", "VAR=1 rm x", "command rm x",
])
def test_bypasses_are_not_readonly(cmd):
    assert not is_readonly(cmd)


@pytest.mark.parametrize("cmd", [
    "/usr/bin/rm -rf x", "command rm x", "env rm x", "bash -c 'echo hi'", "xargs rm < liste",
    "ls\nrm x", "nice -n 5 rm x", "git branch -D x", "systemctl --user restart waybar", "sudo -E make",
])
def test_danger_detection_sees_through_wrappers(cmd):
    from olympus.tools import is_dangerous

    assert is_dangerous(cmd)


def test_session_allow_never_covers_hidden_danger(tmp_path):
    tb = Toolbox(tmp_path)
    tb.session_allow.add("shell")
    assert tb.approval(call("shell", command="/usr/bin/rm -rf x")).danger
    assert tb.approval(call("shell", command="bash -c 'rm -rf x'")).danger
    assert not tb.approval(call("shell", command="make test")).needed


def test_cloud_cannot_read_secrets_via_shell(tmp_path):
    tb = Toolbox(tmp_path, cloud=True)
    assert tb.approval(call("shell", command="cat ~/.ssh/id_rsa")).needed
    assert tb.approval(call("shell", command="cat ../../etc/passwd")).needed
    assert not tb.approval(call("shell", command="cat README.md")).needed
    local = Toolbox(tmp_path)
    assert not local.approval(call("shell", command="cat /etc/hostname")).needed  # lokalt forlater ingenting maskinen


def test_claude_code_never_gets_home_dir(fake_config, monkeypatch):
    from olympus.model_manager import ModelManager
    from olympus.providers import Backends

    monkeypatch.setattr("olympus.providers.find_binary", lambda b: "/bin/true")
    b = Backends(fake_config, ModelManager(fake_config))
    b.cwd = Path.home()
    import asyncio

    backend = asyncio.run(b.get("claude-code/opus"))
    assert backend.project_dir != Path.home()


@pytest.mark.parametrize("cmd", [
    "rg --pre=./evil x", "rg --pre-glob '*' --pre sh x", "sort --compress-program=sh x", "sort --comp=sh x",
    "man -P 'sh -c id' ls", "man --html=firefox ls", "man -Hfirefox ls",
])
def test_exec_capable_flags_are_not_readonly(cmd):
    assert not is_readonly(cmd)


def test_plain_reads_still_readonly():
    assert is_readonly("rg -n TODO src") and is_readonly("sort fil.txt") and is_readonly("man pacman")


@pytest.mark.parametrize("cmd", [
    "cat $HOME/.ssh/id_rsa", "cat /e*/passwd", "cat ~/.ss?/id_rsa", "grep --file=/etc/passwd x",
    "grep -f/etc/shadow x", "cat {a,/etc/passwd}", "cat \\/etc/passwd",
])
def test_cloud_shell_expansion_needs_approval(tmp_path, cmd):
    tb = Toolbox(tmp_path, cloud=True)
    assert tb.approval(call("shell", command=cmd)).needed


@pytest.mark.parametrize("cmd", ["sort -o/tmp/x a", "sort -no/tmp/x a", "sort --out=/tmp/x a", "git diff --outp=/tmp/x", "rg -z x", "rg --search-zip x"])
def test_attached_and_abbreviated_output_flags(cmd):
    assert not is_readonly(cmd)


def test_cloud_symlink_out_of_workdir_needs_approval(tmp_path):
    work = tmp_path / "prosjekt"
    work.mkdir()
    (tmp_path / "hemmelig.txt").write_text("x")
    (work / "lenke").symlink_to(tmp_path)
    tb = Toolbox(work, cloud=True)
    assert tb.approval(call("shell", command="cat lenke/hemmelig.txt")).needed
    (work / "ok.txt").write_text("y")
    assert not tb.approval(call("shell", command="cat ok.txt")).needed


@pytest.mark.parametrize("cmd", ["git diff", "git show HEAD", "git log -p", "git log --stat", "uniq a b",
                                 "file -C -m x", "journalctl --vacuum-size=1M", "journalctl --rotate"])
def test_stricter_readonly(cmd):
    assert not is_readonly(cmd)


def test_git_log_safe_flags_ok():
    assert is_readonly("git log --oneline -n 20") and is_readonly("git status")


def test_sensitive_files_need_approval_for_cloud_even_in_home(tmp_path):
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_ed25519").write_text("x")
    (tmp_path / ".env").write_text("TOKEN=x")
    tb = Toolbox(tmp_path, cloud=True)  # arbeidsmappen er «hjemme»
    assert tb.approval(call("read_file", path=".ssh/id_ed25519")).danger
    assert tb.approval(call("shell", command="cat .env")).needed
    assert tb.approval(call("shell", command="grep -r x .ssh")).needed
    assert not Toolbox(tmp_path).approval(call("read_file", path=".env")).needed  # lokalt: forlater ikke maskinen


@pytest.mark.asyncio
async def test_toctou_symlink_swap_is_refused(tmp_path):
    tb = Toolbox(tmp_path)
    target = tmp_path / "fil.txt"
    target.write_text("original")
    c = call("write_file", path="fil.txt", content="ny")
    assert tb.approval(c).needed
    outside = tmp_path.parent / "utenfor.txt"
    outside.write_text("skal ikke endres")
    target.unlink()
    target.symlink_to(outside)  # byttet ut etter godkjenning
    r = await tb.run(c)
    assert not r.ok and outside.read_text() == "skal ikke endres"


@pytest.mark.asyncio
async def test_cloud_search_hides_secret_hits_and_recursive_shell_asks(tmp_path):
    (tmp_path / ".env").write_text("API_TOKEN=hemmelig\n")
    (tmp_path / "app.py").write_text("token = load('API_TOKEN')\n")
    tb = Toolbox(tmp_path, cloud=True)
    r = await tb.run(call("search_files", pattern="API_TOKEN"))
    assert "app.py" in r.output and "hemmelig" not in r.output
    assert tb.approval(call("shell", command="grep -r TOKEN .")).needed
    assert tb.approval(call("shell", command="rg TOKEN")).needed
    assert not Toolbox(tmp_path).approval(call("shell", command="rg TOKEN")).needed  # lokalt


def test_claude_code_denies_secret_files(tmp_path):
    from olympus.providers import ClaudeCodeBackend

    b = ClaudeCodeBackend(binary="claude", model="opus", workdir=tmp_path, project_dir=tmp_path)
    argv = b._cmd("", "stream-json")
    deny = argv[argv.index("--disallowedTools") + 1:]
    assert f"Read(/{tmp_path.resolve()}/**/.env)" in deny and "Read(~/**/.ssh/**)" in deny


@pytest.mark.parametrize("cmd", ["cat a | tail", "ls; rm x", "echo $HOME", "cat ~/x", "cat 'a b'", "ls *.py", "cat a\\ b"])
def test_only_simple_commands_run_without_asking(cmd):
    assert not is_readonly(cmd)


@pytest.mark.asyncio
async def test_auto_commands_run_without_bash(tmp_path):
    tb = Toolbox(tmp_path)
    c = call("shell", command="echo hei")
    assert not tb.approval(c).needed and tb.direct_exec[c.id] == ["echo", "hei"]
    r = await tb.run(c)
    assert r.ok and "hei" in r.output


def test_claude_deny_covers_found_secrets_and_symlinks(tmp_path):
    from olympus.providers import claude_deny

    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "config.local.env").write_text("x")
    (tmp_path / "hemmelig").write_text("x")
    (proj / "snarvei").symlink_to(tmp_path / "hemmelig")
    rules = claude_deny([proj])
    assert f"Read(/{proj.resolve()}/config.local.env)" in rules
    assert f"Read(/{(tmp_path / 'hemmelig').resolve()})" in rules


def test_session_allow_only_for_simple_commands(tmp_path):
    tb = Toolbox(tmp_path)
    tb.session_allow.add("shell")
    c = call("shell", command="make test")
    assert not tb.approval(c).needed and tb.direct_exec[c.id] == ["make", "test"]
    assert tb.approval(call("shell", command="make test && ./deploy.sh")).needed
    assert tb.approval(call("shell", command="echo $(id)")).needed


def test_snl_tools_need_no_approval(tmp_path):
    tb = Toolbox(tmp_path, cloud=True)
    assert not tb.approval(call("snl_search", query="Galdhøpiggen")).needed
    assert not tb.approval(call("snl_read", permalink="Galdhøpiggen")).needed
