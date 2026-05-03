"""bg_wait must honor the agent's interrupt event.

Symptom from real user trace: agent stuck in bg_wait, user hits Ctrl+C
multiple times, types 'stop'/'exit', nothing happens — eventually has to
close the terminal entirely (and loses venv state). The bg_wait long-poll
loop was using time.sleep(N) and didn't check the interrupt event between
sky RPC calls; the user couldn't escape without the 3x Ctrl+C force-kill.

Post-fix: BgWaitTool exposes a class-level interrupt event that the
AgentLoop wires up at startup. The block=True polling loop checks the
event before each sky RPC and uses Event.wait(timeout) instead of
time.sleep so a Ctrl+C wakes the wait immediately. The block=False
snapshot path also bails early if the event was set before the call.

These tests pin both behaviors so a future refactor can't silently
re-introduce the unstoppable-bg_wait trap.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from sciagent.compute.job import JobResult, JobStatus
from sciagent.tools.atomic.bg_tools import BgWaitTool
from sciagent.tools.registry import BaseTool


@pytest.fixture(autouse=True)
def _clear_interrupt_event():
    """Reset the class-level event so tests don't leak state into each other."""
    BaseTool._shared_interrupt_event = None
    yield
    BaseTool._shared_interrupt_event = None


def _patched_router(get_status_return):
    fake_router = MagicMock()
    if isinstance(get_status_return, list):
        fake_router.get_status.side_effect = get_status_return
    else:
        fake_router.get_status.return_value = get_status_return
    fake_class = MagicMock(return_value=fake_router)
    return patch("sciagent.compute.router.ComputeRouter", fake_class), fake_router


def test_block_false_with_pre_set_interrupt_returns_immediately():
    """If the user already hit Ctrl+C before bg_wait was called, the
    snapshot path must bail before making the sky RPC instead of
    burning one more round-trip the user doesn't want."""
    event = threading.Event()
    event.set()
    BaseTool.set_shared_interrupt_event(event)

    result = JobResult(status=JobStatus.RUNNING, summary="running")
    ctx, router = _patched_router(result)

    with ctx:
        tool = BgWaitTool()
        out = tool.execute(job_id="sciagent-x", block=False)

    assert out.success is True
    assert "interrupted" in out.output.lower()
    # The whole point: no get_status call should have been made.
    router.get_status.assert_not_called()


def test_block_true_with_interrupt_during_poll_exits_quickly():
    """The exact regression: user hits Ctrl+C during a long-poll. The
    polling loop must wake within one poll interval and return the
    'interrupted' result — not keep looping until timeout."""
    event = threading.Event()
    BaseTool.set_shared_interrupt_event(event)

    # Always non-terminal, so without an interrupt the loop would run
    # until the timeout. The interrupt fires from a side thread mid-poll.
    result = JobResult(status=JobStatus.RUNNING, summary="running")
    ctx, router = _patched_router(result)

    def _trip_interrupt_after_short_delay():
        time.sleep(0.05)
        event.set()

    threading.Thread(target=_trip_interrupt_after_short_delay, daemon=True).start()

    with ctx:
        tool = BgWaitTool()
        # Speed up the test: tiny poll interval, generous timeout. The
        # interrupt should beat the timeout by orders of magnitude.
        tool._BLOCK_POLL_INTERVAL_SEC = 0.02  # type: ignore[attr-defined]
        start = time.monotonic()
        out = tool.execute(job_id="sciagent-y", block=True, timeout=10)
        elapsed = time.monotonic() - start

    assert out.success is True
    assert "interrupted" in out.output.lower()
    # Without the fix, this would have been ~10s. With the fix, well
    # under a second.
    assert elapsed < 2.0, (
        f"bg_wait took {elapsed:.2f}s to honor an interrupt; the fix "
        f"should make it sub-second."
    )


def test_block_true_without_interrupt_event_falls_back_to_time_sleep():
    """When the AgentLoop isn't running (standalone tool use, tests, etc.)
    and no interrupt event was wired in, bg_wait must still work — just
    without the interrupt-aware wake-up. Falls back to time.sleep."""
    BaseTool._shared_interrupt_event = None

    # Single non-terminal then terminal.
    results = [
        JobResult(status=JobStatus.RUNNING, summary="running"),
        JobResult(status=JobStatus.COMPLETED, summary="done"),
    ]
    ctx, router = _patched_router(results)

    with ctx:
        tool = BgWaitTool()
        tool._BLOCK_POLL_INTERVAL_SEC = 0.01  # type: ignore[attr-defined]
        out = tool.execute(job_id="sciagent-z", block=True, timeout=5)

    assert out.success is True
    # No interrupt → loop ran to terminal → "completed" path.
    assert "completed" in out.output.lower()


def test_block_false_without_interrupt_set_uses_normal_path():
    """Snapshot path with no interrupt set should be unchanged — single
    get_status, return result. Confirms the new bail-out logic doesn't
    fire spuriously."""
    event = threading.Event()
    # NOT set.
    BaseTool.set_shared_interrupt_event(event)

    result = JobResult(status=JobStatus.COMPLETED, summary="done")
    ctx, router = _patched_router(result)

    fake_fetch = MagicMock(return_value={
        "ok": True, "file_count": 0, "bytes_total": 0, "files": [],
        "bucket": "x", "dest": ".",
    })

    with ctx, patch(
        "sciagent.tools.atomic.compute_fetch.fetch_workspace_outputs",
        fake_fetch,
    ):
        tool = BgWaitTool()
        out = tool.execute(job_id="sciagent-q", block=False)

    assert out.success is True
    # Real status path was taken — completed message, NOT interrupted.
    assert "completed" in out.output.lower()
    assert "interrupted" not in out.output.lower()
    router.get_status.assert_called_once()
