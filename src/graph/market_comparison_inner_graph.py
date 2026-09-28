"""AgentCore Platform v1.0"""

# Inner domain workflow graph for the Cat 2 template. Instantiated by
# MarketComparisonGraphNode.get_subgraph() in graph.py. See docs/02_design.md
# ("Architecture Overview" / "Composition Pattern").
#
# Pipeline:  START -> input_hydrate -> normalize -> comparability -> brief -> END
# with conditional edges so a node error short-circuits straight to END (no partial inner
# output). All inner nodes are ANONYMOUS — S-1 is enforced once at the OUTER boundary.
#
# get_output() is the ONLY inner -> outer channel: it surfaces exactly the narrow boundary
# dict {output, status, comparison_outcome, definition_sheet_version, error_log}. The inner
# working fields (normalized_markets / metric_definitions / provenance / comparability) are
# NEVER surfaced, so they cannot leak into caller-facing State.

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from src.nodes.brief_generate_node import BriefGenerateNode
from src.nodes.comparability_validate_node import ComparabilityValidateNode
from src.nodes.input_hydrate_node import InputHydrateNode
from src.services.llm import AzureOpenAINarrativeLLM
from src.nodes.input_normalize_provenance_node import InputNormalizeProvenanceNode
from src.schemas.state import State


class MarketComparisonInnerGraph(BaseGraph):
    """Deterministic inner workflow: hydrate -> normalize -> comparability -> brief."""

    @property
    def name(self) -> str:
        return "market_comparison_inner"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory inner config. The LLM forwarded to the brief node is non-authoritative;
        # a caller may inject one, otherwise the default keyless adapter is used. An unresolvable
        # value or a provider failure still degrades gracefully at the node.
        return None

    def register_nodes(self) -> None:
        # No super() — BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize/finalize (outer backbone concern).
        self._nodes["input_hydrate"] = InputHydrateNode()
        self._nodes["normalize"] = InputNormalizeProvenanceNode()
        self._nodes["comparability"] = ComparabilityValidateNode()
        # brief consumes the non-authoritative LLM. A caller-supplied `llm` (tests inject a
        # fake) wins; otherwise the default keyless adapter, which resolves the AZURE_OPENAI_*
        # values per-request at call time.
        llm = self.config.get("llm")
        if llm is None:
            llm = AzureOpenAINarrativeLLM()
        self._nodes["brief"] = BriefGenerateNode(llm=llm)

    def add_edges(self) -> None:
        # Linear pipeline with fail-fast short-circuit: any node setting status:error routes
        # straight to END so no downstream node runs and no partial inner output is produced.
        self._sg.add_edge(START, "input_hydrate")
        self._sg.add_conditional_edges(
            "input_hydrate",
            self._continue_or_end,
            {"continue": "normalize", "end": END},
        )
        self._sg.add_conditional_edges(
            "normalize",
            self._continue_or_end,
            {"continue": "comparability", "end": END},
        )
        self._sg.add_conditional_edges(
            "comparability",
            self._continue_or_end,
            {"continue": "brief", "end": END},
        )
        self._sg.add_edge("brief", END)

    def _continue_or_end(self, state: dict[str, Any]) -> str:
        # End on any node error, and on a request already answered with a diagnostic_code. A
        # degraded run ends as SUCCESS, so keying only on the status would let the remaining nodes
        # run on a request that was already answered — and the next one would report the absent
        # handoff as this agent's own fault.
        if state.get("diagnostic_code"):
            return "end"
        return "end" if state.get("status") == AgentStatus.ERROR.value else "continue"

    def route(self, state: dict[str, Any]) -> str:
        # Required by the BaseGraph ABC; delegates to the same fail-fast decision.
        return self._continue_or_end(state)

    def get_output(self, state: dict[str, Any]) -> dict[str, Any]:
        # Narrow inner -> outer channel. `output` is the brief `result` (comparison brief or
        # validation report); working fields are intentionally omitted. error_log is included
        # so the framework reject path can read it under error_strategy="propagate".
        # An inner node that errors is re-raised as SubgraphError *before* merge_output() runs, so
        # an inner error can never carry its code outward. A request the caller can correct
        # therefore completes the inner graph instead, and the marker is projected here.
        return {
            "output": state.get("result"),
            "status": state.get("status"),
            "comparison_outcome": state.get("comparison_outcome"),
            "definition_sheet_version": state.get("definition_sheet_version"),
            "diagnostic_code": state.get("diagnostic_code", ""),
            "error_log": state.get("error_log", []),
        }
