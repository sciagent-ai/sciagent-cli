"""Cloud-agnostic fetch dispatch.

compute_fetch.py used to hardcode `aws s3 sync`. The dispatch table now
maps URI scheme -> cloud-native CLI argv. Adding a cloud is one entry.
These tests pin the table's shape so a future refactor can't silently
break a non-S3 user.

Manifest contract:
  - New manifests carry outputs_uri (full URI with scheme).
  - Legacy manifests had no outputs_uri; the fetch path reconstructs an
    s3:// URI from session_id (S3 was the only cloud the legacy code
    supported, so falling back to s3:// matches what's actually in the
    bucket).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from sciagent.tools.atomic.compute_fetch import (
    _FETCH_DISPATCH,
    _split_uri,
    fetch_workspace_outputs,
)


@pytest.fixture
def tmp_manifest_dir(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".sciagent" / "tasks").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return fake_home / ".sciagent" / "tasks"


def _write_manifest(manifest_dir, job_id, **fields):
    record = {
        "job_id": job_id,
        "session_id": fields.get("session_id", "abc123"),
        "command": fields.get("command", "python hello.py"),
        "intent": {},
        "expected_artifacts": [],
        "owner_pid": 1,
        "started_at": "2026-04-29T00:00:00+00:00",
    }
    record.update({k: v for k, v in fields.items() if k not in record})
    (manifest_dir / f"{job_id}.json").write_text(json.dumps(record))


# ---- dispatch table shapes (mock-only — no live cloud calls) -----------


def test_s3_dispatch_invokes_aws_s3_sync():
    cmd = _FETCH_DISPATCH["s3"]("s3://bucket/jobs/abc/", "/tmp/dest")
    assert cmd == ["aws", "s3", "sync", "s3://bucket/jobs/abc/", "/tmp/dest"]


def test_gs_dispatch_invokes_gsutil_rsync():
    cmd = _FETCH_DISPATCH["gs"]("gs://bucket/jobs/abc/", "/tmp/dest")
    assert cmd == ["gsutil", "-m", "rsync", "-r", "gs://bucket/jobs/abc/", "/tmp/dest"]


def test_az_dispatch_invokes_az_storage():
    cmd = _FETCH_DISPATCH["az"]("az://container/jobs/abc/", "/tmp/dest")
    assert cmd[:6] == [
        "az", "storage", "blob", "download-batch",
        "--source", "container",
    ]
    assert "--destination" in cmd
    assert "/tmp/dest" in cmd
    assert "--pattern" in cmd


def test_r2_dispatch_rewrites_to_s3_for_aws_cli():
    """R2 speaks the S3 API; user must have AWS CLI configured with R2
    endpoint via env or profile. Dispatch rewrites r2:// to s3:// so the
    aws CLI accepts it."""
    cmd = _FETCH_DISPATCH["r2"]("r2://bucket/p/", "/tmp/dest")
    assert cmd == ["aws", "s3", "sync", "s3://bucket/p/", "/tmp/dest"]


def test_oci_dispatch_invokes_oci_bulk_download():
    cmd = _FETCH_DISPATCH["oci"]("oci://bucket/jobs/abc/", "/tmp/dest")
    assert cmd[:5] == [
        "oci", "os", "object", "bulk-download", "--bucket-name",
    ]
    assert "bucket" in cmd
    assert "--prefix" in cmd


def test_split_uri_parses_scheme_bucket_prefix():
    assert _split_uri("s3://b/p/") == ("s3", "b", "p/")
    assert _split_uri("gs://b") == ("gs", "b", "")
    assert _split_uri("az://container/jobs/abc/") == (
        "az", "container", "jobs/abc/",
    )


# ---- fetch_workspace_outputs end-to-end (mocked subprocess) -----------


def test_unsupported_scheme_returns_skip(tmp_manifest_dir, tmp_path):
    """Unknown scheme in outputs_uri -> structured skip with 'supported
    schemes' hint."""
    _write_manifest(
        tmp_manifest_dir,
        "abc",
        session_id="sess1",
        outputs_uri="foo://bucket/abc/",
    )
    out = fetch_workspace_outputs("abc", working_dir=str(tmp_path))
    assert out["ok"] is False
    assert "unsupported cloud scheme" in out["reason"].lower()
    assert "supported schemes" in out["reason"].lower()


def test_legacy_manifest_with_no_outputs_uri_falls_back_to_s3(
    tmp_manifest_dir, tmp_path
):
    """A pre-multi-cloud manifest has no outputs_uri. The fetch path
    reconstructs s3://sciagent-workspace-<sess>/_outputs/<job_id>/ — the
    layout the legacy code wrote. AWS CLI invocation matches."""
    _write_manifest(tmp_manifest_dir, "abc", session_id="sess1")
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )

    with patch(
        "sciagent.tools.atomic.compute_fetch.shutil.which",
        return_value="/usr/local/bin/aws",
    ), patch(
        "sciagent.tools.atomic.compute_fetch.subprocess.run",
        return_value=fake_completed,
    ) as mock_run:
        out = fetch_workspace_outputs("abc", working_dir=str(tmp_path))

    assert out["ok"] is True
    assert out["scheme"] == "s3"
    cmd = mock_run.call_args[0][0]
    assert cmd[:3] == ["aws", "s3", "sync"]
    # Legacy path: bucket=sciagent-workspace-<sess>, prefix=_outputs/<job_id>/
    assert cmd[3] == "s3://sciagent-workspace-sess1/_outputs/abc/"


def test_new_manifest_with_gs_outputs_uri_dispatches_to_gsutil(
    tmp_manifest_dir, tmp_path
):
    """Manifest with gs:// outputs_uri -> gsutil rsync (not aws s3 sync)."""
    _write_manifest(
        tmp_manifest_dir,
        "abc",
        session_id="sess9",
        outputs_uri="gs://sciagent-workspace-sess9/abc/",
        outputs_prefix="abc/",
    )
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )

    with patch(
        "sciagent.tools.atomic.compute_fetch.shutil.which",
        return_value="/usr/local/bin/gsutil",
    ), patch(
        "sciagent.tools.atomic.compute_fetch.subprocess.run",
        return_value=fake_completed,
    ) as mock_run:
        out = fetch_workspace_outputs("abc", working_dir=str(tmp_path))

    assert out["ok"] is True
    assert out["scheme"] == "gs"
    cmd = mock_run.call_args[0][0]
    assert cmd[:2] == ["gsutil", "-m"]
    assert "gs://sciagent-workspace-sess9/abc/" in cmd


def test_new_manifest_with_az_outputs_uri_dispatches_to_az_cli(
    tmp_manifest_dir, tmp_path
):
    _write_manifest(
        tmp_manifest_dir,
        "abc",
        session_id="sess9",
        outputs_uri="az://workspace-container/abc/",
    )
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )

    with patch(
        "sciagent.tools.atomic.compute_fetch.shutil.which",
        return_value="/usr/local/bin/az",
    ), patch(
        "sciagent.tools.atomic.compute_fetch.subprocess.run",
        return_value=fake_completed,
    ) as mock_run:
        out = fetch_workspace_outputs("abc", working_dir=str(tmp_path))

    assert out["ok"] is True
    assert out["scheme"] == "az"
    cmd = mock_run.call_args[0][0]
    assert cmd[:4] == ["az", "storage", "blob", "download-batch"]


def test_required_cli_missing_surfaces_install_hint(tmp_manifest_dir, tmp_path):
    """When the required CLI for the chosen scheme isn't on PATH, return
    a clear install hint — same shape as the legacy aws-missing path."""
    _write_manifest(
        tmp_manifest_dir,
        "abc",
        session_id="sess9",
        outputs_uri="gs://b/abc/",
    )
    with patch(
        "sciagent.tools.atomic.compute_fetch.shutil.which", return_value=None
    ):
        out = fetch_workspace_outputs("abc", working_dir=str(tmp_path))

    assert out["ok"] is False
    assert "gsutil" in out["reason"]


def test_caller_prefix_override_keeps_scheme_changes_path(
    tmp_manifest_dir, tmp_path
):
    """Cross-tool sharing: caller passes prefix= to read another job's
    outputs. The bucket and scheme stay; the path is replaced."""
    _write_manifest(
        tmp_manifest_dir,
        "abc",
        session_id="sess9",
        outputs_uri="gs://sciagent-workspace-sess9/abc/",
    )
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )

    with patch(
        "sciagent.tools.atomic.compute_fetch.shutil.which",
        return_value="/usr/local/bin/gsutil",
    ), patch(
        "sciagent.tools.atomic.compute_fetch.subprocess.run",
        return_value=fake_completed,
    ) as mock_run:
        out = fetch_workspace_outputs(
            "abc",
            working_dir=str(tmp_path),
            prefix="other-job-id/",
        )

    assert out["ok"] is True
    cmd = mock_run.call_args[0][0]
    # Scheme + bucket unchanged; prefix replaced.
    assert "gs://sciagent-workspace-sess9/other-job-id/" in cmd


def test_bucket_missing_marker_surfaced_for_each_cloud(
    tmp_manifest_dir, tmp_path
):
    """`bucket does not exist` is recognized across cloud-specific
    error markers (NoSuchBucket / BucketNotFoundException / etc.)."""
    _write_manifest(
        tmp_manifest_dir,
        "abc",
        session_id="sess9",
        outputs_uri="gs://missing/abc/",
    )
    fake_failed = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="error: BucketNotFoundException ...",
    )

    with patch(
        "sciagent.tools.atomic.compute_fetch.shutil.which",
        return_value="/usr/local/bin/gsutil",
    ), patch(
        "sciagent.tools.atomic.compute_fetch.subprocess.run",
        return_value=fake_failed,
    ):
        out = fetch_workspace_outputs("abc", working_dir=str(tmp_path))

    assert out["ok"] is False
    assert "bucket does not exist" in out["reason"]
