// Olympus på telefonen. Ren JavaScript, ingen biblioteker, ingenting lastes fra nettet.
"use strict";
const $ = (s) => document.querySelector(s);
const log = $("#log"), input = $("#input"), statusEl = $("#status");
let ws, state = {}, current = null, raw = "", turn = [], busy = false;

// --- Markdown: escape først, så et lite, trygt utvalg (kodeblokker, inline-kode, fet, lister, overskrifter) ---
const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function md(src) {
  const blocks = [];
  let s = src.replace(/```[^\n]*\n([\s\S]*?)(```|$)/g, (_, code) => { blocks.push(code); return `\u0000${blocks.length - 1}\u0000`; });
  const links = [];  // lenker (bare http/https) – åpnes i riktig fane i Olympus Hub-appen
  const link = (href, text) => { links.push(`<a href="${href}" target="_blank" rel="noopener">${text}</a>`); return `\u0001${links.length - 1}\u0001`; };
  const codes = [];
  s = esc(s)
    .replace(/`([^`\n]+)`/g, (_, c) => { codes.push(`<code>${c}</code>`); return `\u0002${codes.length - 1}\u0002`; })
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, t, h) => link(h, t))
    .replace(/(https?:\/\/[^\s<)\u0001\u0002]+)/g, (h) => link(h, h))
    .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
    .replace(/^#{1,4} (.+)$/gm, "<h3>$1</h3>")
    .replace(/^(?:&gt; ?.*\n?)+/gm, (m) => "<blockquote>" + m.trim().split("\n").map((l) => l.replace(/^&gt; ?/, "")).join("<br>") + "</blockquote>")
    .replace(/^(?:[-*•] .+\n?)+/gm, (m) => "<ul>" + m.trim().split("\n").map((l) => `<li>${l.replace(/^[-*•] /, "")}</li>`).join("") + "</ul>")
    .replace(/^(?:\d+\. .+\n?)+/gm, (m) => "<ol>" + m.trim().split("\n").map((l) => `<li>${l.replace(/^\d+\. /, "")}</li>`).join("") + "</ol>")
    .split(/\n{2,}/).map((p) => (/^<(ul|ol|h3|pre|blockquote)/.test(p) || p.includes("\u0000") ? p : `<p>${p.replace(/\n/g, "<br>")}</p>`)).join("");
  return s.replace(/\u0002(\d+)\u0002/g, (_, i) => codes[+i]).replace(/\u0001(\d+)\u0001/g, (_, i) => links[+i]).replace(/\u0000(\d+)\u0000/g, (_, i) => `<pre><code>${esc(blocks[+i])}</code></pre>`);
}

// --- visning ---
const atBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 80;
function scroll(force) { if (force || atBottom()) log.scrollTop = log.scrollHeight; }
function el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
function hideWelcome() { const w = $("#welcome"); if (w) w.remove(); }
function add(node) { hideWelcome(); const stick = atBottom(); log.appendChild(node); scroll(stick); return node; }
function setStatus(t) { statusEl.hidden = !t; statusEl.textContent = t || ""; }
function setBusy(b) { busy = b; $("#send").hidden = b; $("#stop").hidden = !b; if (!b) setStatus(""); }

function agentMsg(m) {
  const box = el("div", "msg agent");
  const who = el("div", "who", `<span style="color:${esc(String(m.color || ""))}">${esc(m.icon)} ${esc(m.display)}</span><span class="meta"></span>`);
  box.append(who, el("div", "body"));
  who.addEventListener("click", () => box.classList.contains("draft") || box.dataset.folded ? (box.classList.toggle("draft"), (box.dataset.folded = "1")) : null);
  return box;
}
function render(box, text) { box.querySelector(".body").innerHTML = md(text); }

function renderModel(m) {
  // Modellen Zeus svarer med; sky vises tydelig (samtalen sendes ut av PC-en).
  const b = $("#model");
  b.textContent = m.cloud ? `☁ ${m.model} · sky` : m.model;
  b.classList.toggle("cloud", !!m.cloud);
  b.title = m.cloud ? "Sky – samtalen sendes ut. Trykk for å bytte." : "Lokal modell – trykk for å bytte.";
  const w = $("#model-warn"); w.hidden = !m.warning; w.textContent = m.warning || "";
}
function renderState(s) {
  state = s;
  renderModel(s);
  setBusy(!!s.busy);
}
async function loadModels() {
  const d = await (await fetch("/api/v1/models")).json();
  const sel = $("#models"); sel.innerHTML = "";
  for (const c of d.choices) {
    const o = el("option", "", `${c.cloud ? "☁ " : ""}${esc(c.label)}${c.size ? " · " + esc(c.size) : ""}`);
    o.value = c.id; o.selected = c.id === d.current; sel.append(o);
  }
}

// --- websocket ---
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => $("#conn").classList.add("on");
  ws.onclose = () => { $("#conn").classList.remove("on"); setTimeout(connect, 2000); };
  ws.onmessage = (e) => handle(JSON.parse(e.data));
}
const send = (m) => ws && ws.readyState === 1 && ws.send(JSON.stringify(m));

function reject(b, why) {
  b.classList.add("draft");
  if (why) b.querySelector(".who").insertAdjacentHTML("afterend", `<div class="verdict bad">✗ forkastet: ${esc(why.split("\n")[0].slice(0, 160))}</div>`);
}

function handle(m) {
  switch (m.type) {
    case "state": renderState(m.state); break;
    case "model": renderModel(m); if (!$("#drawer").hidden) loadModels(); break;
    case "user": add(el("div", "msg user", esc(m.text))); setBusy(true); turn = []; break;
    case "status": setStatus(`${m.icon || ""} ${m.display || ""} ${m.text}`.trim()); break;
    case "notice": add(el("div", "notice", esc(m.text))); break;
    case "error": add(el("div", "notice error", esc(m.text))); break;
    case "agent_start":
      setStatus(""); raw = ""; current = add(agentMsg(m));
      current.querySelector(".meta").textContent = (m.cloud ? "☁ " : "") + (m.model || "");
      turn.push(current);
      break;
    case "chunk": if (current) { raw += m.text; render(current, raw); scroll(); } break;
    case "tool": add(el("div", "tool", `⚙ ${esc(m.name)} ${esc(m.detail)}`)).id = "t-" + m.id; break;
    case "tool_result": {
      const t = document.getElementById("t-" + m.id);
      if (t) t.insertAdjacentHTML("beforeend", m.ok ? ` <span class="ok">✓</span>` : ` <span class="bad">✗ ${esc(m.summary)}</span>`);
      break;
    }
    case "approval": {
      const a = add(el("div", "approval" + (m.danger ? " danger" : ""),
        `<b>${esc(m.reason)}</b>${m.preview ? `<pre>${esc(m.preview)}</pre>` : ""}
         <div class="row"><button class="yes">Godkjenn</button><button class="no">Avslå</button>${m.danger ? "" : '<button class="always">Alltid</button>'}</div>`));
      a.id = "a-" + m.id;
      a.querySelector(".yes").onclick = () => send({ type: "approve", id: m.id, answer: "yes" });
      a.querySelector(".no").onclick = () => send({ type: "approve", id: m.id, answer: "no" });
      const al = a.querySelector(".always"); if (al) al.onclick = () => send({ type: "approve", id: m.id, answer: "always" });
      if (navigator.vibrate) navigator.vibrate(80);
      break;
    }
    case "approval_done": { const a = document.getElementById("a-" + m.id); if (a) a.remove(); break; }
    case "judge_rejected": if (current) reject(current, m.text); break;  // påsto noe uten verktøy: forkastet
    case "done": setBusy(false); current = null; if (m.state) renderState(m.state); loadSessions(); break;
    case "cleared": log.innerHTML = ""; renderState(m.state); break;
    case "history":
      log.innerHTML = "";
      for (const it of m.items) {
        if (it.kind === "user") add(el("div", "msg user", esc(it.text)));
        else {
          const b = add(agentMsg(it)); render(b, it.text);
          if (it.draft) reject(b, it.rejected || "");
        }
      }
      scroll(true); break;
  }
}

// --- økter og skuff ---
async function loadSessions() {
  const list = await (await fetch("/api/sessions")).json();
  const ul = $("#sessions"); ul.innerHTML = "";
  for (const s of list) {
    const li = el("li", s.id === state.session ? "cur" : "", `${esc(s.title)}<div class="when">${new Date(s.updated * 1000).toLocaleString("no")}</div>`);
    li.onclick = () => { send({ type: "open", sid: s.id }); drawer(false); };
    ul.append(li);
  }
}
function drawer(open) { $("#drawer").hidden = !open; $("#scrim").hidden = !open; if (open) { loadSessions(); loadModels(); } }

// --- hendelser ---
$("#menu").onclick = () => drawer(true);
$("#close-drawer").onclick = $("#scrim").onclick = () => drawer(false);
$("#new").onclick = () => { send({ type: "new" }); drawer(false); };
$("#models").onchange = (e) => send({ type: "command", name: "model", model: e.target.value });
$("#model").onclick = () => drawer(true);
$("#stop").onclick = () => send({ type: "interrupt" });
for (const b of document.querySelectorAll(".examples button")) b.onclick = () => { input.value = b.dataset.q; input.focus(); grow(); };
function grow() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, innerHeight * 0.4) + "px"; }
input.addEventListener("input", grow);
input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && matchMedia("(pointer:fine)").matches) { e.preventDefault(); $("#form").requestSubmit(); } });
$("#form").onsubmit = (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || busy) return;
  send({ type: "send", text });
  input.value = ""; grow();
};
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
connect();
