"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict (see ADR-005 for the prohibited
# alternatives). LangGraph checkpoints use msgpack serialization, so only
# plain serializable fields are allowed. Do NOT add credentials or secrets.

from typing import Any, Literal, NotRequired

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Destination market-comparison agent state.

    Flat TypedDict on top of the inherited ``AgentState``. Only JSON/msgpack-safe
    fields are added; inherited fields (``user_input``, ``validated_input``,
    ``result``, ``formatted_output``, ``status``, ``node_history``, ``error_log``,
    ``execution_time``, ``response_metadata``, ...) are NOT redeclared. Every added
    field is ``NotRequired`` so each node returns only its own delta (never the
    whole state).

    Field ownership (see ``docs/02_design.md``):

    Inner-only working fields — produced inside the inner graph and NEVER projected
    to caller-facing State by ``merge_output`` / ``get_output``:
      - ``normalized_markets``  — canonical normalised tables for the two markets
      - ``metric_definitions``  — canonical metric-definition sheet (declared rules)
      - ``provenance``          — per-input source metadata sufficient for citation
      - ``comparability``       — per-metric comparable/non-comparable decision + reason

    Projected to caller-facing State (the only additions that cross the inner→outer
    boundary, alongside inherited ``result`` / ``formatted_output``):
      - ``definition_sheet_version`` — effective sheet version, stamped on the brief
      - ``comparison_outcome``       — result discriminator (``comparison`` or
        ``no_comparable_metrics``); failure is expressed by inherited ``status: error``,
        never by this field.

    All ``dict`` values hold JSON/msgpack-safe leaves only (str, int, float, bool,
    None, and nested dict/list of the same). No Pydantic, dataclass,
    ``InvocationContext``, LLM client, DataFrame, CSV reader, credential, or raw
    request/CSV/payload blob is stored here — State keeps only the normalised tables,
    comparability decisions, and the minimal provenance needed downstream for citation.
    """

    # --- inner-only working fields (not projected past the inner graph) ---
    normalized_markets: NotRequired[dict[str, Any]]
    metric_definitions: NotRequired[dict[str, Any]]
    provenance: NotRequired[dict[str, Any]]
    comparability: NotRequired[dict[str, Any]]

    # --- projected to caller-facing State (with inherited result / formatted_output) ---
    definition_sheet_version: NotRequired[str]
    comparison_outcome: NotRequired[Literal["comparison", "no_comparable_metrics"]]

    # Allowlisted, non-sensitive code naming why no brief was produced, on a run the caller can
    # correct (src/services/degradation.py). Set by the outer envelope gate or by the inner
    # normalize node, and projected inner -> outer so the terminal gate can turn it into the
    # caller-facing sentence. Absent on a normal run and on a fail-closed one; never carries a
    # request value, table cell or source reference.
    diagnostic_code: NotRequired[str]
