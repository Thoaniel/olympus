#!/usr/bin/env python3
"""Falsk llama-server for tester.

- /health
- POST /v1/chat/completions
  * uten stream (oppsummering o.l.): «Forbedret: <tekst>»
  * stream: ekko med agentnavnet fra systemprompten («Du er <Navn>»)
"""
import argparse, json, re, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int); ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--alias", default="fake")
ap.add_argument("--list-devices", action="store_true")
args, _ = ap.parse_known_args()
if args.list_devices:
    print("Available devices:\n  Vulkan0: Fake GPU (1024 MiB, 1024 MiB free)"); raise SystemExit


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        if self.path == "/v1/models":
            return self._json({"object": "list", "data": [{"id": args.alias, "object": "model"}]})
        self.send_response(200 if self.path == "/health" else 404); self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        msgs = body["messages"]
        system, last = msgs[0]["content"], msgs[-1]["content"]
        if not body.get("stream"):
            m = re.search(r"<<<\n(.*)\n>>>", last, re.S)
            return self._json({"choices": [{"message": {"content": "Forbedret: " + (m.group(1) if m else last)}}]})
        names = re.findall(r"Du er ([A-ZÆØÅ]\w+)", system)  # rolleprompten kommer etter SOUL
        name = names[-1] if names else "?"
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        # Verktøy: «VERKTØY navn {json}» i brukermeldingen gir et verktøykall; svaret etterpå
        # oppsummerer verktøyresultatet.
        if msgs[-1]["role"] == "tool":
            out = msgs[-1]["content"].replace("\n", " ")[:60]
            ev = {"choices": [{"delta": {"content": f"[{name}] verktøy sa: {out}"}}]}
            self.wfile.write(f"data: {json.dumps(ev)}\n\ndata: [DONE]\n\n".encode())
            return
        m = re.search(r"VERKTØY (\w+) (\{.*\})", last)
        if m and body.get("tools"):
            args = m.group(2)
            for i, piece in enumerate([args[: len(args) // 2], args[len(args) // 2 :]]):
                tc = {"index": 0, "function": {"arguments": piece}}
                if i == 0:
                    tc.update({"id": "call_1", "type": "function"}); tc["function"]["name"] = m.group(1)
                ev = {"choices": [{"delta": {"tool_calls": [tc]}}]}
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        words = [f"[{name}@{body['model']}]", *last.split(" ")]
        for w in words:
            ev = {"choices": [{"delta": {"content": w + " "}}]}
            self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode()); self.wfile.flush(); time.sleep(0.005)
        ev = {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": len(words), "total_tokens": 10 + len(words)}}
        self.wfile.write(f"data: {json.dumps(ev)}\n\ndata: [DONE]\n\n".encode())


ThreadingHTTPServer((args.host, args.port), H).serve_forever()
