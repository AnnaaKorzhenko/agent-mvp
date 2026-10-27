"""Provider switch, the adapter (teammate's code mocked), and the cloud refusal rule."""
import sys
import types
from types import SimpleNamespace

import pytest

from brain.domain.sensitivity import SensitivityError
from brain.integrations import local_models
from brain.integrations.backends import AnthropicBackend, LocalPipelineBackend, get_backend


class FakeAudit:
    def __init__(self):
        self.calls = []

    async def model_call(self, **kw):
        self.calls.append(kw)


class FakeAnthropic:
    def __init__(self, stop_reason="end_turn"):
        self.requests = []
        self.stop_reason = stop_reason
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.requests.append(kw)
        return SimpleNamespace(stop_reason=self.stop_reason, stop_details=SimpleNamespace(category="cyber"),
                               content=[SimpleNamespace(type="text", text="відповідь")], model=kw["model"],
                               usage=SimpleNamespace(input_tokens=12, output_tokens=3))


@pytest.fixture
def fake_summarizer(monkeypatch):
    """Stands in for the teammate's summarizer module (client + MODEL)."""
    sent = []

    async def create(**kw):
        sent.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=" локально "))],
                               usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2))

    mod = types.ModuleType("summarizer")
    mod.MODEL = "qwen2.5:7b-instruct"
    mod.client = SimpleNamespace(base_url="http://localhost:11434/v1",
                                 chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setitem(sys.modules, "summarizer", mod)
    return sent


def test_switch(settings_factory):
    audit = FakeAudit()
    assert isinstance(get_backend(settings_factory(), audit), LocalPipelineBackend)
    assert isinstance(get_backend(settings_factory(MODEL_BACKEND="anthropic", ANTHROPIC_API_KEY="test"), audit),
                      AnthropicBackend)


async def test_cloud_backend_refuses_confidential_before_any_call(settings_factory):
    audit, client = FakeAudit(), FakeAnthropic()
    backend = AnthropicBackend(audit, settings_factory(), client=client)
    for label in ("confidential", "restricted"):
        with pytest.raises(SensitivityError):
            await backend.complete([{"role": "user", "content": "x"}], sensitivity=label)
    assert client.requests == []
    assert [c["outcome"] for c in audit.calls] == ["refused", "refused"]


async def test_cloud_backend_internal_call_is_audited(settings_factory):
    audit, client = FakeAudit(), FakeAnthropic()
    backend = AnthropicBackend(audit, settings_factory(ANTHROPIC_MODEL="claude-opus-5-5"), client=client)
    r = await backend.complete([{"role": "user", "content": "x"}], system="sys", temperature=0.9,
                               sensitivity="internal")
    assert (r.text, r.backend, r.model, r.input_tokens, r.output_tokens) == \
        ("відповідь", "anthropic", "claude-opus-5-5", 12, 3)
    [req] = client.requests
    assert req["system"] == "sys" and "temperature" not in req and req["fallbacks"] == "default"
    assert audit.calls[-1] == {"backend": "anthropic", "model": "claude-opus-5-5", "max_sensitivity": "internal",
                               "input_tokens": 12, "output_tokens": 3, "user_id": None}


async def test_cloud_backend_refusal_stop_reason_is_an_error(settings_factory):
    audit = FakeAudit()
    backend = AnthropicBackend(audit, settings_factory(), client=FakeAnthropic(stop_reason="refusal"))
    with pytest.raises(RuntimeError, match="declined"):
        await backend.complete([{"role": "user", "content": "x"}])
    assert audit.calls[-1]["outcome"] == "error"


async def test_local_backend_goes_through_adapter(fake_summarizer):
    audit = FakeAudit()
    backend = LocalPipelineBackend(audit, local_max="restricted")
    r = await backend.complete([{"role": "user", "content": "x"}], system="sys", sensitivity="restricted")
    assert (r.text, r.model, r.backend) == ("локально", "qwen2.5:7b-instruct", "local")
    [req] = fake_summarizer
    assert req["model"] == "qwen2.5:7b-instruct" and req["messages"][0] == {"role": "system", "content": "sys"}
    assert audit.calls[-1]["max_sensitivity"] == "restricted"
    assert local_models.local_model_info() == {"model": "qwen2.5:7b-instruct",
                                               "base_url": "http://localhost:11434/v1"}


async def test_local_backend_respects_configured_local_max(fake_summarizer):
    backend = LocalPipelineBackend(FakeAudit(), local_max="confidential")
    with pytest.raises(SensitivityError):
        await backend.complete([{"role": "user", "content": "x"}], sensitivity="restricted")
    assert fake_summarizer == []


async def test_adapter_reports_missing_client(monkeypatch):
    monkeypatch.setitem(sys.modules, "summarizer", types.ModuleType("summarizer"))
    with pytest.raises(local_models.LocalModelUnavailable, match="A1"):
        await local_models.local_chat([{"role": "user", "content": "x"}])
