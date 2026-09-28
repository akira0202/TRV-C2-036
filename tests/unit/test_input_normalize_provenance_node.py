"""Unit tests for InputNormalizeProvenanceNode (input normalization + provenance).

Scope: this node only — input-contract validation, canonical normalization,
minimal provenance, and the domain S-2 structural screen. Comparability / brief /
outer wiring belong to later issues and are not exercised here.
"""

import pytest

import src.nodes.input_normalize_provenance_node as node_mod
from src.nodes.input_normalize_provenance_node import (
    InputNormalizeProvenanceNode,
    _Reject,
    _screen_structural,
    _validate_and_normalize,
)


def _valid_envelope():
    return {
        "markets": [
            {
                "market": "Tokyo",
                "reporting_period": "2024-01/2024-12",
                "population_coverage": "all_visitors",
                "unit": "persons",
                "rows": [
                    {"metric": "overnight_visitors", "value": 100},
                    {"metric": "same_day_visitors", "value": None},
                ],
                "source": {
                    "source_id": "jnto-tokyo-2024",
                    "export_date": "2025-01-10",
                    "title": "JNTO Tokyo",
                    "url": "https://statistics.jnto.go.jp/tokyo",
                    "footnotes": ["provisional"],
                },
            },
            {
                "market": "Osaka",
                "reporting_period": "2024-01/2024-12",
                "population_coverage": "all_visitors",
                "unit": "persons",
                "rows": [{"metric": "overnight_visitors", "value": 80}],
                "source": {"source_id": "jnto-osaka-2024", "export_date": "2025-01-10"},
            },
        ],
        "metric_definition_sheet": {
            "version": "sheet-v1.2",
            "metrics": [
                {
                    "metric": "overnight_visitors",
                    "semantics": "overnight visitor count",
                    "required_period_alignment": "exact",
                    "eligible": True,
                }
            ],
        },
        "requested_metrics": ["overnight_visitors"],
    }


class _Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, event_type, payload, state):
        self.calls.append((event_type, payload))


@pytest.fixture
def spy(monkeypatch):
    s = _Spy()
    monkeypatch.setattr(node_mod, "emit_trace_event", s)
    return s


# ---------- pure validation / normalization ----------

def test_valid_envelope_produces_canonical_records_and_provenance():
    d = _validate_and_normalize(_valid_envelope())
    recs = d["normalized_markets"]["records"]
    assert len(recs) == 3  # 2 Tokyo rows + 1 Osaka row
    assert d["definition_sheet_version"] == "sheet-v1.2"
    assert d["metric_definitions"]["version"] == "sheet-v1.2"
    assert set(d["provenance"]) == {"Tokyo", "Osaka"}
    # minimal provenance only — no raw rows carried
    assert "rows" not in d["provenance"]["Tokyo"]
    assert d["provenance"]["Tokyo"]["footnotes"] == ["provisional"]


def test_source_ref_matches_input_and_no_fabricated_citation():
    d = _validate_and_normalize(_valid_envelope())
    tokyo0 = next(r for r in d["normalized_markets"]["records"] if r["market"] == "Tokyo" and r["source_ref"]["row"] == 0)
    assert tokyo0["source_ref"] == {"source_id": "jnto-tokyo-2024", "table": "Tokyo", "row": 0}
    # Osaka source has no url; nothing is fabricated into its provenance
    assert "url" not in d["provenance"]["Osaka"]


def test_null_value_is_preserved_not_imputed():
    d = _validate_and_normalize(_valid_envelope())
    same_day = next(r for r in d["normalized_markets"]["records"] if r["metric"] == "same_day_visitors")
    assert same_day["value"] is None


def test_row_unit_period_default_to_table_level():
    d = _validate_and_normalize(_valid_envelope())
    r = d["normalized_markets"]["records"][0]
    assert r["unit"] == "persons" and r["period"] == "2024-01/2024-12" and r["population"] == "all_visitors"


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda e: e["markets"].pop(), "ERR_MARKET_COUNT"),  # 1 market
        (lambda e: e["markets"].__setitem__(1, dict(e["markets"][0])), "ERR_MARKET_DUPLICATE"),  # 2 markets, same name
        (lambda e: e.__setitem__("requested_metrics", []), "ERR_REQUESTED_METRICS"),
        (lambda e: e["metric_definition_sheet"].__setitem__("version", ""), "ERR_SHEET_VERSION"),
        (lambda e: e["markets"][0].pop("source"), "ERR_SOURCE_SHAPE"),
        (lambda e: e["markets"][0]["rows"][0].__setitem__("value", "100"), "ERR_ROW_VALUE"),
        (lambda e: e["markets"][0]["rows"][0].__setitem__("value", True), "ERR_ROW_VALUE"),  # bool != number
        (lambda e: e.__setitem__("surprise", 1), "ERR_UNKNOWN_TOP_KEY"),
        (lambda e: e["markets"][0].pop("reporting_period"), "ERR_MARKET_PERIOD"),
        (lambda e: e["markets"][0]["source"].__setitem__("title", 123), "ERR_SOURCE_TITLE"),
        (lambda e: e["markets"][0]["source"].__setitem__("footnotes", "notalist"), "ERR_SOURCE_FOOTNOTES"),
        (lambda e: e["metric_definition_sheet"]["metrics"][0].__setitem__("required_population", 5), "ERR_METRICDEF_POPULATION"),
        (lambda e: e["metric_definition_sheet"]["metrics"][0].__setitem__(
            "allowed_unit_conversions", [{"from": "a", "to": "b", "factor": "x"}]), "ERR_METRICDEF_CONVERSION_SHAPE"),
    ],
)
def test_contract_violations_fail_closed(mutate, code):
    e = _valid_envelope()
    mutate(e)
    with pytest.raises(_Reject) as ei:
        _validate_and_normalize(e)
    assert ei.value.code == code


def test_three_distinct_markets_rejected_by_count():
    e = _valid_envelope()
    e["markets"].append({**e["markets"][0], "market": "Kyoto"})
    with pytest.raises(_Reject) as ei:
        _validate_and_normalize(e)
    assert ei.value.code == "ERR_MARKET_COUNT"


def test_free_text_and_non_object_inputs_rejected():
    for bad in ["just a sentence", 42, ["a", "b"], None]:
        with pytest.raises(_Reject):
            _validate_and_normalize(bad)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_row_value_rejected(bad_value):
    # isinstance(x, float) admits NaN/Infinity — not JSON/msgpack-safe; must be rejected
    e = _valid_envelope()
    e["markets"][0]["rows"][0]["value"] = bad_value
    with pytest.raises(_Reject) as ei:
        _validate_and_normalize(e)
    assert ei.value.code == "ERR_ROW_VALUE"


@pytest.mark.parametrize("bad_factor", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_conversion_factor_rejected(bad_factor):
    e = _valid_envelope()
    e["metric_definition_sheet"]["metrics"][0]["allowed_unit_conversions"] = [
        {"from": "a", "to": "b", "factor": bad_factor}
    ]
    with pytest.raises(_Reject) as ei:
        _validate_and_normalize(e)
    assert ei.value.code == "ERR_METRICDEF_CONVERSION_SHAPE"


def test_execute_answers_a_non_finite_value_partial_free(spy):
    node = InputNormalizeProvenanceNode()
    e = _valid_envelope()
    e["markets"][0]["rows"][0]["value"] = float("inf")
    out = node.execute({"validated_input": e})
    assert out["status"] == "success"          # the caller's own cell — theirs to correct
    assert set(out) == {"status", "diagnostic_code"}  # no partial domain fields, no error_log
    assert [c[0] for c in spy.calls] == ["input_degraded"]


def test_duplicate_metric_row_in_a_market_rejected():
    # "one per market x metric": a second row for the same metric is ambiguous
    e = _valid_envelope()
    e["markets"][0]["rows"].append({"metric": "overnight_visitors", "value": 999})
    with pytest.raises(_Reject) as ei:
        _validate_and_normalize(e)
    assert ei.value.code == "ERR_DUPLICATE_METRIC_ROW"


def test_duplicate_metric_definition_rejected():
    e = _valid_envelope()
    e["metric_definition_sheet"]["metrics"].append(
        {"metric": "overnight_visitors", "semantics": "dup", "required_period_alignment": "none", "eligible": False}
    )
    with pytest.raises(_Reject) as ei:
        _validate_and_normalize(e)
    assert ei.value.code == "ERR_DUPLICATE_METRIC_DEF"


def test_one_record_per_market_metric_invariant():
    d = _validate_and_normalize(_valid_envelope())
    keys = [(r["market"], r["metric"]) for r in d["normalized_markets"]["records"]]
    assert len(keys) == len(set(keys))  # canonical contract: one record per (market, metric)


def test_huge_int_row_value_rejected_without_crash():
    # math.isfinite(10**1000) raises OverflowError; must be a graceful _Reject instead
    e = _valid_envelope()
    e["markets"][0]["rows"][0]["value"] = 10 ** 1000
    with pytest.raises(_Reject) as ei:
        _validate_and_normalize(e)
    assert ei.value.code == "ERR_ROW_VALUE"


def test_huge_int_conversion_factor_rejected_without_crash():
    e = _valid_envelope()
    e["metric_definition_sheet"]["metrics"][0]["allowed_unit_conversions"] = [
        {"from": "a", "to": "b", "factor": 10 ** 1000}
    ]
    with pytest.raises(_Reject) as ei:
        _validate_and_normalize(e)
    assert ei.value.code == "ERR_METRICDEF_CONVERSION_SHAPE"


def test_execute_answers_a_huge_int_partial_free(spy):
    node = InputNormalizeProvenanceNode()
    e = _valid_envelope()
    e["markets"][0]["rows"][0]["value"] = 10 ** 1000
    out = node.execute({"validated_input": e})
    assert out["status"] == "success"
    assert set(out) == {"status", "diagnostic_code"}
    assert [c[0] for c in spy.calls] == ["input_degraded"]


def _deep():
    """A structure past the screen's depth ceiling (_MAX_DEPTH is 8)."""
    node = "leaf"
    for _ in range(12):
        node = {"n": node}
    return node


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda e: e.__setitem__("long", "x" * 10_001), "ERR_STRING_TOO_LONG"),  # _MAX_STR_LEN is 10_000
        (lambda e: e.__setitem__("ctl", "a" + chr(1) + "b"), "ERR_CONTROL_CHAR"),
        (lambda e: e.__setitem__("deep", _deep()), "ERR_STRUCTURE_TOO_DEEP"),
    ],
)
def test_the_structural_screen_still_refuses_and_is_never_answered(mutate, code, spy):
    """The screen exists to stop a payload that is not really a document from being walked at
    all. Reaching it on the second pass must not turn that refusal into a completed request."""
    node = InputNormalizeProvenanceNode()
    e = _valid_envelope()
    mutate(e)
    out = node.execute({"validated_input": e})
    assert out["status"] == "error", code
    assert "diagnostic_code" not in out
    assert [c[0] for c in spy.calls] == ["input_rejected"]


def test_conversion_extra_or_nested_key_rejected():
    e = _valid_envelope()
    e["metric_definition_sheet"]["metrics"][0]["allowed_unit_conversions"] = [
        {"from": "a", "to": "b", "factor": 1.0, "raw_blob": {"x": 1}}
    ]
    with pytest.raises(_Reject) as ei:
        _validate_and_normalize(e)
    assert ei.value.code == "ERR_METRICDEF_CONVERSION_SHAPE"


def test_success_conversions_are_canonical_no_unknown_keys():
    e = _valid_envelope()
    e["metric_definition_sheet"]["metrics"][0]["allowed_unit_conversions"] = [
        {"from": "persons", "to": "thousands", "factor": 0.001}
    ]
    d = _validate_and_normalize(e)
    convs = d["metric_definitions"]["metrics"][0]["allowed_unit_conversions"]
    assert convs == [{"from": "persons", "to": "thousands", "factor": 0.001}]
    assert all(set(c) == {"from", "to", "factor"} for c in convs)


# ---------- structural S-2 screen ----------

def test_screen_accepts_clean_json():
    _screen_structural(_valid_envelope())  # no raise


def test_screen_rejects_nul_and_control_chars():
    with pytest.raises(_Reject) as ei:
        _screen_structural({"k": "a\x00b"})
    assert ei.value.code == "ERR_CONTROL_CHAR"
    with pytest.raises(_Reject):
        _screen_structural({"k": "a\x07b"})


def test_screen_allows_tab_newline_cr():
    _screen_structural({"note": "line1\nline2\tcol\r"})  # no raise


def test_screen_rejects_oversize_and_too_deep_and_non_json():
    with pytest.raises(_Reject) as ei:
        _screen_structural("x" * 10_001)
    assert ei.value.code == "ERR_STRING_TOO_LONG"
    deep = cur = {}
    for _ in range(12):
        cur["n"] = {}
        cur = cur["n"]
    with pytest.raises(_Reject) as ei2:
        _screen_structural(deep)
    assert ei2.value.code == "ERR_STRUCTURE_TOO_DEEP"
    with pytest.raises(_Reject) as ei3:
        _screen_structural({"b": b"bytes"})
    assert ei3.value.code == "ERR_NON_JSON_TYPE"


# ---------- node execute() / hook ----------

def test_execute_success_returns_only_owned_delta(spy):
    node = InputNormalizeProvenanceNode()
    out = node.execute({"validated_input": _valid_envelope()})
    assert out["status"] == "success"
    assert set(out) == {"normalized_markets", "metric_definitions", "provenance", "definition_sheet_version", "status"}
    # never writes downstream-owned fields
    assert "comparability" not in out and "comparison_outcome" not in out
    assert "result" not in out and "formatted_output" not in out
    assert [c[0] for c in spy.calls] == ["input_normalized"]  # exactly one domain event


def test_execute_answer_is_partial_free_and_emits_once(spy):
    node = InputNormalizeProvenanceNode()
    e = _valid_envelope()
    e["markets"].pop()  # 1 market -> answered
    out = node.execute({"validated_input": e})
    assert out["status"] == "success"
    assert set(out) == {"status", "diagnostic_code"}  # no partial normalized_markets/provenance
    assert [c[0] for c in spy.calls] == ["input_degraded"]
    # the delta carries only the code, no raw input
    assert out["diagnostic_code"] == "ERR_MARKET_COUNT"


def test_execute_reject_error_log_has_no_raw_input(spy):
    node = InputNormalizeProvenanceNode()
    out = node.execute({"validated_input": {"markets": [{"market": "Secret Co\x00"}], "surprise": 1}})
    assert out["status"] == "error"
    joined = " ".join(out["error_log"])
    assert "Secret Co" not in joined  # raw input value never echoed


def test_extra_gate_rejects_control_chars_and_sets_error(spy):
    node = InputNormalizeProvenanceNode()
    e = _valid_envelope()
    e["markets"][0]["source"]["title"] = "bad\x00title"
    state = {"validated_input": e}
    out = node._extra_security_gate_input(state)
    assert out["status"] == "error"
    assert "ERR_CONTROL_CHAR" in out["error_log"][0]
    assert "normalized_markets" not in out  # no domain field written by the gate
    assert [c[0] for c in spy.calls] == ["input_structural_reject"]


def test_extra_gate_passes_clean_input_unchanged(spy):
    node = InputNormalizeProvenanceNode()
    state = {"validated_input": _valid_envelope()}
    out = node._extra_security_gate_input(state)
    assert out.get("status") != "error"
    assert spy.calls == []
