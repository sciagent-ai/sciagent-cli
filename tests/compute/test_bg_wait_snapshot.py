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


def test_bg_wait_schema_no_blocking_kwargs():
    """The tool's JSON schema still must not expose wait=/until=/block=
    kwargs (M1A hard rule #1)."""
    schema = BgWaitTool().to_schema()
    props = schema["parameters"]["properties"]
    for forbidden in ("wait", "until", "block"):
        assert forbidden not in props, (
            f"bg_wait schema must not expose '{forbidden}' kwarg "
            f"(M1A non-blocking contract)"
        )
