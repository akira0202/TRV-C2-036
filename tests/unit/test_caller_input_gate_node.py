"""Unit tests for CallerInputGateNode (outer S-1 boundary + minimal envelope gate)."""

import json
import pathlib

import pytest

import src.nodes.pre_process_node as node_mod
from src.nodes.pre_process_node import CallerInputGateNode
from src.services import failure_message
from src.services.degradation import explain_degradation


def _envelope():
    return {
        "markets": [{"market": "Tokyo"}, {"market": "Osaka"}],
        "metric_definition_sheet": {"version": "sheet-v1.2", "metrics": []},
        "requested_metrics": ["overnight_visitors"],
    }


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


def test_trust_level_is_verified_external():
    from framework.schemas.trust_level import TrustLevel
    assert CallerInputGateNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL


def test_valid_envelope_parsed_into_validated_input(spy):
    env = _envelope()
    out = CallerInputGateNode().execute({"user_input": json.dumps(env)})
    assert out["status"] == "success"
    assert out["validated_input"] == env
    assert set(out) == {"validated_input", "status"}
    assert [c[0] for c in spy.calls] == ["caller_input_accepted"]


@pytest.mark.parametrize(
    "raw,code",
    [
        ("", "ERR_ENVELOPE_EMPTY"),
        ("   ", "ERR_ENVELOPE_EMPTY"),
        ("{not json", "ERR_ENVELOPE_PARSE"),
        ("[1,2]", "ERR_ENVELOPE_NOT_OBJECT"),
        ("42", "ERR_ENVELOPE_NOT_OBJECT"),
    ],
)
def test_unparseable_or_non_object_is_answered(raw, code, spy):
    """The envelope is the caller's to fix, so the run completes carrying the code."""
    out = CallerInputGateNode().execute({"user_input": raw})
    assert out["status"] == "success"
    assert set(out) == {"status", "diagnostic_code"}  # partial-free, and nothing failed
    assert out["diagnostic_code"] == code
    assert [c[0] for c in spy.calls] == ["caller_input_degraded"]


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda e: e.__setitem__("markets", [{"market": "Tokyo"}]), "ERR_ENVELOPE_MARKETS"),
        (lambda e: e.__setitem__("markets", "nope"), "ERR_ENVELOPE_MARKETS"),
        (lambda e: e["metric_definition_sheet"].__setitem__("version", ""), "ERR_ENVELOPE_SHEET_VERSION"),
        (lambda e: e.pop("metric_definition_sheet"), "ERR_ENVELOPE_SHEET_VERSION"),
        (lambda e: e.__setitem__("requested_metrics", []), "ERR_ENVELOPE_REQUESTED"),
        (lambda e: e.pop("requested_metrics"), "ERR_ENVELOPE_REQUESTED"),
    ],
)
def test_envelope_shape_violations_are_answered(mutate, code):
    env = _envelope()
    mutate(env)
    out = CallerInputGateNode().execute({"user_input": json.dumps(env)})
    assert out["status"] == "success"
    assert out["diagnostic_code"] == code
    assert "validated_input" not in out and "error_log" not in out


def test_the_notice_does_not_echo_raw_input(spy):
    env = _envelope()
    env["markets"] = [{"market": "SecretMarketName"}]  # 1 market -> answered, not compared
    out = CallerInputGateNode().execute({"user_input": json.dumps(env)})
    assert "SecretMarketName" not in json.dumps(out)
    assert "SecretMarketName" not in explain_degradation(out["diagnostic_code"])
    assert spy.calls[0][1] == {"reason_code": "ERR_ENVELOPE_MARKETS"}


def test_every_code_this_gate_sends_has_a_sentence_of_its_own():
    """The notice IS the output here, so a code with no sentence would answer the caller with
    the generic fallback — worse than an error, because it reads as a completed request."""
    import ast

    src = pathlib.Path("src/nodes/pre_process_node.py").read_text()
    codes = {
        n.args[0].value
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call)
        and getattr(n.func, "attr", "") == "_reject"
        and n.args
        and isinstance(n.args[0], ast.Constant)
    }
    assert codes, "no rejection codes found — the scan would pass vacuously"
    for code in sorted(codes):
        assert not explain_degradation(code).startswith(failure_message._FALLBACK), code
