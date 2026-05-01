"""Tests for the workspace-output sync helper used by bg_wait.

Folded into bg_wait (rather than exposed as a standalone tool) so the
agent's mental model is "I run a job and get files back" — no extra tool
call. These tests pin the helper's contract so the bg_wait integration
stays predictable.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from sciagent.tools.atomic.compute_fetch import fetch_workspace_outputs


@pytest.fixture
def tmp_manifest_dir(tmp_path, monkeypatch):
    """Redirect ~/.sciagent/tasks to a tmpdir so tests don't touch
    the user's real manifest store."""
    fake_home = tmp_path / "home"
    (fake_home / ".sciagent" / "tasks").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return fake_home / ".sciagent" / "tasks"


def _write_manifest(manifest_dir: Path, job_id: str, **fields) -> None:
    record = {
        "job_id": job_id,
        "session_id": fields.get("session_id", "abc123"),
        "command": fields.get("command", "python hello.py"),
        "image": fields.get("image", "python:3.11"),
        "service": fields.get("service"),
        "intent": {},
        "expected_artifacts": [],
        "owner_pid": 1,
        "started_at": "2026-04-29T00:00:00+00:00",
        "managed_job_id": fields.get("managed_job_id", 1),
        "timeout_sec": 0,
    }
    (manifest_dir / f"{job_id}.json").write_text(json.dumps(record))


def test_missing_manifest_returns_skip_with_reason(tmp_manifest_dir):
    out = fetch_workspace_outputs("does-not-exist", working_dir=str(tmp_manifest_dir.parent))
    assert out["ok"] is False
    assert "no manifest" in out["reason"]


def test_manifest_without_session_id_returns_skip_with_actionable_reason(tmp_manifest_dir):
    """Skipping is correct here, not failing — bg_wait still wants to
    report COMPLETED. The reason text must point the agent at the cause
    so the next compute_run call can fix it."""
    _write_manifest(tmp_manifest_dir, "no-sess", session_id=None)
    out = fetch_workspace_outputs("no-sess", working_dir=str(tmp_manifest_dir.parent))
    assert out["ok"] is False
    assert "session_id" in out["reason"]
    assert "workspace=False" in out["reason"] or "non-skypilot" in out["reason"]


def test_aws_cli_missing_returns_skip_with_manual_instructions(tmp_manifest_dir):
    _write_manifest(tmp_manifest_dir, "abc", session_id="sess1")
    with patch("sciagent.tools.atomic.compute_fetch.shutil.which", return_value=None):
        out = fetch_workspace_outputs("abc", working_dir=str(tmp_manifest_dir.parent))
    assert out["ok"] is False
    assert "aws CLI" in out["reason"]
    assert "sciagent-workspace-sess1" in out["reason"]  # manual fallback uses the right bucket


def test_happy_path_invokes_aws_s3_sync_with_correct_args(tmp_manifest_dir, tmp_path):
    """Lock the s3 sync invocation so a future refactor (multi-cloud
    dispatch, bucket name change) doesn't silently break the CLI shape."""
    _write_manifest(tmp_manifest_dir, "abc", session_id="sess1")
    dest = tmp_path / "project"
    dest.mkdir()

    fake_completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch("sciagent.tools.atomic.compute_fetch.shutil.which", return_value="/usr/local/bin/aws"), \
         patch("sciagent.tools.atomic.compute_fetch.subprocess.run", return_value=fake_completed) as mock_run:
        out = fetch_workspace_outputs("abc", working_dir=str(dest))

    assert out["ok"] is True
    assert out["bucket"] == "sciagent-workspace-sess1"
    # Default prefix is per-job: _outputs/<job_id>/. Matches the
    # implicit-mount symlink target in skypilot.py:resolve_command,
    # so parallel jobs in the same bucket don't collide.
    assert out["prefix"] == "_outputs/abc/"
    cmd = mock_run.call_args[0][0]
    assert cmd[:3] == ["aws", "s3", "sync"]
    assert cmd[3] == "s3://sciagent-workspace-sess1/_outputs/abc/"
    # Files mirror into <dest>/_outputs/<job_id>/ — same shape locally.
    assert cmd[4].endswith("/_outputs/abc")


def test_no_such_bucket_error_is_surfaced_clearly(tmp_manifest_dir, tmp_path):
    _write_manifest(tmp_manifest_dir, "abc", session_id="sess1")
    dest = tmp_path / "project"
    dest.mkdir()

    fake_failed = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="fatal error: An error occurred (NoSuchBucket) when calling the ListObjectsV2 operation: ...",
    )

    with patch("sciagent.tools.atomic.compute_fetch.shutil.which", return_value="/usr/local/bin/aws"), \
         patch("sciagent.tools.atomic.compute_fetch.subprocess.run", return_value=fake_failed):
        out = fetch_workspace_outputs("abc", working_dir=str(dest))

    assert out["ok"] is False
    assert "bucket does not exist" in out["reason"]


def test_files_listing_walks_synced_dir(tmp_manifest_dir, tmp_path):
    """After a successful sync the helper walks the local target and
    reports files + sizes. bg_wait surfaces those paths to the agent so
    file_ops can read them next."""
    _write_manifest(tmp_manifest_dir, "abc", session_id="sess1")
    dest = tmp_path / "project"
    dest.mkdir()

    # Simulate aws s3 sync by writing the files ourselves before the
    # mocked subprocess "returns" success.
    def _fake_sync(*args, **kwargs):
        target_dir = Path(args[0][4])  # cmd[4] is the local target
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "result.json").write_text("{}")
        (target_dir / "log.txt").write_text("hi" * 50)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with patch("sciagent.tools.atomic.compute_fetch.shutil.which", return_value="/usr/local/bin/aws"), \
         patch("sciagent.tools.atomic.compute_fetch.subprocess.run", side_effect=_fake_sync):
        out = fetch_workspace_outputs("abc", working_dir=str(dest))

    assert out["ok"] is True
    assert out["file_count"] == 2
    paths = {f["path"] for f in out["files"]}
    # Files land under the per-job prefix locally: _outputs/<job_id>/.
    assert "_outputs/abc/result.json" in paths
    assert "_outputs/abc/log.txt" in paths
    assert out["bytes_total"] > 0
