"""AgentCore Platform v1.0"""

# Outer-backbone `pre_process` node: the S-1 caller trust boundary + caller-envelope
# contract gate. See docs/02_design.md (`pre_process` / CallerInputGateNode).
#
# required_trust_level = VERIFIED_EXTERNAL — this is the reachable outer node that backs the
# manifest's declared trust level; the S-1 gate in BaseNode.__call__() refuses an
# insufficient-trust caller BEFORE execute() runs. All INNER nodes stay ANONYMOUS (S-1 is
# enforced here, once, at the boundary). The framework @final S-2 PII scan runs on
# `user_input` automatically.
#
# Responsibility (MINIMAL envelope gate only): `user_input` arrives as a JSON string (the
# /invoke adapter sets state["user_input"] to the raw string). Parse it fail-closed and
# verify the caller envelope shape at a high level — exactly two market tables + a
# referenced, versioned metric-definition sheet + a non-empty requested_metrics list must be
# present. The DEEP field-level contract validation + canonicalisation is the inner
# `normalize` node's job; this gate does not duplicate it (avoids drift).
#
# fail-closed: a non-string / empty / non-parseable / non-object payload, or an envelope
# missing the required top-level shape, returns {status:error} with a non-sensitive code
# only (partial-delta reject on the verified route — AgentBaseGraph.route() sends the errored
# state to finalize, so no downstream node runs and there is no caller-facing output). Raw
# input is never echoed into error_log or the audit event.

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress


class CallerInputGateNode(FunctionNode):
    """S-1 caller trust boundary + minimal caller-envelope contract gate."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _reject(self, code: str, state: dict[str, Any]) -> dict[str, Any]:
        """Complete the run carrying the reason: every condition this gate checks is the caller's
        envelope, so a rejection here names something they can change.

        Only the allow-listed code is recorded — never payload content — and no error is logged,
        because nothing failed. The terminal gate turns the code into the caller-facing sentence,
        so no progress line is sent here: the notice is the output, not a message alongside it.
        The method keeps its name because every call site is still a rejection of the request;
        what changed is how that rejection is reported.
        """
        emit_trace_event("caller_input_degraded", {"reason_code": code}, state)
        return {"diagnostic_code": code, "status": AgentStatus.SUCCESS.value}

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        emit_progress("Checking the request...")
        raw = state.get("user_input", "")
        if not isinstance(raw, str) or not raw.strip():
            return self._reject("ERR_ENVELOPE_EMPTY", state)
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            return self._reject("ERR_ENVELOPE_PARSE", state)
        if not isinstance(envelope, dict):
            return self._reject("ERR_ENVELOPE_NOT_OBJECT", state)

        # high-level envelope shape only (deep validation is the inner `normalize` node's job)
        markets = envelope.get("markets")
        if not isinstance(markets, list) or len(markets) != 2:
            return self._reject("ERR_ENVELOPE_MARKETS", state)
        sheet = envelope.get("metric_definition_sheet")
        if not isinstance(sheet, dict) or not (isinstance(sheet.get("version"), str) and sheet.get("version")):
            return self._reject("ERR_ENVELOPE_SHEET_VERSION", state)
        requested = envelope.get("requested_metrics")
        if not isinstance(requested, list) or not requested:
            return self._reject("ERR_ENVELOPE_REQUESTED", state)

        emit_trace_event("caller_input_accepted", {"markets": len(markets)}, state)
        # hand the parsed envelope to the inner graph via validated_input; the GraphNode
        # re-serialises it to a JSON string for BaseGraph.invoke(user_input: str).
        return {"validated_input": envelope, "status": AgentStatus.SUCCESS.value}
