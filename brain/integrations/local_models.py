"""The ONLY module in brain/ that imports the local-model track's code.

If the teammate's client changes, only this file changes. See docs/REQUESTS_TO_LOCAL_MODEL_TRACK.md #1.

ASSUMPTIONS (written against the code as of 2026-10-26; to confirm with the local-model track):
  A1. summarizer.py exposes `client`: an OpenAI-compatible AsyncOpenAI pointed at the local runtime
      (Ollama by default, LLM_BASE_URL / LLM_API_KEY), and `MODEL` (LLM_MODEL, qwen2.5:7b-instruct).
  A2. The runtime accepts chat.completions with `messages`, `max_tokens`, `temperature`.
  A3. Settings stay in her module's env vars; we read them through her module and never duplicate them.
The import is lazy: importing summarizer also imports her db.py and cleaning.py, which must not run
at server start-up or in tests that do not need a model.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import ModuleType
from typing import Any

PIPELINE_CLIENT_MODULE = "summarizer"   # A1


class LocalModelUnavailable(Exception):
    """The local-model track's client could not be imported or reached."""


@dataclass(frozen=True)
class LocalChatResult:
    text: str
    input_tokens: int
    output_tokens: int
    model: str


def _module() -> ModuleType:
    try:
        return importlib.import_module(PIPELINE_CLIENT_MODULE)
    except Exception as e:  # noqa: BLE001 - any import failure means "not available here"
        raise LocalModelUnavailable(f"Cannot import the local-model client ({PIPELINE_CLIENT_MODULE}): "
                                    f"{type(e).__name__}: {e}") from e


def local_model_info() -> dict[str, str]:
    mod = _module()
    return {"model": str(getattr(mod, "MODEL", "")), "base_url": str(getattr(mod.client, "base_url", ""))}


async def local_chat(messages: list[dict[str, Any]], max_tokens: int = 1024,
                     temperature: float = 0.2) -> LocalChatResult:
    mod = _module()
    client, model = getattr(mod, "client", None), getattr(mod, "MODEL", None)
    if client is None or not model:
        raise LocalModelUnavailable(f"{PIPELINE_CLIENT_MODULE} has no `client` / `MODEL` (assumption A1 broken)")
    try:
        resp = await client.chat.completions.create(model=model, messages=messages,
                                                    max_tokens=max_tokens, temperature=temperature)
    except Exception as e:  # noqa: BLE001
        raise LocalModelUnavailable(f"Local model call failed: {type(e).__name__}: {str(e)[:200]}") from e
    usage = getattr(resp, "usage", None)
    return LocalChatResult(text=(resp.choices[0].message.content or "").strip(),
                           input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                           output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                           model=str(model))
