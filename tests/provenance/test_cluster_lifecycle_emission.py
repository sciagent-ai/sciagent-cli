"""Cluster-mode provenance emission.

Three events sciagent must emit so a verifier can audit cluster-mode
work after the fact:

  - compute_job_launched with mode="cluster_launch" on initial provision
    via launch_cluster (sky.launch).
  - compute_job_launched with mode="cluster_exec" on each follow-up via
    exec_on_cluster (sky.exec).
  - compute_job_launched with mode="cluster_refresh_mounts" on
    refresh_cluster_mounts (sky.launch --no-setup).
  - compute_cluster_down on cluster teardown (success or failure), so an
    auditor can see the lifecycle endpoint.

Pinned here so a future refactor can't silently break the audit trail
on cluster-mode work.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sciagent.compute.backends.skypilot import SkyPilotBackend
from sciagent.compute.job import ComputeRequirements, Job
from sciagent.provenance_log import (
    ProvenanceLog,
    get_provenance_log,
    reset_provenance_logs,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_provenance_logs()
    yield
    reset_provenance_logs()


def _read_events(session_dir: Path):
    log_path = session_dir / "provenance.jsonl"
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


def _job_with_session(session_id="sess1"):
    return Job(
        service="custom",
        image="python:3.11",
        command="echo hi",
        working_dir=".",
        requirements=ComputeRequirements(cpus=2, memory_gb=4, gpus=0),
        session_id=session_id,
    )


def _wire_succeeded(mock_sky, int_job_id=7):
    payload = MagicMock()
    payload.status.name = "SUCCEEDED"
    mock_sky.api_status.return_value = [payload]
    mock_sky.get.return_value = (int_job_id, MagicMock())


def _backend(mock_sky):
    b = SkyPilotBackend()
    b._sky = mock_sky
    return b


# ---- emit_compute_job_launched mode field ----------------------------


def test_emit_launched_default_mode_is_managed_jobs(tmp_path, monkeypatch):
    """The existing run() callers don't pass mode=. The schema must default
    to 'managed_jobs' so the audit log records what kind of integer is in
    managed_job_id (controller-assigned vs per-cluster index)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    log = get_provenance_log("sess1", base_dir=tmp_path / ".sciagent" / "sessions")
    log.emit_compute_job_launched(
        job_id="sciagent-job-x",
        managed_job_id=42,
        backend="skypilot",
        service="scipy-base",
        image="ghcr.io/sciagent-ai/scipy-base",
        command_original="echo hi",
        command_resolved="echo hi",
        mount_path=None,
        mount_bucket=None,
        requirements={"cpus": 2, "memory_gb": 4, "gpus": 0},
        intent=None,
        expected_artifacts=None,
    )
    events = _read_events(tmp_path / ".sciagent" / "sessions" / "sess1")
    assert len(events) == 1
    ev = events[0]
    assert ev["mode"] == "managed_jobs"
    assert ev["cluster_name"] is None
    assert ev["cluster_job_id"] is None


def test_launch_cluster_emits_with_mode_cluster_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.launch.return_value = "rid"
    _wire_succeeded(mock_sky, int_job_id=1)

    b = _backend(mock_sky)
    b.launch_cluster(cluster_name="c1", job=_job_with_session(), autostop_minutes=15)

    events = _read_events(tmp_path / ".sciagent" / "sessions" / "sess1")
    launched = [e for e in events if e["event_kind"] == "compute_job_launched"]
    assert len(launched) == 1
    ev = launched[0]
    assert ev["mode"] == "cluster_launch"
    assert ev["cluster_name"] == "c1"
    assert ev["cluster_job_id"] == 1
    # managed_job_id is duplicated for back-compat with existing readers.
    assert ev["managed_job_id"] == 1


def test_exec_on_cluster_emits_with_mode_cluster_exec(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.exec.return_value = "rid"
    _wire_succeeded(mock_sky, int_job_id=2)

    b = _backend(mock_sky)
    b.exec_on_cluster(cluster_name="c1", job=_job_with_session())

    events = _read_events(tmp_path / ".sciagent" / "sessions" / "sess1")
    launched = [e for e in events if e["event_kind"] == "compute_job_launched"]
    assert len(launched) == 1
    assert launched[0]["mode"] == "cluster_exec"


def test_refresh_cluster_mounts_emits_with_correct_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.launch.return_value = "rid"
    _wire_succeeded(mock_sky, int_job_id=3)

    b = _backend(mock_sky)
    b.refresh_cluster_mounts(cluster_name="c1", job=_job_with_session())

    events = _read_events(tmp_path / ".sciagent" / "sessions" / "sess1")
    launched = [e for e in events if e["event_kind"] == "compute_job_launched"]
    assert len(launched) == 1
    assert launched[0]["mode"] == "cluster_refresh_mounts"


# ---- compute_cluster_down ------------------------------------------


def test_cluster_down_success_emits_event(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.launch.return_value = "rid"
    _wire_succeeded(mock_sky, int_job_id=1)

    b = _backend(mock_sky)
    b.launch_cluster(cluster_name="c2", job=_job_with_session("sess2"))

    mock_sky.down.return_value = "rid-down"
    mock_sky.stream_and_get.return_value = None

    ok = b.cluster_down("c2")
    assert ok is True

    events = _read_events(tmp_path / ".sciagent" / "sessions" / "sess2")
    downs = [e for e in events if e["event_kind"] == "compute_cluster_down"]
    assert len(downs) == 1
    ev = downs[0]
    assert ev["cluster_name"] == "c2"
    assert ev["graceful"] is True
    assert ev["success"] is True


def test_cluster_down_failure_still_emits_event_with_reason(tmp_path, monkeypatch):
    """Even when sky.down rejects, we want the audit log to show the
    attempted teardown — that's exactly the ambiguous case an auditor
    needs to investigate."""
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.launch.return_value = "rid"
    _wire_succeeded(mock_sky, int_job_id=1)

    b = _backend(mock_sky)
    b.launch_cluster(cluster_name="c3", job=_job_with_session("sess3"))

    mock_sky.down.side_effect = RuntimeError("cluster gone already")
    ok = b.cluster_down("c3")
    assert ok is False

    events = _read_events(tmp_path / ".sciagent" / "sessions" / "sess3")
    downs = [e for e in events if e["event_kind"] == "compute_cluster_down"]
    assert len(downs) == 1
    ev = downs[0]
    assert ev["success"] is False
    assert "cluster gone" in (ev["reason"] or "")


def test_cluster_down_without_session_skips_event_silently(tmp_path, monkeypatch):
    """When there's no manifest (orphan cluster, or manifest missing), there's
    no session_id to write the event under. Skip silently rather than crash."""
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.down.return_value = "rid"
    mock_sky.stream_and_get.return_value = None

    b = _backend(mock_sky)
    # No prior launch_cluster => no manifest => no session_id.
    ok = b.cluster_down("orphan")
    assert ok is True  # The down itself succeeds.
    # No session log should exist.
    sessions_dir = tmp_path / ".sciagent" / "sessions"
    if sessions_dir.exists():
        assert list(sessions_dir.iterdir()) == []
