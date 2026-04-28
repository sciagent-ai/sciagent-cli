"""B9 — launch-failure surfacing (mocked, $0).

v4.1 §2 acceptance: a deliberately broken job (e.g. invalid image_id)
returns a structured error within 60 s, not after a 10-min poll loop.

PR #4 already lands the implementation (LaunchError + fail-fast poll +
ComputeTool surfacing). This file is the explicit B9 contract test:
end-to-end through ComputeTool with realistic Sky payloads + a wall-clock
guard, so a regression that re-introduces the 10-min poll path is caught
by CI without paying for an AWS launch.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sciagent.compute.job import LaunchError


def _failed_payload(msg: str) -> SimpleNamespace:
    return SimpleNamespace(
        status=SimpleNamespace(name="FAILED"),
        status_msg=msg,
        error=None,
    )


def test_b9_invalid_image_surfaces_structured_error_under_budget():
    """B9 acceptance: invalid image_id → structured ToolResult failure with
    the original Sky message preserved, well under the 60 s fail-fast budget.

    Wall-clock < 5 s is a generous CI-safe bound; the actual mocked path
    completes in milliseconds. The point of timing the call is to catch the
    regression where someone accidentally re-introduces a long blocking poll
    on the launch-failure path."""
    from sciagent.tools.atomic.compute import ComputeTool
    from sciagent.compute.backends.skypilot import SkyPilotBackend

    # Wire a real SkyPilotBackend with a mocked sky module so the fail-fast
    # poll runs end-to-end (no shortcut via patching the backend method).
    backend = SkyPilotBackend()
    mock_sky = MagicMock()
    mock_sky.launch.return_value = "fake-request-id"
    mock_sky.api_status.return_value = [
        _failed_payload("image pull failed: no matching manifest for linux/amd64")
    ]
    mock_sky.Resources = MagicMock()
    mock_sky.Task = MagicMock()
    backend._sky = mock_sky

    # Forward router.run to the real backend.run so the fail-fast poll runs
    # end-to-end against the mocked sky module.
    def _route(job, backend=None, background=True):
        return backend_under_test.run(job, background=background)

    backend_under_test = backend
    fake_router = MagicMock()
    fake_router.list_backends.return_value = ["skypilot"]
    fake_router._backends = {"skypilot": backend_under_test}
    fake_router.select.return_value = (backend_under_test, "Using requested backend: skypilot")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.1}
    fake_router.run.side_effect = _route

    ComputeTool._shared_session_id = None
    tool = ComputeTool()

    start = time.monotonic()
    with patch.object(tool, "_get_router", return_value=fake_router), patch(
        "sciagent.compute.backends.skypilot.time.sleep"
    ):
        result = tool.execute(
            command="echo hi",
            image="bogus:tag",
            backend="skypilot",
        )
    elapsed = time.monotonic() - start

    assert result.success is False
    assert result.output["failure_type"] == "launch_rejected"
    # The original Sky message must be preserved verbatim — debug paths
    # rely on it; "Compute job failed: ..." with a generic wrapper is not
    # acceptable per B9.
    assert "no matching manifest" in (result.error or "")
    # Generous CI-safe bound on elapsed wall-clock; real budget is 60 s.
    assert elapsed < 5.0, f"fail-fast path took {elapsed:.2f}s — should be near-instant when mocked"


def test_b9_returns_structured_failure_not_raw_exception():
    """Regression guard: a LaunchError must be CONVERTED to a structured
    ToolResult by ComputeTool — never propagated as a raw exception. The
    agent-facing contract for compute_run is "always returns a ToolResult"."""
    from sciagent.tools.atomic.compute import ComputeTool

    fake_skypilot = MagicMock()
    fake_skypilot.name = "skypilot"
    fake_router = MagicMock()
    fake_router.list_backends.return_value = ["skypilot"]
    fake_router._backends = {"skypilot": fake_skypilot}
    fake_router.select.return_value = (fake_skypilot, "ok")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.0}
    fake_router.run.side_effect = LaunchError("controller-side reject: no resources")

    ComputeTool._shared_session_id = None
    tool = ComputeTool()

    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(command="echo hi", image="alpine", backend="skypilot")

    assert result.success is False
    assert result.error is not None
    assert "no resources" in result.error
    assert result.output["failure_type"] == "launch_rejected"
    # output must include enough context for the agent to act:
    assert result.output["backend_attempted"] == "skypilot"
    assert result.output["image"] == "alpine"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
