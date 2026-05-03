"""Mocked signature tests for SkyPilotBackend's managed-jobs path (M1A).

M0 had B1/B2/B3 tests pinning the cluster-mode kwargs. After M1A's
sky.jobs.* migration there are no clusters in the hot path; these tests
pin the kwargs we send to sky.jobs.queue_v2 / sky.jobs.cancel /
sky.jobs.tail_logs and assert get_status's transient-PENDING fallback on
query failure (the recursion-guard contract carried forward in spirit).
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

from sciagent.compute.backends.skypilot import SkyPilotBackend
from sciagent.compute.job import JobResult, JobStatus


def _make_backend_with_mock_sky(mock_sky):
    backend = SkyPilotBackend()
    backend._sky = mock_sky
    return backend


# ---- queue_v2 -------------------------------------------------------------


def test_get_managed_job_queue_uses_queue_v2_no_refresh():
    """sky.jobs.queue_v2 must be called with refresh=False — refresh=True
    forces the controller to re-query every cloud, which is multi-second
    and unsuitable for the agent's hot status-poll path."""
    mock_sky = MagicMock()
    mock_sky.jobs.queue_v2.return_value = "request-id"
    mock_sky.stream_and_get.return_value = ([], 0, {}, 0)

    backend = _make_backend_with_mock_sky(mock_sky)
    backend._get_managed_job_queue()

    mock_sky.jobs.queue_v2.assert_called_once()
    _, kwargs = mock_sky.jobs.queue_v2.call_args
    assert kwargs.get("refresh") is False, (
        "sky.jobs.queue_v2 must be called with refresh=False on the hot path"
    )


def test_get_managed_job_queue_signature_matches_installed_sky():
    """Belt-and-braces: the kwargs we send must bind against the real
    sky.jobs.queue_v2 signature in the installed package."""
    import sky.jobs as sj

    sig = inspect.signature(sj.queue_v2)
    sig.bind_partial(refresh=False, skip_finished=False)


def test_get_managed_job_queue_unwraps_tuple_payload():
    """queue_v2 returns (records, total, status_counts, total_no_filter);
    the helper must hand callers just the records list."""
    mock_sky = MagicMock()
    fake_records = [MagicMock(job_name="sciagent-x")]
    mock_sky.jobs.queue_v2.return_value = "request-id"
    mock_sky.stream_and_get.return_value = (fake_records, 1, {"RUNNING": 1}, 1)

    backend = _make_backend_with_mock_sky(mock_sky)
    out = backend._get_managed_job_queue()
    assert out == fake_records


# ---- cancel ---------------------------------------------------------------


def test_cleanup_uses_jobs_cancel_by_name():
    """cleanup must cancel the managed job by name and drain via stream_and_get."""
    mock_sky = MagicMock()
    mock_sky.jobs.cancel.return_value = "cancel-request-id"

    backend = _make_backend_with_mock_sky(mock_sky)
    ok = backend.cleanup("sciagent-abc123")

    assert ok is True
    mock_sky.jobs.cancel.assert_called_once()
    _, kwargs = mock_sky.jobs.cancel.call_args
    assert kwargs.get("name") == "sciagent-abc123"
    mock_sky.stream_and_get.assert_called_once_with("cancel-request-id")


def test_cleanup_signature_matches_installed_sky():
    import sky.jobs as sj

    sig = inspect.signature(sj.cancel)
    sig.bind_partial(name="sciagent-abc123")


# ---- tail_logs ------------------------------------------------------------


def test_get_logs_uses_jobs_tail_logs_with_follow_false():
    """get_logs must call sky.jobs.tail_logs with follow=False (otherwise it
    blocks until the job ends, freezing the agent loop on a long-running case)."""
    mock_sky = MagicMock()

    def _stub_tail_logs(name=None, job_id=None, follow=False, tail=None,
                       output_stream=None, **kwargs):
        if output_stream is not None:
            output_stream.write("hello world\n")
        return 0

    mock_sky.jobs.tail_logs.side_effect = _stub_tail_logs

    backend = _make_backend_with_mock_sky(mock_sky)
    out = backend.get_logs("sciagent-abc123", tail=50)

    mock_sky.jobs.tail_logs.assert_called_once()
    _, kwargs = mock_sky.jobs.tail_logs.call_args
    assert kwargs["name"] == "sciagent-abc123"
    assert kwargs["follow"] is False, "tail_logs must never be called with follow=True"
    assert kwargs["tail"] == 50
    assert "hello world" in out


def test_tail_logs_signature_matches_installed_sky():
    import sky.jobs as sj

    sig = inspect.signature(sj.tail_logs)
    sig.bind_partial(name="sciagent-abc123", job_id=None, follow=False, tail=50)


def test_get_logs_prefers_int_managed_job_id_when_provided():
    """When the caller has the integer Sky-side job id, get_logs must call
    tail_logs(job_id=<int>, name=None) — not tail_logs(name=<str>). Sky's
    name lookup only resolves non-terminal jobs, so a FAILED job's logs are
    only retrievable via the int form. Passing the int through avoids the
    'No running managed job found' sentinel that bg_wait used to surface as
    the user's error preview."""
    mock_sky = MagicMock()

    captured = {}

    def _stub_tail_logs(name=None, job_id=None, follow=False, tail=None,
                       output_stream=None, **kwargs):
        captured["name"] = name
        captured["job_id"] = job_id
        if output_stream is not None:
            output_stream.write("real error: container exited 1\n")
        return 0

    mock_sky.jobs.tail_logs.side_effect = _stub_tail_logs

    backend = _make_backend_with_mock_sky(mock_sky)
    out = backend.get_logs("sciagent-abc123", tail=50, managed_job_id=42)

    assert captured["name"] is None, (
        "When the int is in hand, name must be None — name= only resolves "
        "non-terminal jobs and would return Sky's 'not found' sentinel for "
        "any FAILED job."
    )
    assert captured["job_id"] == 42
    assert "real error" in out


def test_get_logs_filters_sky_nonterminal_name_sentinel():
    """If Sky returns the 'No running managed job found with name X' string
    (server-side sentinel for terminal jobs queried by name), get_logs must
    treat it as 'no logs' rather than passing it through. Otherwise
    _extract_error_line matches the 'not found' keyword and the sentinel
    gets surfaced as the user's error — the original bg_wait-lying bug."""
    mock_sky = MagicMock()

    def _stub_tail_logs(name=None, job_id=None, follow=False, tail=None,
                       output_stream=None, **kwargs):
        if output_stream is not None:
            output_stream.write(
                "No running managed job found with name 'sciagent-abc123'.\n"
            )
        return 0

    mock_sky.jobs.tail_logs.side_effect = _stub_tail_logs
    # Empty queue → no int recoverable → falls back to name lookup, which
    # is the path that produces the sentinel.
    mock_sky.jobs.queue_v2.return_value = "rid"
    mock_sky.stream_and_get.return_value = ([], 0, {}, 0)

    backend = _make_backend_with_mock_sky(mock_sky)
    out = backend.get_logs("sciagent-abc123", tail=50)

    assert out == "", (
        "Sky's non-terminal-name sentinel must be filtered to empty, not "
        "returned as logs."
    )


# ---- status / failure recovery -------------------------------------------


def test_get_status_returns_pending_on_query_failure():
    """B1-style recovery contract carried forward to managed jobs: a query
    that raises must NOT cascade — return a transient PENDING and let the
    next poll retry. Recursion is structurally impossible now (single-layer
    lookup) but the failure-recovery shape is what callers depend on."""
    mock_sky = MagicMock()
    mock_sky.jobs.queue_v2.side_effect = RuntimeError("sky API offline")

    backend = _make_backend_with_mock_sky(mock_sky)
    result = backend.get_status("sciagent-abc123")

    assert isinstance(result, JobResult)
    assert result.status is JobStatus.PENDING


def test_get_job_status_alias_returns_same_result_as_get_status():
    """get_job_status is a backwards-compat alias for get_status (M1A folds
    the M0 cluster-vs-job two-layer dance into a single managed-job query).
    Both methods must return the same JobResult shape so M0 callers don't break."""
    mock_sky = MagicMock()
    mock_sky.jobs.queue_v2.side_effect = RuntimeError("sky API offline")

    backend = _make_backend_with_mock_sky(mock_sky)

    a = backend.get_status("sciagent-abc123")
    b = backend.get_job_status("sciagent-abc123")
    assert a.status == b.status
    assert a.summary[:8] == b.summary[:8]  # both start with "querying"


def test_get_status_returns_failed_when_job_absent_from_queue():
    """A name not present in queue_v2's records is reported as FAILED with a
    descriptive summary — never silently as PENDING (which would let the
    agent poll forever for a job that never existed)."""
    mock_sky = MagicMock()
    mock_sky.jobs.queue_v2.return_value = "request-id"
    mock_sky.stream_and_get.return_value = ([], 0, {}, 0)

    backend = _make_backend_with_mock_sky(mock_sky)
    result = backend.get_status("sciagent-ghost")

    assert result.status is JobStatus.FAILED
    assert "not found" in result.summary.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
