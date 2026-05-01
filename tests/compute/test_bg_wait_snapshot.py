"""bg_wait must be snapshot-only for cloud jobs (M1A hard rule #1).

The M0 implementation polled router.get_status every 5 seconds for up to
30 seconds before returning. That sleep-inside-the-tool pattern is exactly
what M2A's wait/resume substrate has to NOT have to fight: an atomic tool
that owns the call stack for tens of seconds blocks the agent loop and
defeats persistability.

These tests pin three behaviors:

  1. The cloud-job branch makes exactly ONE get_status call — no polling
     loop, no sleep.
  2. ``timeout=`` is accepted in the schema (backwards-compat) but ignored
     for cloud jobs.
  3. Each terminal state (COMPLETED / FAILED / CANCELLED) and the non-
     terminal snapshot all return a structured ToolResult with the agent's
     recovery options spelled out.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sciagent.compute.job import JobResult, JobStatus
from sciagent.tools.atomic.bg_tools import BgWaitTool


def _patched_router(get_status_return: JobResult):
    fake_router = MagicMock()
    fake_router.get_status.return_value = get_status_return
    fake_class = MagicMock(return_value=fake_router)
    return patch("sciagent.compute.router.ComputeRouter", fake_class), fake_router


def test_bg_wait_cloud_makes_single_get_status_call():
    """One round-trip — no polling loop. Regression guard against re-introducing
    a sleep inside the cloud-job branch of bg_wait."""
    result = JobResult(status=JobStatus.RUNNING, summary="job is running")
    ctx, fake_router = _patched_router(result)

    with ctx, patch("time.sleep") as fake_sleep:
        tool = BgWaitTool()
        out = tool.execute(job_id="sciagent-abc", timeout=999)

    assert out.success is True
    assert "snapshot only" in out.output.lower()
    fake_router.get_status.assert_called_once_with("sciagent-abc")
    fake_sleep.assert_not_called()


def test_bg_wait_cloud_ignores_timeout_kwarg():
    """``timeout`` is preserved on the schema for local jobs but does
    nothing for cloud — the call is one-shot regardless of value."""
    result = JobResult(status=JobStatus.PENDING, summary="job pending")
    ctx, fake_router = _patched_router(result)

    with ctx:
        tool = BgWaitTool()
        out_short = tool.execute(job_id="sciagent-abc", timeout=1)
        out_long = tool.execute(job_id="sciagent-abc", timeout=86400)

    # Both calls return immediately with the same shape.
    assert "snapshot only" in out_short.output.lower()
    assert "snapshot only" in out_long.output.lower()
    # And neither sleeps. (We assert call count instead of timing because
    # timing is flaky in CI; the absence of any sleep call is the contract.)
    assert fake_router.get_status.call_count == 2


def test_bg_wait_cloud_completed_returns_success():
    result = JobResult(status=JobStatus.COMPLETED, summary="job done")
    ctx, _ = _patched_router(result)

    with ctx:
        tool = BgWaitTool()
        out = tool.execute(job_id="sciagent-abc")

    assert out.success is True
    assert "completed" in out.output.lower()
    assert "snapshot only" not in out.output.lower()


def test_bg_wait_cloud_failed_returns_failure_with_error_preview():
    result = JobResult(
        status=JobStatus.FAILED,
        summary="job failed on sciagent-abc",
        error_preview="ImportError: numpy",
        output_file="_logs/sciagent-abc.log",
    )
    ctx, _ = _patched_router(result)

    with ctx:
        tool = BgWaitTool()
        out = tool.execute(job_id="sciagent-abc")

    assert out.success is False
    assert "ImportError: numpy" in out.output
    assert "_logs/sciagent-abc.log" in out.output
    assert out.error == "ImportError: numpy"


def test_bg_wait_cloud_cancelled_returns_failure_distinct_from_failed():
    result = JobResult(status=JobStatus.CANCELLED, summary="cancelled by user")
    ctx, _ = _patched_router(result)

    with ctx:
        tool = BgWaitTool()
        out = tool.execute(job_id="sciagent-abc")

    assert out.success is False
    assert "cancelled" in out.output.lower()
    assert "cancelled" in (out.error or "").lower()


def test_bg_wait_cloud_recovering_is_treated_as_non_terminal_snapshot():
    """RECOVERING is one of the M1A-introduced statuses; it must NOT be
    surfaced as terminal — the spot-recovery is in progress."""
    result = JobResult(status=JobStatus.RECOVERING, summary="spot recovery")
    ctx, _ = _patched_router(result)

    with ctx:
        tool = BgWaitTool()
        out = tool.execute(job_id="sciagent-abc")

    assert out.success is True
    assert "snapshot only" in out.output.lower()
    assert "recovering" in out.output.lower()


def test_bg_wait_default_is_still_snapshot_only_for_cloud():
    """M1A hard rule #1 evolved: the *default* must still be snapshot-only
    for cloud jobs (no polling, no sleep). ``block=True`` is opt-in for
    cases where the caller knows the wait is short and wants to collapse
    N polling turns into 1; the default keeps the M1A contract intact so
    M2A's wait/resume substrate isn't fighting a tool that sleeps inside
    itself by accident."""
    schema = BgWaitTool().to_schema()
    props = schema["parameters"]["properties"]

    # `block` IS exposed (opt-in long-poll), but its default must be False
    # so the M1A non-blocking contract is the default behavior.
    assert "block" in props, "block parameter should be exposed for opt-in long-poll"
    assert props["block"].get("default") is False, (
        "block default must be False — agents must opt in explicitly to "
        "long-poll, otherwise M1A hard rule #1 is silently violated"
    )

    # Other names from older designs should remain unused.
    for forbidden in ("wait", "until"):
        assert forbidden not in props, (
            f"bg_wait schema must not expose deprecated '{forbidden}' kwarg"
        )


def test_bg_wait_block_true_polls_until_terminal():
    """Long-poll path: block=True polls every interval until the status
    becomes terminal. Pin the loop's exit condition (one terminal status
    ends the poll) and that the result is the COMPLETED branch result."""
    # Sequence: RUNNING, RUNNING, COMPLETED — should make 3 get_status
    # calls and exit on the third with the COMPLETED structured output.
    states = [
        JobResult(status=JobStatus.RUNNING, summary="step 1"),
        JobResult(status=JobStatus.RUNNING, summary="step 2"),
        JobResult(status=JobStatus.COMPLETED, summary="done"),
    ]
    fake_router = MagicMock()
    fake_router.get_status.side_effect = states
    fake_class = MagicMock(return_value=fake_router)

    with patch("sciagent.compute.router.ComputeRouter", fake_class), \
         patch("sciagent.tools.atomic.bg_tools.time.sleep") as fake_sleep, \
         patch("sciagent.tools.atomic.bg_tools.time.monotonic", side_effect=[0, 0, 1, 2, 3, 4]):
        tool = BgWaitTool()
        out = tool.execute(job_id="sciagent-loop", block=True, timeout=600)

    assert out.success is True
    assert "completed" in out.output.lower()
    assert fake_router.get_status.call_count == 3
    # Slept twice (between the 3 polls), at the configured interval.
    assert fake_sleep.call_count == 2


def test_bg_wait_block_true_returns_snapshot_on_timeout():
    """Long-poll deadline reached without terminal — return a snapshot
    that tells the agent the budget expired. Don't error: the job is
    still progressing on sky's controller; the agent can re-issue with a
    longer timeout or fall back to sparse re-polling."""
    states = [JobResult(status=JobStatus.RUNNING, summary="still going")] * 5
    fake_router = MagicMock()
    fake_router.get_status.side_effect = states
    fake_class = MagicMock(return_value=fake_router)

    # monotonic sequence: deadline = 0 + 60 = 60. Make second monotonic
    # call return 100 to trip the deadline check on the next iteration.
    monotonic_values = iter([0, 0, 100, 100, 100])

    with patch("sciagent.compute.router.ComputeRouter", fake_class), \
         patch("sciagent.tools.atomic.bg_tools.time.sleep"), \
         patch("sciagent.tools.atomic.bg_tools.time.monotonic", side_effect=lambda: next(monotonic_values)):
        tool = BgWaitTool()
        out = tool.execute(job_id="sciagent-slow", block=True, timeout=60)

    assert out.success is True
    assert "long-poll budget" in out.output.lower()
    # Should not have polled forever — at most a couple of times before
    # the deadline trip.
    assert fake_router.get_status.call_count <= 2
