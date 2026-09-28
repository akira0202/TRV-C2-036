"""AgentCore Platform v1.0"""

# Cat 2 outer graph: the fixed L1 AgentBaseGraph backbone with the domain workflow hidden
# inside the `main` slot via a GraphNode wrapping the inner BaseGraph. See docs/02_design.md
# ("Architecture Overview" / "Composition Pattern").
#
#   Outer backbone (add_edges NOT overridden):
#     START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
#
#   pre_process   = CallerInputGateNode   (S-1 VERIFIED_EXTERNAL + envelope gate)
#   main          = MarketComparisonGraphNode (wraps MarketComparisonInnerGraph)
#   post_process  = OutputPolicyGateNode   (S-3 terminal egress gate)
#
# S-1 is enforced once at the outer boundary; all inner nodes are ANONYMOUS. The inner ->
# outer channel is the inner get_output() narrow dict only, mapped explicitly by
# merge_output() — inner working fields cannot leak to caller-facing State.

import json
from typing import Any, ClassVar

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from shared.utils.audit_logger import emit_trace_event

from src.graph.market_comparison_inner_graph import MarketComparisonInnerGraph
from src.nodes.post_process_node import OutputPolicyGateNode
from src.nodes.pre_process_node import CallerInputGateNode
from src.schemas.state import State
from src.services.progress import emit_progress


class MarketComparisonGraphNode(GraphNode):
    """`main` slot: wraps the inner MarketComparisonInnerGraph (in-process subgraph)."""

    # propagate: re-raise an inner error as SubgraphError before merge_output, so no partial
    # inner output reaches caller-facing State; AgentBaseGraph.route() then sends the errored
    # outer state to finalize (skipping post_process) — graceful status:error, no output.
    error_strategy: ClassVar[str] = "propagate"
    # False: the inner graph does not inherit hitl_allowed from the parent (no HITL here).
    propagate_hitl: ClassVar[bool] = False

    def __init__(self, node_config: dict[str, Any] | None = None) -> None:
        super().__init__()
        # forwarded to the inner graph (carries the optional, non-authoritative `llm`)
        self._config = node_config or {}

    def get_subgraph(self) -> MarketComparisonInnerGraph:
        return MarketComparisonInnerGraph(config=self._config)

    def execute(self, state: AgentState) -> dict[str, Any]:
        # A request already answered at the envelope gate carries no validated_input, so there is
        # nothing to hand over: running the inner workflow would only re-reject an empty payload
        # and report that as this agent's own broken handoff.
        code = state.get("diagnostic_code")
        if code:
            emit_trace_event("market_comparison_skipped", {"reason_code": code}, state)
            return {"status": AgentStatus.SUCCESS.value}
        result: dict[str, Any] = super().execute(state)
        return result

    def extract_input(self, state: AgentState) -> str:
        # BaseGraph.invoke(user_input: str) sets only the inner user_input, so serialise the
        # parsed envelope back to a deterministic JSON string; input_hydrate re-parses it.
        emit_progress("Comparing the markets on the eligible metrics...")
        return json.dumps(state.get("validated_input", {}), ensure_ascii=True, sort_keys=True)

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        # Explicit mapping over the inner get_output() narrow dict (NOT inner state). Only the
        # terminal boundary fields cross into caller-facing State — no raw pass-through.
        # The marker is mapped like every other boundary field: post_process reads it to state the
        # reason instead of a brief. The outer marker wins — it names the specific reason, while an
        # inner node can only report its own.
        return {
            "result": sub_result.get("output"),
            "comparison_outcome": sub_result.get("comparison_outcome"),
            "definition_sheet_version": sub_result.get("definition_sheet_version"),
            "status": sub_result.get("status"),
            "diagnostic_code": state.get("diagnostic_code") or sub_result.get("diagnostic_code") or "",
        }


class DestinationMarketComparisonAgent(AgentBaseGraph):
    """Destination Market-Comparison Evidence Brief Generator (Cat 2 outer graph)."""

    @property
    def name(self) -> str:
        return "trv_c2_036"

    @property
    def state_schema(self) -> type:
        return State

    def get_output(self, state: dict[str, Any]) -> dict[str, Any]:
        # Completed with a notice instead of a brief: `output` is the code's fixed sentence, so
        # surface the code too — a caller can then branch on it rather than parse the sentence.
        out: dict[str, Any] = super().get_output(state)
        code = state.get("diagnostic_code")
        if code:
            out["diagnostic_code"] = str(code)
        return out

    def register_nodes(self) -> None:
        super().register_nodes()  # injects initialize + finalize (required first)
        self._nodes["pre_process"] = CallerInputGateNode()
        self._nodes["main"] = MarketComparisonGraphNode(node_config=self.config)
        self._nodes["post_process"] = OutputPolicyGateNode()

    # add_edges() is NOT overridden — the standard backbone wiring belongs to the framework.


def build_agent(config: dict[str, Any] | None = None) -> DestinationMarketComparisonAgent:
    """Construct the outer agent. `config` may carry the optional `llm` (BaseLLM) forwarded
    to the inner brief node; omitted by default (deterministic brief)."""
    return DestinationMarketComparisonAgent(config=config or {})
