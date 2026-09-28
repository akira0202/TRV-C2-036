"""AgentCore Platform v1.0"""

# Inner-graph node: normalise the two supplied market tables + the metric-definition
# sheet into canonical records, record minimal citation provenance, and stamp the
# effective definition-sheet version. See docs/02_design.md (`normalize`).
#
# Node contract: extend FunctionNode; override execute(state) -> dict returning ONLY
# this node's delta. required_trust_level = ANONYMOUS — S-1 is enforced at the outer
# boundary; this is an inner node (do not raise it here). Domain S-2 structural
# sanitisation of the untrusted supplied envelope is added via
# _extra_security_gate_input(); prompt-injection detection is NOT done here (that is
# the S-3 terminal output gate's responsibility). No external fetch, no inference of
# missing values, no export reconstruction, no traveller-level data.

import math
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.degradation import is_structural_screen
from src.services.failure_message import explain
from src.services.progress import emit_progress

# --- structural S-2 limits for the untrusted supplied envelope ---
_MAX_STR_LEN = 10_000
_MAX_DEPTH = 8
_MAX_NODES = 50_000
# C0 controls never valid inside a supplied data leaf (NUL + the rest of C0 except
# TAB/LF/CR) plus DEL.
_FORBIDDEN_CTRL = frozenset({c for c in range(0x00, 0x20) if c not in (0x09, 0x0A, 0x0D)} | {0x7F})

_ALLOWED_TOP_KEYS = {"markets", "metric_definition_sheet", "requested_metrics"}
_ALLOWED_ALIGNMENT = {"exact", "same_year", "none"}


class _Reject(Exception):
    """Internal fail-closed violation carrying a non-sensitive reason code.

    Never leaves the node — callers convert it into ``status: error`` + a
    ``error_log`` entry that contains only the code, never raw input.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _screen_structural(value: Any, depth: int = 0, budget: Any = None) -> None:
    """Reject NUL/C0/DEL, oversize strings, over-deep/over-large or non-JSON
    structures. Raises ``_Reject`` on violation; returns ``None`` on success."""
    if budget is None:
        budget = [_MAX_NODES]
    if depth > _MAX_DEPTH:
        raise _Reject("ERR_STRUCTURE_TOO_DEEP")
    budget[0] -= 1
    if budget[0] < 0:
        raise _Reject("ERR_STRUCTURE_TOO_LARGE")
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return
    if isinstance(value, str):
        if len(value) > _MAX_STR_LEN:
            raise _Reject("ERR_STRING_TOO_LONG")
        if any(ord(ch) in _FORBIDDEN_CTRL for ch in value):
            raise _Reject("ERR_CONTROL_CHAR")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise _Reject("ERR_NON_STRING_KEY")
            _screen_structural(k, depth + 1, budget)
            _screen_structural(v, depth + 1, budget)
        return
    if isinstance(value, (list, tuple)):
        for v in value:
            _screen_structural(v, depth + 1, budget)
        return
    raise _Reject("ERR_NON_JSON_TYPE")


# Bound on integer magnitude: fits a msgpack signed int64 (< 2**63 ≈ 9.2e18) and is far
# above any realistic aggregate count. Kept as an explicit digit-scale limit so a huge
# JSON integer is rejected fail-closed rather than crashing `math.isfinite` (which raises
# OverflowError when Python converts an over-large int to float).
_MAX_INT_MAGNITUDE = 10**18


def _is_number_or_null(v: Any) -> bool:
    # JSON/msgpack-safe number: a bounded finite int, a finite float, or None. `bool` is
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


def _require(cond: Any, code: str) -> None:
    if not cond:
        raise _Reject(code)


def _validate_and_normalize(vi: Any) -> dict[str, Any]:
    """Validate the supplied envelope against the V1 input contract and normalise it.

    Returns the node's State delta (``normalized_markets`` / ``metric_definitions`` /
    ``provenance`` / ``definition_sheet_version``) on success; raises ``_Reject(code)``
    on any contract violation. Pure — no I/O, no inference of missing values, no
    fabricated citations. Unsupported formats (Excel/PDF/image) and free-text arrive
    as a non-conforming shape and are rejected by the shape checks below.
    """
    _require(isinstance(vi, dict), "ERR_INPUT_NOT_OBJECT")
    _require(set(vi.keys()) <= _ALLOWED_TOP_KEYS, "ERR_UNKNOWN_TOP_KEY")

    requested = vi.get("requested_metrics")
    _require(
        isinstance(requested, list) and len(requested) > 0 and all(isinstance(m, str) and m for m in requested),
        "ERR_REQUESTED_METRICS",
    )

    sheet = vi.get("metric_definition_sheet")
    _require(isinstance(sheet, dict), "ERR_SHEET_SHAPE")
    version = sheet.get("version")
    _require(isinstance(version, str) and version, "ERR_SHEET_VERSION")
    defs = sheet.get("metrics")
    _require(isinstance(defs, list), "ERR_SHEET_METRICS")
    norm_defs = []
    seen_defs = set()
    for d in defs:
        _require(isinstance(d, dict), "ERR_METRICDEF_SHAPE")
        _require(isinstance(d.get("metric"), str) and d.get("metric"), "ERR_METRICDEF_METRIC")
        _require(d["metric"] not in seen_defs, "ERR_DUPLICATE_METRIC_DEF")
        seen_defs.add(d["metric"])
        _require(isinstance(d.get("semantics"), str), "ERR_METRICDEF_SEMANTICS")
        _require(d.get("required_period_alignment") in _ALLOWED_ALIGNMENT, "ERR_METRICDEF_ALIGNMENT")
        _require(isinstance(d.get("eligible"), bool), "ERR_METRICDEF_ELIGIBLE")
        req_pop = d.get("required_population")
        _require(req_pop is None or isinstance(req_pop, str), "ERR_METRICDEF_POPULATION")
        conv = d.get("allowed_unit_conversions", [])
        _require(isinstance(conv, list), "ERR_METRICDEF_CONVERSIONS")
        norm_conv = []
        for c in conv:
            # exact-key allowlist — no unknown/nested keys survive into canonical State
            _require(
                isinstance(c, dict)
                and set(c.keys()) == {"from", "to", "factor"}
                and isinstance(c["from"], str)
                and isinstance(c["to"], str)
                and _is_finite_number(c["factor"]),
                "ERR_METRICDEF_CONVERSION_SHAPE",
            )
            norm_conv.append({"from": c["from"], "to": c["to"], "factor": c["factor"]})
        norm_defs.append(
            {
                "metric": d["metric"],
                "semantics": d["semantics"],
                "required_period_alignment": d["required_period_alignment"],
                "required_population": d.get("required_population"),
                "allowed_unit_conversions": norm_conv,
                "eligible": d["eligible"],
            }
        )

    markets = vi.get("markets")
    _require(isinstance(markets, list) and len(markets) == 2, "ERR_MARKET_COUNT")
    records = []
    provenance = {}
    seen = set()
    for mkt in markets:
        _require(isinstance(mkt, dict), "ERR_MARKET_SHAPE")
        name = mkt.get("market")
        _require(isinstance(name, str) and name, "ERR_MARKET_NAME")
        _require(name not in seen, "ERR_MARKET_DUPLICATE")
        seen.add(name)
        period = mkt.get("reporting_period")
        pop = mkt.get("population_coverage")
        unit = mkt.get("unit")
        _require(isinstance(period, str) and period, "ERR_MARKET_PERIOD")
        _require(isinstance(pop, str) and pop, "ERR_MARKET_POPULATION")
        _require(isinstance(unit, str) and unit, "ERR_MARKET_UNIT")
        rows = mkt.get("rows")
        _require(isinstance(rows, list), "ERR_MARKET_ROWS")
        src = mkt.get("source")
        _require(isinstance(src, dict), "ERR_SOURCE_SHAPE")
        source_id = src.get("source_id")
        export_date = src.get("export_date")
        _require(isinstance(source_id, str) and source_id, "ERR_SOURCE_ID")
        _require(isinstance(export_date, str) and export_date, "ERR_SOURCE_EXPORT_DATE")
        s_title = src.get("title")
        s_url = src.get("url")
        s_footnotes = src.get("footnotes")
        _require(s_title is None or isinstance(s_title, str), "ERR_SOURCE_TITLE")
        _require(s_url is None or isinstance(s_url, str), "ERR_SOURCE_URL")
        _require(
            s_footnotes is None or (isinstance(s_footnotes, list) and all(isinstance(f, str) for f in s_footnotes)),
            "ERR_SOURCE_FOOTNOTES",
        )
        seen_row_metrics = set()
        for i, row in enumerate(rows):
            _require(isinstance(row, dict), "ERR_ROW_SHAPE")
            metric = row.get("metric")
            _require(isinstance(metric, str) and metric, "ERR_ROW_METRIC")
            _require(metric not in seen_row_metrics, "ERR_DUPLICATE_METRIC_ROW")
            seen_row_metrics.add(metric)
            _require("value" in row and _is_number_or_null(row["value"]), "ERR_ROW_VALUE")
            r_unit = row.get("unit", unit)
            r_period = row.get("period", period)
            _require(isinstance(r_unit, str) and r_unit, "ERR_ROW_UNIT")
            _require(isinstance(r_period, str) and r_period, "ERR_ROW_PERIOD")
            records.append(
                {
                    "market": name,
                    "metric": metric,
                    "value": row["value"],
                    "unit": r_unit,
                    "period": r_period,
                    "population": pop,
                    "source_ref": {"source_id": source_id, "table": name, "row": i},
                }
            )
        # minimal citation provenance only — never the raw rows / payload / CSV text
        prov = {
            "source_id": source_id,
            "export_date": export_date,
            "reporting_period": period,
            "market": name,
            "population": pop,
            "unit": unit,
        }
        for opt in ("title", "url", "footnotes"):
            if opt in src:
                prov[opt] = src[opt]
        provenance[name] = prov

    return {
        "normalized_markets": {"records": records},
        "metric_definitions": {"version": version, "metrics": norm_defs},
        "provenance": provenance,
        "definition_sheet_version": version,
    }


class InputNormalizeProvenanceNode(FunctionNode):
    """Normalise supplied market tables + metric-definition sheet; record provenance."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def _extra_security_gate_input(self, state: dict[str, Any]) -> dict[str, Any]:
        # Domain S-2: structural sanitisation of the untrusted supplied envelope
        # (NUL/C0/DEL, oversize strings, over-deep/over-large structures). No PII or
        # prompt-injection detection here — the data is aggregate, and injection
        # screening is the S-3 terminal output gate's responsibility. Rejection sets
        # status:error and returns the (full) state, so __call__ short-circuits.
        vi = state.get("validated_input")
        if vi is None:
            return state
        try:
            _screen_structural(vi)
        except _Reject as r:
            code = r.code
        except RecursionError:
            code = "ERR_RECURSION"
        else:
            return state
        emit_trace_event("input_structural_reject", {"reason_code": code}, state)
        state["status"] = AgentStatus.ERROR.value
        state["error_log"] = state.get("error_log", []) + [f"InputNormalizeProvenanceNode: {code}"]
        return state

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        vi = state.get("validated_input")
        try:
            _screen_structural(vi)  # defense-in-depth (also runs in the S-2 hook)
            delta = _validate_and_normalize(vi)
        except _Reject as r:
            code = r.code
        except RecursionError:
            code = "ERR_RECURSION"
        else:
            emit_trace_event(
                "input_normalized",
                {
                    "records": len(delta["normalized_markets"]["records"]),
                    "definition_sheet_version": delta["definition_sheet_version"],
                },
                state,
            )
            delta["status"] = AgentStatus.SUCCESS.value
            return delta
        # No partial normalized_markets / provenance, and no raw input echoed, either way.
        #
        # The structural screen refuses a payload that is not really a document (oversized,
        # over-nested, control-character-bearing, non-JSON-typed). That refusal already ran in the
        # S-2 hook before execute(); reaching it again here must not turn it into a completed
        # request, so it stays fail-closed. Everything else this node raises is the first look at
        # the caller's own tables, sources and definition sheet — theirs to correct, so the run
        # completes carrying the code and the terminal gate states the reason.
        if is_structural_screen(code):
            emit_trace_event("input_rejected", {"reason_code": code}, state)
            emit_progress(explain(code))
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"InputNormalizeProvenanceNode: {code}"],
            }
        emit_trace_event("input_degraded", {"reason_code": code}, state)
        return {"diagnostic_code": code, "status": AgentStatus.SUCCESS.value}
