"""Mocked signature tests for SkyPilotBackend (covers B1, B2, B3 from M0).

These guard against the regression class that has bitten us repeatedly:
sciagent calling sky.* with parameters that don't match the installed
SkyPilot version. The tests pin the kwargs we send to sky.status/sky.queue
and assert the recursion guard in get_job_status.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sciagent.compute.backends.skypilot import SkyPilotBackend
from sciagent.compute.job import JobResult, JobStatus


def _make_backend_with_mock_sky(mock_sky):
    backend = SkyPilotBackend()
    backend._sky = mock_sky
    return backend


def test_get_clusters_uses_status_refresh_mode_enum():
    """B3: sky.status must be called with cluster_names=List[str] and a
    StatusRefreshMode enum (not the string 'NONE')."""
    from sky.utils.common import StatusRefreshMode

    mock_sky = MagicMock()
    mock_sky.status.return_value = "request-id"
    mock_sky.stream_and_get.return_value = []

    backend = _make_backend_with_mock_sky(mock_sky)
    backend._get_clusters("sciagent-abc123")

    mock_sky.status.assert_called_once()
    _, kwargs = mock_sky.status.call_args
    assert kwargs["cluster_names"] == ["sciagent-abc123"]
    assert isinstance(kwargs["refresh"], StatusRefreshMode), (
        f"refresh must be a StatusRefreshMode enum, got {type(kwargs['refresh']).__name__}"
    )
    # AUTO: let Sky lazily refresh stale records without forcing a full refresh.
    assert kwargs["refresh"] is StatusRefreshMode.AUTO


def test_get_queue_drops_refresh_kwarg():
    """B2: sky.queue in 0.12 has no `refresh` parameter; we must not pass one."""
    mock_sky = MagicMock()
    mock_sky.queue.return_value = "request-id"
    mock_sky.stream_and_get.return_value = []

    backend = _make_backend_with_mock_sky(mock_sky)
    backend._get_queue("sciagent-abc123")

    mock_sky.queue.assert_called_once()
    _, kwargs = mock_sky.queue.call_args
    assert kwargs == {"cluster_name": "sciagent-abc123"}, (
        f"sky.queue should be called with only cluster_name; got {kwargs}"
    )


def test_get_queue_signature_matches_installed_sky():
    """Belt-and-braces: the kwargs we send must be accepted by the real sky.queue
    signature in the installed package. Catches drift without launching anything."""
    import inspect

    import sky

    sig = inspect.signature(sky.queue)
    # Bind only what _get_queue sends today; this raises TypeError on a mismatch.
    sig.bind_partial(cluster_name="sciagent-abc123")


def test_get_status_signature_matches_installed_sky():
    """Same belt-and-braces check for sky.status."""
    import inspect

    import sky
    from sky.utils.common import StatusRefreshMode

    sig = inspect.signature(sky.status)
    sig.bind_partial(
        cluster_names=["sciagent-abc123"],
        refresh=StatusRefreshMode.AUTO,
    )


def test_get_job_status_does_not_recurse_on_query_failure():
    """B1: when the queue query raises, get_job_status must NOT fall back to
    get_status() — that path calls back into get_job_status() and recurses
    until the stack blows. Return a transient PENDING instead."""
    mock_sky = MagicMock()
    mock_sky.queue.side_effect = RuntimeError("sky API offline")

    backend = _make_backend_with_mock_sky(mock_sky)

    # Spy on get_status so we can assert it is NOT invoked from the except branch.
    with patch.object(backend, "get_status", wraps=backend.get_status) as spy_get_status:
        result = backend.get_job_status("sciagent-abc123")

    assert isinstance(result, JobResult)
    assert result.status is JobStatus.PENDING, (
        f"expected transient PENDING on query failure, got {result.status}"
    )
    spy_get_status.assert_not_called()


def test_get_job_status_recursion_bounded_under_repeated_failure():
    """Regression guard: even if both APIs fail repeatedly, no recursion."""
    import sys

    mock_sky = MagicMock()
    mock_sky.queue.side_effect = RuntimeError("queue offline")
    mock_sky.status.side_effect = RuntimeError("status offline")

    backend = _make_backend_with_mock_sky(mock_sky)

    # Cap recursion low enough that the old behavior would trip it.
    original_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(50)
    try:
        result = backend.get_job_status("sciagent-abc123")
    finally:
        sys.setrecursionlimit(original_limit)

    assert result.status is JobStatus.PENDING


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
