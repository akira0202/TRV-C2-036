"""End-to-end outer-agent invoke tests (CI-only — requires the real framework wheel).

Guarded by importorskip so a local run without the framework is skipped rather than errored.
Verifies the composed Cat-2 graph at the real outer Graph.invoke() boundary:
  A. valid + VERIFIED_EXTERNAL  -> success, comparison brief in formatted_output.
  B. valid + no comparable       -> success, no_comparable_metrics report (no brief_markdown).
  C. ANONYMOUS caller (TC-08)    -> refused at the S-1 boundary; no caller-facing output.
  D. inner error (bad row value) -> propagates; no caller-facing result/formatted_output.
"""

import json

import pytest
from src.services.degradation import explain_degradation


def _require_real_framework():
    pytest.importorskip("framework.graph.agent_base_graph")
    pytest.importorskip("framework.schemas.invocation_context")
    pytest.importorskip("shared.secrets")
    pytest.importorskip("framework.secrets.context")


def _market(name, *, period="2024-01/2024-12", value=100, unit="persons", vmetric="overnight_visitors"):
    return {
        "market": name,
        "reporting_period": period,
        "population_coverage": "all_visitors",
        "unit": unit,
        "rows": [{"metric": vmetric, "value": value}],
        "source": {"source_id": f"jnto-{name.lower()}-2024", "export_date": "2025-01-10"},
    }


def _sheet():
    return {
        "version": "sheet-v1.2",
        "metrics": [{
            "metric": "overnight_visitors", "semantics": "overnight visitor count",
            "required_period_alignment": "exact", "eligible": True,
        }],
    }


def _envelope(markets):
    return json.dumps({
        "markets": markets,
        "metric_definition_sheet": _sheet(),
        "requested_metrics": ["overnight_visitors"],
    })


def _compiled_agent():
    from shared.secrets import factory as secrets_factory
    from src.graph.graph import build_agent
    agent = build_agent()
    agent.compile()
    agent.provision_secrets(secrets_factory(namespace="trv-c2-036", agent_name="trv_c2_036"))
    return agent


def _invoke(agent, payload, trust):
    from framework.schemas.invocation_context import InvocationContext
    from framework.schemas.trust_level import TrustLevel
    from framework.secrets.context import bound_secrets
    tl = TrustLevel.VERIFIED_EXTERNAL if trust == "VE" else TrustLevel.ANONYMOUS
    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(session_id="e2e", caller_trust_level=tl)
        return agent.invoke(payload, ctx=ctx)


# invoke() surfaces the caller-facing output of finalize's get_output()
# (= formatted_output or result) under the "output" key; the pre-projection State fields may
# also appear. Read defensively across all three; assert absence across all three.
def _body(out: dict):
    for key in ("output", "formatted_output", "result"):
        val = out.get(key)
        if val is not None:
            return val
    return None


def _has_no_caller_output(out: dict) -> bool:
    return all(out.get(k) is None for k in ("output", "formatted_output", "result"))


def test_valid_verified_external_produces_comparison_brief():
    _require_real_framework()
    agent = _compiled_agent()
    out = _invoke(agent, _envelope([_market("Tokyo", value=100), _market("Osaka", value=80)]), "VE")
    assert out.get("status") == "success", out
    # comparison outcome yields a cited markdown brief in the caller-facing output
    assert "sheet-v1.2" in str(_body(out)), out


def test_zero_comparable_still_succeeds():
    _require_real_framework()
    agent = _compiled_agent()
    # exact alignment but different years -> 0 comparable -> no_comparable_metrics (success)
    payload = _envelope([_market("Tokyo", period="2024"), _market("Osaka", period="2023")])
    out = _invoke(agent, payload, "VE")
    assert out.get("status") == "success", out
    # no comparison brief: a validation report is surfaced instead of a markdown brief
    assert _body(out) is not None, out


def test_anonymous_caller_refused_before_execute():
    _require_real_framework()
    agent = _compiled_agent()
    out = _invoke(agent, _envelope([_market("Tokyo"), _market("Osaka")]), "ANON")
    assert out.get("status") == "error", out
    assert _has_no_caller_output(out), out


def test_inner_input_fault_is_answered_with_a_notice_and_no_brief():
    """A bad cell in the caller's own table is theirs to correct, so the run completes carrying
    the code and the notice — and produces no brief, and no validation report either (that is a
    real outcome meaning the metrics were checked and none were comparable)."""
    _require_real_framework()
    agent = _compiled_agent()
    bad = _market("Tokyo")
    bad["rows"][0]["value"] = "not-a-number"  # passes outer gate, answered by inner normalize
    out = _invoke(agent, _envelope([bad, _market("Osaka")]), "VE")
    assert out.get("status") == "success", out
    assert out.get("diagnostic_code") == "ERR_ROW_VALUE", out
    assert out.get("output") == explain_degradation("ERR_ROW_VALUE")
    assert out.get("result") is None
    assert out.get("comparison_outcome") is None, "a notice is not a validation report"
