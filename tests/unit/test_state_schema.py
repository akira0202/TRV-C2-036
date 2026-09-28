"""Unit tests for the flat State schema (schema contract only).

Verifies the field contract from docs/02. A TypedDict subclass MERGES its parent's
annotations into ``__annotations__``, so the fields State *adds* are exactly those
not already present on the inherited ``AgentState``. Scope is the schema alone —
node/graph behaviour and runtime projection are verified in later issues.
"""

from framework.schemas.agent_state import AgentState

from src.schemas.state import State

# The six domain fields State adds on top of the inherited AgentState.
DOMAIN_FIELDS = {
    "normalized_markets",
    "metric_definitions",
    "provenance",
    "comparability",
    "definition_sheet_version",
    "comparison_outcome",
    # Set by the envelope gate or the inner normalize node and projected inner -> outer, so the
    # terminal gate can state the reason when no brief was produced.
    "diagnostic_code",
}

# Ownership partition (documented; runtime projection is checked in the
# graph-composition issue, not this schema-only issue).
INNER_ONLY = {"normalized_markets", "metric_definitions", "provenance", "comparability"}
PROJECTED = {"definition_sheet_version", "comparison_outcome", "diagnostic_code"}

_INHERITED = set(AgentState.__annotations__)


def _own():
    """The fields State ADDS — those not inherited from AgentState."""
    return {k: v for k, v in State.__annotations__.items() if k not in _INHERITED}


def test_exactly_the_declared_domain_fields():
    assert set(_own()) == DOMAIN_FIELDS


def test_added_fields_do_not_collide_with_inherited():
    # not redeclaring / shadowing any inherited AgentState field
    assert DOMAIN_FIELDS.isdisjoint(_INHERITED)


def test_ownership_partition_is_complete_and_disjoint():
    assert INNER_ONLY | PROJECTED == DOMAIN_FIELDS
    assert INNER_ONLY.isdisjoint(PROJECTED)


def test_all_added_fields_are_notrequired_delta_only():
    for name, ann in _own().items():
        assert "NotRequired" in repr(ann), f"{name} must be NotRequired (delta-only writes): {ann!r}"


def test_comparison_outcome_is_two_value_literal_without_error():
    ann = repr(_own()["comparison_outcome"])
    assert "Literal" in ann
    assert "comparison" in ann and "no_comparable_metrics" in ann
    # failure is inherited status:error, never a comparison_outcome value
    assert "'error'" not in ann


def test_annotations_are_json_msgpack_safe_no_forbidden_types():
    forbidden = ("basemodel", "pydantic", "dataclass", "invocationcontext",
                 "dataframe", "csv", "bytes", "object")
    for name, ann in _own().items():
        r = repr(ann).lower()
        for f in forbidden:
            assert f not in r, f"{name} annotation references forbidden type {f!r}: {ann!r}"


def test_no_credential_like_field_names():
    bad = ("token", "secret", "api_key", "apikey", "password", "credential", "context")
    for name in _own():
        low = name.lower()
        assert not any(b in low for b in bad), f"suspicious field name: {name}"
