"""`olympus bar`: broen mellom Olymparch-baren (JSON-linjer på stdin/stdout) og den lokale Olympus-serveren."""

import asyncio
import json
import socket
from datetime import date

import pytest
from aiohttp.test_utils import TestServer

from olympus.bar import Bar, split_plan_text
from olympus.web.server import HUB, get_token, make_app


class Pipe:
    """Falsk stdin/stdout: linjer inn gjennom en kø, alt ut samles og kan ventes på."""

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self.out: list[dict] = []
        self.raw: list[str] = []

    def write(self, s: str) -> None:
        assert s.endswith("\n") and s.count("\n") == 1  # nøyaktig én JSON-linje per skriving
        self.raw.append(s)
        self.out.append(json.loads(s))

    def send(self, obj) -> None:
        self.q.put_nowait(obj if isinstance(obj, str) else json.dumps(obj))

    def close(self) -> None:
        self.q.put_nowait(None)

    async def lines(self):
        while (line := await self.q.get()) is not None:
            yield line

    async def wait(self, pred, timeout: float = 10.0) -> dict:
        async def go():
            seen = 0
            while True:
                for m in self.out[seen:]:
                    if pred(m):
                        return m
                seen = len(self.out)
                await asyncio.sleep(0.01)
        return await asyncio.wait_for(go(), timeout)


async def start(fake_config, **kw):
    app = make_app(fake_config)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    pipe = Pipe()
    bar = Bar(f"http://127.0.0.1:{server.port}", get_token(fake_config), pipe.write, **kw)
    task = asyncio.create_task(bar.run(pipe.lines()))
    await pipe.wait(lambda m: m.get("type") == "bridge" and m["status"] == "connected")
    return server, app, pipe, bar, task


async def stop(server, pipe, task):
    pipe.close()
    assert await asyncio.wait_for(task, 10) == 0  # stdin lukket: avslutter rent
    await server.close()


@pytest.mark.asyncio
async def test_forwards_server_messages_and_stdin_and_never_prints_the_token(fake_config):
    server, _app, pipe, _bar, task = await start(fake_config)
    try:
        await pipe.wait(lambda m: m.get("type") == "hello")
        await pipe.wait(lambda m: m.get("type") == "state" and "busy" in m["state"])
        bar = await pipe.wait(lambda m: m.get("type") == "bar" and m["connected"] and m["model"] == "qwen35-9b")
        assert bar["state"] == "idle" and bar["approvals"] == [] and bar["cloud"] is False
        n = sum(1 for m in pipe.out if m.get("type") == "stats")
        pipe.send({"type": "stats"})  # vanlig websocket-melding: sendes uendret videre
        await pipe.wait(lambda m: m.get("type") == "stats" and sum(1 for x in pipe.out if x.get("type") == "stats") > n)
        # statuslinja fylles fra HTTP-oppslagene også (plan og telefoner)
        await pipe.wait(lambda m: m.get("type") == "bar" and m["plan"] == [] and m["phones"] == [])
    finally:
        await stop(server, pipe, task)
    assert get_token(fake_config) not in "".join(pipe.raw)


@pytest.mark.asyncio
async def test_http_proxy_allow_list(fake_config):
    server, _app, pipe, _bar, task = await start(fake_config)
    try:
        pipe.send({"type": "http", "rid": 1, "method": "GET", "path": "/api/v1/status"})
        ok = await pipe.wait(lambda m: m.get("type") == "http" and m["rid"] == 1)
        assert ok["status"] == 200 and ok["json"]["model"] == "qwen35-9b" and ok["error"] is None
        bad = [(2, "GET", "/etc/passwd"), (3, "DELETE", "/api/v1/plan/x"), (4, "PUT", "/api/v1/status"),
               (5, "GET", "/api/v1/../ws"), (6, "GET", "http://evil.example/api/v1/status"), (7, "GET", None)]
        for rid, method, path in bad:
            pipe.send({"type": "http", "rid": rid, "method": method, "path": path})
        for rid, *_ in bad:
            r = await pipe.wait(lambda m, rid=rid: m.get("type") == "http" and m["rid"] == rid)
            assert r["status"] == 400 and r["json"] is None and r["error"]
        pipe.send({"type": "http", "rid": 8, "method": "POST", "path": "/api/v1/models", "body": {"model": "finnes-ikke"}})
        r = await pipe.wait(lambda m: m.get("type") == "http" and m["rid"] == 8)
        assert r["status"] == 400 and "ukjent" in r["error"]  # POST med cookien går gjennom (ingen CSRF-avvisning)
    finally:
        await stop(server, pipe, task)


@pytest.mark.asyncio
async def test_malformed_lines_and_unknown_commands_do_not_crash(fake_config):
    server, _app, pipe, _bar, task = await start(fake_config)
    try:
        for line in ("ikke json", "[1, 2]", '{"no": "type"}', '{"cmd": "flagg"}', '{"cmd": "approve"}', "", "   "):
            pipe.send(line)
        await pipe.wait(lambda m: m.get("type") == "bridge" and "unknown cmd" in str(m.get("detail")))
        errors = [m for m in pipe.out if m.get("type") == "bridge" and m["status"] == "error"]
        assert len(errors) >= 4
        await pipe.wait(lambda m: m.get("type") == "cmd" and m["cmd"] == "approve" and m["ok"] is False)
        pipe.send({"type": "http", "rid": 9, "method": "GET", "path": "/api/v1/status"})  # lever fortsatt
        await pipe.wait(lambda m: m.get("type") == "http" and m["rid"] == 9)
    finally:
        await stop(server, pipe, task)


@pytest.mark.asyncio
async def test_reconnect_messages_when_server_is_down():
    with socket.socket() as s:  # en port der ingen lytter
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    pipe = Pipe()
    bar = Bar(f"http://127.0.0.1:{port}", "hemmelig-nøkkel", pipe.write, backoff=(0.01, 0.02))
    task = asyncio.create_task(bar.run(pipe.lines()))
    first = await pipe.wait(lambda m: m.get("type") == "bridge" and m["status"] == "disconnected")
    assert first["retry_in"] == 0.01
    await pipe.wait(lambda m: m.get("type") == "bridge" and m["status"] == "disconnected" and m["retry_in"] == 0.02)
    pipe.send({"type": "send", "text": "hei"})  # ingen forbindelse: sies fra om, ingen krasj
    await pipe.wait(lambda m: m.get("type") == "bridge" and m["status"] == "error" and "not connected" in m["detail"])
    assert any(m.get("type") == "bar" and m["connected"] is False for m in pipe.out)
    pipe.close()
    assert await asyncio.wait_for(task, 5) == 0
    assert "hemmelig-nøkkel" not in "".join(pipe.raw)


@pytest.mark.asyncio
async def test_reconnects_after_server_restart(fake_config):
    server, app, pipe, bar, task = await start(fake_config, backoff=(0.05,))
    try:
        port = server.port
        await server.close()
        await pipe.wait(lambda m: m.get("type") == "bridge" and m["status"] == "disconnected")
        server = TestServer(make_app(fake_config), host="127.0.0.1", port=port)
        await server.start_server()
        n = sum(1 for m in pipe.out if m.get("type") == "bridge" and m["status"] == "connected")
        await pipe.wait(lambda m: sum(1 for x in pipe.out if x.get("type") == "bridge" and x["status"] == "connected") > n)
    finally:
        await stop(server, pipe, task)


@pytest.mark.asyncio
async def test_commands_ai_plan_add_undo_and_pending_approval_card(fake_config):
    server, app, pipe, bar, task = await start(fake_config)
    hub = app[HUB]
    try:
        pipe.send({"cmd": "ai", "on": False, "rid": "a"})
        r = await pipe.wait(lambda m: m.get("type") == "cmd" and m["rid"] == "a")
        assert r["ok"] and r["result"] == {"ai": False}
        await pipe.wait(lambda m: m.get("type") == "bar" and m["ai"] is False and m["state"] == "off")
        pipe.send({"cmd": "ai", "on": True})
        await pipe.wait(lambda m: m.get("type") == "bar" and m["ai"] is True and m["state"] == "idle")

        pipe.send({"cmd": "plan_add", "text": "Tannlege i dag 23:59", "rid": "p"})
        r = await pipe.wait(lambda m: m.get("type") == "cmd" and m["rid"] == "p")
        assert r["ok"] and r["result"]["title"] == "Tannlege" and r["result"]["start"].startswith(date.today().isoformat())
        await pipe.wait(lambda m: m.get("type") == "bar" and [e["title"] for e in m["plan"] or []] == ["Tannlege"])
        pipe.send({"cmd": "undo", "rid": "u"})
        assert (await pipe.wait(lambda m: m.get("type") == "cmd" and m["rid"] == "u"))["ok"]
        await pipe.wait(lambda m: m.get("type") == "bar" and m["plan"] == [])
        pipe.send({"cmd": "plan_add", "text": "bare tekst"})
        await pipe.wait(lambda m: m.get("type") == "cmd" and m["cmd"] == "plan_add" and m["ok"] is False)
        pipe.send({"cmd": "model", "id": "finnes-ikke", "rid": "m"})
        assert (await pipe.wait(lambda m: m.get("type") == "cmd" and m["rid"] == "m"))["ok"] is False

        # Et godkjenningskort som venter: vises i statuslinja, og «deny» svarer Zeus nei
        from olympus.tools import Approval, ToolCall

        call = ToolCall("c1", "shell", '{"command": "rm -rf /tmp/x"}')
        waiting = asyncio.create_task(hub.approve(call, Approval(True, "sletter filer", True, "$ rm -rf /tmp/x")))
        b = await pipe.wait(lambda m: m.get("type") == "bar" and m["state"] == "approval")
        card = b["approvals"][0]
        assert card["tool"] == "shell" and card["danger"] is True and card["reason"] == "sletter filer"
        assert any(m.get("type") == "approval" and m["id"] == card["id"] for m in pipe.out)  # rålinja også
        pipe.send({"cmd": "deny", "id": card["id"]})
        assert await asyncio.wait_for(waiting, 5) == "no"
        await pipe.wait(lambda m: m.get("type") == "bar" and m["approvals"] == [] and m["state"] == "idle")
    finally:
        await stop(server, pipe, task)


@pytest.mark.asyncio
async def test_server_resends_waiting_approval_cards_to_a_new_connection(fake_config):
    app = make_app(fake_config)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    hub = app[HUB]
    from olympus.tools import Approval, ToolCall

    waiting = asyncio.create_task(hub.approve(ToolCall("c1", "shell", "{}"), Approval(True, "kjøre", False, "$ ls")))
    await asyncio.sleep(0.05)
    pipe = Pipe()  # baren startes på nytt mens Zeus venter på svar
    task = asyncio.create_task(Bar(f"http://127.0.0.1:{server.port}", get_token(fake_config), pipe.write).run(pipe.lines()))
    try:
        card = await pipe.wait(lambda m: m.get("type") == "approval")
        await pipe.wait(lambda m: m.get("type") == "bar" and m["state"] == "approval")
        pipe.send({"cmd": "approve", "id": card["id"]})
        assert await asyncio.wait_for(waiting, 5) == "yes"
    finally:
        await stop(server, pipe, task)


def test_split_plan_text():
    assert split_plan_text("Tannlege fre 10:30") == ("Tannlege", "fre 10:30")
    assert split_plan_text("Middag med Kari i morgen 18") == ("Middag med Kari", "i morgen 18")
    assert split_plan_text("bare tekst") is None
