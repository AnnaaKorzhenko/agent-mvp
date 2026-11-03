"""Model backends for MCP-side tasks that need a model (later: research_topic, outward verification).

v0 tools never call a model. Every call is audited with the highest sensitivity label it sends.
TODO(next): research_topic and the outward-verification gate plug in here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
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

    def __init__(self, audit: AuditLog, settings: Settings, client: Any | None = None, model: str | None = None):
        super().__init__(audit)
        self.model = model or settings.anthropic_model
        if client is None:
            import anthropic
            key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
            client = anthropic.AsyncAnthropic(api_key=key) if key else anthropic.AsyncAnthropic()
        self.client = client

    async def _model_name(self) -> str:
        return self.model

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
        import anthropic
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
            for attempt in (1, 2):
                try:
                    async with self.client.beta.messages.stream(**kwargs) as s:
                        async for event in s:
                            if event.type == "text":
                                yield event.text
                        final = await s.get_final_message()
                    break
                except anthropic.BadRequestError as e:    # a model without programmatic tool calling: direct search
                    search = next((t for t in tools if t.get("type") == self.SEARCH_TOOL), None)
                    if attempt == 2 or search is None or "allowed_callers" not in str(e):
                        raise
                    search["allowed_callers"] = ["direct"]
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


def _input_tokens(usage) -> int:
    """Every input token billed in full: uncached plus written to the cache (cache reads are a tenth of the price
    and are left out)."""
    return (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)


def get_backend(settings: Settings, audit: AuditLog) -> ModelBackend:
    if settings.model_backend == "anthropic":
        return AnthropicBackend(audit, settings)
    return LocalPipelineBackend(audit, settings.max_sensitivity_local)
