"""Model backends for MCP-side tasks that need a model (later: research_topic, outward verification).

v0 tools never call a model. Every call is audited with the highest sensitivity label it sends.
TODO(next): research_topic and the outward-verification gate plug in here.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Protocol

import anyio

from brain.audit.log import AuditLog
from brain.config import Settings
from brain.domain.sensitivity import SensitivityError, max_for, require_allowed
from brain.integrations import local_models

logger = logging.getLogger("brain.backends")


@dataclass(frozen=True)
class ModelResult:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    backend: str
    stop_reason: str | None = None        # e.g. "max_tokens" when the answer was cut off
    web_searches: int = 0                 # search() only: searches run (billed per search)
    sources: tuple = ()                   # search() only: every result the searches returned: {url, title, page_age}


class ModelBackend(Protocol):
    name: str
    trust: str                  # "local": may receive anything the user may see; "cloud": up to internal

    async def complete(self, messages: list[dict[str, Any]], system: str | None = None, max_tokens: int = 1024,
                       temperature: float = 0.2, sensitivity: str = "internal",
                       user_id: int | None = None) -> ModelResult: ...


class _AuditedBackend:
    name = "base"
    trust = "cloud"

    def __init__(self, audit: AuditLog, local_max: str = "restricted"):
        self.audit = audit
        self.max_label = max_for(self.trust, local_max)

    async def _model_name(self) -> str:
        return "?"

    async def complete(self, messages: list[dict[str, Any]], system: str | None = None, max_tokens: int = 1024,
                       temperature: float = 0.2, sensitivity: str = "internal",
                       user_id: int | None = None) -> ModelResult:
        try:   # refuse before anything leaves the process
            require_allowed(sensitivity, self.max_label, f"payload for backend '{self.name}'")
        except SensitivityError as e:
            await self.audit.model_call(backend=self.name, model=await self._model_name(),
                                        max_sensitivity=sensitivity, outcome="refused", error=str(e),
                                        user_id=user_id)
            raise
        try:
            result = await self._call(messages, system, max_tokens, temperature)
        except Exception as e:
            await self.audit.model_call(backend=self.name, model=await self._model_name(),
                                        max_sensitivity=sensitivity, outcome="error",
                                        error=f"{type(e).__name__}: {e}", user_id=user_id)
            raise
        await self.audit.model_call(backend=self.name, model=result.model, max_sensitivity=sensitivity,
                                    input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                                    user_id=user_id)
        return result

    async def _call(self, messages, system, max_tokens, temperature) -> ModelResult:  # pragma: no cover
        raise NotImplementedError


    async def search(self, messages: list[dict[str, Any]], system: str | None = None, max_tokens: int = 4096,
                     max_uses: int = 5, sensitivity: str = "internal",
                     user_id: int | str | None = None) -> ModelResult:
        """Like complete(), with the model searching the web (at most max_uses searches). The result's `sources`
        are the search results themselves, so a caller can check every URL the model names against them."""
        try:
            require_allowed(sensitivity, self.max_label, f"payload for backend '{self.name}'")
        except SensitivityError as e:
            await self.audit.model_call(backend=self.name, model=await self._model_name(),
                                        max_sensitivity=sensitivity, outcome="refused", error=str(e),
                                        user_id=user_id)
            raise
        try:
            result = await self._search(messages, system, max_tokens, max_uses)
        except Exception as e:
            await self.audit.model_call(backend=self.name, model=await self._model_name(),
                                        max_sensitivity=sensitivity, outcome="error",
                                        error=f"{type(e).__name__}: {e}", user_id=user_id)
            raise
        await self.audit.model_call(backend=self.name, model=result.model, max_sensitivity=sensitivity,
                                    input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                                    user_id=user_id, web_searches=result.web_searches)
        return result

    async def _search(self, messages, system, max_tokens, max_uses) -> ModelResult:
        raise NotImplementedError(f"backend '{self.name}' can't search the web")


class LocalPipelineBackend(_AuditedBackend):
    """The teammate's local model, through the adapter. The default and the system's future."""
    name = "local"
    trust = "local"

    async def _model_name(self) -> str:
        try:
            return local_models.local_model_info()["model"]
        except local_models.LocalModelUnavailable:
            return "?"

    async def _call(self, messages, system, max_tokens, temperature) -> ModelResult:
        full = ([{"role": "system", "content": system}] if system else []) + list(messages)
        r = await local_models.local_chat(full, max_tokens=max_tokens, temperature=temperature)
        return ModelResult(r.text, r.input_tokens, r.output_tokens, r.model, self.name)


class AnthropicBackend(_AuditedBackend):
    """Temporary cloud fallback for MCP-side tasks only. Refuses anything above `internal`.

    `temperature` is accepted for interface parity but not sent: current Claude models reject sampling
    parameters. Server-side refusal fallbacks are enabled ("default" routing).
    """
    name = "anthropic"
    trust = "cloud"
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, audit: AuditLog, settings: Settings | None, client: Any | None = None,
                 model: str | None = None, api_key: str | None = None):
        super().__init__(audit)
        self.model = model or settings.anthropic_model
        if client is None:
            if api_key is None and settings is not None and settings.anthropic_api_key:
                api_key = settings.anthropic_api_key.get_secret_value()
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()
        self.client = client

    async def _model_name(self) -> str:
        return self.model

    def with_model(self, model: str) -> "AnthropicBackend":
        """The same client with another model (the chat's web research)."""
        return AnthropicBackend(self.audit, None, client=self.client, model=model)

    async def _call(self, messages, system, max_tokens, temperature) -> ModelResult:
        kwargs: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens, "messages": list(messages),
                                  "betas": [self.FALLBACK_BETA], "fallbacks": "default"}
        if system:
            kwargs["system"] = system
        resp = await self.client.beta.messages.create(**kwargs)
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"Model declined the request (category: "
                               f"{getattr(resp.stop_details, 'category', None)})")
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        return ModelResult(text, resp.usage.input_tokens, resp.usage.output_tokens, resp.model, self.name,
                           resp.stop_reason)

    SEARCH_TOOL = "web_search_20260318"       # dynamic filtering: results are filtered before they reach the model
    MAX_CONTINUATIONS = 4

    async def _search(self, messages, system, max_tokens, max_uses) -> ModelResult:
        """A paused turn (pause_turn) is resumed by sending it back unchanged, at most MAX_CONTINUATIONS times. A
        model without programmatic tool calling gets the direct search (allowed_callers ["direct"])."""
        import anthropic
        tool: dict[str, Any] = {"type": self.SEARCH_TOOL, "name": "web_search", "max_uses": max_uses}
        convo = list(messages)
        tokens_in = tokens_out = searches = 0
        sources: dict[str, dict] = {}
        text_parts: list[str] = []
        resp = None
        for _ in range(self.MAX_CONTINUATIONS + 1):
            kwargs: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens, "messages": convo,
                                      "tools": [tool], "betas": [self.FALLBACK_BETA], "fallbacks": "default"}
            if system:
                kwargs["system"] = system
            try:
                resp = await self.client.beta.messages.create(**kwargs)
            except anthropic.BadRequestError as e:
                if "allowed_callers" not in str(e) or tool.get("allowed_callers"):
                    raise
                tool["allowed_callers"] = ["direct"]
                continue
            if resp.stop_reason == "refusal":
                raise RuntimeError(f"Model declined the request (category: "
                                   f"{getattr(resp.stop_details, 'category', None)})")
            tokens_in += resp.usage.input_tokens
            tokens_out += resp.usage.output_tokens
            searches += getattr(getattr(resp.usage, "server_tool_use", None), "web_search_requests", 0) or 0
            for b in resp.content:
                kind = getattr(b, "type", None)
                if kind == "text":
                    text_parts.append(b.text)
                elif kind == "web_search_tool_result" and isinstance(getattr(b, "content", None), list):
                    for r in b.content:
                        if getattr(r, "url", None):
                            sources.setdefault(r.url, {"url": r.url, "title": getattr(r, "title", "") or "",
                                                       "page_age": getattr(r, "page_age", None)})
            if resp.stop_reason != "pause_turn":
                break
            convo = list(messages) + [{"role": "assistant", "content": resp.content}]
        return ModelResult("".join(text_parts).strip(), tokens_in, tokens_out, resp.model if resp else self.model,
                           self.name, resp.stop_reason if resp else None, searches, tuple(sources.values()))


    async def stream(self, messages: list[dict[str, Any]], system: Any, tools: list[dict], max_tokens: int = 16000,
                     sensitivity: str = "internal", user_id: int | str | None = None):
        """One round of a chat with tools (brain/web/chat.py), streamed: yields each text delta (str), then the
        final message. Gated and audited like complete(): refused before anything leaves the process, and one
        model_call per round, also when the caller stops reading (outcome "cancelled")."""
        try:
            require_allowed(sensitivity, self.max_label, f"payload for backend '{self.name}'")
        except SensitivityError as e:
            await self.audit.model_call(backend=self.name, model=self.model, max_sensitivity=sensitivity,
                                        outcome="refused", error=str(e), user_id=user_id)
            raise
        kwargs: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens, "messages": messages,
                                  "system": system, "tools": tools, "thinking": {"type": "adaptive"},
                                  "cache_control": {"type": "ephemeral"},     # the prefix so far: system, tools, turns
                                  "betas": [self.FALLBACK_BETA], "fallbacks": "default"}
        final, outcome, error = None, "cancelled", "the caller stopped reading"
        try:
            async with self.client.beta.messages.stream(**kwargs) as s:
                async for event in s:
                    if event.type == "text":
                        yield event.text
                final = await s.get_final_message()
            outcome, error = "ok", None
        except Exception as e:
            outcome, error = "error", f"{type(e).__name__}: {e}"
            raise
        finally:
            usage = getattr(final, "usage", None)
            with anyio.CancelScope(shield=True):          # the audit entry is written even on a cancelled stream
                await self.audit.model_call(
                    backend=self.name, model=getattr(final, "model", None) or self.model,
                    max_sensitivity=sensitivity, input_tokens=_input_tokens(usage),
                    output_tokens=getattr(usage, "output_tokens", 0) or 0, outcome=outcome, error=error,
                    user_id=user_id,
                    web_searches=getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0)
        yield final


# ---- OpenAI and OpenAI-compatible APIs --------------------------------------------------------------------------

def _get(block: Any, key: str, default: Any = None) -> Any:
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(_get(b, "text") or "" for b in content or [] if _get(b, "type") == "text").strip()


def to_openai_messages(messages: list[dict[str, Any]], system: Any = None) -> list[dict[str, Any]]:
    """The chat's turns, written the Anthropic way (system blocks, tool_use blocks in the assistant's turn,
    tool_result blocks in the user's), as Chat Completions messages."""
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": _text_of(system)})
    for m in messages:
        role, content = m["role"], m.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            calls = [{"id": _get(b, "id"), "type": "function", "function": {
                         "name": _get(b, "name"), "arguments": json.dumps(_get(b, "input") or {}, ensure_ascii=False)
                         if not isinstance(_get(b, "input"), str) else _get(b, "input")}}
                     for b in content or [] if _get(b, "type") == "tool_use"]
            msg: dict[str, Any] = {"role": "assistant", "content": _text_of(content) or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        text = _text_of(content)
        for b in content or []:
            if _get(b, "type") == "tool_result":
                body = _get(b, "content")
                body = body if isinstance(body, str) else _text_of(body)
                out.append({"role": "tool", "tool_call_id": _get(b, "tool_use_id"),
                            "content": ("Error: " if _get(b, "is_error") else "") + (body or "")})
        if text:
            out.append({"role": "user", "content": text})
    return out


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t.get("input_schema") or {"type": "object"}}}
            for t in tools if "name" in t and "input_schema" in t]


STOP_REASONS = {"tool_calls": "tool_use", "function_call": "tool_use", "length": "max_tokens",
                "content_filter": "refusal", "stop": "end_turn"}


class OpenAIBackend(_AuditedBackend):
    """OpenAI, or any OpenAI-compatible API at `base_url` (OpenRouter, Groq, Mistral, DeepSeek, Together, Ollama,
    …): Chat Completions for complete() and the web chat's stream(). stream() yields what AnthropicBackend.stream()
    yields (text deltas, then a message with content blocks text / tool_use, stop_reason and usage the Anthropic
    way), so the chat's loop doesn't depend on the provider. search() uses the Responses API's web_search tool:
    OpenAI itself only; elsewhere it fails and the chat answers from the news base."""
    name = "openai"
    trust = "cloud"
    SEARCH_TOOLS = ("web_search", "web_search_preview")

    def __init__(self, audit: AuditLog, model: str, api_key: str | None = None, base_url: str | None = None,
                 client: Any | None = None):
        super().__init__(audit)
        self.model, self.base_url = model, base_url
        if client is None:
            import openai
            client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url or None)
        self.client = client

    async def _model_name(self) -> str:
        return self.model

    def with_model(self, model: str) -> "OpenAIBackend":
        return OpenAIBackend(self.audit, model, base_url=self.base_url, client=self.client)

    def _limit(self, max_tokens: int) -> dict[str, int]:
        # OpenAI's reasoning models take only max_completion_tokens; other APIs mostly know max_tokens
        return {"max_tokens": max_tokens} if self.base_url else {"max_completion_tokens": max_tokens}

    async def _call(self, messages, system, max_tokens, temperature) -> ModelResult:
        resp = await self.client.chat.completions.create(
            model=self.model, messages=to_openai_messages(list(messages), system), **self._limit(max_tokens))
        choice = resp.choices[0]
        if choice.finish_reason == "content_filter":
            raise RuntimeError("Model declined the request (content_filter)")
        usage = resp.usage
        return ModelResult((choice.message.content or "").strip(), getattr(usage, "prompt_tokens", 0) or 0,
                           getattr(usage, "completion_tokens", 0) or 0, resp.model or self.model, self.name,
                           STOP_REASONS.get(choice.finish_reason, choice.finish_reason))

    async def _search(self, messages, system, max_tokens, max_uses) -> ModelResult:
        import openai
        convo = [{"role": m["role"], "content": _text_of(m.get("content"))} for m in messages]
        resp = None
        for tool in self.SEARCH_TOOLS:          # the older name for accounts or models without the current one
            try:
                resp = await self.client.responses.create(model=self.model, instructions=system, input=convo,
                                                          tools=[{"type": tool}])
                break
            except openai.BadRequestError as e:
                if tool == self.SEARCH_TOOLS[-1] or "web_search" not in str(e):
                    raise
        sources: dict[str, dict] = {}
        searches = 0
        for item in resp.output or []:
            kind = _get(item, "type")
            if kind == "web_search_call":
                searches += 1
            elif kind == "message":
                for part in _get(item, "content") or []:
                    for a in _get(part, "annotations") or []:
                        if _get(a, "type") == "url_citation" and _get(a, "url"):
                            sources.setdefault(_get(a, "url"), {"url": _get(a, "url"), "title": _get(a, "title") or "",
                                                                "page_age": None})
        usage = resp.usage
        return ModelResult((resp.output_text or "").strip(), getattr(usage, "input_tokens", 0) or 0,
                           getattr(usage, "output_tokens", 0) or 0, resp.model or self.model, self.name,
                           resp.status, searches, tuple(sources.values()))

    async def stream(self, messages: list[dict[str, Any]], system: Any, tools: list[dict], max_tokens: int = 16000,
                     sensitivity: str = "internal", user_id: int | str | None = None):
        """See AnthropicBackend.stream(): the same events, gated and audited the same way."""
        try:
            require_allowed(sensitivity, self.max_label, f"payload for backend '{self.name}'")
        except SensitivityError as e:
            await self.audit.model_call(backend=self.name, model=self.model, max_sensitivity=sensitivity,
                                        outcome="refused", error=str(e), user_id=user_id)
            raise
        kwargs: dict[str, Any] = {"model": self.model, "messages": to_openai_messages(messages, system),
                                  "stream": True, "stream_options": {"include_usage": True}, **self._limit(max_tokens)}
        if tools:
            kwargs["tools"] = to_openai_tools(tools)
        text: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish, model, usage = None, self.model, None
        final, outcome, error = None, "cancelled", "the caller stopped reading"
        try:
            async for chunk in await self.client.chat.completions.create(**kwargs):
                model = getattr(chunk, "model", None) or model
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                for choice in getattr(chunk, "choices", None) or []:
                    delta = choice.delta
                    if getattr(delta, "content", None):
                        text.append(delta.content)
                        yield delta.content
                    for tc in getattr(delta, "tool_calls", None) or []:
                        call = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                        call["id"] = tc.id or call["id"]
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            call["name"] = fn.name or call["name"]
                            call["arguments"] += fn.arguments or ""
                    finish = choice.finish_reason or finish
            final = _as_message(text, calls, finish, model, usage)
            outcome, error = "ok", None
        except Exception as e:
            outcome, error = "error", f"{type(e).__name__}: {e}"
            raise
        finally:
            u = getattr(final, "usage", None)
            with anyio.CancelScope(shield=True):
                await self.audit.model_call(backend=self.name, model=model, max_sensitivity=sensitivity,
                                            input_tokens=_input_tokens(u), output_tokens=getattr(u, "output_tokens", 0) or 0,
                                            outcome=outcome, error=error, user_id=user_id)
        yield final


def _as_message(text: list[str], calls: dict[int, dict[str, str]], finish: str | None, model: str, usage: Any):
    """A streamed Chat Completion as the Anthropic-shaped message the chat reads."""
    content: list[Any] = [SimpleNamespace(type="text", text="".join(text))] if text else []
    for i in sorted(calls):
        c = calls[i]
        try:
            args = json.loads(c["arguments"] or "{}")
        except ValueError:
            args = c["arguments"]               # the chat answers the model that its input was invalid JSON
        content.append(SimpleNamespace(type="tool_use", id=c["id"] or f"call_{i}", name=c["name"], input=args))
    stop = "tool_use" if calls and finish in (None, "stop", "tool_calls", "function_call") else \
        STOP_REASONS.get(finish, finish or "end_turn")
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    return SimpleNamespace(content=content, stop_reason=stop, model=model, usage=SimpleNamespace(
        input_tokens=prompt - cached, output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        cache_read_input_tokens=cached, cache_creation_input_tokens=0, server_tool_use=None))


def chat_backend(settings: Settings, audit: AuditLog) -> "AnthropicBackend | OpenAIBackend | None":
    """The web chat's model (WEB_CHAT_PROVIDER, Settings.chat_*), or None without a key: the chat is then off."""
    key = settings.chat_api_key
    if not key:
        return None
    if settings.chat_provider == "openai":
        return OpenAIBackend(audit, settings.chat_model(), api_key=key, base_url=settings.web_chat_base_url)
    return AnthropicBackend(audit, settings, model=settings.chat_model(), api_key=key)


def _input_tokens(usage) -> int:
    """Every input token billed in full: uncached plus written to the cache (cache reads are a tenth of the price
    and are left out)."""
    return (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)


def get_backend(settings: Settings, audit: AuditLog) -> ModelBackend:
    if settings.model_backend == "anthropic":
        return AnthropicBackend(audit, settings)
    return LocalPipelineBackend(audit, settings.max_sensitivity_local)
