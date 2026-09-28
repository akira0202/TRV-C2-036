"""Unit tests for the Cat-2 composition boundary (no full compile — pure methods only).

Covers the inner->outer channel: MarketComparisonGraphNode.extract_input / merge_output and
MarketComparisonInnerGraph.get_output. Full graph compile()/invoke() routing is verified in
CI (tests/integration/test_agent_invoke_e2e.py) against the real framework.
"""

import json

from src.graph.graph import MarketComparisonGraphNode
from src.graph.market_comparison_inner_graph import MarketComparisonInnerGraph


def test_extract_input_serialises_validated_input_deterministically():
    node = MarketComparisonGraphNode(node_config={})
    env = {"requested_metrics": ["m"], "markets": [{"market": "Tokyo"}, {"market": "Osaka"}]}
    s = node.extract_input({"validated_input": env})
    assert isinstance(s, str)
    assert json.loads(s) == env
    # deterministic (sorted keys) so the inner hand-off is stable
    assert node.extract_input({"validated_input": env}) == s


def test_extract_input_defaults_to_empty_object():
    node = MarketComparisonGraphNode(node_config={})
    assert json.loads(node.extract_input({})) == {}


def test_merge_output_maps_only_boundary_fields():
    node = MarketComparisonGraphNode(node_config={})
    sub_result = {
        "output": {"brief_markdown": "x"},
        "status": "success",
        "comparison_outcome": "comparison",
        "definition_sheet_version": "sheet-v1.2",
        # working fields that must NOT be surfaced even if present:
        "normalized_markets": {"records": [1]},
        "provenance": {"Tokyo": {}},
        "comparability": {"decisions": []},
        "error_log": [],
    }
    delta = node.merge_output({}, sub_result)
    assert delta == {
        "result": {"brief_markdown": "x"},
        "comparison_outcome": "comparison",
        "definition_sheet_version": "sheet-v1.2",
        "status": "success",
        # the notice marker is a boundary field: empty on a run that produced a brief
        "diagnostic_code": "",
    }
    for leaked in ("normalized_markets", "provenance", "comparability"):
        assert leaked not in delta


def test_graphnode_class_flags():
    assert MarketComparisonGraphNode.error_strategy == "propagate"
    assert MarketComparisonGraphNode.propagate_hitl is False


def test_inner_get_output_is_narrow_and_hides_working_fields():
    g = MarketComparisonInnerGraph(config={})
    state = {
        "result": {"brief_markdown": "b"},
        "status": "success",
        "comparison_outcome": "comparison",
        "definition_sheet_version": "sheet-v1.2",
        "error_log": [],
        # inner working fields that must never cross the boundary:
        "normalized_markets": {"records": [1, 2]},
        "metric_definitions": {"version": "v"},
        "provenance": {"Tokyo": {}},
        "comparability": {"decisions": [{"metric": "m"}]},
        "validated_input": {"secret": 1},
    }
    out = g.get_output(state)
    assert set(out) == {
        "output", "status", "comparison_outcome", "definition_sheet_version",
        "diagnostic_code", "error_log",
    }
    assert out["output"] == {"brief_markdown": "b"}
    for hidden in ("normalized_markets", "metric_definitions", "provenance", "comparability", "validated_input"):
        assert hidden not in out


def test_inner_route_is_fail_fast():
    g = MarketComparisonInnerGraph(config={})
    assert g.route({"status": "error"}) == "end"
    assert g.route({"status": "success"}) == "continue"


def test_the_marker_crosses_the_boundary_when_the_inner_graph_answers():
    """An inner node that errors is re-raised as SubgraphError before merge_output runs, so the
    inner graph has to *complete* for its code to reach the outer state at all."""
    g = MarketComparisonInnerGraph(config={})
    out = g.get_output({"status": "success", "diagnostic_code": "ERR_ROW_VALUE", "result": None})
    assert out["diagnostic_code"] == "ERR_ROW_VALUE"

    node = MarketComparisonGraphNode(node_config={})
    delta = node.merge_output({}, out)
    assert delta["diagnostic_code"] == "ERR_ROW_VALUE"
    assert delta["result"] is None


def test_the_outer_marker_wins_over_an_inner_one():
    """pre_process answers the envelope before the inner graph runs; its code names the specific
    reason, while an inner node could only report its own."""
    node = MarketComparisonGraphNode(node_config={})
    delta = node.merge_output(
        {"diagnostic_code": "ERR_ENVELOPE_MARKETS"},
        {"status": "success", "diagnostic_code": "ERR_ROW_VALUE", "output": None},
    )
    assert delta["diagnostic_code"] == "ERR_ENVELOPE_MARKETS"
