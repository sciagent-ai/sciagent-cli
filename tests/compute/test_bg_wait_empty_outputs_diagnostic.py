"""bg_wait must explain WHY auto-fetch came up empty.

Symptom from real traces: a compute job completes successfully but the
agent's run command never wrote anything to ``$OUTPUTS_DIR``. The bucket
sync succeeds (returncode 0) but yields zero files. Pre-fix, bg_wait
returned ``"Fetched 0 file(s) (0 bytes) from ..."`` — technically true,
operationally useless. The agent then guesses ("workspace mount issue?
S3 access?") and goes down rabbit holes.

Post-fix: an empty fetch must surface the actual root cause — the run
command didn't write to /outputs/<job_id>/ — and tell the agent how to
diagnose (sky jobs logs).

Also pinned: the sky CLI hint emitted on the COMPLETED path uses the
managed-jobs form (`sky jobs logs`), not the cluster-mode form
(`sky logs <name>`) which would surface ClusterDoesNotExist for managed
jobs whose cluster has been auto-torn-down.
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


def test_completed_with_zero_files_explains_outputs_dir_contract():
    """The exact regression: job completed, sync ok, zero files. Output
    must name the cause (run command didn't write to $OUTPUTS_DIR) and
    point at sky jobs logs for diagnosis."""
    result = JobResult(status=JobStatus.COMPLETED, summary="job done")
    ctx, _ = _patched_router(result)

    fake_fetch = MagicMock(return_value={
        "ok": True,
        "file_count": 0,
        "bytes_total": 0,
        "files": [],
        "bucket": "sciagent-workspace-abc",
        "dest": ".",
    })

    with ctx, patch(
        "sciagent.tools.atomic.compute_fetch.fetch_workspace_outputs",
        fake_fetch,
    ):
        tool = BgWaitTool()
        out = tool.execute(job_id="sciagent-empty")

    assert out.success is True
    body = out.output.lower()
    # The agent must see the actionable cause, not the misleading
    # "Fetched 0 file(s)" success line.
    assert "didn't write outputs" in body or "didn't write" in body
    assert "$outputs_dir" in body or "/outputs/sciagent-empty" in body
    # And the diagnostic command — the right one for managed jobs.
    assert "sky jobs logs" in body
    # The pre-fix misleading "Fetched 0 file(s)" must NOT appear.
    assert "fetched 0 file(s)" not in body


def test_completed_with_real_files_still_lists_them():
    """Don't regress the happy path. When the agent's command DID write
    to $OUTPUTS_DIR, the fetch should still list the files."""
    result = JobResult(status=JobStatus.COMPLETED, summary="job done")
    ctx, _ = _patched_router(result)

    fake_fetch = MagicMock(return_value={
        "ok": True,
        "file_count": 2,
        "bytes_total": 1024,
        "files": [
            {"path": "_outputs/result.json", "bytes": 512},
            {"path": "_outputs/log.txt", "bytes": 512},
        ],
        "bucket": "sciagent-workspace-abc",
        "dest": ".",
    })

    with ctx, patch(
        "sciagent.tools.atomic.compute_fetch.fetch_workspace_outputs",
        fake_fetch,
    ):
        tool = BgWaitTool()
        out = tool.execute(job_id="sciagent-good")

    assert out.success is True
    assert "Fetched 2 file(s)" in out.output
    assert "result.json" in out.output


def test_completed_uses_sky_jobs_logs_not_sky_logs():
    """The CLI hint on the completed path must be `sky jobs logs <id>`,
    not `sky logs <id>`. The latter raises ClusterDoesNotExist for any
    managed job whose cluster has been torn down — exactly the trap the
    agent kept falling into."""
    result = JobResult(status=JobStatus.COMPLETED, summary="job done")
    ctx, _ = _patched_router(result)

    fake_fetch = MagicMock(return_value={
        "ok": True, "file_count": 0, "bytes_total": 0, "files": [],
        "bucket": "sciagent-workspace-abc", "dest": ".",
    })

    with ctx, patch(
        "sciagent.tools.atomic.compute_fetch.fetch_workspace_outputs",
        fake_fetch,
    ):
        tool = BgWaitTool()
        out = tool.execute(job_id="sciagent-cli-hint")

    assert "sky jobs logs sciagent-cli-hint" in out.output
    # The legacy wrong form must not appear.
    assert "Use 'sky logs sciagent-cli-hint'" not in out.output
