"""Unit tests for InputHydrateNode (inner head: JSON string -> validated_input)."""

import json

import pytest

import src.nodes.input_hydrate_node as node_mod
from src.nodes.input_hydrate_node import InputHydrateNode


class _Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, event, payload, state):
        self.calls.append((event, payload))


@pytest.fixture
def spy(monkeypatch):
    s = _Spy()
    monkeypatch.setattr(node_mod, "emit_trace_event", s)
    return s


def test_trust_level_is_anonymous():
    from framework.schemas.trust_level import TrustLevel
    assert InputHydrateNode.required_trust_level == TrustLevel.ANONYMOUS


def test_valid_json_object_hydrated(spy):
    env = {"markets": [1, 2], "requested_metrics": ["m"]}
    out = InputHydrateNode().execute({"user_input": json.dumps(env)})
    assert out["status"] == "success"
    assert out["validated_input"] == env
    assert set(out) == {"validated_input", "status"}
    assert [c[0] for c in spy.calls] == ["input_hydrated"]


@pytest.mark.parametrize(
    "raw,code",
    [
        ("", "ERR_HYDRATE_EMPTY"),
        (123, "ERR_HYDRATE_EMPTY"),
        ("  ", "ERR_HYDRATE_EMPTY"),
        ("{bad", "ERR_HYDRATE_PARSE"),
        ("[1,2]", "ERR_HYDRATE_NOT_OBJECT"),
        ("\"str\"", "ERR_HYDRATE_NOT_OBJECT"),
    ],
)
def test_bad_payload_fail_closed(raw, code, spy):
    out = InputHydrateNode().execute({"user_input": raw})
    assert out["status"] == "error"
    assert set(out) == {"status", "error_log"}
    assert code in out["error_log"][0]
    assert [c[0] for c in spy.calls] == ["input_hydrate_rejected"]
