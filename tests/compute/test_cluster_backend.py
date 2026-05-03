"""Backend cluster-mode tests (sky.launch + sky.exec + lifecycle).

Pins the kwargs sciagent sends to:
  - sky.launch(cluster_name=, idle_minutes_to_autostop=, no_setup=)
  - sky.exec(cluster_name=)
  - sky.status(cluster_names=[...])
  - sky.down(cluster_name, graceful=)
  - sky.autostop(cluster_name, idle_minutes=, wait_for=, hook=)

Plus the manifest write side-effects on launch_cluster / exec / refresh.
Mocks SkyPilotBackend._sky so tests run without cloud creds.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sciagent.compute.backends.skypilot import SkyPilotBackend
from sciagent.compute.job import ComputeRequirements, Job


def _backend(mock_sky):
    b = SkyPilotBackend()
    b._sky = mock_sky
    return b


def _job(command="echo hi"):
    return Job(
        service="custom",
        image="python:3.11",
        command=command,
        working_dir=".",
        requirements=ComputeRequirements(cpus=2, memory_gb=4, gpus=0),
    )


def _stub_request(int_job_id=7):
    """sky.launch / sky.exec async pattern: returns a request_id; sky.get
    later returns (Optional[int], handle). We short-circuit by stubbing
    api_status to immediately report SUCCEEDED so the fail-fast budget
    polling exits the same iteration."""
    return ("rid", int_job_id)


def _wire_succeeded_request(mock_sky, int_job_id=7):
    """Wire the mock so the first sky.api_status poll returns SUCCEEDED
    and sky.get returns (int_job_id, handle). Avoids real time.sleep
    delays inside the fail-fast budget."""
    payload = MagicMock()
    payload.status.name = "SUCCEEDED"
    mock_sky.api_status.return_value = [payload]
    mock_sky.get.return_value = (int_job_id, MagicMock())


# ---- launch_cluster --------------------------------------------------


def test_launch_cluster_passes_cluster_name_and_autostop(tmp_path, monkeypatch):
    """sky.launch must receive cluster_name and idle_minutes_to_autostop —
    the warm-cluster contract depends on Sky seeing both."""
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.launch.return_value = "rid"
    _wire_succeeded_request(mock_sky, int_job_id=11)

    b = _backend(mock_sky)
    cluster, jid = b.launch_cluster(
        cluster_name="my-cluster",
        job=_job(),
        autostop_minutes=15,
    )

    mock_sky.launch.assert_called_once()
    _, kwargs = mock_sky.launch.call_args
    assert kwargs.get("cluster_name") == "my-cluster"
    assert kwargs.get("idle_minutes_to_autostop") == 15
    assert cluster == "my-cluster"
    assert jid == 11


def test_launch_cluster_writes_manifest(tmp_path, monkeypatch):
    """Local manifest at ~/.sciagent/clusters/<name>.json must record
    autostop_minutes + service so compute_cluster(action='status') can
    enrich Sky's bare response."""
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.launch.return_value = "rid"
    _wire_succeeded_request(mock_sky, int_job_id=3)

    b = _backend(mock_sky)
    job = _job()
    job.service = "openfoam-swak4foam-2012"
    b.launch_cluster(cluster_name="boussinesq", job=job, autostop_minutes=30)

    manifest_path = tmp_path / ".sciagent" / "clusters" / "boussinesq.json"
    assert manifest_path.exists()
    record = json.loads(manifest_path.read_text())
    assert record["cluster_name"] == "boussinesq"
    assert record["autostop_minutes"] == 30
    assert record["service"] == "openfoam-swak4foam-2012"
    assert 3 in record["last_job_ids"]


def test_launch_cluster_with_hook_calls_autostop(tmp_path, monkeypatch):
    """sky.launch doesn't accept hook= directly. When the caller wants a
    hook, the backend must follow the launch with sky.autostop() to apply
    it. Without this, the hook is silently dropped."""
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.launch.return_value = "rid"
    mock_sky.autostop.return_value = "rid2"
    _wire_succeeded_request(mock_sky, int_job_id=1)

    b = _backend(mock_sky)
    b.launch_cluster(
        cluster_name="x",
        job=_job(),
        autostop_minutes=10,
        autostop_hook="aws s3 sync /scratch s3://b/",
    )

    mock_sky.autostop.assert_called_once()
    _, kwargs = mock_sky.autostop.call_args
    assert kwargs.get("idle_minutes") == 10
    assert kwargs.get("hook") == "aws s3 sync /scratch s3://b/"


# ---- exec_on_cluster ------------------------------------------------


def test_exec_on_cluster_calls_sky_exec(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.exec.return_value = "rid"
    _wire_succeeded_request(mock_sky, int_job_id=42)

    b = _backend(mock_sky)
    cluster, jid = b.exec_on_cluster(cluster_name="x", job=_job("ls"))

    mock_sky.exec.assert_called_once()
    _, kwargs = mock_sky.exec.call_args
    assert kwargs.get("cluster_name") == "x"
    # exec must NOT touch sky.launch — that would re-provision.
    mock_sky.launch.assert_not_called()
    assert jid == 42


# ---- refresh_cluster_mounts (sky launch --no-setup) -------------------


def test_refresh_cluster_mounts_uses_no_setup(tmp_path, monkeypatch):
    """The whole point of refresh_mounts is to skip setup. Pin no_setup=True
    on the wrapped sky.launch call so a future refactor can't silently
    re-introduce setup re-runs (which would burn hours on compiled
    scientific stacks)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.launch.return_value = "rid"
    _wire_succeeded_request(mock_sky, int_job_id=5)

    b = _backend(mock_sky)
    b.refresh_cluster_mounts(cluster_name="x", job=_job())

    mock_sky.launch.assert_called_once()
    _, kwargs = mock_sky.launch.call_args
    assert kwargs.get("cluster_name") == "x"
    assert kwargs.get("no_setup") is True


# ---- cluster_status -------------------------------------------------


def test_cluster_status_combines_sky_response_with_manifest(tmp_path, monkeypatch):
    """status() must return both Sky's UP/STOPPED status AND the local
    manifest's autostop / service / last_job_ids. Either alone leaves the
    agent guessing."""
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.launch.return_value = "rid"
    _wire_succeeded_request(mock_sky, int_job_id=1)

    b = _backend(mock_sky)
    b.launch_cluster(cluster_name="probe", job=_job(), autostop_minutes=20)

    # Wire status response.
    status_payload = MagicMock()
    status_payload.status.name = "UP"
    status_payload.autostop = 20
    status_payload.to_down = False
    mock_sky.status.return_value = "rid-status"
    # status uses stream_and_get, not get
    mock_sky.stream_and_get.return_value = [status_payload]

    info = b.cluster_status("probe")
    assert info["exists"] is True
    assert info["status"] == "UP"
    assert info["autostop"]["idle_minutes"] == 20
    assert info["manifest"] is not None
    assert info["manifest"]["autostop_minutes"] == 20


def test_cluster_status_missing_cluster_returns_not_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.status.return_value = "rid"
    mock_sky.stream_and_get.return_value = []  # Sky: no clusters by that name.

    b = _backend(mock_sky)
    info = b.cluster_status("ghost")
    assert info["exists"] is False
    assert info["status"] is None


# ---- cluster_down ---------------------------------------------------


def test_cluster_down_calls_sky_down_graceful_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.down.return_value = "rid"
    mock_sky.stream_and_get.return_value = None

    b = _backend(mock_sky)
    ok = b.cluster_down("x")
    assert ok is True
    mock_sky.down.assert_called_once_with("x", graceful=True)


def test_cluster_down_removes_local_manifest(tmp_path, monkeypatch):
    """After a successful down, the local manifest should be removed so a
    stale entry doesn't surface in subsequent status listings."""
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.launch.return_value = "rid"
    _wire_succeeded_request(mock_sky, int_job_id=1)
    b = _backend(mock_sky)
    b.launch_cluster(cluster_name="goner", job=_job(), autostop_minutes=10)

    manifest_path = tmp_path / ".sciagent" / "clusters" / "goner.json"
    assert manifest_path.exists()

    mock_sky.down.return_value = "rid-down"
    mock_sky.stream_and_get.return_value = None
    b.cluster_down("goner")
    assert not manifest_path.exists()


# ---- autostop wrapper ------------------------------------------------


def test_set_cluster_autostop_passes_kwargs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    mock_sky = MagicMock()
    mock_sky.autostop.return_value = "rid"
    mock_sky.stream_and_get.return_value = None

    b = _backend(mock_sky)
    ok = b._set_cluster_autostop(
        cluster_name="x",
        idle_minutes=45,
        wait_for="jobs",
        hook="echo done",
    )
    assert ok is True
    mock_sky.autostop.assert_called_once()
    args, kwargs = mock_sky.autostop.call_args
    assert args[0] == "x"
    assert kwargs.get("idle_minutes") == 45
    assert kwargs.get("hook") == "echo done"
    # wait_for should be coerced to the AutostopWaitFor enum (not the raw str).
    wait_arg = kwargs.get("wait_for")
    assert wait_arg is not None
    assert getattr(wait_arg, "value", None) == "jobs"
