"""Unit tests for BriefGenerateNode (bounded comparison synthesis + cited brief draft).

Scope: this node only — the three terminal outcomes it owns (reject / comparison /
no_comparable_metrics), the cited-draft structure built verbatim from canonical records,
the fail-closed handling of corrupt State / broken citations, the owned-delta and
single-audit-event contract, and the OPTIONAL, non-authoritative LLM narrative (fake
client only; never live). Outer projection, the terminal S-3 egress gate, graph wiring,
and STG belong to later issues and are NOT exercised here.
"""

import pytest

import src.nodes.brief_generate_node as node_mod
from src.nodes.brief_generate_node import (
    BriefGenerateNode,
    _Reject,
    _index_records,
    _validate_decisions,
)


# ---------------------------------------------------------------- builders ----

def _rec(market, metric, value, *, unit="persons", period="2024", population="all",
         row=0, source_id=None):
    sid = source_id or f"jnto-{market.lower()}"
    return {
        "market": market,
        "metric": metric,
        "value": value,
        "unit": unit,
        "period": period,
        "population": population,
        "source_ref": {"source_id": sid, "table": market, "row": row},
    }


def _decision(metric, comparable, reason, ref_a=None, ref_b=None):
    return {
        "metric": metric,
        "comparable": comparable,
        "reason": reason,
        "market_a_ref": ref_a,
        "market_b_ref": ref_b,
    }


def _comparable(metric, rec_a, rec_b):
    return _decision(metric, True, "comparable", rec_a["source_ref"], rec_b["source_ref"])


def _state(records, decisions, *, version="sheet-v1.2", provenance=None):
    return {
        "definition_sheet_version": version,
        "comparability": {"decisions": decisions},
        "normalized_markets": {"records": records},
        "provenance": provenance if provenance is not None else {},
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


class _FakeLLM:
    def __init__(self, content):
        self._content = content
        self.called = 0

    def complete(self, messages):
        self.called += 1
        return {"content": self._content}


class _RaisingLLM:
    def complete(self, messages):
        raise TimeoutError("upstream timeout")


# ----------------------------------------- 1. comparison brief + requested order

def test_comparison_brief_with_excluded_preserves_requested_order(spy):
    ra = _rec("Tokyo", "overnight", 100, row=0)
    rb = _rec("Osaka", "overnight", 80, row=0)
    decisions = [
        _comparable("overnight", ra, rb),
        _decision("same_day", False, "metric_absent", ref_a=_rec("Tokyo", "same_day", 5, row=1)["source_ref"]),
        _decision("spend", False, "metric_undefined"),
    ]
    out = BriefGenerateNode().execute(_state([ra, rb], decisions))
    assert out["status"] == "success"
    assert out["comparison_outcome"] == "comparison"
    r = out["result"]
    assert [c["metric"] for c in r["compared_metrics"]] == ["overnight"]
    assert [e["metric"] for e in r["excluded_metrics"]] == ["same_day", "spend"]  # order kept
    cm = r["compared_metrics"][0]
    assert cm["market_a"] == {"value": 100, "unit": "persons", "period": "2024"}
    assert cm["market_b"] == {"value": 80, "unit": "persons", "period": "2024"}
    assert cm["citations"] == [
        {"source_id": "jnto-tokyo", "table": "Tokyo", "row": 0},
        {"source_id": "jnto-osaka", "table": "Osaka", "row": 0},
    ]
    assert "100" in r["brief_markdown"] and "80" in r["brief_markdown"]
    assert r["definition_sheet_version"] == "sheet-v1.2"


def test_excluded_source_field_mapping():
    ra = _rec("Tokyo", "overnight", 100)
    rb = _rec("Osaka", "overnight", 80, period="2019")
    # one comparable (so we get an excluded_metrics list) + one of each non-comparable reason
    ra2 = _rec("Kyoto", "ok", 1)
    rb2 = _rec("Nara", "ok", 2)
    decisions = [
        _comparable("ok", ra2, rb2),
        _decision("overnight", False, "period_mismatch", ra["source_ref"], rb["source_ref"]),
        _decision("m2", False, "population_mismatch"),
        _decision("m3", False, "unit_incompatible"),
        _decision("m4", False, "metric_undefined"),
        _decision("m5", False, "missing_required_metadata"),
        _decision("m6", False, "metric_absent"),
    ]
    out2 = BriefGenerateNode().execute(_state([ra, rb, ra2, rb2], decisions))
    emap = {e["metric"]: e["source_field"] for e in out2["result"]["excluded_metrics"]}
    assert emap == {
        "overnight": "reporting_period",
        "m2": "population_coverage",
        "m3": "unit",
        "m4": "metric_definition_sheet",
        "m5": "metadata",
        "m6": "rows",
    }


# --------------------------------------------------- 2. no_comparable_metrics

def test_zero_comparable_returns_validation_report_only(spy):
    ra = _rec("Tokyo", "overnight", 100, period="2024")
    rb = _rec("Osaka", "overnight", 80, period="2019")
    decisions = [
        _decision("overnight", False, "period_mismatch", ra["source_ref"], rb["source_ref"]),
        _decision("spend", False, "metric_undefined"),
    ]
    out = BriefGenerateNode().execute(_state([ra, rb], decisions))
    assert out["status"] == "success"
    assert out["comparison_outcome"] == "no_comparable_metrics"
    r = out["result"]
    assert "brief_markdown" not in r and "compared_metrics" not in r
    assert r["validation_report"]["requested_metrics"] == ["overnight", "spend"]
    assert r["validation_report"]["per_metric"] == [
        {"metric": "overnight", "comparable": False, "reason": "period_mismatch"},
        {"metric": "spend", "comparable": False, "reason": "metric_undefined"},
    ]
    assert r["definition_sheet_version"] == "sheet-v1.2"
    assert [c[0] for c in spy.calls] == ["brief_generated"]
    assert spy.calls[0][1] == {"outcome": "no_comparable_metrics", "compared": 0, "excluded": 2}


# --------------------------------------------------- 3. broken citation reject

def test_comparable_missing_one_citation_fail_closed(spy):
    ra = _rec("Tokyo", "overnight", 100)
    rb = _rec("Osaka", "overnight", 80)
    # comparable decision but market_b_ref is None -> both citations mandatory
    bad = _decision("overnight", True, "comparable", ra["source_ref"], None)
    out = BriefGenerateNode().execute(_state([ra, rb], [bad]))
    assert out["status"] == "error"
    assert "result" not in out and "comparison_outcome" not in out
    assert "ERR_CITATION_MISSING" in out["error_log"][0]
    assert [c[0] for c in spy.calls] == ["brief_rejected"]


def test_citation_not_matching_record_fail_closed():
    ra = _rec("Tokyo", "overnight", 100, row=0)
    rb = _rec("Osaka", "overnight", 80, row=0)
    # decision cites row=9 which no record has -> mismatch, fail-closed with no partial result
    ref_bad = {"source_id": "jnto-osaka", "table": "Osaka", "row": 9}
    dec = _decision("overnight", True, "comparable", ra["source_ref"], ref_bad)
    out = BriefGenerateNode().execute(_state([ra, rb], [dec]))
    assert out["status"] == "error"
    assert "result" not in out
    assert "ERR_CITATION_MISMATCH" in out["error_log"][0]


def test_structurally_broken_citation_in_decision_fail_closed():
    ra = _rec("Tokyo", "overnight", 100)
    rb = _rec("Osaka", "overnight", 80)
    broken = _decision("overnight", True, "comparable", ra["source_ref"], {"table": "Osaka"})
    with pytest.raises(_Reject) as ei:
        _validate_decisions({"decisions": [broken]})
    assert ei.value.code == "ERR_CITATION_SHAPE"


def test_broken_record_citation_fail_closed():
    ra = _rec("Tokyo", "overnight", 100)
    ra["source_ref"] = {"source_id": "x", "table": "Tokyo"}  # missing row
    with pytest.raises(_Reject) as ei:
        _index_records({"records": [ra]})
    assert ei.value.code == "ERR_CITATION_SHAPE"


# ----------------------------------------------- 4. missing definition version

@pytest.mark.parametrize("version", [None, "", 123])
def test_missing_definition_version_fail_closed(version):
    ra = _rec("Tokyo", "overnight", 100)
    rb = _rec("Osaka", "overnight", 80)
    dec = _comparable("overnight", ra, rb)
    s = _state([ra, rb], [dec], version="sheet")
    s["definition_sheet_version"] = version
    out = BriefGenerateNode().execute(s)
    assert out["status"] == "error"
    assert "result" not in out
    assert "ERR_DEFINITION_VERSION" in out["error_log"][0]


# ------------------------- 5. non-comparable never leaks into compared_metrics

def test_non_comparable_excluded_from_compared_metrics():
    ra = _rec("Tokyo", "overnight", 100)
    rb = _rec("Osaka", "overnight", 80)
    # a non-comparable metric with a real value in records must NOT appear in compared_metrics
    na = _rec("Tokyo", "leaky", 555, row=1)
    nb = _rec("Osaka", "leaky", 666, row=1, period="1999")
    decisions = [
        _comparable("overnight", ra, rb),
        _decision("leaky", False, "period_mismatch", na["source_ref"], nb["source_ref"]),
    ]
    out = BriefGenerateNode().execute(_state([ra, rb, na, nb], decisions))
    compared_metrics = [c["metric"] for c in out["result"]["compared_metrics"]]
    assert compared_metrics == ["overnight"]
    assert "leaky" not in compared_metrics
    # the non-comparable figures must not be pulled into any compared entry
    values = [c["market_a"]["value"] for c in out["result"]["compared_metrics"]] + \
             [c["market_b"]["value"] for c in out["result"]["compared_metrics"]]
    assert 555 not in values and 666 not in values


# ------------------------------ 6. values verbatim; no computation / ranking

def test_values_verbatim_no_diff_ratio_or_ranking():
    ra = _rec("Tokyo", "overnight", 100.0, unit="persons")
    rb = _rec("Osaka", "overnight", 80.0, unit="thousand_persons")  # differing unit passed comparable upstream
    out = BriefGenerateNode().execute(_state([ra, rb], [_comparable("overnight", ra, rb)]))
    cm = out["result"]["compared_metrics"][0]
    # values / units copied exactly — never converted, averaged, differenced, or percentaged
    assert cm["market_a"]["value"] == 100.0 and cm["market_a"]["unit"] == "persons"
    assert cm["market_b"]["value"] == 80.0 and cm["market_b"]["unit"] == "thousand_persons"
    md = out["result"]["brief_markdown"].lower()
    for banned in ("winner", "best", "rank", "recommend", "forecast", "優", "順位", "おすすめ",
                   "% change", "growth", "vs.", "difference of"):
        assert banned not in md


# ----------------------------------------- 7. optional LLM degrade (no/raise)

def test_no_llm_still_succeeds_deterministically(spy):
    ra = _rec("Tokyo", "overnight", 100)
    rb = _rec("Osaka", "overnight", 80)
    out = BriefGenerateNode(llm=None).execute(_state([ra, rb], [_comparable("overnight", ra, rb)]))
    assert out["status"] == "success" and out["comparison_outcome"] == "comparison"
    assert "Not generated" in out["result"]["brief_markdown"]
    assert spy.calls[0][1]["narrative"] == "omitted"


def test_llm_exception_degrades_to_deterministic_brief(spy):
    ra = _rec("Tokyo", "overnight", 100)
    rb = _rec("Osaka", "overnight", 80)
    out = BriefGenerateNode(llm=_RaisingLLM()).execute(_state([ra, rb], [_comparable("overnight", ra, rb)]))
    assert out["status"] == "success"
    assert out["result"]["compared_metrics"][0]["market_a"]["value"] == 100
    # a raising LLM must not add a second audit event, and outcome is unaffected
    assert [c[0] for c in spy.calls] == ["brief_generated"]
    assert spy.calls[0][1]["narrative"] == "omitted"


def test_llm_prose_is_included_when_available(spy):
    ra = _rec("Tokyo", "overnight", 100)
    rb = _rec("Osaka", "overnight", 80)
    llm = _FakeLLM("Both markets report the same annual period.")
    out = BriefGenerateNode(llm=llm).execute(_state([ra, rb], [_comparable("overnight", ra, rb)]))
    assert llm.called == 1
    assert "Both markets report the same annual period." in out["result"]["brief_markdown"]
    assert spy.calls[0][1]["narrative"] == "generated"


# ------------------------- 8. hallucinated LLM values/citations never authoritative

def test_llm_hallucination_never_enters_brief_or_authoritative_data():
    ra = _rec("Tokyo", "overnight", 100)
    rb = _rec("Osaka", "overnight", 80)
    state = _state([ra, rb], [_comparable("overnight", ra, rb)])
    baseline = BriefGenerateNode(llm=None).execute(state)["result"]
    hallucinating = _FakeLLM("Market Z is the clear winner with 999999 visitors [src: fake-src-999].")
    out = BriefGenerateNode(llm=hallucinating).execute(state)["result"]
    # authoritative structured data is identical to the deterministic (no-LLM) result
    assert out["compared_metrics"] == baseline["compared_metrics"]
    for cm in out["compared_metrics"]:
        assert cm["market_a"]["value"] == 100 and cm["market_b"]["value"] == 80
        for cit in cm["citations"]:
            assert cit["source_id"] in {"jnto-tokyo", "jnto-osaka"}
    # unverifiable prose is dropped: hallucinated figure / citation / ranking word is NOT
    # anywhere in the caller-facing brief_markdown either
    md = out["brief_markdown"]
    assert "999999" not in md and "fake-src-999" not in md and "winner" not in md.lower()
    assert "Not generated" in md


def test_clean_source_grounded_prose_is_admitted(spy):
    ra = _rec("Tokyo", "overnight", 100)
    rb = _rec("Osaka", "overnight", 80)
    llm = _FakeLLM("Both markets report the supplied overnight figures for the same annual period.")
    out = BriefGenerateNode(llm=llm).execute(_state([ra, rb], [_comparable("overnight", ra, rb)]))
    assert "Both markets report the supplied overnight figures" in out["result"]["brief_markdown"]
    assert spy.calls[0][1]["narrative"] == "generated"


def test_prose_with_fabricated_number_is_omitted(spy):
    ra = _rec("Tokyo", "overnight", 100)
    rb = _rec("Osaka", "overnight", 80)
    llm = _FakeLLM("The supplied figure is 123456 units in total.")  # 123456 not canonical
    out = BriefGenerateNode(llm=llm).execute(_state([ra, rb], [_comparable("overnight", ra, rb)]))
    assert out["status"] == "success"
    assert "123456" not in out["result"]["brief_markdown"]
    assert "Not generated" in out["result"]["brief_markdown"]
    assert spy.calls[0][1]["narrative"] == "omitted"


def test_prose_with_comparative_claim_is_omitted():
    # a comparative/ranking claim (no fabricated digits) must still be rejected — the human
    # reviewer draws the comparison, not the node.
    ra = _rec("Tokyo", "overnight", 100)
    rb = _rec("Osaka", "overnight", 80)
    for claim in ("Tokyo is higher than Osaka.", "Osaka shows lower demand.",
                  "Tokyo grew and is the better market.", "東京の方が多い。"):
        out = BriefGenerateNode(llm=_FakeLLM(claim)).execute(
            _state([ra, rb], [_comparable("overnight", ra, rb)]))
        assert out["status"] == "success"
        assert "Not generated" in out["result"]["brief_markdown"]


def test_prose_with_url_is_omitted():
    ra = _rec("Tokyo", "overnight", 100)
    rb = _rec("Osaka", "overnight", 80)
    llm = _FakeLLM("See https://evil.example/report for the full figures.")
    out = BriefGenerateNode(llm=llm).execute(_state([ra, rb], [_comparable("overnight", ra, rb)]))
    assert "evil.example" not in out["result"]["brief_markdown"]
    assert "Not generated" in out["result"]["brief_markdown"]


# --------------------------------------------- 9. owned delta / 10. one event

def test_success_returns_only_owned_delta():
    ra = _rec("Tokyo", "overnight", 100)
    rb = _rec("Osaka", "overnight", 80)
    out = BriefGenerateNode().execute(_state([ra, rb], [_comparable("overnight", ra, rb)]))
    assert set(out) == {"result", "comparison_outcome", "status"}
    assert "formatted_output" not in out  # outer S-3 / post_process owns this


def test_reject_returns_only_status_and_error_log(spy):
    out = BriefGenerateNode().execute({"comparability": {"decisions": []}})  # no version
    assert set(out) == {"status", "error_log"}
    assert "result" not in out and "comparison_outcome" not in out
    assert [c[0] for c in spy.calls] == ["brief_rejected"]


def test_audit_event_payload_has_no_values_or_market_names(spy):
    ra = _rec("Tokyo", "overnight", 999999)
    rb = _rec("Osaka", "overnight", 111111)
    BriefGenerateNode().execute(_state([ra, rb], [_comparable("overnight", ra, rb)]))
    assert len(spy.calls) == 1
    event, payload = spy.calls[0]
    assert event == "brief_generated"
    blob = str(payload)
    assert "999999" not in blob and "111111" not in blob and "Tokyo" not in blob


def test_reject_error_log_and_event_have_no_market_data(spy):
    ra = _rec("SecretMarket", "overnight", 424242)
    rb = _rec("Osaka", "overnight", 80)
    bad = _decision("overnight", True, "comparable", ra["source_ref"], None)  # missing citation
    out = BriefGenerateNode().execute(_state([ra, rb], [bad]))
    joined = " ".join(out["error_log"])
    assert "SecretMarket" not in joined and "424242" not in joined
    assert spy.calls[0][1] == {"reason_code": "ERR_CITATION_MISSING"}


# ---------------------------------------------- corrupt-shape fail-closed family

@pytest.mark.parametrize(
    "comparability,code",
    [
        ("nope", "ERR_COMPARABILITY_SHAPE"),
        ({"decisions": "nope"}, "ERR_COMPARABILITY_SHAPE"),
        ({"decisions": [{"metric": "m", "comparable": "yes", "reason": "comparable"}]},
         "ERR_COMPARABILITY_SHAPE"),
        ({"decisions": [{"metric": "m", "comparable": False, "reason": "made_up_reason"}]},
         "ERR_COMPARABILITY_SHAPE"),
    ],
)
def test_malformed_comparability_fail_closed(comparability, code):
    with pytest.raises(_Reject) as ei:
        _validate_decisions(comparability)
    assert ei.value.code == code


def test_comparable_with_null_value_fail_closed():
    # a comparable decision whose record value is None (not a comparable number) is corrupt
    ra = _rec("Tokyo", "overnight", None)
    rb = _rec("Osaka", "overnight", 80)
    out = BriefGenerateNode().execute(_state([ra, rb], [_comparable("overnight", ra, rb)]))
    assert out["status"] == "error"
    assert "result" not in out
    assert "ERR_COMPARABLE_VALUE" in out["error_log"][0]


def test_record_missing_unit_fail_closed():
    ra = _rec("Tokyo", "overnight", 100)
    del ra["unit"]
    with pytest.raises(_Reject) as ei:
        _index_records({"records": [ra]})
    assert ei.value.code == "ERR_RECORD_SHAPE"


def test_duplicate_record_same_market_metric_fail_closed():
    ra = _rec("Tokyo", "overnight", 100, row=0)
    dup = _rec("Tokyo", "overnight", 101, row=1)
    with pytest.raises(_Reject) as ei:
        _index_records({"records": [ra, dup]})
    assert ei.value.code == "ERR_DUPLICATE_RECORD"


@pytest.mark.parametrize(
    "comparable,reason",
    [(True, "period_mismatch"), (False, "comparable")],
)
def test_comparable_reason_inconsistency_fail_closed(comparable, reason):
    # internal invariant: comparable is True <=> reason == "comparable". A broken pair is
    # corrupt State and must not flow into the brief as the opposite result.
    ra = _rec("Tokyo", "overnight", 100)
    rb = _rec("Osaka", "overnight", 80)
    dec = _decision("overnight", comparable, reason, ra["source_ref"], rb["source_ref"])
    with pytest.raises(_Reject) as ei:
        _validate_decisions({"decisions": [dec]})
    assert ei.value.code == "ERR_COMPARABILITY_SHAPE"


def test_comparable_reason_inconsistency_partial_free_via_execute(spy):
    ra = _rec("Tokyo", "overnight", 100)
    rb = _rec("Osaka", "overnight", 80)
    dec = _decision("overnight", True, "unit_incompatible", ra["source_ref"], rb["source_ref"])
    out = BriefGenerateNode().execute(_state([ra, rb], [dec]))
    assert out["status"] == "error"
    assert "result" not in out and "comparison_outcome" not in out
    assert "ERR_COMPARABILITY_SHAPE" in out["error_log"][0]


def test_execute_malformed_normalized_markets_partial_free(spy):
    dec = [_decision("m", False, "metric_undefined")]
    # 0 comparable so records aren't strictly needed, but normalized_markets shape is still
    # validated -> a broken shape is fail-closed with no partial result
    s = _state([], dec)
    s["normalized_markets"] = "broken"
    out = BriefGenerateNode().execute(s)
    assert out["status"] == "error"
    assert set(out) == {"status", "error_log"}
    assert "ERR_NORMALIZED_SHAPE" in out["error_log"][0]
