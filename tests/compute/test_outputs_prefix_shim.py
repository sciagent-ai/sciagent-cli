"""Manifest back-compat shim for the bucket-prefix migration.

The bucket-side path layout changed from ``_outputs/<job_id>/`` to
``<job_id>/`` (no leading segment). New manifests carry ``outputs_uri``
(full URI, scheme included) and ``outputs_prefix`` (just ``<job_id>/``).
Legacy manifests have neither.

The fetch path resolves URIs in this order:

  1. ``manifest["outputs_uri"]`` if present — wins, scheme included.
  2. Else reconstruct ``s3://sciagent-workspace-<session>/<prefix>/`` from
     ``session_id`` + (``outputs_prefix`` or legacy ``_outputs/<job_id>/``).
     Pre-multi-cloud manifests were S3-only so the s3:// fallback matches
     reality.

These tests pin the resolution behavior so in-flight jobs from older
sciagent versions keep auto-fetching cleanly.
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
    fake_home = tmp_path / "home"
    (fake_home / ".sciagent" / "tasks").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return fake_home / ".sciagent" / "tasks"


def _write(manifest_dir, job_id, **fields):
    record = {
        "job_id": job_id,
        "session_id": fields.get("session_id", "abc123"),
        "command": "python hello.py",
        "intent": {},
        "expected_artifacts": [],
        "owner_pid": 1,
        "started_at": "2026-04-29T00:00:00+00:00",
    }
    record.update({k: v for k, v in fields.items() if k not in record})
    (manifest_dir / f"{job_id}.json").write_text(json.dumps(record))


def _stub_subprocess_success():
    """Patch shutil.which + subprocess.run so the fetch path completes
    without hitting any real cloud CLI."""
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )
    return (
        patch(
            "sciagent.tools.atomic.compute_fetch.shutil.which",
            return_value="/usr/local/bin/aws",
        ),
        patch(
            "sciagent.tools.atomic.compute_fetch.subprocess.run",
            return_value=fake_completed,
        ),
    )


# ---- legacy manifest (no outputs_uri, no outputs_prefix) -------------


def test_legacy_manifest_falls_back_to_s3_underscore_outputs_layout(
    tmp_manifest_dir, tmp_path
):
    """Pre-multi-cloud, pre-prefix-migration manifest: only session_id is
    set. Fetch reconstructs s3://sciagent-workspace-<sess>/_outputs/<job_id>/."""
    _write(tmp_manifest_dir, "abc", session_id="sess1")

    which_patch, run_patch = _stub_subprocess_success()
    with which_patch, run_patch as mock_run:
        out = fetch_workspace_outputs("abc", working_dir=str(tmp_path))

    assert out["ok"] is True
    assert out["scheme"] == "s3"
    cmd = mock_run.call_args[0][0]
    assert cmd[3] == "s3://sciagent-workspace-sess1/_outputs/abc/"


def test_legacy_manifest_with_explicit_outputs_prefix_uses_it(
    tmp_manifest_dir, tmp_path
):
    """Mid-migration: outputs_prefix written without outputs_uri (e.g. an
    older code version that wrote prefix-only). Use the prefix; assume
    s3:// for the scheme."""
    _write(
        tmp_manifest_dir,
        "abc",
        session_id="sess1",
        outputs_prefix="abc/",  # new layout, no leading _outputs/
    )

    which_patch, run_patch = _stub_subprocess_success()
    with which_patch, run_patch as mock_run:
        out = fetch_workspace_outputs("abc", working_dir=str(tmp_path))

    assert out["ok"] is True
    cmd = mock_run.call_args[0][0]
    assert cmd[3] == "s3://sciagent-workspace-sess1/abc/"


# ---- new manifest with full outputs_uri ------------------------------


def test_new_manifest_with_outputs_uri_uses_it_verbatim(
    tmp_manifest_dir, tmp_path
):
    """Fully-migrated manifest: outputs_uri is the source of truth. The
    fetch path uses it as-is and dispatches by scheme."""
    _write(
        tmp_manifest_dir,
        "abc",
        session_id="sess9",
        outputs_uri="s3://sciagent-workspace-sess9/abc/",
        outputs_prefix="abc/",
    )

    which_patch, run_patch = _stub_subprocess_success()
    with which_patch, run_patch as mock_run:
        out = fetch_workspace_outputs("abc", working_dir=str(tmp_path))

    assert out["ok"] is True
    cmd = mock_run.call_args[0][0]
    assert cmd[3] == "s3://sciagent-workspace-sess9/abc/"


def test_new_manifest_outputs_uri_wins_over_legacy_session_reconstruction(
    tmp_manifest_dir, tmp_path
):
    """If both outputs_uri and session_id are present, outputs_uri wins —
    its scheme/bucket may differ from the legacy reconstruction."""
    _write(
        tmp_manifest_dir,
        "abc",
        session_id="sess9",
        outputs_uri="gs://different-bucket/abc/",  # GCS, different bucket name
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
    assert "gs://different-bucket/abc/" in cmd
    # Legacy reconstruction would have produced sciagent-workspace-sess9 — verify
    # it didn't override.
    assert "sciagent-workspace-sess9" not in " ".join(cmd)
