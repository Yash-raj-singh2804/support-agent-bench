"""Thin provider-agnostic chat + tool-calling layer.

Providers: Anthropic, OpenAI, Groq (open-weight models via Groq's OpenAI-compatible
API) and Google Gemini (native google-genai SDK). Models are addressed as
"provider:model".

Messages use one neutral format:
    {"role": "user", "content": str}
    {"role": "assistant", "content": str | None, "tool_calls": [{"id", "name", "arguments": dict}]}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}
"""

import base64
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from .config import get_settings
from .ratelimit import call_with_limits, estimate_tokens

log = logging.getLogger(__name__)

PROVIDERS = ("anthropic", "openai", "groq", "gemini", "sarvam")

# Shown in the UI model picker. Any other "provider:model" string also works.
MODEL_PRESETS = [
    {"spec": "anthropic:claude-sonnet-5-5", "label": "Claude Sonnet 5.5", "open_weights": False},
    {"spec": "anthropic:claude-haiku-5-5", "label": "Claude Haiku 5.5", "open_weights": False},
    {"spec": "anthropic:claude-opus-5-5", "label": "Claude Opus 5.5", "open_weights": False},
    {"spec": "groq:openai/gpt-oss-120b", "label": "gpt-oss-120b (Groq)", "open_weights": True},
    {"spec": "groq:openai/gpt-oss-20b", "label": "gpt-oss-20b (Groq)", "open_weights": True},
    {"spec": "groq:qwen/qwen3.8-27b", "label": "Qwen3.8 27B (Groq)", "open_weights": True},
    {"spec": "groq:llama-3.3-70b-versatile", "label": "Llama 3.3 70B (Groq)", "open_weights": True},
    {"spec": "gemini:gemini-3.5-flash", "label": "Gemini 3.5 Flash", "open_weights": False},
    {"spec": "gemini:gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro (preview)", "open_weights": False},
    {"spec": "gemini:gemini-3.1-flash-lite", "label": "Gemini 3.1 Flash-Lite", "open_weights": False},
    {"spec": "gemini:gemini-2.5-flash", "label": "Gemini 2.5 Flash", "open_weights": False},
    {"spec": "sarvam:deepseekv4.1-flash-1017", "label": "DeepSeek V4.1 Flash 1017 (Sarvam)", "open_weights": True},
    {"spec": "sarvam:glm5.3-flash", "label": "GLM 5.3 Flash (Sarvam)", "open_weights": True},
    {"spec": "sarvam:gemma4", "label": "Gemma 4 31B (Sarvam)", "open_weights": True},
    {"spec": "sarvam:sarvam-105b", "label": "Sarvam 105B", "open_weights": True},
    {"spec": "openai:gpt-6-sol", "label": "GPT-6 Sol", "open_weights": False},
]


def parse_spec(spec: str) -> tuple[str, str]:
    """'groq:openai/gpt-oss-120b' -> ('groq', 'openai/gpt-oss-120b'). Bare names use LLM_PROVIDER."""
    head, sep, rest = spec.partition(":")
    if sep and head.lower() in PROVIDERS:
        return head.lower(), rest
    return get_settings().llm_provider.lower(), spec


def provider_configured(provider: str) -> bool:
    s = get_settings()
    return bool({"anthropic": s.anthropic_api_key, "openai": s.openai_api_key, "groq": s.groq_api_key,
                 "gemini": s.gemini_key, "sarvam": s.sarvam_api_key}.get(provider))


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    input_tokens: int = 0  # all prompt tokens, including cached ones
    output_tokens: int = 0  # all generated tokens, including reasoning/thinking
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    wait_ms: float = 0.0  # time spent waiting on rate limits / retries
    reasoning: str | None = None  # visible thinking text, for providers that return it (e.g. reasoning_content)


class LLM(Protocol):
    model: str

    def chat(self, system: str, messages: list[dict], tools: list[dict] | None = None,
             temperature: float | None = 0.0) -> LLMResponse: ...


class AnthropicLLM:
    def __init__(self, model: str):
        import anthropic

        self.model = model
        self.client = anthropic.Anthropic(api_key=get_settings().anthropic_api_key, max_retries=5)

    @staticmethod
    def _convert(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                block = {"type": "text", "text": m["content"] or "(empty)"}
                role = "user"
            elif m["role"] == "assistant":
                blocks = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["arguments"]})
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "(empty)"}]})
                continue
            else:  # tool result -> user turn
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
                role = "user"
            if out and out[-1]["role"] == role == "user":
                out[-1]["content"].append(block)  # merge consecutive user/tool_result blocks
            else:
                out.append({"role": role, "content": [block]})
        if out and out[0]["role"] == "assistant":  # API requires a user turn first
            out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(customer connected)"}]})
        return out

    def chat(self, system, messages, tools=None, temperature=0.0) -> LLMResponse:
        # Current Claude models/SDK don't take a sampling temperature; it's ignored here.
        kwargs = dict(model=self.model, max_tokens=4096, system=system, messages=self._convert(messages))
        if tools:
            kwargs["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]
        resp = self.client.messages.create(**kwargs)
        r = LLMResponse()
        for b in resp.content:
            if b.type == "text":
                r.content += b.text
            elif b.type == "tool_use":
                r.tool_calls.append({"id": b.id, "name": b.name, "arguments": dict(b.input or {})})
        usage = getattr(resp, "usage", None)
        cached = (getattr(usage, "cache_read_input_tokens", 0) or 0)
        r.input_tokens = (getattr(usage, "input_tokens", 0) or 0) + cached + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        r.output_tokens = getattr(usage, "output_tokens", 0) or 0
        r.cached_tokens = cached
        return r


class OpenAICompatLLM:
    """OpenAI chat-completions; also used for Groq, Sarvam and any compatible endpoint."""

    pass_reasoning = False  # send reasoning_content back on assistant tool-call turns

    def __init__(self, model: str, api_key: str | None, base_url: str | None = None, provider: str = "openai"):
        import openai

        self.model = model
        self.provider = provider
        # retries (incl. 429s) are handled by ratelimit.call_with_limits
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=120)

    @staticmethod
    def _classify(e: Exception):
        import openai

        if isinstance(e, openai.RateLimitError):
            text = str(e).lower()
            headers = getattr(getattr(e, "response", None), "headers", {}) or {}
            try:
                retry_after = float(headers.get("retry-after")) if headers.get("retry-after") else None
            except ValueError:
                retry_after = None
            daily = any(k in text for k in ("per day", "(tpd)", "(rpd)", "tokens per day", "requests per day", "quota"))
            return ("daily" if daily else "rate"), retry_after
        if isinstance(e, (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError)):
            return "transient", None
        if isinstance(e, openai.APIStatusError) and getattr(e, "status_code", 0) in (408, 409, 502, 503, 504):
            return "transient", None
        return None, None

    def extra_kwargs(self) -> dict:
        return {}

    def _convert(self, system: str, messages: list[dict]) -> list[dict]:
        out = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "assistant":
                msg = {"role": "assistant", "content": m.get("content") or ""}
                if self.pass_reasoning and m.get("reasoning_content") and m.get("tool_calls"):
                    msg["reasoning_content"] = m["reasoning_content"]
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{"id": tc["id"], "type": "function",
                                          "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}}
                                         for tc in m["tool_calls"]]
                out.append(msg)
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                out.append({"role": "user", "content": m["content"]})
        return out

    def chat(self, system, messages, tools=None, temperature=0.0) -> LLMResponse:
        import openai

        kwargs = dict(model=self.model, messages=self._convert(system, messages), **self.extra_kwargs())
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = [{"type": "function", "function": t} for t in tools]

        def once():
            # Open-weight models occasionally emit malformed tool calls; Groq rejects those
            # with a 400 "tool_use_failed". Retry a couple of times before giving up.
            for attempt in range(3):
                try:
                    return self.client.chat.completions.create(**kwargs)
                except openai.BadRequestError as e:
                    if "tool_use_failed" not in str(e) or attempt == 2:
                        raise
                    log.warning("tool_use_failed from %s, retrying (%d)", self.model, attempt + 1)

        resp, waited = call_with_limits(
            self.provider, self.model, estimate_tokens(system, kwargs["messages"], kwargs.get("tools")), once,
            self._classify, lambda r: getattr(getattr(r, "usage", None), "total_tokens", 0) or 0)
        msg = resp.choices[0].message
        r = LLMResponse(content=msg.content or "")
        extra = getattr(msg, "model_extra", None) or {}
        r.reasoning = getattr(msg, "reasoning_content", None) or extra.get("reasoning_content") or None
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            r.tool_calls.append({"id": tc.id, "name": tc.function.name, "arguments": args if isinstance(args, dict) else {"_raw": args}})
        usage = getattr(resp, "usage", None)
        r.input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        r.output_tokens = getattr(usage, "completion_tokens", 0) or 0
        r.cached_tokens = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        r.reasoning_tokens = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", 0) or 0
        r.wait_ms = waited * 1000
        return r


class SarvamLLM(OpenAICompatLLM):
    """Sarvam Token Factory (OpenAI-compatible v2 API) serving open-weight models.

    * reasoning shows up as ``message.reasoning_content`` and is billed as completion tokens;
      it's kept and sent back on tool-call turns so multi-step tool use keeps its chain of thought.
    * thinking counts against ``max_tokens``, so we give it more room than the 2048 default.
    * ``SARVAM_REASONING_EFFORT`` (none/low/medium/high) overrides each model's default; GLM 5.3 can't
      turn thinking off, so "none" is not sent to it.
    """

    pass_reasoning = True

    def __init__(self, model: str, api_key: str | None, base_url: str):
        super().__init__(model, api_key, base_url, provider="sarvam")
        # per-instance overrides (e.g. the grounding verifier), so they don't change the agents' settings
        self.max_tokens: int | None = None
        self.reasoning_effort: str | None = None

    def extra_kwargs(self) -> dict:
        kw: dict = {"max_tokens": self.max_tokens or get_settings().sarvam_max_tokens}
        effort = (self.reasoning_effort if self.reasoning_effort is not None
                  else get_settings().sarvam_reasoning_effort or "").strip().lower()
        if effort and not (effort == "none" and self.model.startswith("glm")):
            kw["reasoning_effort"] = effort
        return kw

    def chat(self, system, messages, tools=None, temperature=0.0) -> LLMResponse:
        import openai

        try:
            return super().chat(system, messages, tools, temperature)
        except openai.BadRequestError as e:
            # if the server rejects echoed reasoning, retry once without it
            if self.pass_reasoning and "reasoning" in str(e).lower():
                log.warning("sarvam rejected reasoning_content pass-back; disabling for %s", self.model)
                self.pass_reasoning = False
                return super().chat(system, messages, tools, temperature)
            raise


# Gemini 3+ attaches an opaque "thought signature" to function calls that must be sent
# back unchanged on the next turn. For calls that did not come from Gemini (e.g. a
# transcript started on another model) Google documents this placeholder value.
_SKIP_SIGNATURE = b"skip_thought_signature_validator"


class GeminiLLM:
    """Google Gemini via the native google-genai SDK (preserves thought signatures)."""

    def __init__(self, model: str, api_key: str | None):
        from google import genai

        self.model = model
        self.client = genai.Client(api_key=api_key)

    @staticmethod
    def _convert(messages: list[dict]) -> list:
        from google.genai import types

        out: list = []
        native_ids = {tc["id"] for m in messages if m["role"] == "assistant"
                      for tc in (m.get("tool_calls") or []) if tc.get("native_id")}
        for m in messages:
            if m["role"] == "user":
                part, role = types.Part(text=m["content"] or "(empty)"), "user"
            elif m["role"] == "assistant":
                parts = []
                if m.get("content"):
                    parts.append(types.Part(text=m["content"]))
                for tc in m.get("tool_calls") or []:
                    sig = tc.get("thought_signature")
                    parts.append(types.Part(
                        function_call=types.FunctionCall(name=tc["name"], args=tc["arguments"],
                                                         id=tc["id"] if tc.get("native_id") else None),
                        thought_signature=base64.b64decode(sig) if sig else _SKIP_SIGNATURE,
                    ))
                out.append(types.Content(role="model", parts=parts or [types.Part(text="(empty)")]))
                continue
            else:  # tool result
                part = types.Part(function_response=types.FunctionResponse(
                    name=m.get("name") or "tool", response={"result": m["content"]},
                    id=m["tool_call_id"] if m["tool_call_id"] in native_ids else None))
                role = "user"
            if out and out[-1].role == "user" and role == "user":
                out[-1].parts.append(part)  # merge consecutive user/tool-result parts
            else:
                out.append(types.Content(role=role, parts=[part]))
        if out and out[0].role == "model":
            out.insert(0, types.Content(role="user", parts=[types.Part(text="(customer connected)")]))
        return out

    def chat(self, system, messages, tools=None, temperature=0.0) -> LLMResponse:
        from google.genai import errors, types

        cfg = types.GenerateContentConfig(
            system_instruction=system,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        if tools:
            cfg.tools = [types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t["name"], description=t["description"], parameters_json_schema=t["parameters"])
                for t in tools])]
        # Gemini 3 models are tuned for their default temperature; only set it on older ones.
        if temperature is not None and self.model.startswith("gemini-2"):
            cfg.temperature = temperature
        contents = self._convert(messages)

        def classify(e: Exception):
            if not isinstance(e, errors.APIError):
                return None, None
            if getattr(e, "code", None) == 429:
                text = str(e)
                m = re.search(r"retry(?:Delay| in)[\"':\s]*([\d.]+)s", text, re.I)
                daily = "PerDay" in text or "per day" in text.lower()
                return ("daily" if daily else "rate"), (float(m.group(1)) if m else None)
            if getattr(e, "code", None) in (500, 502, 503, 504):
                return "transient", None
            return None, None

        resp, waited = call_with_limits(
            "gemini", self.model, estimate_tokens(system, messages, tools),
            lambda: self.client.models.generate_content(model=self.model, contents=contents, config=cfg), classify,
            lambda r: getattr(getattr(r, "usage_metadata", None), "total_token_count", 0) or 0)
        r = LLMResponse(wait_ms=waited * 1000)
        cand = (resp.candidates or [None])[0]
        for p in (cand.content.parts if cand and cand.content and cand.content.parts else []):
            if p.thought:
                continue
            if p.function_call:
                fc = p.function_call
                r.tool_calls.append({
                    "id": fc.id or f"gem_{uuid.uuid4().hex[:12]}", "native_id": bool(fc.id),
                    "name": fc.name, "arguments": dict(fc.args or {}),
                    "thought_signature": base64.b64encode(p.thought_signature).decode() if p.thought_signature else None,
                })
            elif p.text:
                r.content += p.text
        u = resp.usage_metadata
        if u:
            r.reasoning_tokens = u.thoughts_token_count or 0
            r.input_tokens = (u.prompt_token_count or 0) + (u.tool_use_prompt_token_count or 0)
            r.output_tokens = (u.candidates_token_count or 0) + r.reasoning_tokens
            r.cached_tokens = u.cached_content_token_count or 0
        return r


class MissingAPIKey(RuntimeError):
    pass


def make_llm(spec: str) -> LLM:
    provider, model = parse_spec(spec)
    s = get_settings()
    if provider in PROVIDERS and not provider_configured(provider):
        raise MissingAPIKey(f"{provider.upper()}_API_KEY is not set: add it to .env and restart the backend "
                            f"(needed for model '{spec}').")
    if provider == "anthropic":
        return AnthropicLLM(model)
    if provider == "openai":
        return OpenAICompatLLM(model, s.openai_api_key, s.openai_base_url, provider="openai")
    if provider == "groq":
        return OpenAICompatLLM(model, s.groq_api_key, s.groq_base_url, provider="groq")
    if provider == "gemini":
        return GeminiLLM(model, s.gemini_key)
    if provider == "sarvam":
        return SarvamLLM(model, s.sarvam_api_key, s.sarvam_base_url)
    raise ValueError(f"unknown LLM provider {provider!r}")
