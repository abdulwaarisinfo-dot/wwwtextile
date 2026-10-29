"""Optional external providers (LLM chat, speech-to-text, text-to-speech). Standard library only.

Nothing here runs unless the operator configures WW_AI_* / WW_STT_* / WW_TTS_* on the SERVER; keys never reach the browser.
Providers are configurable adapters: Anthropic Messages API, and any OpenAI-compatible endpoint.
They are tested in this repo against local FAKE servers only; they have not been exercised against a live provider.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
import uuid
from typing import Callable

from ..config import Config

TIMEOUT = 40


class ProviderError(Exception):
    """Message is safe to show to users (never contains keys)."""


def _post(url: str, headers: dict, body: bytes, timeout: int = TIMEOUT) -> bytes:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 - operator-configured https URL
            return r.read()
    except urllib.error.HTTPError as e:
        raise ProviderError(f"provider returned HTTP {e.code}") from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        raise ProviderError("provider unreachable or timed out") from None


# ------------------------------------------------------------------ status
def llm_status(cfg: Config) -> dict:
    if not cfg.ai_provider:
        return {"configured": False, "provider": None, "detail": "No LLM provider configured; the built-in rule-based engine answers a fixed set of question patterns."}
    if cfg.ai_provider not in ("anthropic", "openai"):
        return {"configured": False, "provider": cfg.ai_provider, "detail": f"Unknown provider '{cfg.ai_provider}' (use 'anthropic' or 'openai')."}
    if not cfg.ai_api_key or not cfg.ai_model:
        return {"configured": False, "provider": cfg.ai_provider, "detail": "WW_AI_API_KEY and WW_AI_MODEL must both be set."}
    return {"configured": True, "provider": cfg.ai_provider, "model": cfg.ai_model, "detail": "Configured (not verified until used)."}


def stt_status(cfg: Config) -> dict:
    if cfg.stt_provider == "openai" and cfg.stt_api_key:
        return {"configured": True, "provider": "openai-compatible", "detail": "Server-side transcription configured."}
    return {"configured": False, "provider": None, "detail": "No server speech-to-text provider; the browser's own speech recognition is used where supported."}


def tts_status(cfg: Config) -> dict:
    if cfg.tts_provider == "openai" and cfg.tts_api_key:
        return {"configured": True, "provider": "openai-compatible", "detail": "Server-side speech synthesis configured."}
    return {"configured": False, "provider": None, "detail": "No server text-to-speech provider; the browser's built-in voice is used where supported."}


# ------------------------------------------------------------------ LLM with tool calling
ToolCaller = Callable[[str, dict], str]


def run_llm(cfg: Config, system: str, history: list[dict], user_text: str, tools: list[dict], call_tool: ToolCaller, max_steps: int = 5) -> str:
    """Run a bounded tool-calling loop. `tools` use {name, description, input_schema}. Returns the final text."""
    st = llm_status(cfg)
    if not st["configured"]:
        raise ProviderError(st["detail"])
    try:
        if cfg.ai_provider == "anthropic":
            return _run_anthropic(cfg, system, history, user_text, tools, call_tool, max_steps)
        return _run_openai(cfg, system, history, user_text, tools, call_tool, max_steps)
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError, IndexError, UnicodeDecodeError):
        # A provider (or proxy) returned something that is not the documented shape. Never let that surface as a server error.
        raise ProviderError("provider returned an unexpected response") from None


def _run_anthropic(cfg, system, history, user_text, tools, call_tool, max_steps):
    base = (cfg.ai_base_url or "https://api.anthropic.com").rstrip("/")
    msgs = [{"role": m["role"], "content": m["content"]} for m in history] + [{"role": "user", "content": user_text}]
    headers = {"content-type": "application/json", "x-api-key": cfg.ai_api_key, "anthropic-version": "2023-06-01"}
    for _ in range(max_steps):
        body = json.dumps({"model": cfg.ai_model, "max_tokens": 1024, "system": system, "messages": msgs, "tools": tools}).encode()
        data = json.loads(_post(base + "/v1/messages", headers, body))
        blocks = data.get("content") or []
        uses = [b for b in blocks if b.get("type") == "tool_use"]
        if not uses:
            return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        msgs.append({"role": "assistant", "content": blocks})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": u["id"], "content": call_tool(u["name"], u.get("input") or {})} for u in uses]})
    raise ProviderError("the model did not finish within the tool-call limit")


def _run_openai(cfg, system, history, user_text, tools, call_tool, max_steps):
    base = (cfg.ai_base_url or "https://api.openai.com/v1").rstrip("/")
    msgs = [{"role": "system", "content": system}] + [{"role": m["role"], "content": m["content"]} for m in history] + [{"role": "user", "content": user_text}]
    fn_tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in tools]
    headers = {"content-type": "application/json", "authorization": f"Bearer {cfg.ai_api_key}"}
    for _ in range(max_steps):
        body = json.dumps({"model": cfg.ai_model, "messages": msgs, "tools": fn_tools}).encode()
        data = json.loads(_post(base + "/chat/completions", headers, body))
        msg = ((data.get("choices") or [{}])[0]).get("message") or {}
        calls = msg.get("tool_calls") or []
        if not calls:
            return (msg.get("content") or "").strip()
        msgs.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
        for c in calls:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"__invalid__": True}
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": call_tool(c["function"]["name"], args)})
    raise ProviderError("the model did not finish within the tool-call limit")


def llm_json(cfg: Config, system: str, user_text: str) -> str:
    """Single completion without tools (used for document field extraction)."""
    return run_llm(cfg, system, [], user_text, [], lambda n, a: "tools are not available", max_steps=1)


# ------------------------------------------------------------------ speech
def transcribe(cfg: Config, audio: bytes, filename: str, mime: str) -> str:
    if not stt_status(cfg)["configured"]:
        raise ProviderError("No speech-to-text provider is configured on the server.")
    base = (cfg.stt_base_url or "https://api.openai.com/v1").rstrip("/")
    boundary = uuid.uuid4().hex
    def part(name, value):
        return (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode()
    body = part("model", cfg.stt_model) + part("response_format", "json") + (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode() + audio + f"\r\n--{boundary}--\r\n".encode()
    data = json.loads(_post(base + "/audio/transcriptions", {"authorization": f"Bearer {cfg.stt_api_key}", "content-type": f"multipart/form-data; boundary={boundary}"}, body, timeout=60))
    return (data.get("text") or "").strip()


def synthesize(cfg: Config, text: str) -> tuple[bytes, str]:
    if not tts_status(cfg)["configured"]:
        raise ProviderError("No text-to-speech provider is configured on the server.")
    base = (cfg.tts_base_url or "https://api.openai.com/v1").rstrip("/")
    body = json.dumps({"model": cfg.tts_model, "input": text[:1000], "voice": cfg.tts_voice, "response_format": "mp3"}).encode()
    return _post(base + "/audio/speech", {"authorization": f"Bearer {cfg.tts_api_key}", "content-type": "application/json"}, body, timeout=60), "audio/mpeg"
