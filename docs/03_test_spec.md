# Test Specification

This document maps the template's acceptance criteria to the tests that already exist in
this repository. It is a traceability record, not new test work — every row references a
real test file/function present at this revision (or a CI gate). Behaviour under test was
implemented across the node/graph MRs; this spec is the reader's map from requirement →
proof.

## Test Strategy

- **Test types**: unit (per-node `execute()` + pure helpers), composition-boundary unit
  (GraphNode `extract_input`/`merge_output`, inner `get_output`), proof-of-boundary (PB),
  and outer `agent.invoke()` integration (E2E).
- **Determinism / no live infrastructure**: the core path is deterministic. No live LLM,
  external retrieval, database, or network is required to run the suite. The optional
  comparison-prose LLM is exercised with a fake client only; there is no live connection.
- **Enforcement (the coverage gate for this template)**: CI `run-tests` (all unit + PB +
  integration must pass) plus `gate-stub-check` (no all-stub test files) and the source
  gates (`gate-import-isolation`, `gate-composition`, `gate-invoke-chain`,
  `gate-credential-scan`, `gate-trust-level-check`). Line-coverage is not a fixed numeric
  gate for this template; the gate is a green `run-tests`.
- **Local vs CI**: unit + PB run under the framework provided by CI. The outer
  `agent.invoke()` E2E requires the real framework wheel and is `pytest.importorskip`-gated
  (skipped locally, executed in CI).

## Three-Outcome Terminal Contract (spec)

Source of truth: `docs/02_design.md` → *Data Flow* / *Four-outcome terminal contract*.
Every test below is consistent with these four, and only these four, terminal outcomes:

1. **Notice (caller-correctable, no brief)** — the caller's envelope, or the caller's own market
   tables / sources / definition sheet, cannot be accepted → `status: success` with
   `diagnostic_code` = the allow-listed code and `output` = the sentence that code carries.
   **No brief and no validation report** (a report means the metrics were checked and none were
   comparable — a different thing from a request that was never examined), no `comparison_outcome`,
   and nothing the request sent anywhere in the response.
1a. **Reject (fail-closed floor)** — the structural screen refusing a payload that is not really a
   document (oversize / over-deep / control characters / non-JSON types), a broken handoff into
   the inner graph, corrupt inner State reaching `comparability` / `brief`, or a terminal egress
   block → `status: error` with **no caller-facing output** (`output`/`result`/`formatted_output`
   all `None`) and a non-sensitive code only. No partial brief; no raw data / PII in `error_log`
   or trace.
2. **Comparison** — ≥1 requested metric comparable → `status: success`,
   `comparison_outcome: "comparison"`, a cited Markdown brief in `formatted_output`.
   Non-comparable metrics appear in the brief's `excluded_metrics` (normal degradation).
3. **No comparable metric** — 0 comparable → `status: success`,
   `comparison_outcome: "no_comparable_metrics"`, a structured `validation_report` passed
   through as `result` (**no `brief_markdown`**). This is a normal human-review report, not
   an error and not a ranking.

## Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Result (traced to) |
|-------|------|----------------|--------|
| TC-01 | State contract: flat TypedDict | Type check pass, no Pydantic/dataclass | `tests/unit/test_state_schema.py` (`test_exactly_the_declared_domain_fields`, `test_all_added_fields_are_notrequired_delta_only`, `test_annotations_are_json_msgpack_safe_no_forbidden_types`); PB-2 |
| TC-02 | SecurityViolationError fires on invalid input | Error raised / caller refused | S-1 trust gate — `tests/integration/test_agent_invoke_e2e.py::test_anonymous_caller_refused_before_execute` (framework refuses → `status:error`, no output); CI `gate-trust-level-check` |
| TC-03 | No JWT/Credential in State | CI `gate-credential-scan`: 0 violations | `gate-credential-scan` (CI) + `tests/unit/test_state_schema.py::test_no_credential_like_field_names`; S-3 credential block `tests/unit/test_output_policy_gate_node.py::test_credential_in_output_blocked` |
| TC-04 | InvocationContext via configurable only | Direct construction absent in nodes | No `InvocationContext(...)` construction in `src/` — nodes read only `state` (verified by inspection; PB-4 import scan). No node depends on `ctx` directly |
| TC-05 | S-4: no duplicate lifecycle events in `execute()` | `node_start`/`node_complete`/`node_error` absent from `execute()` bodies | Nodes emit only domain events in `execute()`; single-domain-event assertions: `tests/unit/test_comparability_validate_node.py::test_success_emits_exactly_one_domain_event_without_observed_values`, `tests/unit/test_brief_generate_node.py::test_audit_event_payload_has_no_values_or_market_names`, `tests/unit/test_input_normalize_provenance_node.py::test_execute_answer_is_partial_free_and_emits_once` |
| TC-06 | S-2: `_security_gate_input()` not overridden (`FunctionNode`) | `TypeError` at class definition if overridden (`@final`) | `tests/unit/test_framework_compliance_tc06_tc07.py::test_tc06_security_gate_input_cannot_be_overridden` |
| TC-07 | S-3: `_security_gate_output()` not overridden (`FunctionNode`) | `TypeError` at class definition if overridden (`@final`) | `tests/unit/test_framework_compliance_tc06_tc07.py::test_tc07_security_gate_output_cannot_be_overridden` |
| TC-08 | `required_trust_level` enforced | Insufficient trust → refused **before** `execute()`, no output | Runtime: `tests/integration/test_agent_invoke_e2e.py::test_anonymous_caller_refused_before_execute`; declarations: `test_caller_input_gate_node.py::test_trust_level_is_verified_external`, `test_output_policy_gate_node.py::test_trust_level_is_verified_external`, `test_input_hydrate_node.py::test_trust_level_is_anonymous`; PB-6; CI `gate-trust-level-check` |
| TC-09 | S-2: `_extra_security_gate_input()` non-trivial | Domain input checks execute (structural sanitisation) | `tests/unit/test_input_normalize_provenance_node.py::test_extra_gate_rejects_control_chars_and_sets_error`, `test_extra_gate_passes_clean_input_unchanged` |
| TC-10 | S-3: `_extra_security_gate_output()` non-trivial | Domain output checks execute (auxiliary re-scan) | `tests/unit/test_output_policy_gate_node.py::test_extra_gate_raises_on_prohibited_delta` |
| TC-11 | S-4: ≥1 domain `emit_trace_event()` inside each `execute()` | Domain event emitted on every path | Per-node audit assertions (success + reject) across `test_input_normalize_provenance_node.py`, `test_comparability_validate_node.py`, `test_brief_generate_node.py`, `test_output_policy_gate_node.py`, `test_caller_input_gate_node.py`, `test_input_hydrate_node.py`; PB-1 |

## Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Test | Expected Result | Result (traced to) |
|-------|----------|------|----------------|--------|
| PB-1 | BaseNode → EventEmitter | `emit_trace_event()` fires on every path | No silent failures | Per-node audit assertions (see TC-11); order proof `tests/proof_of_boundary/test_pb_invoke_order.py` |
| PB-2 | State serialization | Post-invoke State is primitives only | No Pydantic/dataclass | `tests/proof_of_boundary/test_state_safety.py`; `tests/unit/test_state_schema.py::test_annotations_are_json_msgpack_safe_no_forbidden_types` |
| PB-3 | Level 1 → External service | *(N/A — no external service in scope)* | Not applicable | **N/A**: this is a bounded, deterministic workflow over supplied inputs — no external retrieval, no live LLM on the core path, no traveller-level data path exists (see §Test Strategy and `docs/02` scope). No PB-3 connection to assert |
| PB-4 | Import isolation | No Level 0 imports | AST scan: 0 violations | `tests/proof_of_boundary/test_import_isolation.py::test_no_prohibited_imports_in_src`; CI `gate-import-isolation` |
| PB-5 | Checkpoint safety | No JWT/Pydantic in checkpoint | Inspection pass | `tests/proof_of_boundary/test_state_safety.py`; `tests/unit/test_state_schema.py::test_no_credential_like_field_names` |
| PB-6 | Invoke execution order | `__call__()`: S-1 → S-4 `node_start` → S-2 → `execute()` → S-3 → S-4 `node_complete` | Order verified for every node | `tests/proof_of_boundary/test_pb_invoke_order.py::test_call_order_for_every_node` |
| PB-7 | HITL interrupt propagation *(conditional)* | Required only when `hitl.enabled: true` | **Auto-waived — non-HITL** | `config/agent.yaml` has no top-level `hitl.enabled` (non-HITL by design — `docs/01`/`docs/02`); `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` reads `config/agent.yaml` and `skipif`-auto-skips. Not required; must not block the gate |

> **Pre-review gate checklist:** PB-1 through PB-6 are mandatory (all present above). PB-7 is
> **Auto-waived — non-HITL** for this template. PB-3 is **N/A** — there is no external
> service / retrieval / traveller-data path to connect to.

## Business Logic Tests

Domain acceptance-criteria coverage. Each row maps a required behaviour to the test file
that proves it at this revision.

| BL-ID | Acceptance criterion | Expected Result | Result (traced to) |
|-------|------|----------------|--------|
| BL-01 | **Caller envelope gate**: parse `user_input` JSON; require exactly two markets + a versioned definition sheet + non-empty requested_metrics; every condition here is the caller's envelope, so a violation is **answered**, not errored; no raw echo | Valid → `validated_input`; malformed → `status:success` + `diagnostic_code`, partial-free, no `error_log`; every code this gate sends has a sentence of its own | `tests/unit/test_caller_input_gate_node.py` (all cases) |
| BL-02 | **Normalize + provenance**: accept only V1 CSV/canonical-JSON; an unsupported format or schema violation in the caller's own material is **answered** with `diagnostic_code`; canonical records with citation provenance; no value imputation; structural S-2 (NUL/control-char/oversize/over-deep/non-JSON-type) stays **fail-closed on both passes**; `null` preserved | Canonical records + provenance; caller-material violations → `status:success` + code, partial-free; structural-screen violations → `status:error`, never a notice | `tests/unit/test_input_normalize_provenance_node.py` (all cases) |
| BL-03 | **Comparability** on incompatible axes: period (`exact`/`same_year`/`none`), population, unit (+ declared conversion), definition/eligibility; fixed reason priority; `null`/non-finite not comparable | Per-metric decision in requested order; conflicts → non-comparable reason; corrupt State fail-closed | `tests/unit/test_comparability_validate_node.py` (all cases) |
| BL-04 | **Brief / zero-comparable**: ≥1 comparable → cited brief (values verbatim, both citations, no diff/ratio/rank); 0 comparable → `no_comparable_metrics` success (report only, no `brief_markdown`); citation integrity fail-closed; optional LLM graceful degrade + source-grounded prose only | Cited brief or validation report; LLM unavailable/failing → deterministic success | `tests/unit/test_brief_generate_node.py` (all cases) |
| BL-05 | **S-3 terminal egress** (outer `post_process`): block forecast/rank/recommend/causal/injection/credential/PII across **every** caller-facing leaf **including `brief_markdown`** (Markdown-only / LLM-narrative-only PII included); on block, `result` **and** `formatted_output` both `None`, non-sensitive code only, no brief body / PII in `error_log` or trace | Prohibited content → `status:error`, both output keys cleared, no leak; clean comparison → `formatted_output`; no-comparable → report passthrough | `tests/unit/test_output_policy_gate_node.py` (all cases) |
| BL-06 | **Composition boundary**: only terminal fields cross inner→outer; `normalized_markets`/`metric_definitions`/`provenance`/`comparability`/`validated_input` never reach caller-facing State; `diagnostic_code` is a boundary field — an inner node that errors is re-raised before `merge_output` runs, so the inner graph must *complete* for its code to cross, and the outer marker wins over an inner one | `merge_output`/`get_output` narrow; working fields absent; the marker crosses and the outer one takes precedence | `tests/unit/test_graph_composition.py` (all cases) |
| BL-07 | **End-to-end outer `agent.invoke()`**: comparison → success + cited brief; zero-comparable → success + report; ANONYMOUS → refused before execute, no output; a fault in the caller's own table → answered with a notice, no brief and no report, and the inner workflow short-circuits (the marker reaches the routing, or the next node would report the absent handoff as this agent's fault) | Each terminal outcome reachable and distinct at the real invoke boundary | `tests/integration/test_agent_invoke_e2e.py` (CI-run; 4 cases) |

## Additional Regression Coverage

Guards added alongside the changes in this revision. Each entry names the test and the
condition it accepts on; they supplement the mandatory TC / PB items above and replace none
of them.

| Concern | Test | Acceptance condition |
|---|---|---|
| Deployment startup contract | `tests/unit/test_entry_point_import.py` | `src/api/server.py` — the module `uvicorn src.api.server:app` loads — imports successfully and exposes `app` together with a compiled `agent` whose `config` is a mapping. This template ships no `config/config.yaml`, so the empty mapping is what AgentRegistry also passes when the file is absent. |


## Test Execution Summary

- Execution: CI `run-tests` (latest green pipeline for this branch). The job runs two
  pytest invocations; figures below are transcribed from that `run-tests` log.
- Unit + integration invocation: **203 collected → 201 passed, 2 skipped, 0 failed**. The
  four outer `agent.invoke()` E2E cases (`tests/integration/test_agent_invoke_e2e.py`) are
  collected within this invocation.
- Proof-of-boundary invocation (`tests/proof_of_boundary`): **5 collected → 3 passed,
  2 skipped, 0 failed**. The two skips are PB-7's cases (auto-skipped, non-HITL).
- Coverage: gated by a green `run-tests` + `gate-stub-check` (no numeric line-coverage gate
  for this template).

## LLM Availability Matrix

Expected `status` and returned artifact for each state of the Azure OpenAI connection. The three
connection values (`AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT`) are
resolved at call time from the invocation-bound `SecretProvider`. Declaring them in
`config/agent.yaml` `requires.secrets` states the deployment contract; it does not change any row
below. The comparison prose is non-authoritative; the cited deterministic brief is unaffected.

| Condition | Expected `status` | Artifact returned | Result |
|-----------|-------------------|-------------------|--------|
| Connection values resolve, provider responds | `success` | Cited brief + source-grounded prose (ungrounded prose is dropped) | Pass (`test_brief_generate_node::test_llm_prose_is_included_when_available`, `test_brief_generate_node::test_llm_hallucination_never_enters_brief_or_authoritative_data`) |
| A connection value cannot be resolved | `success` | Cited deterministic brief; prose omitted | Pass (`test_llm_adapter::test_missing_connection_value_raises`, `test_brief_generate_node::test_llm_exception_degrades_to_deterministic_brief`) |
| Provider fails (error / timeout / malformed response) | `success` | Cited deterministic brief; prose omitted | Pass (`test_llm_adapter::test_provider_failure_propagates`, `test_brief_generate_node::test_llm_exception_degrades_to_deterministic_brief`) |
