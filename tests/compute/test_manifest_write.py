"""B7 — session manifest at ~/.sciagent/tasks/<job_id>.json.

Tests cover both the task_index module's read/write round-trip and the
ComputeTool integration that writes a manifest after a successful skypilot
launch. Manifest path is patched onto a tmp directory so tests never touch
the user's real ~/.sciagent.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sciagent.compute import task_index


@pytest.fixture
def tmp_manifest_dir(monkeypatch, tmp_path: Path) -> Path:
    """Redirect ~/.sciagent/tasks/ to a tmp dir for the duration of the test."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(task_index, "manifest_dir", lambda: fake_home / ".sciagent" / "tasks")
    return fake_home / ".sciagent" / "tasks"


# ---- task_index round-trip --------------------------------------------------


def test_write_then_read_round_trips(tmp_manifest_dir: Path):
    record = {
        "job_id": "sciagent-rt1",
        "session_id": "ses-rt",
        "intent": {"paper": "X", "case": "y", "run": "1"},
        "expected_artifacts": ["out.dat"],
        "owner_pid": 1234,
        "started_at": "2026-04-28T10:00:00+00:00",
        "command": "bash Allrun",
        "timeout_sec": 3600,
    }
    target = task_index.write_task(record)

    assert target.exists()
    assert target == tmp_manifest_dir / "sciagent-rt1.json"

    got = task_index.read_task("sciagent-rt1")
    assert got == record


def test_write_task_is_atomic(tmp_manifest_dir: Path):
    """write_task uses tempfile + os.replace; a successful write should
    leave only the final file behind, no in-flight tempfiles."""
    task_index.write_task({"job_id": "sciagent-atomic", "intent": None})
    siblings = sorted(p.name for p in tmp_manifest_dir.iterdir())
    assert siblings == ["sciagent-atomic.json"]


def test_write_task_rejects_missing_job_id(tmp_manifest_dir: Path):
    with pytest.raises(ValueError):
        task_index.write_task({"intent": {}})


def test_list_tasks_returns_all_well_formed_manifests(tmp_manifest_dir: Path):
    task_index.write_task({"job_id": "sciagent-a", "intent": None})
    task_index.write_task({"job_id": "sciagent-b", "intent": None})
    # Plant a corrupt manifest — list_tasks must skip it without raising.
    tmp_manifest_dir.mkdir(parents=True, exist_ok=True)
    (tmp_manifest_dir / "corrupt.json").write_text("not json{{{")

    rows = task_index.list_tasks()
    job_ids = sorted(r["job_id"] for r in rows)
    assert job_ids == ["sciagent-a", "sciagent-b"]


def test_delete_task_removes_manifest(tmp_manifest_dir: Path):
    task_index.write_task({"job_id": "sciagent-del", "intent": None})
    assert task_index.read_task("sciagent-del") is not None

    assert task_index.delete_task("sciagent-del") is True
    assert task_index.read_task("sciagent-del") is None
    # Idempotent: second delete returns False, no exception.
    assert task_index.delete_task("sciagent-del") is False


def test_read_task_returns_none_on_corrupt_json(tmp_manifest_dir: Path):
    tmp_manifest_dir.mkdir(parents=True, exist_ok=True)
    (tmp_manifest_dir / "sciagent-broken.json").write_text("{not: valid")
    assert task_index.read_task("sciagent-broken") is None


# ---- ComputeTool writes manifest after a successful skypilot launch ---------


def test_compute_tool_writes_manifest_after_skypilot_launch(tmp_manifest_dir: Path):
    """B7 acceptance: compute_run on skypilot must persist the manifest with
    job_id (cluster name), session_id, intent, expected_artifacts, owner_pid,
    started_at, command, image, timeout_sec — the fields that resume + the
    reaper rely on."""
    from sciagent.tools.atomic.compute import ComputeTool

    ComputeTool._shared_session_id = None
    tool = ComputeTool()

    fake_skypilot = MagicMock()
    fake_skypilot.name = "skypilot"
    fake_skypilot.get_workspace_mount.return_value = None  # not exercised here

    fake_router = MagicMock()
    fake_router.list_backends.return_value = ["skypilot"]
    fake_router._backends = {"skypilot": fake_skypilot}
    fake_router.select.return_value = (fake_skypilot, "Using requested backend: skypilot")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.1}
    fake_router.run.return_value = "sciagent-manifest1"

    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="bash Allrun",
            service="openfoam-swak4foam-2012",
            backend="skypilot",
            intent={"paper": "Boussinesq2024", "case": "typical_c", "run": "rep-1"},
            expected_artifacts=["postProcessing/probes/0/U"],
            timeout_sec=1800,
        )

    assert result.success is True

    manifest = task_index.read_task("sciagent-manifest1")
    assert manifest is not None
    assert manifest["job_id"] == "sciagent-manifest1"
    assert manifest["intent"] == {
        "paper": "Boussinesq2024",
        "case": "typical_c",
        "run": "rep-1",
    }
    assert manifest["expected_artifacts"] == ["postProcessing/probes/0/U"]
    assert manifest["timeout_sec"] == 1800
    assert manifest["command"] == "bash Allrun"
    # owner_pid + started_at must be present for the reaper / sweep paths.
    assert isinstance(manifest["owner_pid"], int)
    assert manifest["owner_pid"] > 0
    assert manifest["started_at"]


def test_compute_tool_does_not_write_manifest_for_local_backend(tmp_manifest_dir: Path):
    """Local jobs are tracked by ProcessManager already — writing a duplicate
    manifest would lie about which store owns the job."""
    from sciagent.tools.atomic.compute import ComputeTool

    ComputeTool._shared_session_id = None
    tool = ComputeTool()

    fake_local = MagicMock()
    fake_local.name = "local"
    fake_router = MagicMock()
    fake_router.list_backends.return_value = ["local"]
    fake_router._backends = {"local": fake_local}
    fake_router.select.return_value = (fake_local, "Using local Docker")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.0}
    fake_router.run.return_value = "local-job-1"

    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="echo hi",
            image="alpine",
            backend="local",
            intent={"some": "intent"},
        )

    assert result.success is True
    # No manifests should have been written.
    if tmp_manifest_dir.exists():
        assert list(tmp_manifest_dir.iterdir()) == []


def test_compute_tool_manifest_write_failure_does_not_break_launch(
    tmp_manifest_dir: Path, monkeypatch
):
    """If write_task raises (read-only home, disk full, etc.), the launch
    must still succeed for the user. The manifest is best-effort."""
    from sciagent.tools.atomic.compute import ComputeTool

    ComputeTool._shared_session_id = None
    tool = ComputeTool()

    monkeypatch.setattr(
        "sciagent.compute.task_index.write_task",
        MagicMock(side_effect=OSError("read-only filesystem")),
    )

    fake_skypilot = MagicMock()
    fake_skypilot.name = "skypilot"
    fake_router = MagicMock()
    fake_router.list_backends.return_value = ["skypilot"]
    fake_router._backends = {"skypilot": fake_skypilot}
    fake_router.select.return_value = (fake_skypilot, "Using requested backend: skypilot")
    fake_router.estimate_cost.return_value = {"estimated_hourly": 0.1}
    fake_router.run.return_value = "sciagent-flaky-fs"

    with patch.object(tool, "_get_router", return_value=fake_router):
        result = tool.execute(
            command="echo hi",
            image="alpine",
            backend="skypilot",
        )

    assert result.success is True
    assert result.output["job_id"] == "sciagent-flaky-fs"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
