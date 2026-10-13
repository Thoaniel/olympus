import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from olympus.agents.orchestrator import Orchestrator
from olympus.model_manager import ModelManager
from olympus.providers import AnthropicBackend, Backends, ClaudeCodeBackend, parse_ref

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"
FAKE_CODEX = Path(__file__).parent / "fake_codex.py"


@pytest.fixture
def claude_bin(tmp_path, monkeypatch):
    log = tmp_path / "claude.log"
    wrapper = tmp_path / "claude"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_CLAUDE} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    return wrapper, log


def test_parse_ref():
    providers = {"claude-code": {}, "anthropic": {}}
    assert parse_ref("qwen35-9b", providers) == ("local", "qwen35-9b")
    assert parse_ref("local/qwen35-9b", providers) == ("local", "qwen35-9b")
    assert parse_ref("claude-code/opus", providers) == ("claude-code", "opus")
    assert parse_ref("anthropic/claude-opus-5-5", providers) == ("anthropic", "claude-opus-5-5")


@pytest.mark.asyncio
async def test_claude_code_streams_with_minimal_flags(claude_bin, tmp_path):
    wrapper, log = claude_bin
    b = ClaudeCodeBackend(binary=str(wrapper), model="opus", workdir=tmp_path / "cc")
    msgs = [{"role": "system", "content": "Du er Hephaestus"},
            {"role": "user", "content": "første"}, {"role": "assistant", "content": "svar"},
            {"role": "user", "content": "andre melding"}]
    chunks = [c async for c in b.stream_chat(msgs)]
    text = "".join(c.text for c in chunks if c.kind == "content")
    assert text.startswith("[claude:opus] andre melding")
    assert [c for c in chunks if c.kind == "usage"][0].usage["total_tokens"] == 510
    call = json.loads(log.read_text().splitlines()[-1])
    argv = call["argv"]
    for flag in ("--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert call["cwd"] == str(tmp_path / "cc")  # ikke brukerens prosjektmappe
    # transkriptet sendes via stdin, ikke argv
    assert "Tidligere i samtalen" not in " ".join(argv)


@pytest.mark.asyncio
async def test_claude_code_json_schema(claude_bin, tmp_path):
    wrapper, _ = claude_bin
    b = ClaudeCodeBackend(binary=str(wrapper), model="haiku", workdir=tmp_path)
    out = await b.complete([{"role": "user", "content": "x"}], json_schema={"type": "object"})
    assert json.loads(out)["route"] == "code"


class _FakeAnthropic(BaseHTTPRequestHandler):
    requests: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _FakeAnthropic.requests.append({"body": body, "beta": self.headers.get("anthropic-beta"),
                                        "key": self.headers.get("x-api-key")})
        last = body["messages"][-1]["content"]
        usage = {"input_tokens": 10, "output_tokens": 4}
        if not body.get("stream"):
            msg = {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                   "content": [{"type": "text", "text": f"ok:{last}"}], "stop_reason": "end_turn",
                   "stop_sequence": None, "usage": usage}
            data = json.dumps(msg).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
            return
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()

        def ev(name, payload):
            self.wfile.write(f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode()); self.wfile.flush()

        ev("message_start", {"type": "message_start", "message": {"id": "msg_1", "type": "message",
           "role": "assistant", "model": body["model"], "content": [], "stop_reason": None,
           "stop_sequence": None, "usage": usage}})
        ev("content_block_start", {"type": "content_block_start", "index": 0,
           "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
        ev("content_block_delta", {"type": "content_block_delta", "index": 0,
           "delta": {"type": "thinking_delta", "thinking": "tenker"}})
        ev("content_block_delta", {"type": "content_block_delta", "index": 0,
           "delta": {"type": "signature_delta", "signature": "sig"}})
        ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        ev("content_block_start", {"type": "content_block_start", "index": 1,
           "content_block": {"type": "text", "text": ""}})
        for w in ["[anthropic] ", last]:
            ev("content_block_delta", {"type": "content_block_delta", "index": 1,
               "delta": {"type": "text_delta", "text": w}})
        ev("content_block_stop", {"type": "content_block_stop", "index": 1})
        ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn",
           "stop_sequence": None}, "usage": {"output_tokens": 6}})
        ev("message_stop", {"type": "message_stop"})


@pytest.fixture
def anthropic_server():
    _FakeAnthropic.requests = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAnthropic)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.mark.asyncio
async def test_anthropic_backend_streams_via_sdk(anthropic_server):
    b = AnthropicBackend(model="claude-opus-5-5", api_key="sk-ant-test", base_url=anthropic_server)
    chunks = [c async for c in b.stream_chat([
        {"role": "system", "content": "Du er Calliope"}, {"role": "user", "content": "hei"}])]
    assert "".join(c.text for c in chunks if c.kind == "content") == "[anthropic] hei"
    assert any(c.kind == "reasoning" and c.text == "tenker" for c in chunks)
    req = _FakeAnthropic.requests[-1]
    body = req["body"]
    assert body["model"] == "claude-opus-5-5" and body["system"] == "Du er Calliope"
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in req["beta"]
    assert "temperature" not in body  # avvises av Opus 5.5
    assert req["key"] == "sk-ant-test"

    out = await b.complete([{"role": "user", "content": "x"}], json_schema={"type": "object"})
    assert out == "ok:x"
    assert _FakeAnthropic.requests[-1]["body"]["output_config"]["format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_zeus_switched_to_claude_code_keeps_brain_local(fake_config, claude_bin):
    wrapper, _ = claude_bin
    fake_config.data["providers"]["claude-code"]["binary"] = str(wrapper)
    fake_config.data["agents"]["zeus"]["model"] = "claude-code/opus"
    manager = ModelManager(fake_config)
    o = Orchestrator(fake_config, manager)
    try:
        evs = [e async for e in o.run("kan du fikse denne Python-feilen jeg får i skriptet mitt")]
        start = next(e for e in evs if e.kind == "agent_start")
        assert start.agent == "zeus" and start.model == "claude-code/opus"
        text = "".join(e.chunk.text for e in evs if e.kind == "chunk" and e.chunk.kind == "content")
        assert text.startswith("[claude:opus]")
        assert manager.loaded() == []  # ingen lokal modell trengs når en skymodell svarer
        assert o.context_used()[1] == 1_000_000
        assert o.messages[-1]["role"] == "assistant"  # samtalen (hjernen) ligger fortsatt her
    finally:
        manager.shutdown()


def test_unavailable_cloud_model_falls_back(fake_config):
    fake_config.data["agents"]["zeus"]["model"] = "groq/llama-3.3-70b-versatile"
    b = Backends(fake_config, ModelManager(fake_config))
    ref, why = b.resolve("zeus")
    assert ref == "qwen35-9b" and "ikke koblet til Groq" in why
    assert b.cost("groq/x") == "gratisnivå" and b.cost("qwen35-9b") == "gratis"
    assert b.trains_on_data("openrouter/x") and not b.trains_on_data("groq/x")


def test_model_choices_switch_and_persist(fake_config, monkeypatch):
    from olympus import config as cfg
    from olympus.model_choices import ModelCatalog, cloud_warning, resolve_choice, set_model, sync_saved
    from olympus.providers import save_auth

    monkeypatch.setattr("olympus.providers.find_binary", lambda name: None)  # ingen `claude` i testen
    b = Backends(fake_config, ModelManager(fake_config))
    cat = ModelCatalog(fake_config, b)
    ids = [c["id"] for c in cat.choices()]
    assert ids == ["qwen35-9b", "qwopus-coder-9b"]  # bare nedlastede lokale; ingen sky uten nøkkel
    save_auth(fake_config, "groq", "gsk-test")
    save_auth(fake_config, "anthropic", "sk-ant-test")
    choices = {c["id"]: c for c in cat.choices()}
    assert choices["groq/openai/gpt-oss-120b"]["cloud"] and not choices["qwen35-9b"]["cloud"]
    assert choices["qwen35-9b"]["size"] == "6.6 GB"
    assert not any(i.startswith("anthropic/") for i in choices)  # betalt per token vises ikke
    assert resolve_choice(cat, "finnes/ikke") is None
    set_model(fake_config, "groq/openai/gpt-oss-120b")
    assert fake_config.agent("zeus")["model"] == "groq/openai/gpt-oss-120b"
    assert cfg.saved_value(fake_config.home, "agents.zeus.model") == "groq/openai/gpt-oss-120b"
    assert cfg.saved_value(fake_config.home, "general.local_model") is None  # sky endrer ikke «tilbake til»
    warn = cloud_warning(fake_config, b, "groq/openai/gpt-oss-120b")
    assert "Groq" in warn and "minnet" in warn
    fake_config.data["memory"]["share_with_cloud"] = False
    assert "minnet" not in cloud_warning(fake_config, b, "groq/openai/gpt-oss-120b")
    assert cloud_warning(fake_config, b, "qwen35-9b") == ""
    assert resolve_choice(cat, "lokal") == "qwen35-9b"  # ett steg tilbake til lokalt
    # en annen prosess (TUI-en uten hjerne) lagrer et bytte: huben tar det inn
    cfg.set_values({"agents.zeus.model": "qwopus-coder-9b"}, fake_config.home)
    assert sync_saved(fake_config) and fake_config.agent("zeus")["model"] == "qwopus-coder-9b"
    assert not sync_saved(fake_config)
    # overlever omstart: ny config fra disk har valget
    assert cfg.load().agent("zeus")["model"] == "qwopus-coder-9b"


class _FakeOpenAI(BaseHTTPRequestHandler):
    status = 200

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        assert self.headers["Authorization"] == "Bearer gsk-test"
        self._send(200, {"data": [{"id": "llama-free:free"}, {"id": "paid-model"}, {"id": "models/gemini-x:free"}]})

    def do_POST(self):
        if _FakeOpenAI.status != 200:
            return self._send(_FakeOpenAI.status, {"error": {"message": "rate limited"}})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for delta in ({"reasoning": "hmm"}, {"content": f"[{body['model']}] "}, {"content": "hei"}):
            self.wfile.write(f"data: {json.dumps({'choices': [{'delta': delta}]})}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def openai_server():
    _FakeOpenAI.status = 200
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


@pytest.mark.asyncio
async def test_openai_compat_stream_list_and_quota(openai_server):
    from olympus.providers import OpenAICompatBackend, QuotaError, list_models

    b = OpenAICompatBackend(label="Groq", base_url=openai_server, api_key="gsk-test", model="llama-free:free")
    chunks = [c async for c in b.stream_chat([{"role": "user", "content": "x"}])]
    assert "".join(c.text for c in chunks if c.kind == "content") == "[llama-free:free] hei"
    assert any(c.kind == "reasoning" for c in chunks)
    models = await list_models({"base_url": openai_server, "model_filter": ":free"}, "gsk-test")
    assert models == ["gemini-x:free", "llama-free:free"]
    _FakeOpenAI.status = 429
    with pytest.raises(QuotaError, match="gratiskvoten"):
        _ = [c async for c in b.stream_chat([{"role": "user", "content": "x"}])]


def test_find_binary_looks_in_local_bin(tmp_path, monkeypatch):
    from olympus.providers import find_binary

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    (tmp_path / ".local/bin").mkdir(parents=True)
    exe = tmp_path / ".local/bin/claude-test-xyz"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert find_binary("claude-test-xyz") == str(exe)
    assert find_binary("finnes-ikke-xyz") is None


@pytest.mark.asyncio
async def test_quota_error_falls_back_to_local(fake_config, openai_server):
    from olympus.providers import save_auth

    _FakeOpenAI.status = 429
    fake_config.data["providers"]["groq"]["base_url"] = openai_server
    save_auth(fake_config, "groq", "gsk-test")
    fake_config.data["agents"]["zeus"]["model"] = "groq/openai/gpt-oss-120b"
    manager = ModelManager(fake_config)
    o = Orchestrator(fake_config, manager)
    try:
        evs = [e async for e in o.run("kan du fikse denne Python-feilen jeg får i skriptet mitt")]
        notices = [e.text for e in evs if e.kind == "notice"]
        # Brukt opp gratiskvote: samme oppgave fortsetter på den lokale modellen (aldri en annen skymodell)
        assert any("gratiskvoten" in n and "Bytter til lokal qwen35-9b" in n for n in notices)
        starts = [e.model for e in evs if e.kind == "agent_start"]
        assert starts == ["groq/openai/gpt-oss-120b", "qwen35-9b"]
        text = "".join(e.chunk.text for e in evs if e.kind == "chunk" and e.chunk.kind == "content")
        assert text.startswith("[Zeus@qwen35-9b]")
    finally:
        manager.shutdown()


@pytest.mark.asyncio
async def test_claude_code_read_tools_for_paid_assistant(claude_bin, tmp_path):
    wrapper, log = claude_bin
    proj = tmp_path / "prosjekt"
    proj.mkdir()
    b = ClaudeCodeBackend(binary=str(wrapper), model="opus", workdir=tmp_path / "cc", project_dir=proj)
    _ = [c async for c in b.stream_chat([{"role": "user", "content": "hei"}])]
    argv = json.loads(log.read_text().splitlines()[-1])["argv"]
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep"
    assert argv[argv.index("--add-dir") + 1] == str(proj)
    assert "Edit" not in " ".join(argv) and "Bash" not in " ".join(argv)


def test_tool_call_extra_content_is_kept():
    from olympus.backends.llamacpp import parse_sse_line

    line = 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"read_file","arguments":"{}"},"extra_content":{"google":{"thought_signature":"abc"}}}]}}]}'
    chunk = parse_sse_line(line)[0]
    assert chunk.tool["extra_content"] == {"google": {"thought_signature": "abc"}}


def test_503_is_transient():
    from olympus.providers import OpenAICompatBackend, QuotaError

    b = OpenAICompatBackend(label="Gemini", base_url="http://x", api_key="k", model="m")
    assert isinstance(b._error(503, "high demand"), QuotaError)
    assert b._error(429, "Please try again in 2.5s").retry_after == 2.5


@pytest.mark.asyncio
async def test_hanging_provider_becomes_transient_error():
    import socket

    from olympus.providers import OpenAICompatBackend, QuotaError

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)  # godtar tilkobling, men svarer aldri
    port = srv.getsockname()[1]
    b = OpenAICompatBackend(label="Treg", base_url=f"http://127.0.0.1:{port}/v1", api_key="k", model="m", timeout=1.0)
    with pytest.raises(QuotaError, match="ingen svar"):
        await b.complete([{"role": "user", "content": "x"}])
    srv.close()


@pytest.mark.asyncio
async def test_claude_code_uses_olympus_tools_via_mcp_with_approval(fake_config, claude_bin, tmp_path):
    """Zeus på Claude Code skal kunne bruke Olympus' verktøy (via MCP), og endringer skal fortsatt godkjennes."""
    from olympus.lab import Lab

    wrapper, _ = claude_bin
    fake_config.data["providers"]["claude-code"]["binary"] = str(wrapper)
    fake_config.data["agents"]["zeus"]["model"] = "claude-code/opus"
    manager = ModelManager(fake_config)
    o = Orchestrator(fake_config, manager)
    o.toolbox.lab = Lab(tmp_path / "lab.db")
    o.toolbox.undo_instead = False  # testen gjelder godkjenning via MCP
    asked = []

    async def approve(call, approval):
        asked.append(call.name)
        return "yes"

    try:
        evs = [e async for e in o.run('VERKTØY: plan {"action": "add", "title": "Tannlege", "start": "2026-10-16T10:00"}',
                                      approve=approve)]
        assert asked == ["plan"]                                       # spurte brukeren først
        assert [e.kind for e in evs if e.kind.startswith("tool_")] == ["tool_call", "tool_result"]
        assert o.toolbox.lab.events()[0]["title"] == "Tannlege"         # og gjorde det faktisk
        text = "".join(e.chunk.text for e in evs if e.kind == "chunk" and e.chunk.kind == "content")
        assert "La til" in text and "FEIL" not in text

        async def deny(call, approval):
            return "no"

        evs = [e async for e in o.run('VERKTØY: plan {"action": "add", "title": "Kino", "start": "2026-10-17T19:00"}',
                                      approve=deny)]
        text = "".join(e.chunk.text for e in evs if e.kind == "chunk" and e.chunk.kind == "content")
        assert "FEIL" in text and len(o.toolbox.lab.events()) == 1  # nei = ingenting gjort
    finally:
        manager.shutdown()


@pytest.mark.asyncio
async def test_mcp_bridge_requires_token_and_keeps_it_out_of_argv():
    import os
    import stat

    import aiohttp

    from olympus.mcp_bridge import OlympusMCP

    async def runner(name, args, emit):
        return "ok", False

    tools = [{"type": "function", "function": {"name": "plan", "description": "", "parameters": {"type": "object"}}}]
    mcp = OlympusMCP(tools, runner, lambda c: None)
    await mcp.start()
    conf_path = mcp.config_file
    try:
        assert stat.S_IMODE(os.stat(mcp.config_file).st_mode) == 0o600
        assert mcp.token not in " ".join(ClaudeCodeBackend(binary="claude", model="opus", workdir=Path("/tmp"))._cmd("", "json", mcp=mcp))
        url = f"http://127.0.0.1:{mcp.port}/mcp"
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        async with aiohttp.ClientSession() as s:
            assert (await s.post(url, json=body)).status == 401
            assert (await s.post(url, json=body, headers={"Authorization": "Bearer feil"})).status == 401
            r = await s.post(url, json=body, headers={"Authorization": f"Bearer {mcp.token}"})
            assert (await r.json())["result"]["tools"][0]["name"] == "plan"
            r = await s.post(url, json=body, headers={"Authorization": f"Bearer {mcp.token}", "Host": "evil.example"})
            assert r.status == 403
    finally:
        await mcp.stop()
    assert not os.path.exists(conf_path)  # fila med nøkkelen er borte etter svaret


def test_fallback_chain_order_and_local_last(fake_config, monkeypatch):
    """general.fallback er kjeden i rekkefølge; «lokal» er den lokale modellen og står alltid sist."""
    from olympus.providers import save_auth

    monkeypatch.setattr("olympus.providers.find_binary", lambda name: None)
    b = Backends(fake_config, ModelManager(fake_config))
    fake_config.data["general"]["fallback"] = ["lokal", "groq/openai/gpt-oss-120b", "gemini/gemini-flash-latest"]
    assert b.fallbacks() == ["groq/openai/gpt-oss-120b", "gemini/gemini-flash-latest", "qwen35-9b"]
    fake_config.data["general"]["fallback"] = []
    assert b.fallbacks() == ["qwen35-9b"]  # uten kjede: den lokale, som før
    fake_config.data["general"]["fallback"] = ["groq/openai/gpt-oss-120b", "lokal"]
    info = b.fallback_info()
    assert [i["id"] for i in info] == ["groq/openai/gpt-oss-120b", "qwen35-9b"]
    assert info[0]["cloud"] and not info[0]["available"] and "Groq" in info[0]["why"]
    save_auth(fake_config, "groq", "gsk-test")
    assert b.fallback_info()[0]["available"]
    # valgt Groq er utilgjengelig uten nøkkel: neste i kjeden som kan brukes
    fake_config.data["agents"]["zeus"]["model"] = "gemini/gemini-flash-latest"
    ref, why = b.resolve("zeus")
    assert ref == "groq/openai/gpt-oss-120b" and "Gemini" in why


@pytest.fixture
def codex_bin(tmp_path, monkeypatch):
    log = tmp_path / "codex.log"
    wrapper = tmp_path / "codex"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_CODEX} \"$@\"\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    return wrapper, log


@pytest.mark.asyncio
async def test_codex_streams_read_only_without_user_config(codex_bin, tmp_path):
    from olympus.providers import CodexBackend

    wrapper, log = codex_bin
    b = CodexBackend(binary=str(wrapper), model="gpt-6-luna", workdir=tmp_path / "cx")
    msgs = [{"role": "system", "content": "Du er Zeus"}, {"role": "user", "content": "første"},
            {"role": "assistant", "content": "svar"}, {"role": "user", "content": "andre melding"}]
    chunks = [c async for c in b.stream_chat(msgs)]
    assert "".join(c.text for c in chunks if c.kind == "content") == "[codex:gpt-6-luna] andre melding"  # item.updated + completed
    assert [c for c in chunks if c.kind == "usage"][0].usage["total_tokens"] == 25
    call = json.loads(log.read_text().splitlines()[-1])
    argv = call["argv"]
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    # Codex' own tools are off: its shell could read any file and send it to the cloud without Olympus asking
    for off in ("features.shell_tool=false", "features.unified_exec=false", 'web_search="disabled"', "tools.view_image=false"):
        assert off in argv
    for flag in ("--ephemeral", "--ignore-user-config", "--skip-git-repo-check"):
        assert flag in argv
    assert argv[argv.index("--cd") + 1] == str(tmp_path / "cx") and call["cwd"] == str(tmp_path / "cx")
    assert "Tidligere i samtalen" not in " ".join(argv) and "Du er Zeus" not in " ".join(argv)  # alt på stdin


@pytest.mark.asyncio
async def test_codex_uses_olympus_tools_via_mcp_token_in_env(fake_config, codex_bin, tmp_path):
    from olympus.lab import Lab

    wrapper, log = codex_bin
    fake_config.data["providers"]["codex"]["binary"] = str(wrapper)
    fake_config.data["agents"]["zeus"]["model"] = "codex/gpt-6-luna"
    manager = ModelManager(fake_config)
    o = Orchestrator(fake_config, manager)
    o.toolbox.lab = Lab(tmp_path / "lab.db")
    o.toolbox.undo_instead = False
    asked = []

    async def approve(call, approval):
        asked.append(call.name)
        return "yes"

    try:
        evs = [e async for e in o.run('VERKTØY: plan {"action": "add", "title": "Tannlege", "start": "2026-10-16T10:00"}',
                                      approve=approve)]
        assert asked == ["plan"] and o.toolbox.lab.events()[0]["title"] == "Tannlege"
        text = "".join(e.chunk.text for e in evs if e.kind == "chunk" and e.chunk.kind == "content")
        assert "La til" in text and "FEIL" not in text
        call = json.loads(log.read_text().splitlines()[-1])
        assert len(call["token"]) > 20                                     # nøkkelen kom i miljøet …
        assert call["token"] not in " ".join(call["argv"])                 # … aldri i argv
        assert 'mcp_servers.olympus.default_tools_approval_mode="approve"' in call["argv"]   # Olympus spør selv
    finally:
        manager.shutdown()


@pytest.mark.asyncio
async def test_fallback_chain_moves_on_to_the_next_cloud_on_quota(fake_config, codex_bin, claude_bin):
    """Rate limit hos den valgte: neste i general.fallback svarer, og den nye modellen meldes (agent_start)."""
    wrapper, _ = codex_bin
    cwrapper, _ = claude_bin
    fake_config.data["providers"]["codex"]["binary"] = str(wrapper)
    fake_config.data["providers"]["claude-code"]["binary"] = str(cwrapper)
    fake_config.data["agents"]["zeus"]["model"] = "codex/gpt-6-luna"
    fake_config.data["general"]["fallback"] = ["claude-code/opus", "lokal"]
    manager = ModelManager(fake_config)
    o = Orchestrator(fake_config, manager)
    try:
        evs = [e async for e in o.run("KVOTE: hei")]
        notices = [e.text for e in evs if e.kind == "notice"]
        assert any("Bytter til skymodell claude-code/opus" in n for n in notices), notices
        assert [e.model for e in evs if e.kind == "agent_start"][-1] == "claude-code/opus"
        text = "".join(e.chunk.text for e in evs if e.kind == "chunk" and e.chunk.kind == "content")
        assert text.startswith("[claude:opus]")
    finally:
        manager.shutdown()


def test_services_status_and_olymparch_keys(fake_config, tmp_path, monkeypatch, claude_bin):
    """Innstillinger → Tjenester: status per leverandør; nøkler fra Olymparchs nøkkellager, aldri ut i lista."""
    from olympus import providers as P

    keys = tmp_path / "bin"
    keys.mkdir()
    (keys / "olymparch-keys").write_text('#!/bin/sh\n[ "$1" = get ] && [ "$2" = mistral ] && { printf "mk-secret"; exit 0; }\nexit 1\n')
    (keys / "olymparch-keys").chmod(0o755)
    monkeypatch.setenv("PATH", f"{keys}:{os.environ['PATH']}")
    P._OLYMPARCH_KEYS.clear()
    wrapper, _ = claude_bin
    fake_config.data["providers"]["claude-code"]["binary"] = str(wrapper)
    fake_config.data["providers"]["claude-code"]["login_status"] = ["claude", "auth", "status"]
    fake_config.data["providers"].setdefault("mistral", {"type": "openai", "label": "Mistral", "cost": "gratisnivå",
                                                        "base_url": "https://api.mistral.ai/v1"})
    b = Backends(fake_config, ModelManager(fake_config))
    assert b.api_key("mistral") == "mk-secret"   # fra olymparch-keys
    s = {i["id"]: i for i in b.services()}
    assert s["mistral"]["status"] == "ready" and s["mistral"]["free"]
    assert s["groq"]["status"] == "missing_key" and s["groq"]["signup"].startswith("https://")
    assert s["claude-code"]["status"] == "not_logged_in" and s["claude-code"]["subscription"]  # falsk claude: exit 1
    assert "mk-secret" not in json.dumps(list(s.values()))
    assert P.olymparch_key("../etc") is None   # bare gyldige navn sendes videre


def test_game_mode_turns_local_ai_off(fake_config, tmp_path):
    """Olymparchs spillmodus: mens flagget finnes er den lokale modellen ikke tilgjengelig og lastes ikke."""
    import asyncio
    import os

    from olympus.model_manager import VramError

    b = Backends(fake_config, ModelManager(fake_config))
    assert b.available("qwen35-9b")[0]
    flag = Path(os.environ["XDG_RUNTIME_DIR"]) / "olymparch" / "gaming"
    flag.parent.mkdir(parents=True)
    flag.touch()
    ok, why = b.available("qwen35-9b")
    assert not ok and "spillmodus" in why
    with pytest.raises(VramError, match="Spillmodus"):
        asyncio.run(b.manager.ensure("qwen35-9b"))
    flag.unlink()
    assert b.available("qwen35-9b")[0]


def test_missing_draft_drops_speculative_args(fake_config):
    """En modell med MTP-draft som ikke er lastet ned skal starte uten --spec-type, ikke krasje."""
    fake_config.data.setdefault("models", {})["qwen35-9b"] = {**fake_config.model("qwen35-9b"), "draft": "finnes-ikke",
                                                              "args": ["--spec-type", "draft-mtp", "--spec-draft-n-max", "2", "-np", "1"]}
    m = ModelManager(fake_config)
    assert m._model_args("qwen35-9b") == ["-np", "1"]
