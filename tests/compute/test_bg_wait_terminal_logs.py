"""End-to-end repro for the bg_wait-lying-about-terminal-jobs bug.

Symptom: a managed job that has finished (FAILED or COMPLETED) was being
reported by bg_wait as `Error: No running managed job found with name
'sciagent-...'` even though bg_status showed the same job fine. The agent
would conclude the job vanished and start launching duplicates.

Root cause was in SkyPilotBackend.get_logs: it called
``sky.jobs.tail_logs(name=job_id)``, but Sky's name lookup only resolves
non-terminal jobs (sky/jobs/utils.py:1587). For terminal jobs, Sky returns
the literal string ``"No running managed job found with name '...'"`` which
``_extract_error_line`` then matched on its ``"not found"`` keyword and
surfaced as the user's error preview.

Fix: prefer the integer ``managed_job_id`` from the queue record (terminal
jobs are visible there); fall back to name only when no int is recoverable.
Defensive sentinel filter in get_logs treats the Sky string as "no logs".

This module asserts the regression doesn't return: a FAILED record with an
integer job_id must produce an error_preview taken from real log content,
never from the Sky name-lookup sentinel.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sciagent.compute.backends.skypilot import SkyPilotBackend
from sciagent.compute.job import JobStatus


def _make_backend(mock_sky):
    backend = SkyPilotBackend()
    backend._sky = mock_sky
    return backend


def _failed_record(name: str, managed_job_id: int):
    """Build a queue record matching what sky.jobs.queue_v2 returns for a
    FAILED managed job. Attribute access (job_name, status, job_id,
    failure_reason) mirrors Sky's pydantic ManagedJobRecord."""
    rec = MagicMock()
    rec.job_name = name
    rec.job_id = managed_job_id
    status = MagicMock()
    status.name = "FAILED"
    rec.status = status
    rec.failure_reason = ""
    return rec


def test_get_status_failed_pulls_logs_via_int_not_name():
    """A FAILED managed job's get_status path must call tail_logs with the
    integer job_id (not the name). Sky's name lookup returns a 'not found'
    sentinel for terminal jobs; using the int is the only form that retrieves
    real logs post-terminal."""
    mock_sky = MagicMock()
    rec = _failed_record("sciagent-job-b9ec94fc", managed_job_id=42)
    mock_sky.jobs.queue_v2.return_value = "rid"
    mock_sky.stream_and_get.return_value = ([rec], 1, {"FAILED": 1}, 1)

    captured = []

    def _stub_tail_logs(name=None, job_id=None, follow=False, tail=None,
                       output_stream=None, **kwargs):
        captured.append({"name": name, "job_id": job_id})
        # Real log content the user would actually want to see.
        if name is None and job_id == 42 and output_stream is not None:
            output_stream.write("Traceback:\n  fatal error: container OOM\n")
        elif output_stream is not None:
            # The legacy buggy path: name lookup of a terminal job returns
            # Sky's sentinel. We assert below that get_status doesn't take
            # this path.
            output_stream.write(
                "No running managed job found with name 'sciagent-job-b9ec94fc'.\n"
            )
        return 0

    mock_sky.jobs.tail_logs.side_effect = _stub_tail_logs

    backend = _make_backend(mock_sky)
    result = backend.get_status("sciagent-job-b9ec94fc")

    assert result.status is JobStatus.FAILED
    # Must have called tail_logs at least once via the int path.
    int_calls = [c for c in captured if c["job_id"] == 42 and c["name"] is None]
    assert int_calls, (
        f"tail_logs was never called with job_id=<int>; calls={captured}. "
        f"Terminal jobs can only be tailed by int; using name= would return "
        f"Sky's 'No running managed job found' sentinel."
    )


def test_bg_wait_lying_regression_no_sentinel_in_error_preview():
    """The original symptom: bg_wait surfaced 'No running managed job found
    with name X' as the tool error for a FAILED-but-existent job. After the
    fix, the error_preview must come from real log content (or the queue
    record's failure_reason), never from Sky's name-lookup sentinel."""
    mock_sky = MagicMock()
    rec = _failed_record("sciagent-job-b9ec94fc", managed_job_id=42)
    mock_sky.jobs.queue_v2.return_value = "rid"
    mock_sky.stream_and_get.return_value = ([rec], 1, {"FAILED": 1}, 1)

    def _stub_tail_logs(name=None, job_id=None, follow=False, tail=None,
                       output_stream=None, **kwargs):
        if output_stream is None:
            return 0
        if job_id == 42:
            output_stream.write(
                "Traceback (most recent call last):\n"
                "  File 'solver.py', line 42\n"
                "RuntimeError: blockMesh failed: invalid mesh topology\n"
            )
        else:
            # If the buggy code path is ever taken, Sky returns the sentinel.
            output_stream.write(
                "No running managed job found with name 'sciagent-job-b9ec94fc'.\n"
            )
        return 0

    mock_sky.jobs.tail_logs.side_effect = _stub_tail_logs

    backend = _make_backend(mock_sky)
    result = backend.get_status("sciagent-job-b9ec94fc")

    assert result.status is JobStatus.FAILED
    preview = (result.error_preview or "").lower()
    assert "no running managed job found" not in preview, (
        f"Sky's name-lookup sentinel leaked into error_preview: {preview!r}. "
        f"This is the bg_wait-lying-about-terminal-jobs bug."
    )
    # Positive: the actual cause should be visible.
    assert "blockmesh" in preview or "runtimeerror" in preview, (
        f"Expected real log content in error_preview; got {preview!r}"
    )


def test_get_logs_orphan_no_int_falls_back_to_name():
    """If the queue lookup returns no record (orphaned manifest, transient
    failure), get_logs must still attempt a name-based tail. Useful for
    still-running jobs where the integer hasn't been captured yet."""
    mock_sky = MagicMock()
    mock_sky.jobs.queue_v2.return_value = "rid"
    mock_sky.stream_and_get.return_value = ([], 0, {}, 0)

    captured = {}

    def _stub_tail_logs(name=None, job_id=None, follow=False, tail=None,
                       output_stream=None, **kwargs):
        captured["name"] = name
        captured["job_id"] = job_id
        if output_stream is not None:
            output_stream.write("running...\n")
        return 0

    mock_sky.jobs.tail_logs.side_effect = _stub_tail_logs

    backend = _make_backend(mock_sky)
    out = backend.get_logs("sciagent-orphan", tail=10)

    assert captured["name"] == "sciagent-orphan"
    assert captured["job_id"] is None
    assert "running" in out
