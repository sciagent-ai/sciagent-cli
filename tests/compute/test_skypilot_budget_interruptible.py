"""SkyPilot's _await_launch_or_fail loop must honor the BaseTool
interrupt event.

Same pattern as bg_wait/task_wait: any tool that polls in a loop must
wake on Ctrl+C instead of holding the agent loop captive for the full
budget. compute_run / compute_exec / launch_cluster / refresh_cluster_mounts
all share this 60s fail-fast budget loop in skypilot.py:_await_launch_or_fail.
A stuck launch could trap the user for a full minute pre-fix.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from sciagent.compute.backends.skypilot import SkyPilotBackend
from sciagent.compute.job import LaunchError
from sciagent.tools.registry import BaseTool


@pytest.fixture(autouse=True)
def _clear_event():
    BaseTool._shared_interrupt_event = None
    yield
    BaseTool._shared_interrupt_event = None


def _backend(mock_sky):
    b = SkyPilotBackend()
    b._sky = mock_sky
    return b


def test_await_launch_pre_set_interrupt_raises_launch_error_immediately():
    """If Ctrl+C fired before _await_launch_or_fail was called, the loop
    must bail with LaunchError on its very first iteration, NOT make any
    sky.api_status RPCs."""
    event = threading.Event()
    event.set()
    BaseTool.set_shared_interrupt_event(event)

    mock_sky = MagicMock()
    b = _backend(mock_sky)

    with pytest.raises(LaunchError) as exc_info:
        b._await_launch_or_fail(
            request_id="rid-x",
            cluster_name="c1",
            budget_sec=60.0,
        )

    assert "interrupted by user" in str(exc_info.value)
    assert exc_info.value.cluster_name == "c1"
    assert exc_info.value.request_id == "rid-x"
    mock_sky.api_status.assert_not_called()


def test_await_launch_interrupt_during_poll_exits_quickly():
    """Interrupt fires mid-poll (after a few RPCs that returned non-
    terminal). Must wake within the next poll interval and raise
    LaunchError, NOT loop until the 60s budget elapses."""
    event = threading.Event()
    BaseTool.set_shared_interrupt_event(event)

    mock_sky = MagicMock()
    # Always return a still-pending payload so the loop would normally
    # run to budget. Interrupt is the only exit condition we're testing.
    pending = MagicMock()
    pending.status.name = "PENDING"
    mock_sky.api_status.return_value = [pending]

    def _trip_after_short_delay():
        time.sleep(0.05)
        event.set()

    threading.Thread(target=_trip_after_short_delay, daemon=True).start()

    b = _backend(mock_sky)
    start = time.monotonic()
    with pytest.raises(LaunchError) as exc_info:
        b._await_launch_or_fail(
            request_id="rid-y",
            cluster_name="c2",
            budget_sec=10.0,
            poll_interval_sec=0.02,
        )
    elapsed = time.monotonic() - start

    assert "interrupted by user" in str(exc_info.value)
    assert elapsed < 2.0, (
        f"_await_launch_or_fail took {elapsed:.2f}s to honor an interrupt; "
        f"the fix should make it sub-second."
    )


def test_await_launch_no_interrupt_event_falls_back_to_time_sleep():
    """When AgentLoop isn't running and no event is wired, the loop must
    still complete normally — just without interrupt-aware wake-up."""
    BaseTool._shared_interrupt_event = None

    mock_sky = MagicMock()
    succeeded = MagicMock()
    succeeded.status.name = "SUCCEEDED"
    mock_sky.api_status.return_value = [succeeded]

    b = _backend(mock_sky)
    out = b._await_launch_or_fail(
        request_id="rid-ok",
        cluster_name="c3",
        budget_sec=5.0,
        poll_interval_sec=0.01,
    )
    assert out is True
