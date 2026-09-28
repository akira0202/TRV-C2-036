"""AgentCore Platform v1.0"""

# Inner-graph head node: deserialise the JSON envelope handed to the inner graph by
# MarketComparisonGraphNode.extract_input() (a JSON string) into a structured
# `validated_input` dict for the downstream `normalize` node. See docs/02_design.md
# (`input_hydrate`).
#
# BaseGraph.invoke(user_input: str) sets only the inner `user_input`, so the inner head
# must re-parse it. required_trust_level = ANONYMOUS — S-1 is enforced at the OUTER
# boundary (CallerInputGateNode / OutputPolicyGateNode); this is an inner node.
#
# fail-closed: a non-string, empty, non-parseable, or non-object payload -> {status:error}
# with a non-sensitive code only (no partial validated_input, no raw payload echoed). This
# is a corrupt hand-off, not business non-comparability.

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import explain
from src.services.progress import emit_progress


class InputHydrateNode(FunctionNode):
    """Deserialise the inner-graph JSON envelope into structured `validated_input`."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def _reject(self, code: str, state: dict[str, Any]) -> dict[str, Any]:
        emit_progress(explain(code))
        emit_trace_event("input_hydrate_rejected", {"reason_code": code}, state)
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"InputHydrateNode: {code}"],
        }

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("user_input", "")
        if not isinstance(raw, str) or not raw.strip():
            return self._reject("ERR_HYDRATE_EMPTY", state)
        try:
            validated = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            return self._reject("ERR_HYDRATE_PARSE", state)
        if not isinstance(validated, dict):
            return self._reject("ERR_HYDRATE_NOT_OBJECT", state)
        emit_trace_event("input_hydrated", {"keys": len(validated)}, state)
        return {"validated_input": validated, "status": AgentStatus.SUCCESS.value}
