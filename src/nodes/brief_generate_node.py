"""AgentCore Platform v1.0"""

# Inner-graph node: bounded comparison synthesis + cited human-review brief generation.
# See docs/02_design.md (`brief`).
#
# Scope: comparison synthesis + cited brief (a "cited draft") only. This node does NOT
# write caller-facing `formatted_output`, does NOT run the terminal S-3 egress gate, does
# NOT wire the graph, and does NOT deploy. Final scrubbing of forecast / ranking /
# recommendation / injection-derived expressions from the draft is the OUTER post_process
# terminal S-3's responsibility (a later issue) — this node produces the cited draft only.
#
# Node contract: extend FunctionNode; override execute(state) -> dict returning ONLY this
# node's owned delta ({"result": ..., "comparison_outcome": ..., "status": ...}). It never
# writes `formatted_output`. required_trust_level = ANONYMOUS — S-1 is enforced at the outer
# boundary; this is an inner node (do not raise it here).
#
# Inputs (from inner State):
#   - comparability            = {"decisions": [<decision>, ...]}  (produced by `comparability`)
#   - normalized_markets       = {"records": [<canonical record>, ...]}  (produced by `normalize`)
#   - provenance               = {market: {source_id, export_date, ...}}  (produced by `normalize`)
#   - definition_sheet_version = str  (produced by `normalize`)
#
# Three terminal outcomes (this node decides outcomes 2/3; outcome 1 = reject):
#   1. Reject      — corrupt State, citation inconsistency, or missing definition version ->
#                    {status: error}, NO partial `result`.
#   2. comparison  — >=1 comparable -> {status: success, comparison_outcome: "comparison",
#                    result: <cited brief>}.
#   3. no_comparable_metrics — 0 comparable -> {status: success,
#                    comparison_outcome: "no_comparable_metrics", result: <validation report>}
#                    (NO brief_markdown).
#
# Authoritative data (values, units, periods, citations, comparability) is taken verbatim
# from the canonical records and the comparability decisions — NEVER computed and NEVER
# sourced from the optional LLM. No value conversion, averaging, differencing, percentage,
# ranking, forecasting, or recommendation is produced here.

import math
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import GENERATION_DEGRADED
from src.services.progress import emit_progress
from src.services.failure_message import explain

_OUTCOME_COMPARISON = "comparison"
_OUTCOME_NONE = "no_comparable_metrics"

# ReasonEnum (mirrors ComparabilityValidateNode / docs/02) -> the supplied input field the
# exclusion is grounded in (for excluded_metrics[].source_field).
_REASON_SOURCE_FIELD = {
    "comparable": "",
    "period_mismatch": "reporting_period",
    "population_mismatch": "population_coverage",
    "unit_incompatible": "unit",
    "metric_undefined": "metric_definition_sheet",
    "missing_required_metadata": "metadata",
    "metric_absent": "rows",
}
_KNOWN_REASONS = frozenset(_REASON_SOURCE_FIELD)

# Comparative / ranking / forecast / recommendation / superiority vocabulary that must never
# appear in the brief. Optional-LLM prose is admitted only as a NEUTRAL source-grounded
# restatement — any evaluative or comparative claim ("higher", "more ... than", "grew",
# "winner", "recommend", ...) is rejected, since the human reviewer draws the comparison, not
# the node. English terms are matched on word boundaries (so "win" does not fire on "window"
# nor "up" on "supplied"); Japanese terms are matched as substrings (no word boundaries).
# Over-rejection is safe: an unverifiable prose is simply omitted and the deterministic brief
# still succeeds.
_BANNED_EN_RE = re.compile(
    r"\b("
    r"winner|wins?|best|better|worse|worst|superior|inferior|leads?|leader|ranks?|ranking|"
    r"top|forecasts?|predicts?|projection|growth|grew|grows?|recommends?|recommendation|"
    r"invest|spend|campaign|promising|outperforms?|advantage|higher|lower|highest|lowest|"
    r"more|less|most|least|increased?|increases|decreased?|decreases|declines?|rose|fell|"
    r"surges?|drops?|strong|stronger|strongest|weak|weaker|significant|significantly|"
    r"notable|notably|exceeds?|fewer|greater|larger|smaller|trend|versus|vs|than|up|down"
    r")\b",
    re.IGNORECASE,
)
_BANNED_JA = (
    "優",
    "順位",
    "推奨",
    "おすすめ",
    "予測",
    "成長",
    "見込",
    "有望",
    "勝",
    "上回",
    "下回",
    "ランキング",
    "多い",
    "少ない",
    "高い",
    "低い",
    "増加",
    "減少",
    "傾向",
    "勧",
)
_DIGITS = re.compile(r"\d+")


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


# Bound on integer magnitude: fits a msgpack signed int64 and matches the canonical `value`
# contract enforced upstream by `normalize` / `comparability`.
_MAX_INT_MAGNITUDE = 10**18


def _is_finite_number(v: Any) -> bool:
    # A finite, bounded, non-bool number. bool excluded; NaN/Infinity rejected; over-large
    # ints rejected by magnitude WITHOUT calling isfinite on them (OverflowError-safe).
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return abs(v) < _MAX_INT_MAGNITUDE
    if isinstance(v, float):
        return math.isfinite(v)
    return False


def _valid_source_ref(ref: Any) -> bool:
    return (
        isinstance(ref, dict)
        and set(ref.keys()) == {"source_id", "table", "row"}
        and _nonempty_str(ref["source_id"])
        and _nonempty_str(ref["table"])
        and isinstance(ref["row"], int)
        and not isinstance(ref["row"], bool)
    )


def _md_cell(v: Any) -> str:
    """Render a value into a Markdown table cell, escaping pipes / newlines so supplied
    strings cannot break the table structure. Values are shown verbatim (no formatting,
    rounding, or computation)."""
    return str(v).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _index_records(normalized_markets: Any) -> dict[tuple[Any, Any], Any]:
    """Validate normalized_markets and index canonical records by (market, metric).

    Raises ``_Reject`` on structural / citation corruption.
    """
    _require(isinstance(normalized_markets, dict), "ERR_NORMALIZED_SHAPE")
    records = normalized_markets.get("records")
    _require(isinstance(records, list), "ERR_NORMALIZED_SHAPE")
    by_key: dict[tuple[Any, Any], Any] = {}
    for rec in records:
        _require(isinstance(rec, dict), "ERR_RECORD_SHAPE")
        market = rec.get("market")
        metric = rec.get("metric")
        _require(_nonempty_str(market) and _nonempty_str(metric), "ERR_RECORD_SHAPE")
        _require(_nonempty_str(rec.get("unit")), "ERR_RECORD_SHAPE")
        _require(_nonempty_str(rec.get("period")), "ERR_RECORD_SHAPE")
        _require("value" in rec, "ERR_RECORD_SHAPE")
        _require(_valid_source_ref(rec.get("source_ref")), "ERR_CITATION_SHAPE")
        key = (market, metric)
        _require(key not in by_key, "ERR_DUPLICATE_RECORD")
        by_key[key] = rec
    return by_key


def _validate_decisions(comparability: Any) -> list[Any]:
    """Validate the comparability payload and return the ordered decision list."""
    _require(isinstance(comparability, dict), "ERR_COMPARABILITY_SHAPE")
    decisions = comparability.get("decisions")
    _require(isinstance(decisions, list), "ERR_COMPARABILITY_SHAPE")
    for d in decisions:
        _require(isinstance(d, dict), "ERR_COMPARABILITY_SHAPE")
        _require(_nonempty_str(d.get("metric")), "ERR_COMPARABILITY_SHAPE")
        _require(isinstance(d.get("comparable"), bool), "ERR_COMPARABILITY_SHAPE")
        _require(d.get("reason") in _KNOWN_REASONS, "ERR_COMPARABILITY_SHAPE")
        # internal invariant: comparable is True <=> reason == "comparable". A broken pair
        # (e.g. comparable=True with reason="period_mismatch") is corrupt State and must not
        # flow into the brief as the opposite result — fail closed.
        _require(d["comparable"] == (d["reason"] == "comparable"), "ERR_COMPARABILITY_SHAPE")
        for ref_key in ("market_a_ref", "market_b_ref"):
            ref = d.get(ref_key)
            _require(ref is None or _valid_source_ref(ref), "ERR_CITATION_SHAPE")
    return list(decisions)


def _compared_entry(decision: Any, index: Any) -> dict[str, Any]:
    """Build one compared_metrics entry from a comparable decision + canonical records.

    Both markets' citations are mandatory; a missing/inconsistent citation on EITHER side
    is fail-closed (``_Reject``). Values/units/periods are copied verbatim — never computed.
    """
    metric = decision["metric"]
    ref_a = decision.get("market_a_ref")
    ref_b = decision.get("market_b_ref")
    # a comparable decision MUST carry both citations
    _require(_valid_source_ref(ref_a) and _valid_source_ref(ref_b), "ERR_CITATION_MISSING")

    sides = []
    for ref in (ref_a, ref_b):
        rec = index.get((ref["table"], metric))
        # citation must resolve to a canonical record whose own source_ref matches exactly
        _require(rec is not None and rec.get("source_ref") == ref, "ERR_CITATION_MISMATCH")
        _require(_is_finite_number(rec.get("value")), "ERR_COMPARABLE_VALUE")
        sides.append({"value": rec["value"], "unit": rec["unit"], "period": rec["period"]})

    return {
        "metric": metric,
        "market_a": sides[0],
        "market_b": sides[1],
        # citations copied verbatim from the canonical records (== the decision refs)
        "citations": [dict(ref_a), dict(ref_b)],
    }


def _prose_is_source_grounded(prose: Any, compared: Any, version: Any) -> bool:
    """True only if optional-LLM prose is a safe, source-grounded restatement.

    Rejects (returns False) any prose that: contains a URL; contains ranking / forecast /
    recommendation / superiority vocabulary; or contains a digit-run not present anywhere in
    the canonical compared_metrics (values, units, periods, citations) or the sheet version.
    This makes admitted prose source-bound by construction — a fabricated figure or citation
    (e.g. a hallucinated "999999" or "fake-src-999") fails the digit check and the prose is
    dropped. Over-rejection is safe: the caller omits the prose and still returns the
    deterministic cited brief.
    """
    low = prose.lower()
    if "http://" in low or "https://" in low or "www." in low:
        return False
    if _BANNED_EN_RE.search(prose) or any(tok in prose for tok in _BANNED_JA):
        return False
    allowed: set[Any] = set()
    allowed.update(_DIGITS.findall(str(version)))
    for c in compared:
        allowed.update(_DIGITS.findall(c["metric"]))
        for side in (c["market_a"], c["market_b"]):
            allowed.update(_DIGITS.findall(str(side["value"])))
            allowed.update(_DIGITS.findall(side["unit"]))
            allowed.update(_DIGITS.findall(side["period"]))
        for cit in c["citations"]:
            allowed.update(_DIGITS.findall(cit["source_id"]))
            allowed.update(_DIGITS.findall(cit["table"]))
            allowed.update(_DIGITS.findall(str(cit["row"])))
    return set(_DIGITS.findall(prose)).issubset(allowed)


def _excluded_entry(decision: Any) -> dict[str, Any]:
    reason = decision["reason"]
    return {
        "metric": decision["metric"],
        "reason": reason,
        "source_field": _REASON_SOURCE_FIELD.get(reason, ""),
    }


def _sources_lines(provenance: Any) -> list[Any]:
    """Best-effort bibliographic source lines from provenance (all supplied data; never
    fabricated). Malformed provenance simply yields no source lines — it is not a reject
    trigger (citations already carry source_id/table/row)."""
    lines: list[Any] = []
    if not isinstance(provenance, dict):
        return lines
    for _market, prov in provenance.items():
        if not isinstance(prov, dict) or not _nonempty_str(prov.get("source_id")):
            continue
        parts = [f"- `{_md_cell(prov['source_id'])}`"]
        if _nonempty_str(prov.get("title")):
            parts.append(_md_cell(prov["title"]))
        if _nonempty_str(prov.get("url")):
            parts.append(f"<{_md_cell(prov['url'])}>")
        if _nonempty_str(prov.get("export_date")):
            parts.append(f"(exported {_md_cell(prov['export_date'])})")
        lines.append(" — ".join(parts) if len(parts) > 1 else parts[0])
    return lines


def _render_markdown(compared: Any, excluded: Any, version: Any, provenance: Any, narrative: Any) -> str:
    """Deterministic cited Markdown draft. Presents supplied figures side by side with
    citations; no diff/ratio/ranking/forecast/recommendation is computed or written."""
    # sentence-cased on purpose: the terminal S-3 gate PII-scans the whole markdown
    # (no display exemption), so avoid consecutive Title-case words in node-authored
    # scaffolding that a name detector could false-positive; real names in supplied/LLM
    # text remain Title-case and are still caught downstream.
    out = ["# Destination market-comparison evidence brief (draft)", ""]
    out.append(f"_Metric-definition sheet version: {_md_cell(version)}_")
    out.append("_Human-review draft. Figures are shown exactly as supplied, side by side, " "for analyst review._")
    out.append("")

    out.append("## Compared metrics")
    out.append("")
    for c in compared:
        a, b = c["market_a"], c["market_b"]
        ca, cb = c["citations"][0], c["citations"][1]
        out.append(f"### {_md_cell(c['metric'])}")
        out.append("")
        out.append("| Market (table) | Value | Unit | Period | Citation |")
        out.append("|---|---|---|---|---|")
        for cit, side in ((ca, a), (cb, b)):
            citation = f"`{_md_cell(cit['source_id'])}` (table {_md_cell(cit['table'])}, row {cit['row']})"
            out.append(
                f"| {_md_cell(cit['table'])} | {_md_cell(side['value'])} | "
                f"{_md_cell(side['unit'])} | {_md_cell(side['period'])} | {citation} |"
            )
        out.append("")

    out.append("## Not comparable / excluded")
    out.append("")
    if excluded:
        out.append("| Metric | Reason | Source field |")
        out.append("|---|---|---|")
        for e in excluded:
            out.append(f"| {_md_cell(e['metric'])} | {_md_cell(e['reason'])} | {_md_cell(e['source_field'])} |")
    else:
        out.append("_None._")
    out.append("")

    source_lines = _sources_lines(provenance)
    if source_lines:
        out.append("## Sources")
        out.append("")
        out.extend(source_lines)
        out.append("")

    out.append("## Draft narrative (non-authoritative — pending review)")
    out.append("")
    out.append(narrative if _nonempty_str(narrative) else "_Not generated._")
    return "\n".join(out)


class BriefGenerateNode(FunctionNode):
    """Bounded comparison synthesis + cited human-review brief (draft) generation."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def __init__(self, llm: Any = None) -> None:
        # `llm` is an OPTIONAL, non-authoritative, read-only injected dependency (a
        # BaseLLM-shaped object). It contributes supplementary prose only and never decides
        # values, citations, or comparability. The graph wires it from config.yaml (`llm`)
        # at graph-composition time (a later issue); absent here by default.
        super().__init__()
        self._llm = llm

    # -- optional, non-authoritative narrative -------------------------------------------

    def _maybe_narrative(self, compared: Any, version: Any) -> str:
        """Return supplementary prose from the optional LLM, or "" on any failure.

        Never on the core path: an absent / erroring / timing-out / mis-shaped LLM yields
        "" and the deterministic brief is still returned successfully. The LLM is given the
        already-decided facts and cannot change values, citations, or comparability. Its raw
        output is admitted into the brief ONLY when it passes `_prose_is_source_grounded()`
        (no fabricated figure/citation, no URL, no ranking/forecast/recommendation) — any
        unverifiable output is dropped to "" while success is maintained. It lands only in
        the clearly-labelled non-authoritative narrative section. (No audit event is emitted
        here — the single per-node domain event is emitted in execute(), carrying whether the
        narrative was generated or omitted.)
        """
        if self._llm is None or not compared:
            emit_progress(GENERATION_DEGRADED)
            return ""
        lines = [
            f"Metric-definition sheet version {version}.",
            "Comparable metrics (values shown as supplied, do not restate figures you were not given):",
        ]
        for c in compared:
            lines.append(
                f"- {c['metric']}: A={c['market_a']['value']} {c['market_a']['unit']} "
                f"({c['market_a']['period']}), B={c['market_b']['value']} {c['market_b']['unit']} "
                f"({c['market_b']['period']})"
            )
        messages = [
            {
                "role": "system",
                "content": (
                    "You write a short, neutral, non-authoritative note for a human reviewer. "
                    "Only restate the supplied comparable metrics in plain language. Do NOT "
                    "invent numbers, sources, or citations; do NOT forecast, rank, recommend "
                    "spend, or state which market is better; do NOT draw causal conclusions."
                ),
            },
            {"role": "user", "content": "\n".join(lines)},
        ]
        try:
            emit_progress("Writing the comparison note...")
            resp = self._llm.complete(messages)
        except Exception:  # noqa: BLE001 — LLM is off the core path; degrade on ANY failure
            emit_progress(GENERATION_DEGRADED)
            return ""
        if isinstance(resp, dict):
            prose = resp.get("content")
        elif isinstance(resp, str):
            prose = resp
        else:
            prose = None
        if not isinstance(prose, str) or not prose.strip():
            return ""
        # admit only source-grounded prose; drop anything unverifiable (fabricated
        # figure/citation, URL, ranking/forecast/recommendation) while keeping success
        return prose if _prose_is_source_grounded(prose, compared, version) else ""

    # -- execute -------------------------------------------------------------------------

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        try:
            version = state.get("definition_sheet_version")
            _require(_nonempty_str(version), "ERR_DEFINITION_VERSION")
            decisions = _validate_decisions(state.get("comparability"))
            index = _index_records(state.get("normalized_markets"))

            comparable = [d for d in decisions if d["comparable"]]
            excluded = [d for d in decisions if not d["comparable"]]

            if comparable:
                compared_entries = [_compared_entry(d, index) for d in comparable]
                excluded_entries = [_excluded_entry(d) for d in excluded]
                narrative = self._maybe_narrative(compared_entries, version)
                brief_markdown = _render_markdown(
                    compared_entries, excluded_entries, version, state.get("provenance"), narrative
                )
                result = {
                    "brief_markdown": brief_markdown,
                    "definition_sheet_version": version,
                    "compared_metrics": compared_entries,
                    "excluded_metrics": excluded_entries,
                }
                outcome = _OUTCOME_COMPARISON
            else:
                result = {
                    "validation_report": {
                        "requested_metrics": [d["metric"] for d in decisions],
                        "per_metric": [
                            {"metric": d["metric"], "comparable": False, "reason": d["reason"]} for d in decisions
                        ],
                    },
                    "definition_sheet_version": version,
                }
                outcome = _OUTCOME_NONE
        except _Reject as r:
            emit_progress(GENERATION_DEGRADED)
            code = r.code
        else:
            # counts only — never observed values, market names, or CSV content. The
            # single per-node domain event also records whether the optional narrative
            # was produced (comparison outcome only).
            payload = {"outcome": outcome, "compared": len(comparable), "excluded": len(excluded)}
            if outcome == _OUTCOME_COMPARISON:
                payload["narrative"] = "generated" if narrative else "omitted"
            emit_trace_event("brief_generated", payload, state)
            return {
                "result": result,
                "comparison_outcome": outcome,
                "status": AgentStatus.SUCCESS.value,
            }
        # fail-closed: no partial `result`, no raw State echoed in log/event
        emit_trace_event("brief_rejected", {"reason_code": code}, state)
        emit_progress(explain(code))
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"BriefGenerateNode: {code}"],
        }
