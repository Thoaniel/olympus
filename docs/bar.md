# `olympus bar`: the stdio bridge for the Olymparch bar

The bar runs **one long-lived process**:

```
olympus bar
```

It connects to the local Olympus server (`http://127.0.0.1:<web.port>`, default 8787) the same way
`olympus zeus` does: the WebSocket `/api/v1/ws` with an `Origin` header and the `olympus_web` cookie from
`~/.olympus/web-token`. The token is never printed and never on argv.

* **stdout**: only JSON, one object per line (UTF-8). Diagnostics go to stderr.
* **stdin**: one JSON object per line.
* Reconnects by itself (waits 1, 2, 5, 10, 10 … s) and says so on stdout.
* Exits with code 0 when stdin closes (EOF). The bar restarts it.

Code: `src/olympus/bar.py`. Tests: `tests/test_bar.py`.

Legend: **[missing]** = does not exist yet in Olympus.

---

## 1. stdout: what the panel receives

### 1.1 Bridge lines (from the bar process itself)

| line | when |
|---|---|
| `{"type":"bridge","status":"connected","detail":"http://127.0.0.1:8787"}` | WebSocket is up |
| `{"type":"bridge","status":"disconnected","detail":"<why>","retry_in":<seconds>}` | lost / could not connect; retries after `retry_in` |
| `{"type":"bridge","status":"error","detail":"<text>"}` | malformed stdin line, unknown `cmd`, message dropped because not connected (then also `"sent":"<type>"`) |

A `detail` of `handshake 401`/`403 (wrong token?)` means the cookie or Origin was refused.

### 1.2 The bar status line `{"type":"bar", …}`

Written on start, on connect/disconnect and **whenever any field changes** (never twice in a row
identical). This is all the bar button itself needs.

```json
{"type":"bar","connected":true,"ai":true,"model":"gemma4-26b-a4b","provider":"lokal","cloud":false,
 "answering":"gemma4-26b-a4b","state":"idle",
 "approvals":[{"id":"9f2c1a0b3d4e","tool":"shell","reason":"kjører en kommando","danger":true,"preview":"$ rm -rf /tmp/x"}],
 "plan":[{"id":"ab12cd34ef56","title":"Tannlege","start":"2026-10-13T10:30","end":null,"all_day":false}],
 "media":{"title":"…","artist":"…","album":"…","playing":true,"paused":false,"position":12.3,"duration":201.0},
 "downloads":{"active":1,"progress":0.42,"items":[{"id":"…","title":"…","status":"laster ned","progress":0.42}]},
 "phones":[{"id":"…","name":"Pixel","last_seen":1760000000.0,"online":true}],
 "last_error":{"text":"Anthropic: rate limit nådd, prøv igjen om litt.","at":1760000000.0}}
```

| field | type | source | real today? |
|---|---|---|---|
| `connected` | bool | WebSocket up | yes |
| `ai` | bool \| null | ws `state.ai`, ws `ai.on`, `GET /api/v1/status` every 10 s | yes (null until first contact) |
| `model` | str \| null | the chosen model id: ws `state`/`model`/`stats`, `/api/v1/status` | yes |
| `provider` | str \| null | provider label of the chosen model (`"lokal"` or e.g. `"Anthropic"`) | yes (from ws `state`/`model`/`stats`) |
| `cloud` | bool \| null | chosen model is a cloud model | yes |
| `answering` | str \| null | model that is answering / answered last (ws `agent_start.model`, `stats.last_model`). Differs from `model` after a cloud fallback | yes (null until the first answer) |
| `state` | `"off"` \| `"approval"` \| `"thinking"` \| `"idle"` | derived, in that priority: `ai==false` → off; pending approvals → approval; busy → thinking | yes |
| `approvals` | list of `{id, tool, reason, danger, preview}` | ws `approval` / `approval_done`. `reason` ≤ 200 chars, `preview` ≤ 400 chars (full preview is in the raw `approval` line) | yes |
| `plan` | list of `{id, title, start, end, all_day}` \| null | today's events, `GET /api/v1/plan?from=<today>&to=<tomorrow>` on connect, on ws `plan_changed`, and at midnight | yes |
| `media` | `{title, artist, album, playing, paused, position, duration}` \| null | ws `player` (what the PC plays). null = nothing playing. `position` is only as fresh as the last `player` message (sent on change, not every second) | yes |
| `downloads` | `{active, progress, items[≤5]}` \| null | ws `downloads` push + one `GET /api/v1/downloads` on connect. `active` counts status `laster ned`/`lastet ned`/`deler`; `progress` = mean 0..1 or null | yes |
| `phones` | list of `{id, name, last_seen, online}` \| null | `GET /api/v1/devices` every 10 s. `online` = `last_seen` within 180 s. **Heuristic**: the server updates `last_seen` at most once a minute per HTTP request/ws connect, so a phone idling on an open WebSocket may show offline. A real "connected now" flag is **[missing]** (the server knows it in `Hub.device_socks` but does not expose it) | partly |
| `last_error` | `{text, at}` \| null | last ws `error`, or a ws `notice` that mentions 429 / rate limit / kvote / quota. `at` = unix time seen by the bar | yes |

On disconnect, `approvals` is emptied and `state` falls back; on reconnect the server **re-sends every
approval card that is still waiting** (added together with this bridge), so cards survive bar restarts.

### 1.3 Everything the server sends on the WebSocket (forwarded unchanged)

On every (re)connect the server sends, in order: `hello`, `state`, `stats`, `player`, then `history`
(only if a conversation is in progress), then any waiting `approval` cards.

| `type` | fields | panel use |
|---|---|---|
| `hello` | `id` (this connection's id) | compare with `user.origin` to know "I sent this" |
| `state` | `state: {busy, session, ai, model, cloud, provider, warning}` | |
| `stats` | `stats: {loaded[], usage[], context:[used,max], messages, last_model, session_tokens, last_usage, active_agent, compressions, last_answers, session, model, cloud, provider}` | Status tab |
| `history` | `session`, `items: [{kind:"user"\|"agent", text, agent?, model?, display?, icon?, color?, tools?:[str], draft?, rejected?}]` | rebuild the chat after reconnect / `open` |
| `user` | `text`, `origin` (connection id of the sender) | show the user bubble (also turns started on the phone) |
| `agent_start` | `agent, display, icon, color, model, cloud` | start an assistant bubble |
| `chunk` | `text` | **append to the current assistant bubble** (streaming) |
| `status` | `agent, display, icon, color, text` | transient status text |
| `tool` | `id, name, detail` | tool line "⚙ name detail" |
| `tool_result` | `id, ok, summary, denied` | mark the tool line ✓/✗ |
| `undoable` | `seq, text` | optional "Angre" button → `POST /api/v1/undo/{seq}` |
| `judge_rejected` | `text, attempt, of` | the last draft was discarded and is being redone: clear/grey the bubble |
| `agent_end` | `agent` | bubble finished |
| `approval` | `id, tool, reason, preview, danger, call:{id,name,arguments}, approval:{needed,reason,danger,preview,preview_lang}` | **approval card** (see 2.2) |
| `approval_done` | `id` | remove the card (answered, timed out after 600 s, or answered elsewhere) |
| `notice` | `text` | toast ("Avbrutt.", "Vent til svaret er ferdig", model unloaded …) |
| `error` | `text` | error in the turn |
| `done` | `session, state, stats` | turn finished (also after interrupt/error): stop spinner, re-enable input |
| `cleared` | `state` | new chat started: empty the chat |
| `model` | `model, cloud, provider, warning` | model switched (`warning` non-empty for cloud: show it) |
| `ai` | `on` | Zeus switched on/off |
| `plan_changed` | `seq` | plan changed (the bar line refreshes itself) |
| `sessions_changed` | – | session list changed |
| `player` | `current:{id,title,artist,album,…}\|null, position, duration, playing, paused, queue, index, upcoming` | now playing |
| `downloads` | `items: [{id, kind, title, method, status, progress, error, added, updated, …}]` | |
| `media_changed` | `what` (`library`/`downloads`/…) | |
| `memory_changed`, `handoff` | | can be ignored by the bar |
| `ev` | `ev: {kind, agent, text, model, value, total, chunk?, call?, result?}` | full orchestrator event for the TUI – **ignore in the bar** (duplicates the lines above) |

Chat streaming sequence for one turn:
`user` → `agent_start` → (`status` | `chunk`* | `tool` → [`approval` → `approval_done`] → `tool_result`)* →
`agent_end` → `done`. On Stop: `notice {"text":"Avbrutt."}` then `done`.

### 1.4 Replies to stdin requests

* HTTP: `{"type":"http","rid":<as sent>,"status":<int>,"json":<parsed body or null>,"error":<str or null>}`
  (`error` is the server's `{"error"}` text for status ≥ 400; 400 without a request for disallowed paths/methods;
  502 network error; 503 not connected after waiting 3 s.)
* Commands: `{"type":"cmd","cmd":"<name>","rid":<as sent or null>,"ok":<bool>,"result":<json or null>,"error":<str or null>}`

---

## 2. stdin: what the panel sends

Three kinds of line. Anything else → `{"type":"bridge","status":"error"}`; the bridge never crashes.

### 2.1 Raw WebSocket messages (any object with a string `type` other than `"http"`), forwarded verbatim

| message | effect |
|---|---|
| `{"type":"send","text":"Hei Zeus"}` | start a turn (ignored with a `notice` if busy or AI off) |
| `{"type":"approve","id":"<approval id>","answer":"yes"\|"no"\|"always"}` | answer a card (`always` becomes `yes` for `danger` calls) |
| `{"type":"interrupt"}` | Stop (only while busy) |
| `{"type":"new"}` | new chat (only when not busy) → `cleared` |
| `{"type":"open","sid":"<session id>"}` | reopen a saved session → `history` |
| `{"type":"stats"}` | ask for a fresh `stats` |
| `{"type":"command","name":"model","model":"<id>"}` | switch model (same as `cmd model`) |
| `{"type":"command","name":"unload","model":"<id>"}` | free a loaded model from VRAM (omit `model` = all) |
| `{"type":"command","name":"komprimer"}` | compress the conversation |

### 2.2 Commands `{"cmd": …}` (optional `"rid"` is echoed back)

| command | maps to |
|---|---|
| `{"cmd":"approve","id":"…"}` | ws `approve` `answer:"yes"` |
| `{"cmd":"deny","id":"…"}` | ws `approve` `answer:"no"` |
| `{"cmd":"always","id":"…"}` | ws `approve` `answer:"always"` |
| `{"cmd":"ai","on":false}` | `POST /api/v1/ai {"on"}` → result `{"ai":false}` (off also frees the GPU) |
| `{"cmd":"model","id":"claude-…"}` | `POST /api/v1/models {"model"}` → result `{current, cloud, warning}`; 409 while Zeus answers, 400 unknown id |
| `{"cmd":"undo"}` | undo the **newest plan change**: `GET /api/v1/log`, then `POST /api/v1/undo/{log[0].seq}`. Undoing an undo redoes. There is **no general undo** outside the plan **[missing]** |
| `{"cmd":"plan_add","text":"Tannlege fre 10:30"}` | splits off the longest time phrase at the end (Norwegian: «i morgen 18», «fre 10:30», «12.10 20:00», «i dag») and `POST /api/v1/plan {title, start}`; result is the new event. Or explicit: `{"cmd":"plan_add","title":"…","start":"…","end"?:…, "all_day"?:bool, "note"?:"…"}` |
| `{"cmd":"pair"}` | new one-time pairing code (like `olympus pair`): result `{"url":"zeus://pair?h=…&c=…&n=…","expires_in":600,"qr_svg":"<svg…>"\|null}` (`qr_svg` needs `qrencode`). Runs in the bar process, not over HTTP |

Approval card: render `tool`, `reason`, `danger` (red, no "always" button when true) and `preview`
(`approval.preview_lang` is a syntax hint; the full text is in the raw `approval` line, `call.arguments` is
the raw JSON string). Buttons → `cmd approve` / `cmd deny` (/ `cmd always`). The card disappears on
`approval_done`. Unanswered cards time out after 600 s as "no".

### 2.3 HTTP proxy

```json
{"type":"http","rid":7,"method":"GET","path":"/api/v1/models"}
{"type":"http","rid":8,"method":"POST","path":"/api/v1/models","body":{"model":"gemma4-26b-a4b"}}
```

Only `GET`/`POST` and only paths starting with `/api/v1/` (no `..`, `//`, `\`, whitespace or `#`). Anything
else is answered with status 400 and no request is made. Query strings are allowed. PATCH/DELETE routes
(edit/delete plan, revoke device, cancel download) are therefore not reachable through the bar.

---

## 3. What each tab needs

### Chat
WebSocket only (sections 1.3 and 2.1). On `bridge connected` the server replays `history` + waiting
cards. Optional warm-up when the panel opens / the user starts typing: `POST /api/v1/zeus/warm` →
`{"warming":true}` (202) or `{"warming":false,"reason":"Zeus er av"}`. Saved sessions:
`GET /api/v1/sessions`, then ws `open` or `POST /api/v1/sessions/{id}/open`; new chat: ws `new`.

### Modell
* List + current: `GET /api/v1/models` → `{current, cloud, warning, choices:[{id, label, cloud, provider, cost, note}]}`
  (cloud lists refresh in the background; ask again after a few seconds for the full list).
* Switch: `cmd model` (or `POST /api/v1/models`). The ws then broadcasts `model`, `state`, `stats`
  (and a `notice` with the cloud warning).
* Loaded in VRAM: `stats.loaded`; unload: ws `{"type":"command","name":"unload","model":id}`.
* **Download / delete model files: [missing] over HTTP.** Only the CLI: `olympus models pull <id>` /
  `olympus models rm <id>` (both ask j/N). Downloads must **always** ask the user first; the panel should at
  most show "run `olympus models pull <id>` in a terminal", never trigger it silently. Smallest addition if
  wanted later: `POST /api/v1/models/pull {"model", "confirm": true}` running `models.pull` in a thread with
  progress over ws – not built.

### Status
* `GET /api/v1/status` → `{ai, model, cloud, models (loaded), busy, seq, server (hostname)}`
* `GET /api/v1/pc/info` → `{host, gpu:{name, temp, vram_used_gb, vram_total_gb}, cpu:{temp}, disk_pct, ai,
  model:{id, cloud, provider, loaded}, services:{unit: active}, backup, devices (count)}` — GPU/VRAM and
  temperatures (`temp` may be null if no sensor).
* ws `stats` (context use, tokens, last answer time), refresh with ws `{"type":"stats"}`.
* AI on/off: `cmd ai`.

### Telefon
* Paired devices: `GET /api/v1/devices` → `[{id, name, created, last_seen}]` (also in the bar line as `phones`).
* New pairing code + QR: `cmd pair` (see 2.2). The code is valid 10 min, single use; the phone app scans the
  `zeus://pair?…` QR and calls `POST /api/v1/pair`. **There is no HTTP route that creates a code [missing]**;
  `cmd pair` does it in-process (same database as the server). Smallest server addition if needed: a
  cookie-only `POST /api/v1/pair/new` returning `{"url": pair_url(c), "expires_in": 600}`.
* Revoke a device: `DELETE /api/v1/devices/{id}` exists but is **not reachable through the bar's proxy**
  (GET/POST only) **[missing for the bar]**.
* "Online now": heuristic only, see `phones.online` above **[missing as a real flag]**.
