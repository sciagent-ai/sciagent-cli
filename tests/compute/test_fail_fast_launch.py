"""B4 — fail-fast on sky.launch via sky.api_status polling (v4.2 §C5).

These tests pin the contract:
- A FAILED launch raises LaunchError inside the budget (no 10-min wait).
- A SUCCEEDED launch returns silently, ending the poll early.
- A still-PENDING launch after the budget returns silently (legitimate
  long-provisioning case; caller proceeds with normal status polling).
- Transient api_status raises don't synthesize phantom LaunchErrors.

All fully mocked — no real sky.launch, no real sky.api_status, no real time.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sciagent.compute.backends.skypilot import SkyPilotBackend
from sciagent.compute.job import (
    ComputeRequirements,
    Job,
    LaunchError,
)


def _payload(status_name: str, status_msg: str = "") -> SimpleNamespace:
    """Build a fake RequestPayload that mimics what sky.api_status returns."""
    status = SimpleNamespace(name=status_name)
    return SimpleNamespace(
        status=status,
        status_msg=status_msg,
        error=None,
    )


def _backend_with_mock_sky() -> SkyPilotBackend:
    backend = SkyPilotBackend()
    mock_sky = MagicMock()
    mock_sky.launch.return_value = "fake-request-id"
    mock_sky.Resources = MagicMock()
    mock_sky.Task = MagicMock()
    backend._sky = mock_sky
    return backend


def _job() -> Job:
    return Job(
        id="abc123",
        service="custom",
        image="alpine",
        command="echo hi",
        requirements=ComputeRequirements(cpus=1, memory_gb=1, timeout_sec=0),
    )


# ---------------------------------------------------------------------------


def test_fail_fast_raises_launch_error_on_failed_status():
    """B4: when sky.api_status reports FAILED, run() must raise LaunchError
    with the controller's status message (no silent fallthrough)."""
    backend = _backend_with_mock_sky()
    backend._sky.api_status.return_value = [
        _payload("FAILED", status_msg="image pull failed: no matching manifest")
    ]

    with patch("sciagent.compute.backends.skypilot.time.sleep"), pytest.raises(
        LaunchError
    ) as exc_info:
        backend.run(_job(), background=True)

    assert "no matching manifest" in str(exc_info.value)


def test_fail_fast_raises_on_cancelled_status():
    """CANCELLED is treated as a launch failure for the agent's purposes —
    the cluster will not come up, so we surface it the same way as FAILED."""
    backend = _backend_with_mock_sky()
    backend._sky.api_status.return_value = [
        _payload("CANCELLED", status_msg="user cancelled launch")
    ]

    with patch("sciagent.compute.backends.skypilot.time.sleep"), pytest.raises(
        LaunchError
    ):
        backend.run(_job(), background=True)


def test_fail_fast_returns_on_succeeded():
    """A SUCCEEDED launch ends the poll early and returns the cluster_name."""
    backend = _backend_with_mock_sky()
    backend._sky.api_status.return_value = [_payload("SUCCEEDED")]

    with patch("sciagent.compute.backends.skypilot.time.sleep"):
        cluster_name = backend.run(_job(), background=True)

    assert cluster_name == "sciagent-abc123"


def test_fail_fast_budget_exceeded_returns_cluster_name():
    """A launch that's still PENDING when the budget elapses must NOT raise —
    that's the legitimate long-provisioning case; the caller proceeds with
    normal status polling. This is the regression guard that protects against
    converting "slow but valid" launches into spurious LaunchErrors."""
    import itertools

    backend = _backend_with_mock_sky()
    backend._sky.api_status.return_value = [_payload("PENDING")]

    # Each monotonic() call advances 10s; after ~7 calls we're past the 60s
    # default budget and the loop exits cleanly.
    fake_clock = itertools.count(start=0.0, step=10.0)
    with patch(
        "sciagent.compute.backends.skypilot.time.monotonic",
        side_effect=lambda: next(fake_clock),
    ), patch("sciagent.compute.backends.skypilot.time.sleep"):
        cluster_name = backend.run(_job(), background=True)

    assert cluster_name == "sciagent-abc123"


def test_fail_fast_tolerates_transient_api_status_errors():
    """A flaky api_status call inside the budget must not synthesize a
    LaunchError; we keep polling. Only an actual FAILED/CANCELLED payload
    surfaces as a launch failure."""
    backend = _backend_with_mock_sky()
    backend._sky.api_status.side_effect = [
        RuntimeError("api server hiccup"),
        [_payload("SUCCEEDED")],
    ]

    with patch("sciagent.compute.backends.skypilot.time.sleep"):
        cluster_name = backend.run(_job(), background=True)

    assert cluster_name == "sciagent-abc123"


def test_compute_tool_surfaces_launch_error_as_structured_failure():
    """B4 acceptance via ComputeTool: a LaunchError surfaces as a structured
    ToolResult(success=False, output['failure_type']='launch_rejected'),
    not the generic 'Compute job failed: ...' wrapping. This is the bar that
    makes B9's mocked test cheap and meaningful."""
    from sciagent.tools.atomic.compute import ComputeTool

    ComputeTool._shared_session_id = None
    tool = ComputeTool()

    fake_skypilot = MagicMock()
    fake_skypilot.name = "skypilot"

    fake_router = MagicMock()
    fake_router.list_backends.return_value = ["skypilot"]
    fake_router._backends = {"skypilot": fake_skypilot}
    fake_router.select.return_value = (
        fake_skypilot,
        "Using requested backend: skypilot",
    )
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.0}
    fake_router.run.side_effect = LaunchError("invalid image_id: docker:bogus:tag")

    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="echo hi",
            image="bogus:tag",
            backend="skypilot",
        )

    assert result.success is False
    assert result.output["failure_type"] == "launch_rejected"
    assert "invalid image_id" in (result.error or "")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
