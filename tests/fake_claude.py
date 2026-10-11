#!/usr/bin/env python3
"""Falsk `claude`-CLI: ekko i stream-json/json-format. Logger argv til $FAKE_CLAUDE_LOG."""
import json, os, sys

args = sys.argv[1:]
if os.environ.get("FAKE_CLAUDE_LOG"):
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
        f.write(json.dumps({"argv": args, "cwd": os.getcwd()}) + "\n")
prompt = sys.stdin.read()
model = args[args.index("--model") + 1]
fmt = args[args.index("--output-format") + 1]
system = args[args.index("--system-prompt") + 1] if "--system-prompt" in args else ""
names = [w for w in system.split() if w.istitle()]
reply = f"[claude:{model}] {prompt.strip().splitlines()[-1]}"
if "--mcp-config" in args and "VERKTØY:" in prompt:
    # Som ekte Claude Code: koble til MCP-serveren, list verktøyene og kall det prompten ber om.
    import urllib.request
    conf = json.load(open(args[args.index("--mcp-config") + 1]))["mcpServers"]["olympus"]
    url = conf["url"]
    def rpc(i, method, params=None):
        body = json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}).encode()
        req = urllib.request.Request(url, body, {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                                                 **conf["headers"]})
        return json.load(urllib.request.urlopen(req))["result"]
    rpc(1, "initialize", {"protocolVersion": "2025-06-18"})
    names = [t["name"] for t in rpc(2, "tools/list")["tools"]]
    allowed = args[args.index("--allowedTools") + 1].split(",")
    line = prompt.split("VERKTØY:")[1].strip().splitlines()[0]
    name, arguments = line.split(" ", 1)
    assert name in names and f"mcp__olympus__{name}" in allowed
    res = rpc(3, "tools/call", {"name": name, "arguments": json.loads(arguments)})
    reply = f"[claude:{model}] {'FEIL ' if res['isError'] else ''}{res['content'][0]['text']}"
if fmt == "json":
    out = {"type": "result", "is_error": False, "result": reply}
    if "--json-schema" in args:
        out["structured_output"] = {"route": "code", "confidence": 0.9, "reason": "r",
                                    "steps": [{"agent": "code", "task": "t"}], "needs_tools": []}
    print(json.dumps(out)); sys.exit()
print(json.dumps({"type": "system", "subtype": "init", "tools": [], "model": model}))
for w in reply.split(" "):
    print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                      "delta": {"type": "text_delta", "text": w + " "}}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": reply,
                  "usage": {"input_tokens": 3, "cache_creation_input_tokens": 500, "cache_read_input_tokens": 0, "output_tokens": 7},
                  "total_cost_usd": 0.001}))
