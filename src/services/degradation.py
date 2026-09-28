"""AgentCore Platform v1.0 — which rejections are answered, and which stay fail-closed.

A run that ends without a brief is reported one of two ways, and the split is by **whose problem
it is**. A request the caller can correct completes carrying an allow-listed ``diagnostic_code``;
the caller-facing sentence is the one ``failure_message.explain()`` already derives from that code
(this module deliberately keeps no second catalogue of wording — one place decides what a code
says, and that place is already covered by ``test_failure_message``). A refusal or a fault this
agent owns stays ``status=ERROR`` with no output.

**Answered (notice).** The caller's envelope (``pre_process``) and the caller's own market tables,
sources and metric-definition sheet (``normalize``). These are the two places where caller material
is examined for the first time, so a rejection there names something the caller can change.

**Fail-closed (unchanged).**

- The **structural screen** below. It runs in ``normalize``'s ``_extra_security_gate_input()``
  before ``execute()``, and again inside ``execute()`` as defence in depth. It exists to stop a
  payload that is not really a document — oversized, over-nested, control-character-bearing,
  non-JSON-typed — from being walked at all, so its refusals are never softened into a completed
  request. The ``execute()`` copies are listed here for that reason: the same condition must not
  become a notice merely because it was caught on the second pass.
- ``input_hydrate``. The outer node hands the inner graph an envelope it has already validated, so
  a failure to re-read it is a broken handoff between the two graphs, not a request to correct.
- ``comparability`` and ``brief``. Both re-check what ``normalize`` produced; their own docstrings
  say a broken shape there means corrupt State rather than a business outcome.
- ``post_process``. The terminal egress gate — withheld output, policy and credential findings.

Classification is per **raise site**, not per code: ``ERR_MARKET_COUNT`` and
``ERR_REQUESTED_METRICS`` are raised both in ``normalize`` (first look at the caller's data → a
notice) and in ``comparability`` (a re-check of upstream output → fail-closed). Each node decides,
which is why this module exposes a screen-code set rather than a global "correctable" list.
"""

from __future__ import annotations

from src.services.failure_message import explain

# Raised by `_screen_structural` in `normalize`. Fail-closed on both passes — see the module
# docstring. Anything else `normalize` raises is the caller's to correct.
STRUCTURAL_SCREEN_CODES = frozenset(
    {
        "ERR_STRUCTURE_TOO_DEEP",
        "ERR_STRUCTURE_TOO_LARGE",
        "ERR_STRING_TOO_LONG",
        "ERR_CONTROL_CHAR",
        "ERR_NON_STRING_KEY",
        "ERR_NON_JSON_TYPE",
        "ERR_RECURSION",
    }
)

_FALLBACK = "The request could not be completed and no comparison brief was produced."


def is_structural_screen(code: str) -> bool:
    """True when this code is the structural screen refusing a payload, not a correctable request."""
    return code in STRUCTURAL_SCREEN_CODES


def explain_degradation(code: str) -> str:
    """Return the caller-facing sentence for a degradation code.

    Delegates to ``failure_message.explain()``, which derives the sentence from the allow-listed
    code alone — only the field name the code itself carries is ever substituted, so no request
    value, table cell or source reference can appear. An empty code has no sentence to give.
    """
    return explain(code) if code else _FALLBACK
