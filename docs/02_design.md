# Template Design Specification

Destination Market-Comparison Evidence Brief Generator — a bounded, read-only,
human-review Cat 2 workflow. Given two supplied JNTO aggregate market tables and an
approved, versioned metric-definition sheet, it normalises the inputs, validates
period/population/unit comparability against the sheet's declared rules, synthesises a
bounded side-by-side comparison over eligible metrics only, and produces a cited
Markdown evidence brief — or a `no_comparable_metrics` validation report when no
requested metric is comparable. Every numeric claim carries its supplied source
citation and the effective definition-sheet version. The output is a human-review
draft; a destination-marketing analyst makes the final call.

## Position in AgentCore Architecture

- **Agent Class**: DestinationMarketComparisonAgent
- **L1 Base**: AgentBaseGraph  *(logical node-backbone pattern: DocGenerationAgent; the
  template inherits `AgentBaseGraph` directly at L1 — it does not inherit the
  DocGenerationAgent class, and never inherits a sibling template ID)*
- **Category**: Cat 2 (bounded multi-step comparison/brief workflow; not autonomous)
- **Three-Layer Separation**:
  - State: flat TypedDict composition (no Pydantic — msgpack incompatible)
  - Node: L1 inheritance (Template Method: `execute(self, state: dict) -> dict` override only)
  - Graph: composition (`register_nodes()` for node substitution; `add_edges()` NOT overridden)

> **Design class names ↔ manifest entry-point.** The classes named in this document
> (`DestinationMarketComparisonAgent`; nodes `CallerInputGateNode`,
> `MarketComparisonGraphNode`, `OutputPolicyGateNode`, `InputHydrateNode`,
> `InputNormalizeProvenanceNode`, `ComparabilityValidateNode`, `BriefGenerateNode`) are the
> design targets, implemented at Stage ③. **The manifest entry-point must always name a class
> that exists and compiles**, so in this design MR `config/agent.yaml` keeps `class: Graph`
> (the scaffold entry-point that is present and loadable), with `base_type: DocGenerationAgent`
> (the design archetype) and `module: src.graph` — no unresolved placeholder, and no reference
> to a not-yet-implemented class. At Stage ③ the outer graph class is renamed
> `Graph` → `DestinationMarketComparisonAgent` (the manifest `class` entry-point; no separate
> `Graph` alias retained), the scaffold node placeholders are replaced with the classes above,
> and `config/agent.yaml` `class` is updated in the SAME MR so the manifest never points to a
> missing class.
>
> **Stage ③ entry gate (acceptance criterion).** The implementation MR MUST make
> `src.graph.DestinationMarketComparisonAgent` importable, registered, and set as the manifest
> `class`, and MUST NOT reach deploy/invoke behaviour verification while `config/agent.yaml`,
> `src/**` (excluding `src/examples/`), or `docs/**` still contain any unresolved scaffold
> placeholder token, or while the manifest `class` names a class absent from `src/`.
> Tracked as a Stage ③ graph-composition acceptance criterion.

## Architecture Overview

This is a Cat 2 template: the fixed outer `AgentBaseGraph` backbone hides the domain
workflow inside the `main` slot via a `GraphNode` subclass that wraps an inner
`BaseGraph`. The outer backbone `add_edges()` is **not** overridden (it is the standard
pipeline). Caller trust (S-1) and the caller-input contract are enforced at the outer
`pre_process` boundary; the S-3 terminal egress gate runs in the outer `post_process`
node; the deterministic domain steps run inside the inner graph.

### Node Configuration

**Outer backbone (`DestinationMarketComparisonAgent(AgentBaseGraph)`)**

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | Framework init (schema_version, response_metadata scaffold) | — | — | InitializeNode (default) |
| pre_process | **S-1 trust boundary** (`required_trust_level = VERIFIED_EXTERNAL`) + caller-envelope contract check. **`user_input` arrives as a JSON string** — the standalone `/invoke` adapter sends `{"input": <json-string>}` and `invoke()` sets `state["user_input"]` to that raw string — so pre_process `json.loads` it and answers a non-parseable string with a `diagnostic_code` notice, then verifies the envelope: exactly two named markets' tables + a referenced, versioned metric-definition sheet must be present. Every condition this gate checks is the caller's envelope, so each rejection completes the run carrying its code rather than erroring. The framework `@final` S-2 PII scan runs on `user_input`. Emits one S-4 domain event. | `user_input` | `validated_input`, `status` | `CallerInputGateNode` (FunctionNode) |
| main | Wraps the inner domain `BaseGraph`; `extract_input` serialises `validated_input` to a JSON string; `merge_output` maps the inner **`get_output()` dict** (`output`→`result`, plus `comparison_outcome`/`definition_sheet_version`/`status`) to caller-facing State — that narrow dict is the only inner→outer channel, so inner working fields cannot leak. `error_strategy = "propagate"`; `propagate_hitl = False`. | `validated_input` | `result`, `comparison_outcome`, `definition_sheet_version`, `status` | `MarketComparisonGraphNode` (GraphNode) |
| post_process | **S-3 terminal egress gate** (`required_trust_level = VERIFIED_EXTERNAL`): when the run carries a `diagnostic_code`, release that code's sentence as the whole output (no brief, no validation report) and stop; otherwise inspect the full candidate brief; redact credential/PII; fail-closed on forecast / rank / recommend / causal-conclusion / injection-derived (instruction-following) content — returns `status: error`, `result: None`, `formatted_output: None`, non-sensitive error code only, clearing BOTH keys since the final output contract is `formatted_output or result`. On a clean `comparison` brief, place the final cited Markdown into `formatted_output`. For the `no_comparable_metrics` outcome (no brief), leave `formatted_output` unset and pass the validation-report `result` through, so `get_output()` (= `formatted_output or result`) returns the report. Emits one S-4 domain event. | `result`, `comparison_outcome` | `formatted_output`, `status` | `OutputPolicyGateNode` (FunctionNode) |
| finalize | Framework finalize (`get_output()` = `formatted_output or result`; builds response_metadata) | — | — | FinalizeNode (default) |

**Inner domain workflow (`MarketComparisonInnerGraph(BaseGraph)`), all inner nodes `required_trust_level = ANONYMOUS`** *(S-1 is enforced at the outer boundary; `BaseGraph.invoke()` does not propagate caller trust into the subgraph)*

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| input_hydrate | Deserialise the JSON envelope handed in by `GraphNode.invoke(user_input: str)` into structured `validated_input` (the inner head; `BaseGraph.invoke` sets only inner `user_input`). Emits one S-4 domain event. | `user_input` | `validated_input` | `InputHydrateNode` (FunctionNode) |
| normalize | Accept only CSV / canonical-JSON V1 inputs; answer a malformed schema or an unsupported/free-text body with a `diagnostic_code` notice — the caller's own material, theirs to correct — while the structural screen below stays fail-closed (`status: error`); normalise both markets' tables and the metric-definition sheet to the documented canonical schema **without inferring missing values, reconstructing an export, or retrieving/refreshing data**; record per-input provenance sufficient for citation (source title/URL or id, export date, reporting period, market, population/coverage, unit, footnotes); stamp `definition_sheet_version`. Domain S-2 via `_extra_security_gate_input()` = structural sanitisation of untrusted free-text (NUL/control-char rejection, length bounds) — **no injection detection here**. Emits one S-4 domain event. | `validated_input` | `normalized_markets`, `metric_definitions`, `provenance`, `definition_sheet_version`, `status` | `InputNormalizeProvenanceNode` (FunctionNode) |
| comparability | Deterministic per-metric comparability against the definition sheet's declared rules only (metric names, semantics, allowed unit conversions, required period alignment, population/coverage, eligibility); **never invent equivalence**; a conflict or missing required metadata makes that metric non-comparable; zero comparable metrics is an explicit outcome, not an error. Emits one S-4 domain event. | `normalized_markets`, `metric_definitions`, `validated_input.requested_metrics` (ordered) | `comparability` | `ComparabilityValidateNode` (FunctionNode) |
| brief | Bounded comparison synthesis over eligible metrics only + cited Markdown brief generation. Sets `comparison_outcome`: if ≥1 requested metric is comparable → `comparison` (brief with a prominent `Not comparable / excluded` section listing every omitted metric + reason + source field); if 0 are comparable → `no_comparable_metrics` (a structured validation report, **no** comparison brief). Every numeric claim cited (source id + table/sheet/row); stamps the effective definition-sheet version; an optional, injected LLM produces **non-authoritative, source-grounded** comparison prose only (it does not decide comparability or invent figures). **LLM is never on the core path**: if no LLM is configured, or the call errors/times out, the node catches it, omits the prose, records that narrative was not generated, and still returns the deterministic cited brief — the run succeeds (no crash, no spurious `status: error`). Emits one S-4 domain event. | `comparability`, `normalized_markets`, `provenance`, `definition_sheet_version` | `result`, `comparison_outcome`, `status` | `BriefGenerateNode` (FunctionNode) |

### Data Flow

**Outer backbone** (standard pipeline; `add_edges()` not overridden):

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
```

**Inner workflow** (inside `MarketComparisonGraphNode.get_subgraph()`):

```
START → input_hydrate → normalize → comparability → brief → END
```

**Four-outcome terminal contract** (encoded as explicit non-error routes under `error_strategy = "propagate"`). The split between the last two is by **whose problem it is**: a request the caller can correct is answered, a refusal or a fault this agent owns is not.

1. **Notice (caller-correctable, no brief).** The caller's envelope (`pre_process`) or the
   caller's own market tables, sources and metric-definition sheet (`normalize`) cannot be
   accepted → `status: success` with `diagnostic_code` = the allow-listed code and the output set
   to the sentence that code already carries. No brief, and **no validation report either**: a
   validation report is a real outcome meaning the metrics were checked and none were comparable,
   which would misdescribe a request that was never examined.
2. **Comparison produced.** ≥1 requested metric comparable → `status: success` with a cited
   brief; every non-comparable metric is normal bounded degradation shown in the
   `Not comparable / excluded` section (not an error). This is a normal `brief` success
   route — no error branch.
3. **No comparable metric.** 0 requested metrics comparable → `status: success` **plus**
   `comparison_outcome: no_comparable_metrics`, a structured validation report and **no**
   comparison brief. A non-exception normal `brief` success route (the discriminator is
   carried on the result payload; `AgentStatus` has no bespoke member).
4. **Reject (fail-closed floor).** The structural screen refusing a payload that is not really a
   document (oversize / over-deep / control characters / non-JSON types — it exists to stop such
   a payload being walked at all, so its refusal is never softened); a broken handoff into the
   inner graph (`input_hydrate`); an invalid state reaching `comparability` or `brief`, whose
   checks re-read what `normalize` produced and therefore mean corrupt State rather than a
   business outcome; and the terminal egress gate. → `status: error`, no output except
   non-sensitive diagnostics. Under `propagate`, the inner error aborts to the outer `finalize` —
   no partial brief.

**The classification is per raise site, not per code.** `ERR_MARKET_COUNT` and
`ERR_REQUESTED_METRICS` are raised both in `normalize` (the first look at the caller's data → a
notice) and in `comparability` (a re-check of upstream output → fail-closed). Each node decides.

**How the marker crosses the inner boundary.** An inner node that errors is re-raised as
`SubgraphError` *before* `merge_output()` runs, so an inner error can never carry its code
outward. A caller-correctable rejection therefore **completes** the inner graph instead: the node
returns `SUCCESS` + `diagnostic_code`, `_continue_or_end()` ends on that marker as well as on the
status, the inner `get_output()` projects it, and `merge_output()` maps it (the outer marker wins —
it names the specific reason). `main` skips the inner graph entirely when `pre_process` already
set it.

The caller-facing sentence is the one `failure_message.explain()` already derives from the code;
this template deliberately keeps **no second catalogue of wording**. Only the field name the code
itself carries is ever substituted, so no request value, table cell or source reference can appear.

The outer `post_process` S-3 gate may additionally convert an outcome 2/3 output to a
terminal `status: error` (result/formatted_output both `None`) if the composed brief
contains prohibited (forecast/rank/recommend/causal) or injection-derived content.

**LLM availability is not a terminal condition.** The optional comparison-prose LLM is
never on the core path. An absent, unconfigured, or failing LLM does **not** produce
outcome 1 (reject) — `brief` degrades gracefully to the deterministic cited brief with the
prose omitted, and outcomes 2/3 are produced exactly as above. This keeps the template
implementable, testable, and releasable without live LLM infrastructure.

### State Definition

Domain fields the template **adds** on top of the inherited `AgentState`
(`user_input`, `validated_input`, `result`, `formatted_output`, `status`,
`response_metadata`, `node_history`, `error_log`, `execution_time`, … are inherited and
**not** redeclared). All fields are primitives / JSON-serialisable.

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| normalized_markets | dict | Canonical normalised tables for the two markets | Produced by `normalize` (inner) |
| metric_definitions | dict | Canonical metric-definition sheet (declared rules) | Produced by `normalize` (inner) |
| provenance | dict | Per-input source metadata sufficient for citation | Produced by `normalize` (inner) |
| definition_sheet_version | str | Effective metric-definition-sheet version, stamped on the brief | Produced by `normalize`; projected to caller |
| diagnostic_code | str | Allowlisted, non-sensitive code naming why no brief was produced, on a run the caller can correct. Turned into the caller-facing sentence by `post_process`. Absent on a normal run and on a fail-closed one; never carries a request value, table cell or source reference | Produced by `pre_process` or inner `normalize`; projected to caller |
| comparability | dict | Per-metric comparable/non-comparable decision + reason + source field | Produced by `comparability` (inner) |
| comparison_outcome | str | Result discriminator: `comparison` \| `no_comparable_metrics` | Produced by `brief`; projected to caller |

**Inner vs outer projection.** The inner→outer channel is the inner graph's `get_output()`
dict (see Composition Pattern), not the inner state. `get_output()` surfaces only
`{output, status, comparison_outcome, definition_sheet_version, diagnostic_code, error_log}`; `merge_output`
maps that to outer `result` (from `output`, the brief or validation report),
`comparison_outcome`, `definition_sheet_version`, and `status`. `post_process` then produces
the final `formatted_output`. The inner-only working fields (`normalized_markets`,
`metric_definitions`, `provenance`, `comparability`) are never surfaced by `get_output()`, so
they cannot reach caller-facing State. `node_history` / `error_log` are `operator.add` reducer
channels and are not projected as caller-facing fields inner→outer.

**State Constraints (mandatory):**
- Flat TypedDict only (primitives + JSON-serializable types)
- No JWT, API keys, credentials in State (checkpoint DB leakage)
- No Pydantic / dataclass / `InvocationContext` in State
- Nodes return only their State delta (changed fields), never `{**state, ...}`

### Data Contracts

Minimal contracts for the caller input, the inner working records, and the terminal
`result`. All are JSON/msgpack-safe. `req` = required; a field marked optional may be
absent. Unknown top-level keys and any input that is not one of these shapes are rejected
fail-closed by `normalize` (`status: error`).

**Input envelope** (`user_input` is a JSON **string** parsed at `pre_process` — the standalone
`/invoke` adapter's `input: str` carries it; CSV market tables are accepted as an equivalent
`rows` encoding of `MarketTable`; Excel/PDF/image/free-text are rejected):

```
{
  "markets": [MarketTable, MarketTable],      // req; exactly 2, distinct "market"
  "metric_definition_sheet": DefinitionSheet, // req
  "requested_metrics": ["<metric_id>", ...]   // req; non-empty
}
```

| Type | Field | Type/values | Req |
|------|-------|-------------|-----|
| MarketTable | market | str | ✓ |
| | reporting_period | str (ISO 8601 date or range, e.g. `2024-01/2024-12`) | ✓ |
| | population_coverage | str (e.g. `all_visitors`) | ✓ |
| | unit | str (e.g. `persons`) | ✓ |
| | rows | list[MetricRow] | ✓ |
| | source | Source | ✓ |
| MetricRow | metric | str | ✓ |
| | value | number \| null | ✓ |
| | unit / period | str (overrides the table-level value) | — |
| Source | source_id | str | ✓ |
| | title / url | str | — |
| | export_date | str (ISO 8601) | ✓ |
| | footnotes | list[str] | — |
| DefinitionSheet | version | str | ✓ |
| | metrics | list[MetricDef] | ✓ |
| MetricDef | metric | str | ✓ |
| | semantics | str | ✓ |
| | required_period_alignment | `exact` \| `same_year` \| `none` | ✓ |
| | required_population | str | — |
| | allowed_unit_conversions | list[{from:str,to:str,factor:number}] | — |
| | eligible | bool | ✓ |

**Canonical normalized record** (produced by `normalize`, one per market × metric):
`{ market:str, metric:str, value:number|null, unit:str, period:str, population:str, source_ref:{source_id:str, table:str, row:int} }` — `table` = the supplied market identifier, `row` = the `MetricRow` index.

**Comparability decision** (produced by `comparability`, one per requested metric):
`{ metric:str, comparable:bool, reason:ReasonEnum, market_a_ref:source_ref|null, market_b_ref:source_ref|null }`
`ReasonEnum` = `comparable` \| `period_mismatch` \| `population_mismatch` \| `unit_incompatible` \| `metric_undefined` \| `missing_required_metadata` \| `metric_absent`.

**`result` — `comparison_outcome: comparison`** (≥1 comparable):
```
{
  "brief_markdown": str,                       // cited Markdown brief
  "definition_sheet_version": str,
  "compared_metrics": [ { metric:str,
      market_a:{value:number|null,unit:str,period:str},
      market_b:{value:number|null,unit:str,period:str},
      citations:[{source_id:str,table:str,row:int}] } ],
  "excluded_metrics": [ { metric:str, reason:ReasonEnum, source_field:str } ]
}
```

**`result` — `comparison_outcome: no_comparable_metrics`** (0 comparable — no brief):
```
{
  "validation_report": { "requested_metrics":[str,...],
      "per_metric":[ {metric:str, comparable:false, reason:ReasonEnum} ] },
  "definition_sheet_version": str
}
```

Every numeric claim/row in a brief carries a `{source_id, table, row}` citation; URLs and
bibliographic details are never fabricated.

**Worked examples**

- *Positive (comparison).* Two markets each supply `overnight_visitors` for
  `2024-01/2024-12`, population `all_visitors`, unit `persons`; the sheet marks the metric
  `eligible` with `required_period_alignment: exact`. A second requested metric
  `same_day_visitors` is present for only one market → `status: success`,
  `comparison_outcome: comparison`, brief compares `overnight_visitors` and lists
  `same_day_visitors` under `excluded_metrics` with `reason: metric_absent`.
- *Negative (no_comparable).* The only requested metric `overnight_visitors` is reported
  for `2024` in market A and `2023` in market B with `required_period_alignment: exact` →
  `status: success`, `comparison_outcome: no_comparable_metrics`, `validation_report` with
  `reason: period_mismatch`, no `brief_markdown`.
- *Reject.* A market table supplied as PDF, or `metric_definition_sheet.version` absent →
  `status: error`, no output (non-sensitive diagnostics only).

### Verification Traceability

Each design/security requirement maps to the verification artifact that will assert it
(authored at the Test stage; this table establishes design → verification traceability —
the referenced test IDs follow the framework TC/PB scheme).

| Design requirement | Verified in |
|--------------------|-------------|
| S-1: anonymous / insufficient-trust caller rejected at the outer `pre_process` **before** `execute()` | `tests/proof_of_boundary/` (input & trust) + `docs/03_test_spec.md` TC-08 |
| S-2: unsupported format / malformed schema / missing definition-sheet version → answered with `status: success` + `diagnostic_code`, no brief and no validation report; the structural screen (oversize / over-deep / control chars / non-JSON types) stays fail-closed `status: error` | `docs/03_test_spec.md` (Business Logic) + `tests/proof_of_boundary/` (input) |
| S-3: forecast/rank/recommend/causal or injection-derived output → `status: error` with **both** `result` and `formatted_output` `None`; no brief body in caller response / `error_log` / trace | `tests/proof_of_boundary/` (output & provenance) + `docs/03_test_spec.md` |
| Three terminal outcomes (reject / comparison / `no_comparable_metrics`) each reachable and distinct | `docs/03_test_spec.md` + `tests/proof_of_boundary/` (output) |
| S-4: exactly one domain `emit_trace_event()` per node `execute()` | `docs/03_test_spec.md` TC-05 + `tests/proof_of_boundary/` (PB-1) |
| `merge_output` projects only terminal fields; inner working fields do not leak to caller State | `docs/03_test_spec.md` + `tests/proof_of_boundary/` (composition) |
| LLM unavailable / unconfigured / failing → deterministic cited brief still produced (prose omitted), no crash, no spurious `status: error` | `docs/03_test_spec.md` (fake LLM + no-LLM configs) |

## Framework Utilization

### Shared Components Used
- [x] InvocationContext (correlation_id, session_id, trust_level, credential handle) — via `InvocationContext.from_state(state)`, never constructed directly
- [x] SecurityViolationError — raised by the S-1 trust gate on insufficient caller trust
- [x] S-1: `required_trust_level` — `VERIFIED_EXTERNAL` on the outer boundary nodes (`pre_process`, `post_process`); `ANONYMOUS` on all inner nodes (S-1 enforced at the outer boundary; `BaseGraph.invoke()` does not propagate caller trust)
- [x] S-2: `_extra_security_gate_input()` — on `normalize`: structural sanitisation of untrusted free-text (NUL/control-char rejection, length bounds) + type/shape validation of the canonical schema. **Structural only — it does not detect or block prompt injection** (that is the S-3 gate's job). Never overrides `_security_gate_input()`.
- [x] S-3: `_extra_security_gate_output()` — auxiliary/node-local redaction only (defense-in-depth). **It is NOT the sole basis for the terminal block.** The terminal S-3 egress decision is taken in the outer `post_process` node's `execute()` over the full composed brief (see below). Never overrides `_security_gate_output()`.
- [x] S-4: `emit_trace_event()` — at least one domain event inside every node's `execute()` (mandatory; `node_start`/`node_complete`/`node_error` are emitted automatically by `BaseNode.__call__()` and are not duplicated)

> **S-3 terminal-block placement (design-critical).** The framework baseline S-3
> credential scan (`@final`, fail-closed) runs automatically. The domain terminal block —
> rejecting forecast/rank/recommend/causal and injection-derived (instruction-following)
> output — is implemented in the **outer `post_process` node's `execute()`**, which sees
> the full composed brief in State and can clear the whole caller-facing output. It
> returns `{status: AgentStatus.ERROR.value, result: None, formatted_output: None,
> error_log: [non-sensitive code]}`, clearing **both** keys because the final output is
> `formatted_output or result`. `_extra_security_gate_output()` only sees a node's own
> delta, so it cannot clear the composed brief already in outer State — it is therefore
> limited to auxiliary redaction and is not relied on for the terminal block.

### Composition Pattern

- **Pattern**: GraphNode (in-process subgraph) — the `main` slot is a `MarketComparisonGraphNode` wrapping `MarketComparisonInnerGraph(BaseGraph)`
- **Composition target**: inner `MarketComparisonInnerGraph`; the inner graph's head is `input_hydrate` (str→`validated_input`) because `BaseGraph.invoke(user_input: str)` sets only the inner `user_input`
- **Error propagation strategy**: propagate (inner error aborts to outer `finalize`; no partial output). `propagate_hitl = False` (no HITL in this template)
- **Inner `get_output()` — mandatory, and the ONLY inner→outer channel.** Verified against
  the framework: `GraphNode.execute()` calls `sub_result = subgraph.invoke(...)`, and
  `BaseGraph.invoke()` returns **`self.get_output(inner_state)` — a narrow dict, NOT the full
  inner state**. `BaseGraph.get_output` is `@abstractmethod`, so `MarketComparisonInnerGraph`
  MUST define it to surface every field the boundary needs. It returns exactly:
  `{"output": inner_state.get("result"), "status": inner_state.get("status"),
  "comparison_outcome": inner_state.get("comparison_outcome"),
  "definition_sheet_version": inner_state.get("definition_sheet_version"),
  "error_log": inner_state.get("error_log", [])}`. `status` is the `AgentStatus.<X>.value`
  string; `error_log` is included so the framework reject path (below) can read it.
- **`merge_output(state, sub_result)` — explicit mapping over the `get_output()` dict** (its
  argument is that narrow dict, **not** the inner state; no raw pass-through):
  `{"result": sub_result["output"], "comparison_outcome": sub_result.get("comparison_outcome"),
  "definition_sheet_version": sub_result.get("definition_sheet_version"),
  "status": sub_result["status"]}`. The inner working fields (`normalized_markets`,
  `metric_definitions`, `provenance`, `comparability`) are never surfaced by `get_output()`, so
  they are not in `sub_result` and structurally cannot leak to caller-facing State.
- **Reject (outcome 1) is taken by the framework before `merge_output`.** When the inner run
  sets `status = "error"`, `GraphNode.execute()` sees `sub_result["status"] == "error"` and,
  under `error_strategy = "propagate"`, raises `SubgraphError` **before** `merge_output` runs —
  so no partial result reaches outer State. The `main` node's `BaseNode.__call__()` catches that
  exception and returns `status: error`; `AgentBaseGraph.route()` then sends the errored state
  straight to `finalize` (skipping `post_process`), and `get_output()` (= `formatted_output or
  result`) is `None`. Net: graceful `status: error`, no output — never an exception out of
  `invoke()`.
- **Comparison-prose LLM**: reached through the keyless adapter in `src/services/llm.py` (`AzureOpenAINarrativeLLM`), which the graph wires by default; a caller-supplied `config["llm"]` wins and is how tests inject a fake. Consumed only by `brief`. It is non-authoritative; when the values cannot be resolved or the call fails the node degrades gracefully (deterministic brief, prose omitted). No live LLM is required to implement, test, or release — unit/PB tests run with a fake LLM and with no LLM configured

## EU AI Act Art.13 Design-Time Evidence (Advisory until 2026-09-01; required from 2026-09-01 when the approved proposal declares Annex III `In scope`)

> Not applicable. The EU AI Act Annex III transparency declaration applies to
> FIN / GOV / HCR / ENE / RET templates. TRV (travel / tourism) is outside that set, so
> the approved proposal records no Annex III `In scope` determination and no Art.13
> design-time evidence is required here.

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: framework/ and shared/ only (no agents/base/ required)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | Bounded, fixed multi-step pipeline (normalise → validate → synthesise → brief); not a self-directed think-act loop |
| Composition pattern | Standalone FunctionNodes | GraphNode (inner subgraph) | **GraphNode** | Cat 2: the deterministic domain workflow is encapsulated in the inner `BaseGraph`, keeping the outer backbone standard and the domain steps reusable |
| S-3 terminal block placement | `_extra_security_gate_output()` hook | Outer `post_process` `execute()` | **Outer `post_process` `execute()`** | The hook only sees a node's own delta and cannot clear the full composed brief in outer State; the terminal block must clear both `result` and `formatted_output` (`formatted_output or result`) |
| Zero-comparable outcome | `status: error` | `status: success` + `comparison_outcome: no_comparable_metrics` | **success + discriminator** | It is a normal, non-exception outcome (a validation report), not a failure; modelled as a non-error route so it is reachable under `error_strategy = "propagate"` |
| Metric-definition sheet | Hidden in-agent standard | Explicit versioned per-invocation input | **Explicit versioned input** | Reproducible, auditable comparability; the effective version is stamped on every brief; no mutable in-agent standard |
| Comparison LLM | Authoritative | Optional, non-authoritative prose only | **Optional, non-authoritative** | Normalisation + comparability are deterministic and authoritative; any LLM is confined to source-grounded prose and never decides comparability or invents figures |
| LLM unavailable | Hard-fail (`status: error`) | Graceful degrade (deterministic brief, prose omitted) | **Graceful degrade** | The LLM is not on the core path; a missing/failing LLM must not block a valid deterministic brief, nor block implementation/test/release while dev LLM provisioning is unresolved. A genuine reject (outcome 1) remains reserved for malformed/uncitable input |
