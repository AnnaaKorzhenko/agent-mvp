"""Model backends for MCP-side tasks that need a model (later: research_topic, outward verification).

v0 tools never call a model. Every call is audited with the highest sensitivity label it sends.
TODO(next): research_topic and the outward-verification gate plug in here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

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

    def __init__(self, audit: AuditLog, settings: Settings, client: Any | None = None):
        super().__init__(audit)
        self.model = settings.anthropic_model
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


def get_backend(settings: Settings, audit: AuditLog) -> ModelBackend:
    if settings.model_backend == "anthropic":
        return AnthropicBackend(audit, settings)
    return LocalPipelineBackend(audit, settings.max_sensitivity_local)
