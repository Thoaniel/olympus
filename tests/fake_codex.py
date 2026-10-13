#!/usr/bin/env python3
"""Falsk `codex`-CLI (`codex exec --json … -`): JSONL-hendelser som i dokumentasjonen. Logger argv og om nøkkelen
(fra miljøet; testen sjekker at den aldri står i argv) til $FAKE_CODEX_LOG. «KVOTE» i prompten gir turn.failed med rate limit."""
import json, os, sys

args = sys.argv[1:]
assert args[:2] == ["exec", "--json"] and args[-1] == "-"
prompt = sys.stdin.read()
cfg = {}
for i, a in enumerate(args):
    if a == "-c":
        k, v = args[i + 1].split("=", 1)
        cfg[k] = v
token_env = cfg.get("mcp_servers.olympus.bearer_token_env_var", '""').strip('"')
if os.environ.get("FAKE_CODEX_LOG"):
    with open(os.environ["FAKE_CODEX_LOG"], "a") as f:
        f.write(json.dumps({"argv": args, "cwd": os.getcwd(), "token": os.environ.get(token_env, "") if token_env else ""}) + "\n")
model = args[args.index("--model") + 1]
print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
print(json.dumps({"type": "turn.started"}))
if "KVOTE" in prompt:
    print(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit (rate limit)."}}))
    sys.exit(1)
reply = f"[codex:{model}] {prompt.strip().splitlines()[-1]}"
if "mcp_servers.olympus.url" in cfg and "VERKTØY:" in prompt:
    import urllib.request
    url = cfg["mcp_servers.olympus.url"].strip('"')
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {os.environ[token_env]}"}
    def rpc(i, method, params=None):
        body = json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}).encode()
        return json.load(urllib.request.urlopen(urllib.request.Request(url, body, headers)))["result"]
    rpc(1, "initialize", {"protocolVersion": "2025-06-18"})
    names = [t["name"] for t in rpc(2, "tools/list")["tools"]]
    line = prompt.split("VERKTØY:")[1].strip().splitlines()[0]
    name, arguments = line.split(" ", 1)
    assert name in names
    res = rpc(3, "tools/call", {"name": name, "arguments": json.loads(arguments)})
    reply = f"[codex:{model}] {'FEIL ' if res['isError'] else ''}{res['content'][0]['text']}"
half = len(reply) // 2
print(json.dumps({"type": "item.updated", "item": {"id": "item_2", "type": "agent_message", "text": reply[:half]}}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": reply}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 20, "cached_input_tokens": 10, "output_tokens": 5,
                                                       "reasoning_output_tokens": 0}}))
