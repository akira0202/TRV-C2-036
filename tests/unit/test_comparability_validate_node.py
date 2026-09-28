"""Unit tests for ComparabilityValidateNode (per-metric comparability decision).

Scope: this node only — the deterministic comparability decision against the approved
metric-definition sheet's declared rules, the FIXED reason priority order, fail-closed
handling of corrupt State, and the owned-delta / single-audit-event contract. Brief
generation, ranking, recommendation, forecasting, and caller-output projection belong to
later issues and are NOT exercised here.
"""

import pytest

import src.nodes.comparability_validate_node as node_mod
from src.nodes.comparability_validate_node import (
    ComparabilityValidateNode,
    _Reject,
    _build_comparability,
    _safe_year,
)


# ---------------------------------------------------------------- builders ----

def _rec(market, metric, value, *, unit="persons", period="2024-01/2024-12",
         population="all_visitors", row=0, source_id=None):
    return {
        "market": market,
        "metric": metric,
        "value": value,
        "unit": unit,
        "period": period,
        "population": population,
        "source_ref": {
            "source_id": source_id or f"jnto-{market.lower()}-2024",
            "table": market,
            "row": row,
        },
    }


def _defn(metric, *, eligible=True, alignment="exact", required_population=None,
          conversions=None):
    return {
        "metric": metric,
        "semantics": f"{metric} count",
        "required_period_alignment": alignment,
        "required_population": required_population,
        "allowed_unit_conversions": conversions or [],
        "eligible": eligible,
    }


def _state(records, defs, requested, version="sheet-v1.2"):
    return {
        "normalized_markets": {"records": records},
        "metric_definitions": {"version": version, "metrics": defs},
        "validated_input": {"requested_metrics": requested},
    }


def _decisions(records, defs, requested):
    return _build_comparability(
        {"records": records},
        {"version": "sheet-v1.2", "metrics": defs},
        requested,
    )["decisions"]


def _only(records, defs, requested):
    d = _decisions(records, defs, requested)
    assert len(d) == 1
    return d[0]


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


# ---------------------------------------------------- 1. comparable happy path

def test_same_period_population_unit_eligible_is_comparable():
    m = "overnight_visitors"
    d = _only(
        [_rec("Tokyo", m, 100), _rec("Osaka", m, 80)],
        [_defn(m)],
        [m],
    )
    assert d["comparable"] is True
    assert d["reason"] == "comparable"
    # both citations retained, tagged by supplied market table
    assert d["market_a_ref"]["table"] == "Tokyo"
    assert d["market_b_ref"]["table"] == "Osaka"


# ------------------------------------------------ 2. exact / same_year period

def test_exact_alignment_positive_and_negative():
    m = "overnight_visitors"
    ok = _only(
        [_rec("Tokyo", m, 1, period="2024-01/2024-12"),
         _rec("Osaka", m, 2, period="2024-01/2024-12")],
        [_defn(m, alignment="exact")], [m])
    assert ok["comparable"] and ok["reason"] == "comparable"

    bad = _only(
        [_rec("Tokyo", m, 1, period="2024-01/2024-12"),
         _rec("Osaka", m, 2, period="2023-01/2023-12")],
        [_defn(m, alignment="exact")], [m])
    assert bad["comparable"] is False and bad["reason"] == "period_mismatch"


def test_same_year_alignment_positive_and_negative():
    m = "overnight_visitors"
    ok = _only(
        [_rec("Tokyo", m, 1, period="2024-01"), _rec("Osaka", m, 2, period="2024-11")],
        [_defn(m, alignment="same_year")], [m])
    assert ok["comparable"] and ok["reason"] == "comparable"

    bad = _only(
        [_rec("Tokyo", m, 1, period="2024"), _rec("Osaka", m, 2, period="2023")],
        [_defn(m, alignment="same_year")], [m])
    assert bad["comparable"] is False and bad["reason"] == "period_mismatch"


@pytest.mark.parametrize(
    "period,year",
    [("2024", "2024"), ("2024-01", "2024"), ("2024-01/2024-12", "2024"), ("2024/2025", "2024")],
)
def test_safe_year_accepts_iso_like(period, year):
    assert _safe_year(period) == year


@pytest.mark.parametrize("period", ["FY2024", "24-01", "last_year", "20244", "202", "", "2024Q1"])
def test_safe_year_rejects_uninterpretable(period):
    assert _safe_year(period) is None


def test_same_year_uninterpretable_period_is_mismatch_not_guessed():
    m = "overnight_visitors"
    d = _only(
        [_rec("Tokyo", m, 1, period="FY2024"), _rec("Osaka", m, 2, period="2024")],
        [_defn(m, alignment="same_year")], [m])
    assert d["comparable"] is False and d["reason"] == "period_mismatch"


def test_none_alignment_skips_period_check():
    m = "overnight_visitors"
    d = _only(
        [_rec("Tokyo", m, 1, period="2024"), _rec("Osaka", m, 2, period="2019")],
        [_defn(m, alignment="none")], [m])
    assert d["comparable"] is True and d["reason"] == "comparable"


# ------------------------------------------------------ 3. population mismatch

def test_required_population_mismatch():
    m = "overnight_visitors"
    d = _only(
        [_rec("Tokyo", m, 1, population="all_visitors"),
         _rec("Osaka", m, 2, population="foreign_visitors")],
        [_defn(m, required_population="all_visitors")], [m])
    assert d["comparable"] is False and d["reason"] == "population_mismatch"


def test_required_population_both_match_is_comparable():
    m = "overnight_visitors"
    d = _only(
        [_rec("Tokyo", m, 1, population="all_visitors"),
         _rec("Osaka", m, 2, population="all_visitors")],
        [_defn(m, required_population="all_visitors")], [m])
    assert d["comparable"] is True


def test_undeclared_required_population_imposes_no_gate():
    m = "overnight_visitors"
    d = _only(
        [_rec("Tokyo", m, 1, population="all_visitors"),
         _rec("Osaka", m, 2, population="foreign_visitors")],
        [_defn(m, required_population=None)], [m])
    assert d["comparable"] is True  # no required_population -> population not gated


# ------------------------------------------------------------------- 4. unit

def test_same_unit_passes():
    m = "overnight_visitors"
    d = _only([_rec("Tokyo", m, 1, unit="persons"), _rec("Osaka", m, 2, unit="persons")],
              [_defn(m)], [m])
    assert d["comparable"] is True


def test_explicit_conversion_makes_units_comparable_either_direction():
    m = "overnight_visitors"
    conv = [{"from": "thousand_persons", "to": "persons", "factor": 1000}]
    # declared from thousand_persons -> persons; comparability is symmetric
    d1 = _only([_rec("Tokyo", m, 1, unit="persons"), _rec("Osaka", m, 2, unit="thousand_persons")],
               [_defn(m, conversions=conv)], [m])
    assert d1["comparable"] is True and d1["reason"] == "comparable"
    d2 = _only([_rec("Tokyo", m, 1, unit="thousand_persons"), _rec("Osaka", m, 2, unit="persons")],
               [_defn(m, conversions=conv)], [m])
    assert d2["comparable"] is True


def test_no_conversion_incompatible_unit():
    m = "overnight_visitors"
    d = _only([_rec("Tokyo", m, 1, unit="persons"), _rec("Osaka", m, 2, unit="nights")],
              [_defn(m)], [m])
    assert d["comparable"] is False and d["reason"] == "unit_incompatible"


def test_conversion_present_but_values_not_rewritten():
    # comparability node decides only; it must never convert/rewrite the figure.
    m = "overnight_visitors"
    conv = [{"from": "thousand_persons", "to": "persons", "factor": 1000}]
    recs = [_rec("Tokyo", m, 5, unit="persons"), _rec("Osaka", m, 5, unit="thousand_persons")]
    _only(recs, [_defn(m, conversions=conv)], [m])
    assert recs[0]["value"] == 5 and recs[1]["value"] == 5  # untouched


# --------------------------------------------------------- 5. metric_undefined

def test_metric_not_in_sheet_is_undefined():
    m = "overnight_visitors"
    d = _only([_rec("Tokyo", m, 1), _rec("Osaka", m, 2)], [], [m])
    assert d["comparable"] is False and d["reason"] == "metric_undefined"


def test_metric_not_eligible_is_undefined():
    m = "overnight_visitors"
    d = _only([_rec("Tokyo", m, 1), _rec("Osaka", m, 2)], [_defn(m, eligible=False)], [m])
    assert d["comparable"] is False and d["reason"] == "metric_undefined"


# --------------------------------- 6. one-side missing / null value / citation

def test_metric_absent_on_one_side():
    m = "overnight_visitors"
    # a 2-market state where Osaka simply lacks this metric row:
    recs = [_rec("Tokyo", m, 1), _rec("Osaka", "other_metric", 2)]
    d = _only(recs, [_defn(m), _defn("other_metric")], [m])
    assert d["comparable"] is False and d["reason"] == "metric_absent"
    assert d["market_a_ref"]["table"] == "Tokyo"  # citation kept on the side that has it
    assert d["market_b_ref"] is None              # absent side -> no citation


def test_null_value_is_not_a_comparable_number():
    m = "overnight_visitors"
    recs = [_rec("Tokyo", m, None), _rec("Osaka", m, 80)]
    d = _only(recs, [_defn(m)], [m])
    assert d["comparable"] is False and d["reason"] == "metric_absent"
    # a null-valued record still HAS a citation (present metadata), so its ref is kept
    assert d["market_a_ref"]["table"] == "Tokyo"
    assert d["market_b_ref"]["table"] == "Osaka"


def test_absent_when_metric_missing_from_both():
    m = "overnight_visitors"
    recs = [_rec("Tokyo", "x", 1), _rec("Osaka", "y", 2)]
    d = _only(recs, [_defn(m), _defn("x"), _defn("y")], [m])
    assert d["comparable"] is False and d["reason"] == "metric_absent"
    assert d["market_a_ref"] is None and d["market_b_ref"] is None


# --------------------------------------------- priority ordering (fixed order)

def test_priority_undefined_beats_absent():
    # metric undefined AND absent on a side -> undefined wins (checked first)
    m = "overnight_visitors"
    recs = [_rec("Tokyo", m, 1), _rec("Osaka", "other", 2)]
    d = _only(recs, [_defn("other")], [m])  # m not defined
    assert d["reason"] == "metric_undefined"


def test_priority_absent_beats_population():
    # value null (absent) AND population mismatch -> absent wins (checked before pop)
    m = "overnight_visitors"
    recs = [_rec("Tokyo", m, None, population="all_visitors"),
            _rec("Osaka", m, 2, population="foreign_visitors")]
    d = _only(recs, [_defn(m, required_population="all_visitors")], [m])
    assert d["reason"] == "metric_absent"


def test_priority_population_beats_period():
    m = "overnight_visitors"
    recs = [_rec("Tokyo", m, 1, population="a", period="2024"),
            _rec("Osaka", m, 2, population="b", period="2019")]
    d = _only(recs, [_defn(m, alignment="exact", required_population="a")], [m])
    assert d["reason"] == "population_mismatch"


def test_priority_period_beats_unit():
    m = "overnight_visitors"
    recs = [_rec("Tokyo", m, 1, period="2024", unit="persons"),
            _rec("Osaka", m, 2, period="2019", unit="nights")]
    d = _only(recs, [_defn(m, alignment="exact")], [m])
    assert d["reason"] == "period_mismatch"


# ------------------------------- 7. zero comparable is success with decisions

def test_order_preserved_one_decision_per_requested():
    recs = [
        _rec("Tokyo", "a", 1), _rec("Osaka", "a", 2),
        _rec("Tokyo", "b", 3), _rec("Osaka", "b", 4),
    ]
    defs = [_defn("a"), _defn("b")]
    d = _decisions(recs, defs, ["b", "a", "b"])  # order + duplicate preserved
    assert [x["metric"] for x in d] == ["b", "a", "b"]


def test_zero_comparable_is_success_with_full_decisions(spy):
    m = "overnight_visitors"
    # period mismatch on the only requested metric -> 0 comparable, still success
    state = _state(
        [_rec("Tokyo", m, 1, period="2024"), _rec("Osaka", m, 2, period="2019")],
        [_defn(m, alignment="exact")], [m])
    out = ComparabilityValidateNode().execute(state)
    assert out["status"] == "success"
    decisions = out["comparability"]["decisions"]
    assert len(decisions) == 1 and decisions[0]["comparable"] is False
    assert [c[0] for c in spy.calls] == ["comparability_validated"]
    assert spy.calls[0][1] == {"requested": 1, "comparable": 0}


# ---------------------------------------------------- 8. fail-closed on corrupt

@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda s: s.__setitem__("normalized_markets", "nope"), "ERR_NORMALIZED_SHAPE"),
        (lambda s: s["normalized_markets"].__setitem__("records", "nope"), "ERR_NORMALIZED_SHAPE"),
        (lambda s: s["normalized_markets"]["records"].append(_rec("Kyoto", "overnight_visitors", 3)),
         "ERR_MARKET_COUNT"),
        (lambda s: s["normalized_markets"]["records"].pop(),  # 1 market only
         "ERR_MARKET_COUNT"),
        (lambda s: s["metric_definitions"].__setitem__("version", ""), "ERR_DEFINITION_VERSION"),
        (lambda s: s["metric_definitions"].__setitem__("metrics", "nope"), "ERR_DEFINITIONS_SHAPE"),
        (lambda s: s["validated_input"].__setitem__("requested_metrics", []), "ERR_REQUESTED_METRICS"),
        (lambda s: s["normalized_markets"]["records"][0].__setitem__("source_ref", {"table": "Tokyo"}),
         "ERR_CITATION_SHAPE"),
        (lambda s: s["normalized_markets"]["records"][0]["source_ref"].__setitem__("row", True),
         "ERR_CITATION_SHAPE"),
    ],
)
def test_corrupt_state_fail_closed(mutate, code):
    m = "overnight_visitors"
    s = _state([_rec("Tokyo", m, 1), _rec("Osaka", m, 2)], [_defn(m)], [m])
    mutate(s)
    with pytest.raises(_Reject) as ei:
        _build_comparability(s["normalized_markets"], s["metric_definitions"],
                             s["validated_input"].get("requested_metrics"))
    assert ei.value.code == code


def test_duplicate_record_fail_closed():
    m = "overnight_visitors"
    recs = [_rec("Tokyo", m, 1, row=0), _rec("Tokyo", m, 2, row=1), _rec("Osaka", m, 3)]
    with pytest.raises(_Reject) as ei:
        _build_comparability({"records": recs}, {"version": "v", "metrics": [_defn(m)]}, [m])
    assert ei.value.code == "ERR_DUPLICATE_RECORD"


def test_duplicate_definition_fail_closed():
    m = "overnight_visitors"
    with pytest.raises(_Reject) as ei:
        _build_comparability(
            {"records": [_rec("Tokyo", m, 1), _rec("Osaka", m, 2)]},
            {"version": "v", "metrics": [_defn(m), _defn(m)]}, [m])
    assert ei.value.code == "ERR_DUPLICATE_DEFINITION"


@pytest.mark.parametrize(
    "bad_value",
    ["unknown", float("nan"), float("inf"), float("-inf"), True, 10 ** 1000],
)
def test_non_numeric_or_non_finite_record_value_fail_closed(bad_value):
    # canonical record value is number|null; a string / NaN / Infinity / bool / huge int
    # is corrupt State -> fail closed, never silently comparable.
    m = "overnight_visitors"
    recs = [_rec("Tokyo", m, bad_value), _rec("Osaka", m, 80)]
    with pytest.raises(_Reject) as ei:
        _build_comparability({"records": recs}, {"version": "v", "metrics": [_defn(m)]}, [m])
    assert ei.value.code == "ERR_RECORD_SHAPE"


def test_non_finite_record_value_does_not_become_comparable():
    # end-to-end: a non-finite value with otherwise-aligned metadata must NOT be comparable
    m = "overnight_visitors"
    s = _state([_rec("Tokyo", m, float("inf")), _rec("Osaka", m, 80)], [_defn(m)], [m])
    out = ComparabilityValidateNode().execute(s)
    assert out["status"] == "error"
    assert "comparability" not in out


@pytest.mark.parametrize(
    "bad_conv",
    [
        [{"from": "persons", "to": "thousands"}],                         # missing factor
        [{"from": "persons", "to": "thousands", "factor": "x"}],          # non-number factor
        [{"from": "persons", "to": "thousands", "factor": float("nan")}],  # non-finite
        [{"from": "persons", "to": "thousands", "factor": float("inf")}],  # non-finite
        [{"from": "persons", "to": "thousands", "factor": 10 ** 1000}],   # huge int
        [{"from": "persons", "to": "thousands", "factor": 1.0, "x": 1}],  # extra key
    ],
)
def test_incomplete_conversion_definition_fail_closed(bad_conv):
    # a broken approved conversion rule is corrupt State, not a pass-through to comparable.
    m = "overnight_visitors"
    recs = [_rec("Tokyo", m, 1, unit="persons"), _rec("Osaka", m, 2, unit="thousands")]
    defn = _defn(m)
    defn["allowed_unit_conversions"] = bad_conv
    with pytest.raises(_Reject) as ei:
        _build_comparability({"records": recs}, {"version": "v", "metrics": [defn]}, [m])
    assert ei.value.code == "ERR_DEFINITION_SHAPE"


def test_incomplete_conversion_does_not_pass_unit_gate():
    # end-to-end: a factorless conversion must not silently make differing units comparable
    m = "overnight_visitors"
    defn = _defn(m)
    defn["allowed_unit_conversions"] = [{"from": "persons", "to": "thousands"}]
    s = _state([_rec("Tokyo", m, 1, unit="persons"), _rec("Osaka", m, 2, unit="thousands")],
               [defn], [m])
    out = ComparabilityValidateNode().execute(s)
    assert out["status"] == "error"
    assert "comparability" not in out


def test_execute_malformed_state_is_partial_free(spy):
    out = ComparabilityValidateNode().execute({"validated_input": None})
    assert out["status"] == "error"
    assert set(out) == {"status", "error_log"}  # no partial comparability
    assert "comparability" not in out
    assert "ERR_VALIDATED_INPUT" in out["error_log"][0]
    assert [c[0] for c in spy.calls] == ["comparability_rejected"]


def test_execute_reject_error_log_and_event_have_no_market_data(spy):
    # a control-char-bearing market name would be corrupt upstream; here we ensure the
    # reject path never echoes State values into log/event — only the code.
    m = "overnight_visitors"
    s = _state([_rec("SecretMarketName", m, 424242)], [_defn(m)], [m])  # 1 market -> reject
    out = ComparabilityValidateNode().execute(s)
    assert out["status"] == "error"
    joined = " ".join(out["error_log"])
    assert "SecretMarketName" not in joined and "424242" not in joined
    assert spy.calls[0][0] == "comparability_rejected"
    assert spy.calls[0][1] == {"reason_code": "ERR_MARKET_COUNT"}


# ------------------------------------------ 9. owned-delta / 10. single event

def test_execute_success_returns_only_owned_delta(spy):
    m = "overnight_visitors"
    s = _state([_rec("Tokyo", m, 1), _rec("Osaka", m, 2)], [_defn(m)], [m])
    out = ComparabilityValidateNode().execute(s)
    assert out["status"] == "success"
    assert set(out) == {"comparability", "status"}
    # never writes downstream-owned / caller-facing fields
    for forbidden in ("comparison_outcome", "result", "formatted_output"):
        assert forbidden not in out


def test_success_emits_exactly_one_domain_event_without_observed_values(spy):
    m = "overnight_visitors"
    s = _state([_rec("Tokyo", m, 999999), _rec("Osaka", m, 111111)], [_defn(m)], [m])
    ComparabilityValidateNode().execute(s)
    assert len(spy.calls) == 1
    event, payload = spy.calls[0]
    assert event == "comparability_validated"
    # payload carries counts only — no observed values, market names, or CSV content
    assert payload == {"requested": 1, "comparable": 1}
    assert "999999" not in str(payload) and "Tokyo" not in str(payload)
