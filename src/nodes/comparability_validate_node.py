"""AgentCore Platform v1.0"""

# Inner-graph node: decide, per requested metric, whether the two normalised markets
# are comparable against the approved metric-definition sheet's declared rules only.
# See docs/02_design.md (`comparability`).
#
# Scope: comparability DECISION only. This node does NOT generate a brief,
# rank/recommend/forecast, project to caller-facing output, or convert any value — even
# when the sheet declares a unit conversion, comparability is decided but the figure is
# never rewritten. Per design, a declared conversion only makes the units COMPARABLE; the
# `brief` node then shows the supplied values verbatim side by side and does NOT convert
# them either. (Applying a conversion factor to displayed figures would be a separate issue
# that must re-design citation + conversion-rule display.)
#
# Node contract: extend FunctionNode; override execute(state) -> dict returning ONLY
# this node's owned delta ({"comparability": ..., "status": ...}). It never writes
# `comparison_outcome`, `result`, or `formatted_output`. required_trust_level = ANONYMOUS
# — S-1 is enforced at the outer boundary; this is an inner node (do not raise it here).
# Zero comparable metrics is a normal success outcome (the downstream `brief` node maps it
# to `no_comparable_metrics`), NOT an error.
#
# Inputs (from inner State):
#   - normalized_markets  = {"records": [<canonical record>, ...]}  (produced by `normalize`)
#   - metric_definitions  = {"version": str, "metrics": [<metric def>, ...]}
#   - validated_input["requested_metrics"] = ordered list of requested metric ids
#
# fail-closed: a structurally broken normalized_markets / metric_definitions, a market
# set that is not exactly two, a missing definition version, a malformed citation, or a
# malformed requested_metrics list is treated as corrupt State -> {status: error} with a
# non-sensitive code only (no partial `comparability`, no market data / numbers in the
# error_log or audit event). Ordinary business non-comparability is NOT an error — it is
# returned as a per-metric reason under status:success.

import math
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import explain
from src.services.progress import emit_progress

# Per-metric non-comparability reasons + the comparable sentinel (design ReasonEnum).
_REASON_COMPARABLE = "comparable"
_REASON_PERIOD = "period_mismatch"
_REASON_POPULATION = "population_mismatch"
_REASON_UNIT = "unit_incompatible"
_REASON_UNDEFINED = "metric_undefined"
_REASON_MISSING_META = "missing_required_metadata"
_REASON_ABSENT = "metric_absent"

_ALLOWED_ALIGNMENT = {"exact", "same_year", "none"}


class _Reject(Exception):
    """Internal fail-closed violation carrying a non-sensitive reason code.

    Never leaves the node — execute() converts it into ``status: error`` + an
    ``error_log`` entry that contains only the code, never raw State data.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _require(cond: Any, code: str) -> None:
    if not cond:
        raise _Reject(code)


def _nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and bool(v)


# Bound on integer magnitude: fits a msgpack signed int64 (< 2**63) and is far above any
# realistic aggregate count. Mirrors the canonical `value` contract enforced upstream by
# `normalize`; a record value or conversion factor outside this bound is corrupt State,
# not a business outcome, and is rejected fail-closed here.
_MAX_INT_MAGNITUDE = 10**18


def _is_number_or_null(v: Any) -> bool:
    # JSON/msgpack-safe number: None, a bounded finite int, or a finite float. `bool` is
    # excluded; NaN/Infinity are rejected (isfinite); over-large ints are rejected by
    # magnitude WITHOUT calling isfinite on them (isfinite(huge_int) raises OverflowError).
    if v is None:
        return True
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return abs(v) < _MAX_INT_MAGNITUDE
    if isinstance(v, float):
        return math.isfinite(v)
    return False


def _is_finite_number(v: Any) -> bool:
    """A number (not None) passing the JSON/msgpack-safe check above."""
    return v is not None and _is_number_or_null(v)


def _valid_source_ref(ref: Any) -> bool:
    # citation shape: {source_id:str, table:str, row:int}. bool is excluded for row
    # (bool is an int subclass but is not a row index).
    return (
        isinstance(ref, dict)
        and set(ref.keys()) == {"source_id", "table", "row"}
        and _nonempty_str(ref["source_id"])
        and _nonempty_str(ref["table"])
        and isinstance(ref["row"], int)
        and not isinstance(ref["row"], bool)
    )


def _safe_year(period: str) -> str | None:
    """Return the safe leading 4-digit year of an ISO-like period, or None when the
    period cannot be interpreted safely.

    Only a specified, safe shape is accepted: the string must start with exactly four
    digits followed by end-of-string or an ISO separator (``-`` or ``/``), e.g.
    ``"2024"``, ``"2024-01"``, ``"2024-01/2024-12"`` -> ``"2024"``. Anything else
    (``"FY2024"``, ``"24-01"``, ``"last_year"``, ``"20244"``) is uninterpretable ->
    None, which the caller turns into ``period_mismatch``. No guessing.
    """
    if not isinstance(period, str) or len(period) < 4:
        return None
    head = period[:4]
    if not head.isdigit():
        return None
    # `.isdigit()` also accepts some non-ASCII digits; restrict to ASCII 0-9.
    if not all("0" <= ch <= "9" for ch in head):
        return None
    if len(period) == 4 or period[4] in ("-", "/"):
        return head
    return None


def _validate_definitions(metric_definitions: Any) -> dict[str, Any]:
    """Validate the canonical metric-definition sheet and index it by metric id.

    Returns ``{metric_id: metric_def}``. Raises ``_Reject`` on any structural
    corruption (these come from the upstream ``normalize`` node; a broken shape here
    means corrupt State, not a business outcome).
    """
    _require(isinstance(metric_definitions, dict), "ERR_DEFINITIONS_SHAPE")
    _require(_nonempty_str(metric_definitions.get("version")), "ERR_DEFINITION_VERSION")
    defs = metric_definitions.get("metrics")
    _require(isinstance(defs, list), "ERR_DEFINITIONS_SHAPE")
    by_metric: dict[str, Any] = {}
    for d in defs:
        _require(isinstance(d, dict), "ERR_DEFINITION_SHAPE")
        metric = d.get("metric")
        _require(_nonempty_str(metric), "ERR_DEFINITION_SHAPE")
        _require(metric not in by_metric, "ERR_DUPLICATE_DEFINITION")
        _require(isinstance(d.get("eligible"), bool), "ERR_DEFINITION_SHAPE")
        _require(d.get("required_period_alignment") in _ALLOWED_ALIGNMENT, "ERR_DEFINITION_SHAPE")
        req_pop = d.get("required_population")
        _require(req_pop is None or isinstance(req_pop, str), "ERR_DEFINITION_SHAPE")
        conv = d.get("allowed_unit_conversions", [])
        _require(isinstance(conv, list), "ERR_DEFINITION_SHAPE")
        for c in conv:
            # exact-key canonical shape {from:str, to:str, factor:finite number} — same
            # contract `normalize` enforces. An incomplete/non-finite/extra-key conversion
            # is a broken approved rule, not a business outcome: fail closed. (The factor
            # itself is never applied here — comparability is decided, values untouched.)
            _require(
                isinstance(c, dict)
                and set(c.keys()) == {"from", "to", "factor"}
                and isinstance(c["from"], str)
                and isinstance(c["to"], str)
                and _is_finite_number(c["factor"]),
                "ERR_DEFINITION_SHAPE",
            )
        by_metric[metric] = d
    return by_metric


def _index_records(normalized_markets: Any) -> tuple[dict[tuple[Any, Any], Any], list[Any]]:
    """Validate normalized_markets and build ``{(market, metric): record}`` plus the
    ordered pair of market names (first-seen order = market_a, market_b).

    Raises ``_Reject`` on structural corruption or a market set that is not exactly two.
    """
    _require(isinstance(normalized_markets, dict), "ERR_NORMALIZED_SHAPE")
    records = normalized_markets.get("records")
    _require(isinstance(records, list), "ERR_NORMALIZED_SHAPE")
    by_key: dict[tuple[Any, Any], Any] = {}
    order: list[Any] = []
    for rec in records:
        _require(isinstance(rec, dict), "ERR_RECORD_SHAPE")
        market = rec.get("market")
        metric = rec.get("metric")
        _require(_nonempty_str(market), "ERR_RECORD_SHAPE")
        _require(_nonempty_str(metric), "ERR_RECORD_SHAPE")
        # value must match the canonical contract (number|null, finite, bounded, non-bool).
        # A non-numeric / NaN / Infinity / over-large value is corrupt State, not a
        # business non-comparability — fail closed rather than let it reach `_decide`.
        _require("value" in rec and _is_number_or_null(rec["value"]), "ERR_RECORD_SHAPE")
        _require(_nonempty_str(rec.get("unit")), "ERR_RECORD_SHAPE")
        _require(_nonempty_str(rec.get("period")), "ERR_RECORD_SHAPE")
        _require(_nonempty_str(rec.get("population")), "ERR_RECORD_SHAPE")
        _require(_valid_source_ref(rec.get("source_ref")), "ERR_CITATION_SHAPE")
        key = (market, metric)
        _require(key not in by_key, "ERR_DUPLICATE_RECORD")
        by_key[key] = rec
        if market not in order:
            order.append(market)
    _require(len(order) == 2, "ERR_MARKET_COUNT")
    return by_key, order


def _decide(metric: str, rec_a: Any, rec_b: Any, defn: Any) -> dict[str, Any]:
    """Decide comparability for one requested metric, applying the FIXED priority order.

    ``rec_a`` / ``rec_b`` are the canonical records for market_a / market_b (or None
    when the metric is absent from that market). ``defn`` is the metric definition (or
    None when the sheet does not define the metric).

    Citation retention: a ``source_ref`` is kept for each side that HAS a record (even
    when non-comparable / null-valued); the absent side is ``None``. Values are never
    read into the decision or rewritten — comparability is decided from metadata only.
    """
    ref_a = rec_a["source_ref"] if rec_a is not None else None
    ref_b = rec_b["source_ref"] if rec_b is not None else None

    def out(comparable: bool, reason: str) -> dict[str, Any]:
        return {
            "metric": metric,
            "comparable": comparable,
            "reason": reason,
            "market_a_ref": ref_a,
            "market_b_ref": ref_b,
        }

    # 1. definition missing or not eligible.
    if defn is None or defn.get("eligible") is not True:
        return out(False, _REASON_UNDEFINED)

    # 2. metric absent on either side. A `null` value is present metadata but NOT a
    #    comparable number, so it is treated as absent (safe-comparison principle).
    if rec_a is None or rec_b is None or rec_a.get("value") is None or rec_b.get("value") is None:
        return out(False, _REASON_ABSENT)

    # 3. required period/population/unit metadata present on both records (defensive:
    #    normalize guarantees these, but a corrupt-but-shaped record fails closed here
    #    as a business non-comparability rather than a comparison on missing metadata).
    for rec in (rec_a, rec_b):
        if not all(_nonempty_str(rec.get(k)) for k in ("period", "population", "unit")):
            return out(False, _REASON_MISSING_META)

    # 4. population: only gated when the definition declares required_population; then
    #    BOTH markets must match it. An undeclared required_population imposes no gate.
    req_pop = defn.get("required_population")
    if req_pop is not None and (rec_a["population"] != req_pop or rec_b["population"] != req_pop):
        return out(False, _REASON_POPULATION)

    # 5. period alignment per the definition.
    align = defn.get("required_period_alignment")
    if align == "exact":
        if rec_a["period"] != rec_b["period"]:
            return out(False, _REASON_PERIOD)
    elif align == "same_year":
        ya = _safe_year(rec_a["period"])
        yb = _safe_year(rec_b["period"])
        if ya is None or yb is None or ya != yb:
            return out(False, _REASON_PERIOD)
    # align == "none": no period comparison.

    # 6. unit: identical units pass; otherwise an explicitly declared conversion (in
    #    either direction) between the two units makes them comparable. The value is
    #    NOT converted here — only comparability is decided.
    ua = rec_a["unit"]
    ub = rec_b["unit"]
    if ua != ub:
        convs = defn.get("allowed_unit_conversions", [])
        convertible = any((c["from"] == ua and c["to"] == ub) or (c["from"] == ub and c["to"] == ua) for c in convs)
        if not convertible:
            return out(False, _REASON_UNIT)

    return out(True, _REASON_COMPARABLE)


def _build_comparability(normalized_markets: Any, metric_definitions: Any, requested_metrics: Any) -> dict[str, Any]:
    """Pure comparability decision over the requested metrics, in requested order.

    Returns ``{"decisions": [<decision>, ...]}`` — exactly one decision per requested
    entry, order preserved. Raises ``_Reject`` on corrupt State.
    """
    _require(
        isinstance(requested_metrics, list)
        and len(requested_metrics) > 0
        and all(_nonempty_str(m) for m in requested_metrics),
        "ERR_REQUESTED_METRICS",
    )
    defs_by_metric = _validate_definitions(metric_definitions)
    by_key, (market_a, market_b) = _index_records(normalized_markets)

    decisions = []
    for metric in requested_metrics:
        rec_a = by_key.get((market_a, metric))
        rec_b = by_key.get((market_b, metric))
        decisions.append(_decide(metric, rec_a, rec_b, defs_by_metric.get(metric)))
    return {"decisions": decisions}


class ComparabilityValidateNode(FunctionNode):
    """Deterministic per-metric comparability decision against the definition sheet."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        validated_input = state.get("validated_input")
        try:
            _require(isinstance(validated_input, dict), "ERR_VALIDATED_INPUT")
            assert isinstance(validated_input, dict)  # _require で保証済み（型検査器向け）
            comparability = _build_comparability(
                state.get("normalized_markets"),
                state.get("metric_definitions"),
                validated_input.get("requested_metrics"),
            )
        except _Reject as r:
            code = r.code
        else:
            decisions = comparability["decisions"]
            # counts only — never observed values, market data, or CSV content
            emit_trace_event(
                "comparability_validated",
                {
                    "requested": len(decisions),
                    "comparable": sum(1 for d in decisions if d["comparable"]),
                },
                state,
            )
            return {"comparability": comparability, "status": AgentStatus.SUCCESS.value}
        # fail-closed: no partial `comparability`, no raw State echoed in log/event
        emit_trace_event("comparability_rejected", {"reason_code": code}, state)
        emit_progress(explain(code))
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"ComparabilityValidateNode: {code}"],
        }
