# TRV-C2-036 — Unit Tests: caller-facing progress events (src/services/progress.py)
#
# The emitter is a no-op outside the Marketplace runtime, so "it did not raise" proves nothing.
# These bind a recording emitter and assert the event actually reaches it.

import pytest

from src.services.progress import emit_progress

events = pytest.importorskip("shared.services.events")


class _Recorder:
    def __init__(self):
        self.sent = []

    def emit_event(self, *, event_type, message, metadata=None, sub_event_type=None):
        self.sent.append((event_type, message, metadata))


class _Failing:
    def emit_event(self, **kwargs):
        raise RuntimeError("event transport unavailable")


def test_progress_event_reaches_the_bound_emitter():
    recorder = _Recorder()
    with events.bound_emitter(recorder):
        emit_progress("phase label")
    assert len(recorder.sent) == 1
    event_type, message, metadata = recorder.sent[0]
    assert event_type is events.EventType.PROGRESS_UPDATE
    assert message == "phase label"
    assert metadata == {}


def test_only_non_terminal_events_are_sent():
    recorder = _Recorder()
    with events.bound_emitter(recorder):
        emit_progress("phase label")
    terminal = {events.EventType.COMPLETION_SUCCESS, events.EventType.COMPLETION_FAILURE}
    assert all(sent[0] not in terminal for sent in recorder.sent)


def test_no_bound_emitter_is_a_silent_no_op():
    emit_progress("phase label")  # NullEventEmitter — must not raise


def test_emitter_failure_never_breaks_the_run():
    with events.bound_emitter(_Failing()):
        emit_progress("phase label")  # swallowed — progress must not change the outcome
