# TRV-C2-036 — Unit Tests: AzureOpenAINarrativeLLM runtime adapter (src/services/llm.py)

import pytest

import src.services.llm as llm_mod
from src.services.llm import AzureOpenAINarrativeLLM

_VALUES = {
    "AZURE_OPENAI_API_KEY": "mock-azure-key-for-testing",
    "AZURE_OPENAI_ENDPOINT": "https://example-resource.services.ai.azure.com",
    "AZURE_OPENAI_DEPLOYMENT": "example-deployment",
}


class _FakeSecrets:
    def __init__(self, values=None, raises=False):
        self._values = values or _VALUES
        self._raises = raises
        self.required_names = []

    def require(self, name):
        self.required_names.append(name)
        if self._raises:
            raise KeyError(f"{name} not provisioned")
        return self._values[name]


class _CapturingClient:
    def __init__(self, config):
        self.config = config
        self.messages = None

    def complete(self, messages):
        self.messages = messages
        return {"content": "generated text"}


def test_connection_values_resolved_from_bound_provider_and_passed_to_client(monkeypatch):
    secrets = _FakeSecrets()
    captured = {}

    def _build(config):
        captured["config"] = config
        return _CapturingClient(config)

    monkeypatch.setattr(llm_mod, "current_secrets", lambda: secrets)
    monkeypatch.setattr(llm_mod, "_build_azure_client", _build)

    out = AzureOpenAINarrativeLLM().complete([{"role": "user", "content": "x"}])

    assert out == {"content": "generated text"}
    # every value came from the invocation-bound provider, never env / State
    assert secrets.required_names == [
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
    ]
    assert captured["config"]["api_key"] == _VALUES["AZURE_OPENAI_API_KEY"]
    assert captured["config"]["azure_endpoint"] == _VALUES["AZURE_OPENAI_ENDPOINT"]
    assert captured["config"]["azure_deployment"] == _VALUES["AZURE_OPENAI_DEPLOYMENT"]


def test_client_receives_the_message_list(monkeypatch):
    client = _CapturingClient({})
    monkeypatch.setattr(llm_mod, "current_secrets", lambda: _FakeSecrets())
    monkeypatch.setattr(llm_mod, "_build_azure_client", lambda config: client)
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "d"}]
    AzureOpenAINarrativeLLM().complete(messages)
    # the shared client received the messages LIST (not a bare string)
    assert client.messages == messages


def test_missing_connection_value_raises(monkeypatch):
    # unresolvable secret -> raises here; the calling node applies its documented behaviour
    monkeypatch.setattr(llm_mod, "current_secrets", lambda: _FakeSecrets(raises=True))
    with pytest.raises(KeyError):
        AzureOpenAINarrativeLLM().complete([{"role": "user", "content": "x"}])


def test_provider_failure_propagates(monkeypatch):
    class _Failing:
        def complete(self, messages):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(llm_mod, "current_secrets", lambda: _FakeSecrets())
    monkeypatch.setattr(llm_mod, "_build_azure_client", lambda config: _Failing())
    with pytest.raises(RuntimeError):
        AzureOpenAINarrativeLLM().complete([{"role": "user", "content": "x"}])


def test_non_dict_response_is_normalised(monkeypatch):
    class _StrClient:
        def complete(self, messages):
            return "plain text"

    monkeypatch.setattr(llm_mod, "current_secrets", lambda: _FakeSecrets())
    monkeypatch.setattr(llm_mod, "_build_azure_client", lambda config: _StrClient())
    assert AzureOpenAINarrativeLLM().complete([{"role": "user", "content": "x"}]) == {"content": "plain text"}
