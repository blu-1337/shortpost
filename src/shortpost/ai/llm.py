"""Ollama client (port of project 07 llm.py): structured output, schema validation, one retry."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import jsonschema

from ..log import logger

PROMPTS = Path(__file__).resolve().parent / "prompts"


def load_prompt(name: str, schema: dict) -> tuple[str, str, str]:
    """(system text, version, sha1 prefix). context.md is prepended; {schema} is replaced by the JSON schema."""
    raw = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    version, body = "0", raw
    if raw.startswith("version:"):
        first, _, body = raw.partition("\n")
        version = first.split(":", 1)[1].strip()
    context = (PROMPTS / "context.md").read_text(encoding="utf-8").strip()
    body = body.strip().replace("{schema}", json.dumps(schema, indent=1))
    text = f"{context}\n\n{body}"
    return text, version, hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


@dataclass
class LLMStats:
    calls: int = 0
    schema_failures: int = 0
    prompt_tokens: int = 0
    gen_tokens: int = 0
    errors: list[str] = field(default_factory=list)


class LLM:
    def __init__(self, cfg: dict, model: str | None = None, client: httpx.Client | None = None):
        self.cfg = cfg["ai"]
        self.model = model or self.cfg["model"]
        self.host = self.cfg["host"].rstrip("/")
        self.client = client or httpx.Client(timeout=float(self.cfg.get("timeout_s", 600)))
        self.stats = LLMStats()

    def _chat(self, messages: list[dict], schema: dict, keep_alive: Any, seed: int) -> dict:
        body = {
            "model": self.model, "stream": False, "think": bool(self.cfg.get("think", False)), "format": schema,
            "keep_alive": keep_alive,
            "options": {"num_ctx": int(self.cfg["num_ctx"]), "temperature": float(self.cfg["temperature"]), "seed": seed},
            "messages": messages,
        }
        r = self.client.post(f"{self.host}/api/chat", json=body)
        r.raise_for_status()
        return r.json()

    def ask(self, system: str, user: str, schema: dict, seed: int, last: bool = False) -> dict | None:
        """One validated JSON answer, or None after one retry. keep_alive 0 on the last call unloads the model."""
        keep: Any = 0 if last else "5m"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for attempt in (1, 2):
            try:
                resp = self._chat(messages, schema, keep, seed)
            except (httpx.HTTPError, ValueError) as e:
                self.stats.errors.append(f"{type(e).__name__}: {e}")
                logger.warning("llm %s: request failed: %s", self.model, e)
                return None
            self.stats.calls += 1
            self.stats.prompt_tokens += int(resp.get("prompt_eval_count", 0))
            self.stats.gen_tokens += int(resp.get("eval_count", 0))
            content = resp.get("message", {}).get("content", "")
            try:
                data = json.loads(content)
                jsonschema.validate(data, schema)
                return data
            except (ValueError, jsonschema.ValidationError) as e:
                error = str(e).splitlines()[0][:300]
                self.stats.schema_failures += 1
                logger.warning("llm %s: answer rejected (attempt %d): %s", self.model, attempt, error)
                messages = messages + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": f"That answer was invalid: {error}. Answer again with valid JSON that follows the schema."},
                ]
        return None


GEMINI_PREFIX = "gemini:"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_gemini_blocked_until = 0.0  # after a quota/rate error the rest of the run goes straight to the local model


def gemini_key() -> str:
    from .. import config

    return str(config.load_secrets().get("gemini_api_key") or "")


def gemini_enabled(cfg: dict) -> bool:
    ai = cfg["ai"]
    return "gemini" in ai.get("providers", ["ollama"]) and bool(ai.get("gemini_model")) and bool(gemini_key())


class GeminiLLM:
    """Same `ask` interface as LLM, on the Gemini API (free tier). Returns None on any trouble so the caller falls back
    to the local model. The key goes in a header, never in the URL, and is never logged."""

    def __init__(self, cfg: dict, model: str | None = None, client: httpx.Client | None = None, key: str | None = None):
        self.cfg = cfg["ai"]
        self.model = (model or self.cfg["gemini_model"]).removeprefix(GEMINI_PREFIX)
        self.client = client or httpx.Client(timeout=float(self.cfg.get("gemini_timeout_s", 120)))
        self.key = key if key is not None else gemini_key()
        self.stats = LLMStats()

    def _chat(self, system: str, messages: list[dict], seed: int) -> dict:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": messages,
            "generationConfig": {"responseMimeType": "application/json", "temperature": float(self.cfg["temperature"]), "seed": seed},
        }
        r = self.client.post(GEMINI_URL.format(model=self.model), json=body, headers={"x-goog-api-key": self.key})
        r.raise_for_status()
        return r.json()

    def ask(self, system: str, user: str, schema: dict, seed: int, last: bool = False) -> dict | None:
        global _gemini_blocked_until
        if time.time() < _gemini_blocked_until:
            self.stats.errors.append("gemini paused after a quota error")
            return None
        messages = [{"role": "user", "parts": [{"text": user}]}]
        for attempt in (1, 2):
            try:
                resp = self._chat(system, messages, seed)
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                self.stats.errors.append(f"HTTP {code}")
                logger.warning("gemini %s: HTTP %s %s", self.model, code, e.response.text[:200].replace(self.key, "***") if self.key else "")
                if code in (429, 403):  # quota / rate limit: stop asking for a while
                    _gemini_blocked_until = time.time() + 30 * 60
                return None
            except (httpx.HTTPError, ValueError) as e:
                self.stats.errors.append(f"{type(e).__name__}")
                logger.warning("gemini %s: request failed: %s", self.model, type(e).__name__)
                return None
            self.stats.calls += 1
            usage = resp.get("usageMetadata", {})
            self.stats.prompt_tokens += int(usage.get("promptTokenCount", 0))
            self.stats.gen_tokens += int(usage.get("candidatesTokenCount", 0))
            try:
                content = "".join(p.get("text", "") for p in resp["candidates"][0]["content"]["parts"])
            except (KeyError, IndexError, TypeError):
                self.stats.errors.append("empty answer")
                logger.warning("gemini %s: empty answer (%s)", self.model, str(resp.get("promptFeedback", ""))[:100])
                return None
            try:
                data = json.loads(content)
                jsonschema.validate(data, schema)
                return data
            except (ValueError, jsonschema.ValidationError) as e:
                error = str(e).splitlines()[0][:300]
                self.stats.schema_failures += 1
                logger.warning("gemini %s: answer rejected (attempt %d): %s", self.model, attempt, error)
                messages = messages + [
                    {"role": "model", "parts": [{"text": content}]},
                    {"role": "user", "parts": [{"text": f"That answer was invalid: {error}. Answer again with valid JSON that follows the schema."}]},
                ]
        return None


def make_llm(cfg: dict, model: str):
    """`gemini:<model>` -> GeminiLLM, anything else -> the local Ollama client."""
    if model.startswith(GEMINI_PREFIX):
        return GeminiLLM(cfg, model)
    return LLM(cfg, model)


class ChainLLM:
    """For one-off questions (related video): Gemini first when enabled, then the local model."""

    def __init__(self, cfg: dict):
        self.parts = ([GeminiLLM(cfg)] if gemini_enabled(cfg) else []) + [LLM(cfg)]

    def ask(self, system: str, user: str, schema: dict, seed: int, last: bool = False) -> dict | None:
        for p in self.parts:
            out = p.ask(system, user, schema, seed, last)
            if out is not None:
                return out
        return None
