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
    # M1A swaps the launch path to sky.jobs.launch — the mock must mirror
    # that. The cluster-mode sky.launch is no longer called from run().
    mock_sky.jobs.launch.return_value = "fake-request-id"
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
    with the controller's status message (no silent fallthrough). The
    LaunchError carries the would-be cluster_name so callers can clean up
    a partially-provisioned cluster."""
    backend = _backend_with_mock_sky()
    backend._sky.api_status.return_value = [
        _payload("FAILED", status_msg="image pull failed: no matching manifest")
    ]

    with patch("sciagent.compute.backends.skypilot.time.sleep"), pytest.raises(
        LaunchError
    ) as exc_info:
        backend.run(_job(), background=True)

    assert "no matching manifest" in str(exc_info.value)
    assert exc_info.value.cluster_name == "sciagent-abc123"


def test_fail_fast_falls_back_when_payload_carries_null_strings():
    """Sky stores empty payload fields as the literal JSON string ``"null"``.
    Surfacing that as ``LaunchError("null")`` is useless — the helper must
    treat ``None``/``""``/``"null"`` as "no info" and surface either (a) the
    actual controller log tail it dug via ``sky api logs <request_id>`` or
    (b) a manual hint with the request_id when the dig itself fails."""
    import subprocess

    backend = _backend_with_mock_sky()
    backend._sky.api_status.return_value = [
        SimpleNamespace(
            status=SimpleNamespace(name="FAILED"),
            status_msg=None,
            error="null",  # the literal four-char string Sky persists
        )
    ]

    # Force the auto-dig to fail (timeout) so we hit the manual-hint fallback
    # path. The "dig succeeded" path is covered separately below.
    with patch("sciagent.compute.backends.skypilot.time.sleep"), patch(
        "sciagent.compute.backends.skypilot.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="sky", timeout=10),
    ), pytest.raises(LaunchError) as exc_info:
        backend.run(_job(), background=True)

    msg = str(exc_info.value)
    assert msg != "null"
    assert "sciagent-abc123" in msg
    assert "sky api logs" in msg.lower()


def test_fail_fast_surfaces_controller_log_tail_when_payload_is_null():
    """When the api_status payload carries no usable error but the
    controller logs do, the auto-dig (`sky api logs <request_id>`) must
    enrich the LaunchError with the actual reason — operator no longer has
    to drop to bash to find out the launch failed because of, e.g., bucket
    auth or image pull."""
    import subprocess

    backend = _backend_with_mock_sky()
    backend._sky.api_status.return_value = [
        SimpleNamespace(
            status=SimpleNamespace(name="FAILED"),
            status_msg="null",
            error=None,
        )
    ]

    fake_log_output = (
        "ERROR: Failed to pull docker:ghcr.io/sciagent-ai/openfoam:latest\n"
        "denied: requested access to the resource is denied"
    )
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=fake_log_output, stderr=""
    )

    with patch("sciagent.compute.backends.skypilot.time.sleep"), patch(
        "sciagent.compute.backends.skypilot.subprocess.run",
        return_value=fake_completed,
    ), pytest.raises(LaunchError) as exc_info:
        backend.run(_job(), background=True)

    msg = str(exc_info.value)
    # The actual controller-side reason is now in the LaunchError, not just
    # "(no detail provided)".
    assert "denied: requested access to the resource is denied" in msg
    assert "Failed to pull docker:ghcr.io/sciagent-ai/openfoam" in msg
    assert "Controller log tail" in msg
    assert "no detail provided" not in msg


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
    """A SUCCEEDED launch ends the poll early and returns (name, managed_job_id).

    M1A: run() now returns a tuple. The integer is None when the test's
    mocked sky.get returns a non-tuple payload — what we care about here is
    that the name flows through unchanged.
    """
    backend = _backend_with_mock_sky()
    backend._sky.api_status.return_value = [_payload("SUCCEEDED")]

    with patch("sciagent.compute.backends.skypilot.time.sleep"):
        name, managed_job_id = backend.run(_job(), background=True)

    assert name == "sciagent-abc123"
    # MagicMock-returned sky.get payload isn't a real tuple → None is correct.
    assert managed_job_id is None


def test_fail_fast_returns_managed_job_id_on_succeeded():
    """When sky.get returns the (job_ids, controller_handle) tuple, run() must
    capture the integer managed_job_id so the manifest carries it."""
    backend = _backend_with_mock_sky()
    backend._sky.api_status.return_value = [_payload("SUCCEEDED")]
    # Sky's launch payload after SUCCEEDED: ([job_ids...], controller_handle).
    backend._sky.get.return_value = ([42], None)

    with patch("sciagent.compute.backends.skypilot.time.sleep"):
        name, managed_job_id = backend.run(_job(), background=True)

    assert name == "sciagent-abc123"
    assert managed_job_id == 42


def test_fail_fast_budget_exceeded_returns_cluster_name():
    """A launch that's still PENDING when the budget elapses must NOT raise —
    that's the legitimate long-provisioning case; the caller proceeds with
    normal status polling. This is the regression guard that protects against
    converting "slow but valid" launches into spurious LaunchErrors.

    Also: when budget-exceeded, run() must NOT call sky.get (which would
    block until the controller finishes) — managed_job_id stays None and
    later status queries can resolve it by name.
    """
    import itertools

    backend = _backend_with_mock_sky()
    backend._sky.api_status.return_value = [_payload("PENDING")]

    fake_clock = itertools.count(start=0.0, step=10.0)
    with patch(
        "sciagent.compute.backends.skypilot.time.monotonic",
        side_effect=lambda: next(fake_clock),
    ), patch("sciagent.compute.backends.skypilot.time.sleep"):
        name, managed_job_id = backend.run(_job(), background=True)

    assert name == "sciagent-abc123"
    assert managed_job_id is None
    # Critically: sky.get must not be invoked when the launch is still in-flight.
    backend._sky.get.assert_not_called()


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
        name, _ = backend.run(_job(), background=True)

    assert name == "sciagent-abc123"


def test_compute_tool_surfaces_launch_error_as_structured_failure():
    """B4 acceptance via ComputeTool: a LaunchError surfaces as a structured
    ToolResult(success=False, output['failure_type']='launch_rejected'),
    not the generic 'Compute job failed: ...' wrapping. M1A: ComputeTool
    calls selected_backend.run directly (so SkyPilot's tuple return flows
    into the manifest), so the mock raises on the backend, not the router.
    """
    from sciagent.tools.atomic.compute import ComputeTool

    ComputeTool._shared_session_id = None
    tool = ComputeTool()

    fake_skypilot = MagicMock()
    fake_skypilot.name = "skypilot"
    fake_skypilot.run.side_effect = LaunchError(
        "invalid image_id: docker:bogus:tag",
        cluster_name="sciagent-cluster42",
    )

    fake_router = MagicMock()
    fake_router.list_backends.return_value = ["skypilot"]
    fake_router._backends = {"skypilot": fake_skypilot}
    fake_router.select.return_value = (
        fake_skypilot,
        "Using requested backend: skypilot",
    )
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.0}

    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="echo hi",
            image="bogus:tag",
            backend="skypilot",
        )

    assert result.success is False
    assert result.output["failure_type"] == "launch_rejected"
    assert "invalid image_id" in (result.error or "")
    # The would-be cluster name must propagate so callers can clean up a
    # partially-provisioned cluster instead of leaving it billing.
    assert result.output.get("job_id") == "sciagent-cluster42"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
