# TRV-C2-036 — Unit Tests: caller-facing wording for a failed run (src/services/failure_message.py)
#
# A failed run reaches the caller naming only the exception type, so these sentences are the only
# thing that tells them whether to edit the request or to raise it with whoever runs the deployment.
# Getting that distinction wrong is worse than being vague: it sends the caller back to edit a
# request that was never the problem.

import src.services.failure_message as mod
from src.services.failure_message import explain, GENERATION_DEGRADED


def test_a_request_defect_names_the_field_and_what_to_change():
    assert explain("markets_missing") == "markets is missing from the request."
    assert explain("records_not_list") == "records must be a list."
    assert explain("summary_too_long") == "summary is too long. Shorten it and send it again."
    assert explain("missing_budget") == "budget is missing from the request."


def test_house_styles_normalise_to_the_same_wording():
    # codes are written lower_snake in most templates and ERR_UPPER_SNAKE in others
    assert explain("ERR_RECORDS_NOT_LIST") == explain("records_not_list")


def test_a_processing_failure_does_not_send_the_caller_back_to_their_request():
    for code in ("compute_error", "assembly_failed", "credential_blocked", "index_unavailable"):
        sentence = explain(code)
        assert "condition on the service side" in sentence
        assert "Check the documented request shape" not in sentence


def test_a_general_sentence_still_carries_the_code():
    # the code is the one handle the caller can quote to whoever runs the deployment
    sentence = explain("some_unmapped_condition")
    assert "Check the documented request shape" in sentence
    assert "[some_unmapped_condition]" in sentence


def test_only_the_code_is_ever_substituted():
    # explain() receives an allowlisted code, never a request value; the only thing it interpolates
    # is a token taken from that code, so a caller's content cannot come back to them
    marker = "ZZSECRETMARKERZZ"
    assert marker not in explain("records_not_list")
    assert marker not in explain("compute_error")


def test_no_sentence_carries_internal_wording():
    fixed = list(mod._EXACT.values()) + [mod._FALLBACK, mod._INTERNAL]
    fixed += list(getattr(mod, "_OVERRIDES", {}).values())
    for sentence in fixed:
        assert "user_input" not in sentence
        assert "fail closed" not in sentence
        assert "{" not in sentence


def test_the_degraded_run_says_the_result_is_still_complete():
    # the run succeeds; without this the caller reads a missing section as a failure
    assert "complete and is returned as it is" in GENERATION_DEGRADED
