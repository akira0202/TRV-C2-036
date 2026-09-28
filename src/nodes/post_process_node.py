"""AgentCore Platform v1.0"""

# Outer-backbone `post_process` node: the S-3 TERMINAL egress gate. See docs/02_design.md
# (`post_process` / OutputPolicyGateNode + "S-3 terminal-block placement").
#
# required_trust_level = VERIFIED_EXTERNAL (outer boundary node). The terminal decision is
# taken HERE in execute() over the FULL composed candidate output (the whole `result` already
# merged into outer State), because only this node sees the complete brief and can clear the
# entire caller-facing output. `_extra_security_gate_output()` only sees this node's own delta
# and is therefore auxiliary defense-in-depth (re-scan) — NOT the terminal basis.
#
# Terminal block contract (design acceptance criteria): on a policy/credential/injection
# hit anywhere in the candidate output, return status:error with BOTH `result` and
# `formatted_output` cleared to None (the final output contract is `formatted_output or
# result`, so clearing one is insufficient) and a non-sensitive code only — never the blocked
# content, raw data, or brief body.
#
# Clean output:
#   - comparison            -> formatted_output = result["brief_markdown"]; status success.
#   - no_comparable_metrics -> leave formatted_output unset and let the validation-report
#                              `result` pass through, so get_output() (= formatted_output or
#                              result) returns the report. status success.

import re
from collections.abc import Iterator
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.degradation import explain_degradation
from src.services.failure_message import explain

# Substrate credential/PII detectors (substrate-reuse rule — never hand-roll). Import-shim tolerant:
# prefer shared.security, fall back to framework.security; None when neither is present (local
# stub runs) — the scan simply skips substrate checks there and CI exercises the real path.
try:  # pragma: no cover - import shim
    from shared.security import detect_credentials, detect_pii
except Exception:  # noqa: BLE001
    try:  # pragma: no cover - import shim
        from framework.security import detect_credentials, detect_pii
    except Exception:  # noqa: BLE001
        detect_credentials = None
        detect_pii = None

_OUTCOME_COMPARISON = "comparison"

# Prohibited agent-conclusion content: forecast / ranking / recommendation / superiority /
# causal-conclusion. The deterministic brief never contains these; this catches anomalous
# content (e.g. optional-LLM narrative) reaching egress. Over-block is the safe failure mode.
_POLICY_RE = re.compile(
    r"\b("
    r"forecast\w*|predict\w*|projection|projected|"
    r"rank\w*|ranking|recommend\w*|advise|advises|advising|"
    r"outperform\w*|superior|inferior|winner|worst|best\s+market|top\s+market|"
    r"should\s+(invest|spend|target|prioriti[sz]e)|"
    r"because\s+of|due\s+to|caused\s+by|leads?\s+to|results?\s+in|driven\s+by|attributable\s+to"
    r")\b",
    re.IGNORECASE,
)
_POLICY_JA = (
    "予測",
    "予想",
    "見込",
    "推奨",
    "おすすめ",
    "推薦",
    "ランキング",
    "順位",
    "優位",
    "劣位",
    "勝",
    "見通し",
    "成長率",
    "起因",
    "による影響",
)

# Injection / instruction-following content (the S-3 gate is the sole owner of this check).
_INJECTION_RE = re.compile(
    r"("
    r"ignore\s+(all\s+|the\s+)?(previous|prior|above|earlier)\s+(instruction|prompt|context)|"
    r"disregard\s+(all\s+|the\s+|previous\s+|prior\s+)?(instruction|rule|prompt)|"
    r"you\s+are\s+now|act\s+as\s+|pretend\s+to\s+be|system\s+prompt|"
    r"</?(system|assistant|user)>|override\s+(the\s+)?(instruction|rule)"
    r")",
    re.IGNORECASE,
)
_INJECTION_JA = ("指示を無視", "命令を無視", "これまでの指示", "システムプロンプト", "上記を無視")


def _string_leaves(value: Any) -> Iterator[str]:
    """Yield every string leaf reachable through dict values / list / tuple (not dict keys)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _string_leaves(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _string_leaves(v)


def _policy_or_injection_hit(text: str) -> bool:
    return bool(
        _POLICY_RE.search(text)
        or _INJECTION_RE.search(text)
        or any(tok in text for tok in _POLICY_JA)
        or any(tok in text for tok in _INJECTION_JA)
    )


class OutputPolicyGateNode(FunctionNode):
    """S-3 terminal egress gate over the full composed comparison output."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _block(self, code: str, state: dict[str, Any]) -> dict[str, Any]:
        emit_trace_event("output_policy_blocked", {"reason_code": code}, state)
        emit_progress(explain(code))
        return {
            "status": AgentStatus.ERROR.value,
            "result": None,
            "formatted_output": None,
            "error_log": [f"OutputPolicyGateNode: {code}"],
        }

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        emit_progress("Preparing the response...")

        # Already answered upstream: no brief exists by design. The output is the reason alone —
        # not a validation report, which is a real outcome meaning "the metrics were checked and
        # none were comparable", and would misdescribe a request that was never examined. The
        # sentence is derived from the allow-listed code, so nothing the refused request carried
        # reaches the caller.
        degraded_code = state.get("diagnostic_code")
        if degraded_code:
            emit_trace_event("output_degraded", {"reason_code": str(degraded_code)}, state)
            return {
                "diagnostic_code": str(degraded_code),
                "result": None,
                "formatted_output": explain_degradation(str(degraded_code)),
                "status": AgentStatus.SUCCESS.value,
            }

        result = state.get("result")
        outcome = state.get("comparison_outcome")

        # Inspect the FULL candidate output — EVERY caller-facing string leaf, including the
        # display markdown (`brief_markdown`), which is what `formatted_output` carries on the
        # comparison path. No leaf is exempt: prohibited-conclusion / injection / credential /
        # PII must all be caught here, because content that appears ONLY in the rendered
        # markdown (e.g. optional-LLM narrative) has no structured-field counterpart to scan.
        display = result.get("brief_markdown") if isinstance(result, dict) else None
        for leaf in _string_leaves(result):
            if _policy_or_injection_hit(leaf):
                return self._block("ERR_OUTPUT_POLICY", state)
            if detect_credentials is not None and detect_credentials(leaf):
                return self._block("ERR_OUTPUT_CREDENTIAL", state)
            if detect_pii is not None and detect_pii(leaf):
                return self._block("ERR_OUTPUT_PII", state)

        if outcome == _OUTCOME_COMPARISON:
            if not (isinstance(display, str) and display.strip()):
                # comparison outcome must carry a rendered brief; a missing one is corrupt
                return self._block("ERR_OUTPUT_MISSING_BRIEF", state)
            emit_trace_event("output_policy_passed", {"outcome": outcome}, state)
            return {"formatted_output": display, "status": AgentStatus.SUCCESS.value}

        # no_comparable_metrics (or any other non-error outcome): pass the validation-report
        # `result` through unchanged; get_output() = formatted_output(None) or result.
        emit_trace_event("output_policy_passed", {"outcome": outcome}, state)
        return {"status": AgentStatus.SUCCESS.value}

    def _extra_security_gate_output(self, result: dict[str, Any]) -> dict[str, Any]:
        # Auxiliary defense-in-depth ONLY (sees this node's own delta, not the composed brief
        # in outer State). Re-scan the delta's own string leaves and hard-fail on a policy /
        # injection / credential hit that slipped through execute(). Not the terminal basis.
        for leaf in _string_leaves(result):
            if _policy_or_injection_hit(leaf):
                raise RuntimeError("S-3: prohibited content in output delta")
            if detect_credentials is not None and detect_credentials(leaf):
                raise RuntimeError("S-3: credential in output delta")
        return result
