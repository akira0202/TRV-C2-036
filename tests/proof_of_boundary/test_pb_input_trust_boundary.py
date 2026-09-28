"""Proof-of-Boundary: caller trust (S-1) + input boundary (envelope gate / structural S-2).

Scope: prove the agent "stops at the entrance". This file does NOT re-test per-node
business logic (that is the unit suite) — it proves, at the OUTER `agent.invoke()` boundary
and the node `__call__()` trust gate, that an insufficient-trust caller or a
malformed/unsupported/non-contract input fails closed BEFORE any business processing runs,
that the inner workflow nodes are never executed on such rejects, and that no raw input
leaks. The output/provenance boundary is covered by a separate suite (not here).

Responsibility split under test (see `src/nodes/pre_process_node.py`): the outer
`CallerInputGateNode` validates only the HIGH-LEVEL envelope (two markets + versioned sheet
+ non-empty requested_metrics); DEEP schema / structural / allowlist validation is the inner
`normalize` node's job. The tests keep these distinct: outer-gate rejects execute NO inner
node; inner-stage rejects reach `input_hydrate` but stop before comparability/brief.

The invoke()/`__call__()` cases need the real framework wheel and are `importorskip`-gated
(run in CI, skipped locally). The external-connection static scan is framework-free.
All fixtures are synthetic — no real market names, traveller data, or credentials.
"""

import ast
import json
import pathlib

import pytest
from src.services.degradation import explain_degradation


def _require_real_framework():
    pytest.importorskip("framework.graph.agent_base_graph")
    pytest.importorskip("framework.schemas.invocation_context")
    pytest.importorskip("shared.secrets")
    pytest.importorskip("framework.secrets.context")


# --------------------------------------------------------------- synthetic fixtures

def _market(name="MKT_A", *, period="2024-01/2024-12", value=100, unit="persons"):
    return {
        "market": name,
        "reporting_period": period,
        "population_coverage": "all_visitors",
        "unit": unit,
        "rows": [{"metric": "overnight_visitors", "value": value}],
        "source": {"source_id": f"src-{name.lower()}", "export_date": "2025-01-10"},
    }


def _sheet():
    return {"version": "sheet-v1", "metrics": [{
        "metric": "overnight_visitors", "semantics": "overnight visitor count",
        "required_period_alignment": "exact", "eligible": True}]}


def _envelope_obj(markets=None):
    return {
        "markets": markets if markets is not None else [_market("MKT_A"), _market("MKT_B")],
        "metric_definition_sheet": _sheet(),
        "requested_metrics": ["overnight_visitors"],
    }


def _no_caller_output(out: dict) -> bool:
    return all(out.get(k) is None for k in ("output", "formatted_output", "result"))


# --------------------------------------------------------------- node execute spies

def _install_execute_spies(monkeypatch):
    """Patch each node class's execute() to count invocations; returns the counter dict.
    Patching the class method affects the compiled agent's node instances (dynamic lookup)."""
    import src.nodes.brief_generate_node as brf
    import src.nodes.comparability_validate_node as cmp
    import src.nodes.input_hydrate_node as hyd
    import src.nodes.input_normalize_provenance_node as norm
    import src.nodes.pre_process_node as pre

    targets = {
        "caller": pre.CallerInputGateNode,
        "hydrate": hyd.InputHydrateNode,
        "normalize": norm.InputNormalizeProvenanceNode,
        "comparability": cmp.ComparabilityValidateNode,
        "brief": brf.BriefGenerateNode,
    }
    counts = {k: 0 for k in targets}
    for key, cls in targets.items():
        orig = cls.execute
        def wrapped(self, state, _k=key, _orig=orig):
            counts[_k] += 1
            return _orig(self, state)
        monkeypatch.setattr(cls, "execute", wrapped)
    return counts


def _build_and_invoke(payload: str, trust: str):
    from framework.schemas.invocation_context import InvocationContext
    from framework.schemas.trust_level import TrustLevel
    from framework.secrets.context import bound_secrets
    from shared.secrets import factory as secrets_factory
    from src.graph.graph import build_agent

    agent = build_agent()
    agent.compile()
    agent.provision_secrets(secrets_factory(namespace="trv-c2-036", agent_name="trv_c2_036"))
    tl = TrustLevel.VERIFIED_EXTERNAL if trust == "VE" else TrustLevel.ANONYMOUS
    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(session_id="pb-input", caller_trust_level=tl)
        return agent.invoke(payload, ctx=ctx)


# ============================ framework-free: no external path ============================

_FORBIDDEN_IO = {
    "requests", "httpx", "aiohttp", "urllib", "urllib3", "socket", "http", "ftplib",
    "boto3", "botocore", "psycopg2", "pymysql", "pymongo", "redis", "elasticsearch",
    "qdrant_client", "pinecone", "cohere", "openai", "anthropic", "google",
}


def test_src_has_no_external_connection_or_retrieval_imports():
    """No HTTP client / retrieval SDK / external-service call path exists in src/ — the
    template only processes the supplied envelope; there is no automatic retrieval."""
    src = pathlib.Path(__file__).parents[2] / "src"
    hits = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in _FORBIDDEN_IO:
                        hits.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in _FORBIDDEN_IO:
                    hits.append(f"{path.name}: from {node.module}")
    assert not hits, f"unexpected external-IO / retrieval imports in src/: {hits}"


# ============================ S-1 trust gate at node __call__() ===========================

def test_node_call_s1_gate_refuses_anonymous_before_execute(monkeypatch):
    """The S-1 trust gate lives in BaseNode.__call__(), not execute(). An ANONYMOUS caller
    is refused BEFORE CallerInputGateNode.execute() runs (VERIFIED_EXTERNAL required)."""
    _require_real_framework()
    from framework.schemas.trust_level import TrustLevel
    import src.nodes.pre_process_node as pre

    calls = {"n": 0}
    orig = pre.CallerInputGateNode.execute
    def spy(self, state, _o=orig):
        calls["n"] += 1
        return _o(self, state)
    monkeypatch.setattr(pre.CallerInputGateNode, "execute", spy)

    node = pre.CallerInputGateNode()
    out = node({
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
        "user_input": json.dumps(_envelope_obj()),
        "correlation_id": "pb-input", "node_history": [], "error_log": [],
    })
    assert calls["n"] == 0, "S-1 must refuse before execute()"
    assert out.get("status") == "error"


# ======================= S-1 at the compiled agent.invoke() boundary ======================

def test_anonymous_caller_executes_no_node(monkeypatch):
    _require_real_framework()
    counts = _install_execute_spies(monkeypatch)
    out = _build_and_invoke(json.dumps(_envelope_obj()), "ANON")
    assert out.get("status") == "error", out
    assert _no_caller_output(out), out
    # S-1 refuses at the outer boundary before pre_process.execute(); nothing downstream runs
    assert counts["caller"] == 0
    assert all(counts[k] == 0 for k in ("hydrate", "normalize", "comparability", "brief"))


# ===================== outer envelope gate: inner never executes =========================

def _no_version_envelope():
    env = _envelope_obj()
    env["metric_definition_sheet"].pop("version")
    return env


def _empty_requested_envelope():
    env = _envelope_obj()
    env["requested_metrics"] = []
    return env


_OUTER_REJECT_CASES = [
    ("empty", ""),
    ("blank", "   "),
    ("malformed_json", "{not valid json"),
    ("free_text_payload", json.dumps("just some free text, not an envelope")),
    ("json_list", json.dumps([1, 2, 3])),
    ("json_number", json.dumps(42)),
    ("one_market", json.dumps(_envelope_obj(markets=[_market("MKT_A")]))),
    ("three_markets", json.dumps(_envelope_obj(
        markets=[_market("MKT_A"), _market("MKT_B"), _market("MKT_C")]))),
    ("no_sheet_version", json.dumps(_no_version_envelope())),
    ("empty_requested", json.dumps(_empty_requested_envelope())),
]


@pytest.mark.parametrize("label,payload", _OUTER_REJECT_CASES, ids=[c[0] for c in _OUTER_REJECT_CASES])
def test_outer_gate_answer_never_executes_inner_nodes(label, payload, monkeypatch):
    """A malformed / unsupported / non-conforming envelope is stopped at the outer gate and
    answered with a notice; the inner workflow (hydrate/normalize/comparability/brief) never
    runs, and no brief or validation report is produced."""
    _require_real_framework()
    counts = _install_execute_spies(monkeypatch)
    out = _build_and_invoke(payload, "VE")
    assert out.get("status") == "success", out
    assert out.get("diagnostic_code"), out
    assert out.get("output") == explain_degradation(out["diagnostic_code"]), out
    assert out.get("result") is None and out.get("comparison_outcome") is None, out
    assert counts["caller"] >= 1, "outer gate should run and answer"
    for inner in ("hydrate", "normalize", "comparability", "brief"):
        assert counts[inner] == 0, f"{inner} must not run on an answered envelope ({label})"


# =============== inner stage rejects: reach hydrate, stop before comparison ===============

def test_non_contract_field_stops_before_comparison(monkeypatch):
    """A field outside the V1 top-level allowlist (e.g. a traveller identifier) passes the
    high-level envelope gate and is stopped by the inner normalize allowlist — business
    comparison/brief is never reached, no brief is produced, and the raw value never leaks."""
    _require_real_framework()
    counts = _install_execute_spies(monkeypatch)
    env = _envelope_obj()
    env["traveller_email"] = "TRAVELLERSENTINEL@example.test"  # not in the V1 input contract
    out = _build_and_invoke(json.dumps(env), "VE")
    assert out.get("status") == "success", out
    assert out.get("diagnostic_code") == "ERR_UNKNOWN_TOP_KEY", out
    assert out.get("result") is None and out.get("comparison_outcome") is None, out
    assert counts["hydrate"] >= 1, "input reached the inner graph head"
    assert counts["comparability"] == 0 and counts["brief"] == 0, "stopped before business processing"
    assert "TRAVELLERSENTINEL" not in json.dumps(out), "raw input must not leak into the response"


def test_control_character_fails_closed_before_brief(monkeypatch):
    """A NUL/control character in supplied free-text passes the high-level envelope gate but
    is rejected by the inner structural S-2 screen; comparison/brief never runs."""
    _require_real_framework()
    counts = _install_execute_spies(monkeypatch)
    env = _envelope_obj()
    env["markets"][0]["source"]["title"] = "bad" + chr(0) + "title"  # NUL control char in a leaf
    out = _build_and_invoke(json.dumps(env), "VE")
    assert out.get("status") == "error", out
    assert _no_caller_output(out), out
    assert counts["hydrate"] >= 1, "input reached the inner graph head"
    assert counts["comparability"] == 0 and counts["brief"] == 0, "stopped before business processing"


# ------------------------------------------------------------------- no-leak at boundary

def test_a_stopped_run_never_leaks_input_sentinels(monkeypatch):
    """Whether the run is answered with a notice or fails closed, no raw input / PII /
    credential sentinel appears anywhere in the caller-facing response. The notice is derived
    from the allow-listed code alone, so it cannot carry what the request sent."""
    _require_real_framework()
    _install_execute_spies(monkeypatch)
    env = _envelope_obj()
    env["traveller_passport"] = "PIISENTINEL-P1234567"          # non-contract top-level field
    env["markets"][0]["source"]["title"] = "CREDSENTINEL-AKIA0"
    out = _build_and_invoke(json.dumps(env), "VE")
    assert out.get("status") in ("success", "error"), out
    assert out.get("result") is None, "no brief on a stopped run"
    blob = json.dumps(out)
    assert "PIISENTINEL" not in blob and "CREDSENTINEL" not in blob
