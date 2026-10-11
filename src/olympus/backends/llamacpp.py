"""llama.cpp-backend: starter/stopper `llama-server` og strømmer svar via
det OpenAI-kompatible API-et på localhost."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx


class BackendError(RuntimeError):
    pass


@dataclass
class Chunk:
    """Én strømmet bit. `kind` er "content", "reasoning" eller "usage"."""

    kind: str  # content | reasoning | usage | tool_call
    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    tool: dict[str, Any] = field(default_factory=dict)  # {index, id, name, arguments} (delvis)
    event: Any = None  # kind="event": en hendelse fra verktøyene (Claude Code via MCP) som skal rett ut i chatten


def parse_sse_line(line: str) -> list[Chunk] | None:
    """Tolk én SSE-linje fra /v1/chat/completions. None betyr [DONE]."""
    line = line.strip()
    if not line.startswith("data:"):
        return []
    payload = line[5:].strip()
    if payload == "[DONE]":
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if "error" in data:
        err = data["error"]
        raise BackendError(err.get("message", str(err)) if isinstance(err, dict) else str(err))
    out: list[Chunk] = []
    for choice in data.get("choices") or []:
        delta = choice.get("delta") or {}
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            out.append(Chunk("reasoning", reasoning))
        if delta.get("content"):
            out.append(Chunk("content", delta["content"]))
        for tc in delta.get("tool_calls") or []:
            fn = tc.get("function") or {}
            out.append(Chunk("tool_call", tool={
                "index": tc.get("index", 0),
                "id": tc.get("id") or "",
                "name": fn.get("name") or "",
                "arguments": fn.get("arguments") or "",
                # Gemini 3 krever at thought_signature sendes tilbake med verktøykallet.
                "extra_content": tc.get("extra_content"),
            }))
    usage = data.get("usage")
    if usage:
        out.append(Chunk("usage", usage=usage))
    return out


def port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


def port_serves(host: str, port: int, alias: str) -> bool:
    """Svarer en llama-server på porten med denne modellen (alias)?"""
    if not port_open(host, port):
        return False
    try:
        r = httpx.get(f"http://{host}:{port}/v1/models", timeout=0.5)
        return any(m.get("id") == alias for m in r.json().get("data", []))
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


class LlamaServer:
    """Én llama-server-prosess som serverer én GGUF-modell."""

    def __init__(
        self,
        *,
        binary: str,
        model_path: Path,
        host: str,
        port: int,
        ctx_size: int,
        gpu_layers: int,
        extra_args: list[str],
        log_path: Path,
        alias: str,
    ) -> None:
        self.binary = binary
        self.model_path = model_path
        self.host = host
        self.port = port
        self.ctx_size = ctx_size
        self.gpu_layers = gpu_layers
        self.extra_args = extra_args
        self.log_path = log_path
        self.alias = alias
        self.proc: subprocess.Popen[bytes] | None = None
        self.external = False  # eid av en annen Olympus-instans: brukes, men stoppes aldri

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def command(self) -> list[str]:
        return [
            self.binary,
            "--model", str(self.model_path),
            "--alias", self.alias,
            "--host", self.host,
            "--port", str(self.port),
            "--ctx-size", str(self.ctx_size),
            "--n-gpu-layers", str(self.gpu_layers),
            *self.extra_args,
        ]  # fmt: skip

    def log_tail(self, lines: int = 15) -> str:
        if not self.log_path.exists():
            return ""
        return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])

    async def start(self) -> None:
        from olympus.providers import find_binary

        resolved = find_binary(self.binary)
        if resolved is None:
            raise BackendError(
                f"Fant ikke `{self.binary}`. Installer llama.cpp, f.eks. "
                "`sudo pacman -S llama-cpp`, eller sett llamacpp.binary i config."
            )
        if not self.model_path.exists():
            raise BackendError(
                f"Modellfilen mangler: {self.model_path}\n"
                f"Last ned med `olympus models pull {self.alias}`."
            )
        self.binary = resolved
        self.binary = resolved
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = self.log_path.open("wb")
        self.proc = subprocess.Popen(
            self.command(),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # Ctrl+C i TUI-en skal ikke drepe serveren
        )
        log.close()
        from olympus import procs

        procs.register(f"llama-{self.port}", self.proc.pid)

    async def wait_healthy(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < deadline:
                if self.proc is not None and self.proc.poll() is not None:
                    raise BackendError(
                        f"llama-server avsluttet med kode {self.proc.returncode}.\n"
                        f"Logg ({self.log_path}):\n{self.log_tail()}"
                    )
                try:
                    r = await client.get(f"{self.base_url}/health")
                    if r.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
        raise BackendError(f"llama-server ble ikke klar innen {timeout:.0f} s.\n{self.log_tail()}")

    def running(self) -> bool:
        if self.external:
            return port_serves(self.host, self.port, self.alias)
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.external or self.proc is None:
            return
        from olympus import procs

        procs.unregister(f"llama-{self.port}")
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        thinking: bool = False,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 1024,
    ) -> str:
        """Ikke-strømmende kall. Med `json_schema` tvinger llama.cpp utdata gjennom en
        grammatikk, så svaret alltid er gyldig JSON etter skjemaet."""
        body: dict[str, Any] = {
            "model": self.alias,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": thinking},
        }
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "strict": True, "schema": json_schema},
            }
        timeout = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(f"{self.base_url}/v1/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise BackendError(f"llama-server svarte ikke ({type(e).__name__}).") from e
        if r.status_code != 200:
            raise BackendError(f"llama-server svarte {r.status_code}: {r.text[:500]}")
        try:
            return r.json()["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, ValueError) as e:
            raise BackendError(f"Uventet svar fra llama-server: {r.text[:300]}") from e

    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        thinking: bool = False,
        tools: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[Chunk]:
        body: dict[str, Any] = {
            "model": self.alias,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": thinking},
            **(extra or {}),
        }
        if tools:
            body["tools"] = tools
        timeout = httpx.Timeout(connect=5.0, read=600.0, write=30.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST", f"{self.base_url}/v1/chat/completions", json=body
            ) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode(errors="replace")
                    raise BackendError(f"llama-server svarte {resp.status_code}: {text[:500]}")
                async for line in resp.aiter_lines():
                    chunks = parse_sse_line(line)
                    if chunks is None:
                        return
                    for chunk in chunks:
                        yield chunk
