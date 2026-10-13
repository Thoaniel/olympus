"""Providers i opencode-stil. En modellreferanse er `provider/modell`:

- `qwen35-9b` eller `local/qwen35-9b`   → llama.cpp på egen GPU (ModelManager)
- `claude-code/opus`                    → den offisielle `claude`-CLI-en (ditt Claude-abonnement)
- `anthropic/claude-opus-5-5`           → Anthropic API med API-nøkkel

Alle backends har samme grensesnitt som LlamaServer: `complete()` og `stream_chat()`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

from olympus.backends.llamacpp import BackendError, Chunk
from olympus.config import Config
from olympus.mcp_bridge import OlympusMCP, ToolRunner
from olympus.model_manager import ModelManager

LOCAL = "local"


class ChatBackend(Protocol):
    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        thinking: bool = False,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 1024,
    ) -> str: ...

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        thinking: bool = False,
    ) -> AsyncIterator[Chunk]: ...


def find_binary(name: str) -> str | None:
    """Som shutil.which, men leter også i vanlige brukermapper. Programmer startet fra
    Hyprland/app-startere har ofte ikke ~/.local/bin i PATH."""
    if Path(name).is_absolute():
        return name if Path(name).exists() else None
    found = shutil.which(name)
    if found:
        return found
    for d in (Path.home() / ".local/bin", Path.home() / ".npm-global/bin", Path.home() / "bin", Path("/usr/local/bin")):
        if (d / name).is_file() and os.access(d / name, os.X_OK):
            return str(d / name)
    return None


def parse_ref(ref: str, providers: dict[str, Any]) -> tuple[str, str]:
    """`claude-code/opus` → ("claude-code", "opus"); `qwen35-9b` → ("local", "qwen35-9b")."""
    head, sep, rest = ref.partition("/")
    if sep and (head in providers or head == LOCAL):
        return head, rest
    return LOCAL, ref


def _split_system(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    if messages and messages[0]["role"] == "system":
        return messages[0]["content"], messages[1:]
    return "", messages


def auth_path(config: Config) -> Path:
    return config.home / "auth.json"


def load_auth(config: Config) -> dict[str, Any]:
    p = auth_path(config)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def save_auth(config: Config, provider: str, key: str | None) -> None:
    data = load_auth(config)
    if key:
        data[provider] = {"type": "api", "key": key}
    else:
        data.pop(provider, None)
    p = auth_path(config)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2))
    p.chmod(0o600)


# --- Claude Code ------------------------------------------------------------
# Claude Code leser selv (Read/Glob/Grep); disse filene skal den aldri få se.
_SECRET_GLOBS = ["**/.ssh/**", "**/.gnupg/**", "**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/*.p12",
                 "**/*.kdbx", "**/id_*", "**/auth.json", "**/.netrc", "**/.git-credentials", "**/.aws/**",
                 "**/.kube/**", "**/*credentials*", "**/*secret*", "**/.claude.json", "**/.npmrc", "**/.pypirc",
                 "**/.docker/config.json", "**/.config/gh/**", "**/*history", "**/.local/share/keyrings/**",
                 "**/.mozilla/**", "**/.password-store/**", "**/*.env", "**/*token*"]


_OLYMPARCH_KEYS: dict[str, tuple[float, str | None]] = {}


def olymparch_key(name: str) -> str | None:
    """Nøkkelen fra Olymparchs nøkkellager (`olymparch-keys get NAME`), når Olympus kjører på Olymparch. Svaret
    huskes i 30 s, så en melding ikke starter en prosess per oppslag. Nøkkelen kommer på stdout, aldri i argv."""
    import subprocess
    import time

    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name) or not shutil.which("olymparch-keys"):
        return None
    hit = _OLYMPARCH_KEYS.get(name)
    if hit and time.monotonic() - hit[0] < 30:
        return hit[1]
    try:
        r = subprocess.run(["olymparch-keys", "get", name], capture_output=True, text=True, timeout=5)
        key = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        key = None
    _OLYMPARCH_KEYS[name] = (time.monotonic(), key)
    return key


def claude_deny(dirs: list[Path]) -> list[str]:
    """Avslagsregler for Claude Code. Relative mønstre gjelder bare Claude Codes egen
    arbeidsmappe, så reglene lages absolutt (//sti/…) for hver mappe den får lese, pluss ~."""
    from olympus.tools import is_sensitive

    roots = [f"/{Path(d).resolve()}" for d in dirs] + ["~"]
    rules = [f"{tool}({root}/{g})" for tool in ("Read", "Grep", "Glob") for root in roots for g in _SECRET_GLOBS]
    # Eksakte regler for private filer og symlenker som peker ut, funnet ved skanning.
    exact: set[str] = set()
    for d in dirs:
        base = Path(d).resolve()
        for i, f in enumerate(base.rglob("*")):
            if i > 20000:
                break
            try:
                real = Path(os.path.realpath(f))
            except OSError:
                continue
            outside = f.is_symlink() and base not in real.parents and real != base
            if outside or is_sensitive(f) or is_sensitive(real):
                exact.update({f"/{f}", f"/{real}", f"/{f}/**", f"/{real}/**"})
    rules += [f"{tool}({path})" for tool in ("Read", "Grep", "Glob") for path in sorted(exact)]
    return rules
class ClaudeCodeBackend:
    """Kaller `claude -p` i minimal modus: ingen egne verktøy, ingen skills, ingen innstillingsfiler og ingen lagret
    økt; Olympus eier samtalen. Olympus' egne verktøy (plan, media, PC …) gis via en MCP-server som bare lever mens
    svaret skrives (OlympusMCP): hvert kall går gjennom Olympus' godkjenning og logg, som for lokale modeller."""

    product = "Claude Code"

    def __init__(self, *, binary: str, model: str, effort: str = "", workdir: Path,
                 project_dir: Path | None = None) -> None:
        self.binary = binary
        self.model = model
        self.effort = effort
        self.workdir = workdir  # nøytral mappe, så CLAUDE.md i prosjekter ikke lastes
        self.project_dir = project_dir  # satt: Claude kan lese (ikke endre) filer her
        self.extra_dirs: list[Path] = []  # f.eks. biblioteket (Arch Wiki)

    def _cmd(self, system: str, fmt: str, extra: list[str] | None = None, mcp: OlympusMCP | None = None) -> list[str]:
        allowed = (["Read", "Glob", "Grep"] if self.project_dir else []) + (mcp.allowed() if mcp else [])
        cmd = [
            self.binary, "-p",
            "--output-format", fmt,
            "--no-session-persistence",
            "--tools", "Read,Glob,Grep" if self.project_dir else "",
            *(["--add-dir", str(self.project_dir), *[a for d in self.extra_dirs for a in ("--add-dir", str(d))],
               "--disallowedTools", *claude_deny([self.project_dir, *self.extra_dirs])] if self.project_dir else []),
            *(["--allowedTools", ",".join(allowed)] if allowed else []),
            *(["--mcp-config", mcp.config_file] if mcp else []),  # fil (0600): nøkkelen skal ikke stå i argv
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--setting-sources", "",
            "--model", self.model,
        ]  # fmt: skip
        if fmt == "stream-json":
            cmd += ["--verbose", "--include-partial-messages"]
        if self.effort:
            cmd += ["--effort", self.effort]
        if system:
            cmd += ["--system-prompt", system]
        return cmd + (extra or [])

    @staticmethod
    def render(messages: list[dict[str, str]]) -> str:
        """`claude -p` tar én prompt; tidligere meldinger legges ved som transkript."""
        if len(messages) == 1:
            return messages[0]["content"]
        parts = ["Tidligere i samtalen:"]
        for m in messages[:-1]:
            who = "Bruker" if m["role"] == "user" else "Assistent"
            parts.append(f"[{who}]\n{m['content']}")
        parts.append(f"---\n\nNy melding fra brukeren:\n{messages[-1]['content']}")
        return "\n\n".join(parts)

    async def _spawn(self, cmd: list[str], prompt: str, env: dict[str, str] | None = None) -> asyncio.subprocess.Process:
        binary = find_binary(self.binary)
        if binary is None:
            raise BackendError(f"Fant ikke `{self.binary}`. Installer {self.product} eller sett providers.<navn>.binary.")
        cmd = [binary, *cmd[1:]]
        self.workdir.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.workdir,
            limit=32 * 1024 * 1024,  # resultatlinjen inneholder hele svaret
            env={**os.environ, **env} if env else None,
        )
        assert proc.stdin is not None
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()
        return proc

    @staticmethod
    async def _reap(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except TimeoutError:
                proc.kill()
                await proc.wait()

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.7,
        thinking: bool = False,
        tools: list[dict[str, Any]] | None = None,
        tool_runner: ToolRunner | None = None,
    ) -> AsyncIterator[Chunk]:
        """Strømmer svaret. Med tools + tool_runner får Claude Olympus' verktøy via MCP; det verktøyene gjør (kall,
        resultat) kommer i samme strøm som Chunk("event")."""
        system, rest = _split_system(messages)
        out: asyncio.Queue[Chunk | BaseException | None] = asyncio.Queue()
        mcp = OlympusMCP(tools, tool_runner, out.put_nowait) if tools and tool_runner else None
        proc = None
        reader = None
        try:
            if mcp:
                await mcp.start()
            # Godkjenning kan ta tid: Claude Code skal vente på brukeren, ikke gi opp kallet.
            proc = await self._spawn(self._cmd(system, "stream-json", mcp=mcp), self.render(rest),
                                     env={"MCP_TOOL_TIMEOUT": str(30 * 60 * 1000)} if mcp else None)
            reader = asyncio.create_task(self._read(proc, out))
            while (item := await out.get()) is not None:
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            if reader is not None:
                reader.cancel()
            if proc is not None:
                await self._reap(proc)
            if mcp:
                await mcp.stop()

    @staticmethod
    async def _read(proc: asyncio.subprocess.Process, out: asyncio.Queue) -> None:
        """Les stream-json fra `claude` inn i køen. None = ferdig; et unntak sendes videre til den som leser."""
        assert proc.stdout is not None and proc.stderr is not None
        try:
            async for raw in proc.stdout:
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                kind = d.get("type")
                if kind == "stream_event":
                    ev = d.get("event", {})
                    if ev.get("type") == "content_block_delta":
                        delta = ev.get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            out.put_nowait(Chunk("content", delta["text"]))
                        elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                            out.put_nowait(Chunk("reasoning", delta["thinking"]))
                elif kind == "result":
                    if d.get("is_error"):
                        raise BackendError(f"Claude Code: {d.get('result') or d.get('subtype')}")
                    u = d.get("usage") or {}
                    total = sum(
                        int(u.get(k) or 0)
                        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
                    )
                    out.put_nowait(Chunk("usage", usage={"total_tokens": total, "cost_usd": d.get("total_cost_usd")}))
            await proc.wait()
            if proc.returncode:
                err = (await proc.stderr.read()).decode(errors="replace").strip()
                raise BackendError(f"`claude` avsluttet med kode {proc.returncode}: {err[-500:]}")
            out.put_nowait(None)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            out.put_nowait(e)

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        thinking: bool = False,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 1024,
    ) -> str:
        system, rest = _split_system(messages)
        extra = ["--json-schema", json.dumps(json_schema)] if json_schema else []
        proc = await self._spawn(self._cmd(system, "json", extra), self.render(rest))
        try:
            out, err = await proc.communicate()
        finally:
            await self._reap(proc)
        try:
            d = json.loads(out)
        except ValueError as e:
            raise BackendError(f"Claude Code svarte ikke med JSON: {err.decode(errors='replace')[-300:]}") from e
        if d.get("is_error"):
            raise BackendError(f"Claude Code: {d.get('result') or d.get('subtype')}")
        if json_schema and d.get("structured_output") is not None:
            return json.dumps(d["structured_output"])
        return d.get("result") or ""


# Codex' own tools, all off (learn.chatgpt.com/docs/config-file/config-reference)
NO_CODEX_TOOLS = ("features.shell_tool=false", "features.unified_exec=false", 'web_search="disabled"',
                  "tools.view_image=false")


class CodexBackend(ClaudeCodeBackend):
    """OpenAI Codex CLI (`codex exec`) med ChatGPT-abonnementet: som Claude Code eier Olympus samtalen. Kjøres i en
    nøytral mappe med skrivebeskyttet sandkasse og uten noen av Codex' egne verktøy (skall, nettsøk, bilder), uten
    brukerens egen config.toml (ingen andre MCP-servere) og uten lagret økt. Olympus' verktøy gis via den samme MCP-serveren (OlympusMCP); adressen gis med -c, nøkkelen i en
    miljøvariabel (bearer_token_env_var), aldri i argv. Kall er forhåndsgodkjent i Codex: Olympus spør selv.
    Hendelsene (--json) er JSONL: item.completed {item: {type: agent_message|reasoning, text}}, turn.completed
    {usage}, turn.failed / error (docs: learn.chatgpt.com/docs/non-interactive-mode)."""

    product = "Codex CLI"
    TOKEN_ENV = "OLYMPUS_MCP_TOKEN"

    def _cmd(self, system: str, fmt: str, extra: list[str] | None = None, mcp: OlympusMCP | None = None) -> list[str]:
        cmd = [self.binary, "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
               "--ignore-user-config", "--cd", str(self.workdir), "--model", self.model]
        # No built-in tools at all: the read-only sandbox only stops writes, and Codex' shell could still read any
        # file (~/.ssh, keys) and send it to the cloud without Olympus asking. Olympus' own tools (MCP) remain.
        for key in NO_CODEX_TOOLS:
            cmd += ["-c", key]
        if mcp:
            base = "mcp_servers.olympus"
            cmd += ["-c", f'{base}.url="http://127.0.0.1:{mcp.port}{mcp.path}"',
                    "-c", f'{base}.bearer_token_env_var="{self.TOKEN_ENV}"',
                    "-c", f"{base}.tool_timeout_sec=1800",   # godkjenning kan ta tid: vent på brukeren
                    "-c", f'{base}.default_tools_approval_mode="approve"',
                    "-c", f"{base}.required=true"]
        if self.effort:
            cmd += ["-c", f'model_reasoning_effort="{self.effort}"']
        return cmd + (extra or []) + ["-"]   # prompten (med systemprompten først) kommer på stdin

    def _prompt(self, system: str, rest: list[dict[str, str]]) -> str:
        return (f"<instruksjoner>\n{system}\n</instruksjoner>\n\n" if system else "") + self.render(rest)

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.7,
        thinking: bool = False,
        tools: list[dict[str, Any]] | None = None,
        tool_runner: ToolRunner | None = None,
    ) -> AsyncIterator[Chunk]:
        system, rest = _split_system(messages)
        out: asyncio.Queue[Chunk | BaseException | None] = asyncio.Queue()
        mcp = OlympusMCP(tools, tool_runner, out.put_nowait) if tools and tool_runner else None
        proc = None
        reader = None
        try:
            if mcp:
                await mcp.start()
            proc = await self._spawn(self._cmd(system, "json", mcp=mcp), self._prompt(system, rest),
                                     env={self.TOKEN_ENV: mcp.token} if mcp else None)
            reader = asyncio.create_task(self._read(proc, out))
            while (item := await out.get()) is not None:
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            if reader is not None:
                reader.cancel()
            if proc is not None:
                await self._reap(proc)
            if mcp:
                await mcp.stop()

    @staticmethod
    def _failure(d: dict[str, Any]) -> BackendError:
        err = d.get("error")
        msg = (err.get("message") if isinstance(err, dict) else err) or d.get("message") or d.get("type")
        text = f"Codex: {msg}"
        if re.search(r"rate.?limit|429|usage limit|quota", str(msg), re.I):
            return QuotaError(text)
        return BackendError(text)

    @staticmethod
    async def _read(proc: asyncio.subprocess.Process, out: asyncio.Queue) -> None:
        assert proc.stdout is not None and proc.stderr is not None
        sent: dict[str, int] = {}   # item-id → hvor mye av teksten som er sendt (item.updated kan komme)
        failed: BackendError | None = None
        try:
            async for raw in proc.stdout:
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                kind = d.get("type", "")
                if kind in ("item.updated", "item.completed"):
                    item = d.get("item") or {}
                    text = item.get("text") or ""
                    if item.get("type") in ("agent_message", "reasoning") and text:
                        iid = str(item.get("id", ""))
                        new = text[sent.get(iid, 0):]
                        sent[iid] = len(text)
                        if new:
                            out.put_nowait(Chunk("content" if item["type"] == "agent_message" else "reasoning", new))
                elif kind == "turn.completed":
                    u = d.get("usage") or {}
                    total = sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens"))
                    out.put_nowait(Chunk("usage", usage={"total_tokens": total}))
                elif kind in ("turn.failed", "error"):
                    failed = failed or CodexBackend._failure(d)
            await proc.wait()
            if failed is not None:
                raise failed
            if proc.returncode:
                err = (await proc.stderr.read()).decode(errors="replace").strip()
                raise BackendError(f"`codex` avsluttet med kode {proc.returncode}: {err[-500:]}")
            out.put_nowait(None)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            out.put_nowait(e)

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        thinking: bool = False,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 1024,
    ) -> str:
        parts = []
        async for chunk in self.stream_chat(messages):
            if chunk.kind == "content":
                parts.append(chunk.text)
        return "".join(parts)


# --- Anthropic API ----------------------------------------------------------
class AnthropicBackend:
    """Anthropic Messages API via det offisielle SDK-et. Adaptiv tenking med
    oppsummert visning; serverside-fallback ved avslag er slått på."""

    BETAS = ["server-side-fallback-2026-07-01"]

    def __init__(self, *, model: str, api_key: str | None, effort: str = "", base_url: str | None = None) -> None:
        import anthropic

        self.model = model
        self.effort = effort
        self.client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)

    def _common(self, system: str) -> dict[str, Any]:
        kw: dict[str, Any] = {"model": self.model, "betas": self.BETAS, "fallbacks": "default"}
        if system:
            kw["system"] = system
        return kw

    @staticmethod
    def _wrap(e: Exception) -> BackendError:
        import anthropic

        if isinstance(e, anthropic.AuthenticationError):
            return BackendError("Anthropic avviste nøkkelen. Kjør `olympus auth login anthropic`.")
        if isinstance(e, anthropic.RateLimitError):
            return BackendError("Anthropic: rate limit nådd, prøv igjen om litt.")
        if isinstance(e, anthropic.APIStatusError):
            return BackendError(f"Anthropic API {e.status_code}: {e.message}")
        if isinstance(e, anthropic.APIConnectionError):
            return BackendError("Får ikke kontakt med Anthropic API (nettverk?).")
        return BackendError(str(e))

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.7,
        thinking: bool = False,
        tools: list[dict[str, Any]] | None = None,  # ikke støttet ennå
    ) -> AsyncIterator[Chunk]:
        import anthropic

        system, rest = _split_system(messages)
        kw = self._common(system)
        kw["thinking"] = {"type": "adaptive", "display": "summarized"}
        if self.effort:
            kw["output_config"] = {"effort": self.effort}
        try:
            async with self.client.beta.messages.stream(max_tokens=64000, messages=rest, **kw) as stream:
                async for event in stream:
                    if event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            yield Chunk("content", event.delta.text)
                        elif event.delta.type == "thinking_delta" and event.delta.thinking:
                            yield Chunk("reasoning", event.delta.thinking)
                final = await stream.get_final_message()
        except anthropic.AnthropicError as e:
            raise self._wrap(e) from e
        u = final.usage
        total = (u.input_tokens or 0) + (u.output_tokens or 0) + (
            getattr(u, "cache_read_input_tokens", 0) or 0
        ) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        yield Chunk("usage", usage={"total_tokens": total})
        if final.stop_reason == "refusal":
            raise BackendError("Claude avslo forespørselen (stop_reason: refusal).")

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        thinking: bool = False,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 1024,
    ) -> str:
        import anthropic

        system, rest = _split_system(messages)
        kw = self._common(system)
        cfg: dict[str, Any] = {"effort": self.effort or "low"}
        if json_schema:
            cfg["format"] = {"type": "json_schema", "schema": json_schema}
        try:
            msg = await self.client.beta.messages.create(
                max_tokens=max(max_tokens, 4096), messages=rest, output_config=cfg, **kw
            )
        except anthropic.AnthropicError as e:
            raise self._wrap(e) from e
        if msg.stop_reason == "refusal":
            raise BackendError("Claude avslo forespørselen (stop_reason: refusal).")
        return "".join(b.text for b in msg.content if b.type == "text")


# --- OpenAI-kompatible (Groq, OpenRouter, Gemini, Cerebras, Mistral …) -------
class QuotaError(BackendError):
    def __init__(self, msg: str, retry_after: float | None = None) -> None:
        super().__init__(msg)
        self.retry_after = retry_after


def _retry_after(headers: Any, text: str) -> float | None:
    """Sekunder provideren ber oss vente (header eller «try again in 2.5s» i teksten)."""
    try:
        if headers is not None and headers.get("retry-after"):
            return float(headers["retry-after"])
    except (TypeError, ValueError):
        pass
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", text)
    if m:
        return float(m.group(1) or 0) * 60 + float(m.group(2))
    return None


class OpenAICompatBackend:
    """Ethvert OpenAI-kompatibelt /chat/completions-API med API-nøkkel."""

    def __init__(self, *, label: str, base_url: str, api_key: str, model: str, timeout: float = 120.0) -> None:
        self.timeout = timeout
        self.label = label
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def _reasoning(self) -> bool:
        return "gpt-oss" in self.model or "reasoning" in self.model

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "X-Title": "Olympus"}

    def _error(self, status: int, text: str, headers: Any = None) -> BackendError:
        if status == 429:
            return QuotaError(
                f"{self.label}: gratiskvoten er brukt opp eller du sender for fort (429).",
                _retry_after(headers, text),
            )
        if status in (500, 502, 503, 504):
            return QuotaError(f"{self.label} er overbelastet eller utilgjengelig akkurat nå ({status}).",
                              _retry_after(headers, text))
        if status in (401, 403):
            return BackendError(f"{self.label} avviste nøkkelen ({status}). Koble til på nytt (⚙ Providers).")
        return BackendError(f"{self.label} svarte {status}: {text[:300]}")

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        thinking: bool = False,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 1024,
    ) -> str:
        import httpx

        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            # Resonneringsmodeller bruker tokens på å tenke før svaret; gi plass til begge.
            "max_tokens": max_tokens + (1024 if self._reasoning() else 0),
        }
        if self._reasoning():
            body["reasoning_effort"] = "low"
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "strict": True, "schema": json_schema},
            }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=10.0)) as client:
                r = await client.post(f"{self.base_url}/chat/completions", json=body, headers=self._headers())
        except httpx.HTTPError as e:
            raise QuotaError(f"{self.label}: ingen svar ({type(e).__name__}).") from e
        if r.status_code != 200:
            raise self._error(r.status_code, r.text, r.headers)
        try:
            return r.json()["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, ValueError) as e:
            raise BackendError(f"Uventet svar fra {self.label}: {r.text[:300]}") from e

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.7,
        thinking: bool = False,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Chunk]:
        import httpx

        from olympus.backends.llamacpp import parse_sse_line

        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = tools
        # read = maks stillhet mellom to biter i strømmen, ikke total tid.
        timeout = httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST", f"{self.base_url}/chat/completions", json=body, headers=self._headers()
                ) as resp:
                    if resp.status_code != 200:
                        raise self._error(resp.status_code, (await resp.aread()).decode(errors="replace"), resp.headers)
                    async for line in resp.aiter_lines():
                        chunks = parse_sse_line(line)
                        if chunks is None:
                            return
                        for chunk in chunks:
                            yield chunk
        except httpx.HTTPError as e:
            raise QuotaError(f"{self.label}: forbindelsen stoppet ({type(e).__name__}).") from e


async def list_models(spec: dict[str, Any], api_key: str) -> list[str]:
    """Hent modellene en OpenAI-kompatibel provider tilbyr akkurat nå."""
    import httpx

    base = spec["base_url"].rstrip("/")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{base}/models", headers={"Authorization": f"Bearer {api_key}"})
    if r.status_code != 200:
        raise BackendError(f"{spec.get('label', base)} svarte {r.status_code}: {r.text[:200]}")
    ids = [m.get("id", "") for m in r.json().get("data", [])]
    ids = [i.removeprefix("models/") for i in ids if i]
    flt = spec.get("model_filter")
    if flt:
        ids = [i for i in ids if flt in i]
    return sorted(set(ids))


# --- samlet oppslag ---------------------------------------------------------
# Abonnement via CLI: type → standard program. Verktøy går via MCP (OlympusMCP), ikke i OpenAI-formatet.
CLI_TYPES = {"claude-code": "claude", "codex": "codex"}


class Backends:
    """Slår opp riktig backend for en modellreferanse."""

    def __init__(self, config: Config, manager: ModelManager) -> None:
        self.config = config
        self.manager = manager
        self.cwd = Path.cwd()  # settes av orkestratoren
        self._cloud: dict[str, ChatBackend] = {}

    @property
    def providers(self) -> dict[str, Any]:
        return self.config.get("providers", {})

    def parse(self, ref: str) -> tuple[str, str]:
        return parse_ref(ref, self.providers)

    def is_cloud(self, ref: str) -> bool:
        return self.parse(ref)[0] != LOCAL

    def provider_label(self, ref: str) -> str:
        prov, _ = self.parse(ref)
        return self.providers.get(prov, {}).get("label", prov)

    def context_limit(self, ref: str) -> int:
        prov, _ = self.parse(ref)
        if prov == LOCAL:
            return int(self.config.get("llamacpp.ctx_size", 0))
        return int(self.providers.get(prov, {}).get("context", 1_000_000))

    def supports_tools(self, ref: str) -> bool:
        """Kan modellen bruke Olympus' verktøy? Lokale og OpenAI-kompatible i OpenAI-formatet, Claude Code via MCP."""
        prov, _ = self.parse(ref)
        return self.openai_tools(ref) or self.providers.get(prov, {}).get("type") in CLI_TYPES

    def openai_tools(self, ref: str) -> bool:
        """Tar modellen verktøy og verktøysvar i OpenAI-formatet (tools/tool_calls i meldingene)?"""
        prov, _ = self.parse(ref)
        return prov == LOCAL or self.providers.get(prov, {}).get("type") == "openai"

    def thinking(self, ref: str) -> bool:
        prov, name = self.parse(ref)
        if prov == LOCAL:
            return bool(self.config.model(name).get("thinking", False))
        return True

    def api_key(self, prov: str) -> str | None:
        """Miljøvariabelen (api_key_env), så Olymparchs nøkkellager (olymparch-keys: Secret Service eller 0600-fil,
        lagt inn én gang for Zeus og agentene), så ~/.olympus/auth.json."""
        spec = self.providers.get(prov, {})
        env = spec.get("api_key_env")
        return (os.environ.get(env) if env else None) or olymparch_key(prov) or load_auth(self.config).get(prov, {}).get("key")

    def anthropic_key(self) -> str | None:
        return self.api_key("anthropic")

    def cost(self, ref: str) -> str:
        """gratis | gratisnivå | abonnement | betalt"""
        prov, _ = self.parse(ref)
        return "gratis" if prov == LOCAL else self.providers.get(prov, {}).get("cost", "betalt")

    def trains_on_data(self, ref: str) -> bool:
        prov, _ = self.parse(ref)
        return bool(self.providers.get(prov, {}).get("may_train", False))

    def available(self, ref: str) -> tuple[bool, str]:
        """(tilgjengelig?, hvorfor ikke)."""
        prov, name = self.parse(ref)
        if prov == LOCAL:
            from olympus.gpu import gaming

            if gaming():
                return False, "spillmodus: lokal AI er av til spillet er ferdig"
            try:
                ok = self.config.model_path(name).exists()
            except KeyError:
                return False, f"ukjent modell {name!r}"
            return ok, "" if ok else f"ikke lastet ned (`olympus models pull {name}`)"
        spec = self.providers.get(prov)
        if spec is None:
            return False, f"ukjent provider {prov!r}"
        if spec.get("type") in CLI_TYPES:
            binary = spec.get("binary", CLI_TYPES[spec["type"]])
            ok = find_binary(binary) is not None
            return ok, "" if ok else f"fant ikke `{binary}` (installer {spec.get('label', prov)} eller sett providers.{prov}.binary)"
        if spec.get("type") == "anthropic":
            ok = bool(self.anthropic_key()) or (Path.home() / ".config/anthropic").exists()
            return ok, "" if ok else "mangler nøkkel (`olympus auth login anthropic`)"
        if spec.get("type") == "openai":
            ok = bool(self.api_key(prov))
            return ok, "" if ok else f"ikke koblet til {spec.get('label', prov)} (⚙ Providers)"
        return False, f"ukjent provider-type {spec.get('type')!r}"

    def reset(self) -> None:
        """Glem skybackends (etter modellbytte eller ny nøkkel)."""
        self._cloud.clear()
        _OLYMPARCH_KEYS.clear()

    def services(self, check_login: bool = True) -> list[dict[str, Any]]:
        """Alle leverandørene med status, for Olymparchs Innstillinger → Tjenester (`olympus providers --json`):
        ready | missing_key | not_installed | not_logged_in. Bare data fra config.toml; ingen nøkler ut."""
        import subprocess

        out = []
        for prov, spec in self.providers.items():
            kind = spec.get("type", "")
            cost = spec.get("cost", "betalt")
            item = {"id": prov, "label": spec.get("label", prov), "type": kind, "cost": cost,
                    "free": "gratis" in cost, "subscription": "abonnement" in cost, "signup": spec.get("signup", ""),
                    "note": spec.get("note", ""), "env": spec.get("env", ""), "login": spec.get("login", []),
                    "may_train": bool(spec.get("may_train"))}
            if kind in CLI_TYPES:
                binary = find_binary(spec.get("binary", CLI_TYPES[kind]))
                status = "not_installed" if binary is None else "ready"
                check = spec.get("login_status") or []
                if binary and check and check_login:
                    try:
                        r = subprocess.run([binary, *check[1:]], capture_output=True, timeout=8)
                        status = "ready" if r.returncode == 0 else "not_logged_in"
                    except (OSError, subprocess.SubprocessError):
                        status = "not_logged_in"
            elif kind == "openai":
                status = "ready" if self.api_key(prov) else "missing_key"
            elif kind == "anthropic":
                status = "ready" if self.anthropic_key() else "missing_key"
            else:
                status = "unknown"
            item["status"] = status
            out.append(item)
        return out

    def fallbacks(self) -> list[str]:
        """Reservekjeden (general.fallback), i rekkefølge: når valgt modell ikke er tilgjengelig, eller en skymodell
        svarer 429 / er nede / nettet svikter, fortsetter neste i lista med det som allerede er gjort. «lokal» (eller
        «local») er den lokale modellen, som alltid står sist. Hvem som er tilgjengelig (nøkkel, CLI) sjekkes der
        lista brukes."""
        local = self.config.get("general.local_model", "")
        out: list[str] = []
        for ref in self.config.get("general.fallback", []) or []:
            ref = local if str(ref).strip().lower() in ("lokal", "local") else str(ref).strip()
            if ref and ref not in out:
                out.append(ref)
        if local and local not in out:
            out.append(local)
        elif local:  # «lokal» midt i lista: flytt den sist, så en skymodell aldri står bak den lokale
            out.remove(local)
            out.append(local)
        return out

    def fallback_info(self) -> list[dict[str, Any]]:
        """Reservekjeden for skjermene (baren, appen): hver med navn og om den kan brukes nå, og hvorfor ikke."""
        out = []
        for ref in self.fallbacks():
            ok, why = self.available(ref)
            out.append({"id": ref, "label": self.provider_label(ref) if self.is_cloud(ref) else "lokal",
                        "cloud": self.is_cloud(ref), "available": ok, "why": why})
        return out

    def resolve(self, agent: str) -> tuple[str, str | None]:
        """Modellen agenten skal bruke, og eventuelt hvorfor det ble en fallback."""
        wanted = self.config.agent(agent).get("model", "")
        if wanted:
            ok, why = self.available(wanted)
            if ok:
                return wanted, None
        else:
            why = "ingen modell satt"
        for fb in self.fallbacks():
            if fb != wanted and self.available(fb)[0]:
                return fb, f"{wanted or agent}: {why}"
        return (self.fallbacks() or [wanted])[-1], f"{wanted or agent}: {why}"

    def local_models_needed(self, agents: list[str]) -> list[str]:
        """Lokale modeller agentene faktisk trenger (for forhåndslasting/fjerning)."""
        out: list[str] = []
        for a in agents:
            ref, _ = self.resolve(a)
            prov, name = self.parse(ref)
            if prov == LOCAL and name not in out:
                out.append(name)
        return out

    async def get(self, ref: str) -> ChatBackend:
        prov, name = self.parse(ref)
        if prov == LOCAL:
            return await self.manager.ensure(name)
        if ref in self._cloud:
            return self._cloud[ref]
        spec = self.providers[prov]
        backend: ChatBackend
        if spec.get("type") == "claude-code":
            backend = ClaudeCodeBackend(
                binary=spec.get("binary", "claude"),
                model=name,
                effort=spec.get("effort", ""),
                workdir=self.config.home / "claude-code",
                # Aldri hele hjemmemappen eller / (ville gitt lesetilgang til f.eks. ~/.ssh).
                project_dir=self.cwd if spec.get("read_tools") and self.cwd.resolve() not in (
                    Path.home().resolve(), Path("/")) else None,
            )
            if spec.get("read_tools"):
                from olympus.library import Library

                backend.extra_dirs = Library(self.config).read_dirs()
                if backend.project_dir is None:  # biblioteket alene: gi lesetilgang til det
                    backend.project_dir, backend.extra_dirs = backend.extra_dirs[0], backend.extra_dirs[1:]
        elif spec.get("type") == "codex":
            backend = CodexBackend(binary=spec.get("binary", "codex"), model=name, effort=spec.get("effort", ""),
                                   workdir=self.config.home / "codex")
        elif spec.get("type") == "anthropic":
            backend = AnthropicBackend(
                model=name,
                api_key=self.anthropic_key(),
                effort=spec.get("effort", ""),
                base_url=spec.get("base_url") or None,
            )
        elif spec.get("type") == "openai":
            key = self.api_key(prov)
            if not key:
                raise BackendError(f"Ikke koblet til {spec.get('label', prov)}. Åpne ⚙ Providers.")
            backend = OpenAICompatBackend(
                label=spec.get("label", prov), base_url=spec["base_url"], api_key=key, model=name
            )
        else:
            raise BackendError(f"Ukjent provider-type for {prov}: {spec.get('type')!r}")
        self._cloud[ref] = backend
        return backend
