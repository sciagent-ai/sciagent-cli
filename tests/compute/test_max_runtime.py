"""B6 — max-runtime enforcement (v4.2 §C2 reuses the existing timeout_sec).

Two layers per v4 §7 OQ2:
- On-VM: _build_task wraps the user command with the GNU `timeout` utility.
- Driver-side: reaper.reap_overdue scans manifests and cancels overdue jobs
  the on-VM wrapper can't reach (hung VMs, controller stuck, etc.).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from sciagent.compute.backends.skypilot import SkyPilotBackend
from sciagent.compute.job import ComputeRequirements, Job
from sciagent.compute.reaper import reap_overdue


# ---- on-VM timeout wrapper --------------------------------------------------


def _build_task_with_timeout(command: str, timeout_sec: int) -> str:
    """Run _build_task and return whatever was passed as `run=` to sky.Task."""
    backend = SkyPilotBackend()
    mock_sky = MagicMock()
    backend._sky = mock_sky

    job = Job(
        id="t1",
        service="custom",
        image="alpine",
        command=command,
        requirements=ComputeRequirements(cpus=1, memory_gb=1, timeout_sec=timeout_sec),
    )
    backend._build_task(job)

    _, kwargs = mock_sky.Task.call_args
    return kwargs["run"]


def test_build_task_wraps_command_with_timeout_when_timeout_sec_positive():
    run = _build_task_with_timeout("bash Allrun", 3600)
    # The wrapped form is `timeout 3600 bash -c <quoted_command>`. We assert
    # on the prefix and that the original command is present (shlex-quoted),
    # rather than pinning the exact escape style.
    assert run.startswith("timeout 3600 bash -c ")
    assert "bash Allrun" in run


def test_build_task_quotes_commands_with_special_chars():
    """Multi-statement commands with quotes and pipes must survive the
    shlex.quote wrap untouched on the inside."""
    raw = "cd /workspace && bash Allrun 2>&1 | tee log.txt"
    run = _build_task_with_timeout(raw, 600)
    assert run.startswith("timeout 600 bash -c ")
    # The original command appears verbatim inside the quotes.
    assert raw in run


def test_build_task_does_not_wrap_when_timeout_zero():
    """Callers can opt out of the timeout wrapper by passing timeout_sec=0."""
    run = _build_task_with_timeout("bash Allrun", 0)
    assert run == "bash Allrun"
    assert "timeout" not in run.split()


# ---- driver-side reaper -----------------------------------------------------


def _record(job_id: str, started_offset_sec: int, timeout_sec: int):
    """Build a fake manifest record `started_offset_sec` ago."""
    started = datetime.now(timezone.utc) - timedelta(seconds=started_offset_sec)
    return {
        "job_id": job_id,
        "session_id": "ses-test",
        "started_at": started.isoformat(),
        "timeout_sec": timeout_sec,
    }


def test_reap_overdue_terminates_overdue_clusters(monkeypatch):
    """A job started 2h ago with timeout_sec=3600 must be reaped; one started
    5min ago with timeout_sec=3600 must be left alone."""
    overdue = _record("sciagent-overdue", started_offset_sec=7200, timeout_sec=3600)
    healthy = _record("sciagent-healthy", started_offset_sec=300, timeout_sec=3600)

    monkeypatch.setattr(
        "sciagent.compute.reaper.list_tasks",
        lambda **kw: [overdue, healthy],
    )
    monkeypatch.setattr(
        "sciagent.compute.reaper.update_task_state",
        lambda *a, **kw: True,
    )
    cleanup = MagicMock(return_value=True)

    reaped = reap_overdue(cleanup=cleanup)

    assert reaped == ["sciagent-overdue"]
    cleanup.assert_called_once_with("sciagent-overdue")


def test_reap_overdue_skips_records_without_timeout(monkeypatch):
    """timeout_sec=0 / missing means "no enforcement" — never reap."""
    no_timeout = _record("sciagent-untimed", started_offset_sec=99999, timeout_sec=0)
    monkeypatch.setattr(
        "sciagent.compute.reaper.list_tasks", lambda **kw: [no_timeout]
    )
    monkeypatch.setattr(
        "sciagent.compute.reaper.update_task_state",
        lambda *a, **kw: True,
    )
    cleanup = MagicMock()

    reaped = reap_overdue(cleanup=cleanup)
    assert reaped == []
    cleanup.assert_not_called()


def test_reap_overdue_tolerates_garbage_timestamps(monkeypatch):
    """A manifest with an unparseable started_at must be skipped, not crash
    the whole reaper sweep."""
    garbage = {
        "job_id": "sciagent-garbage",
        "started_at": "not-a-date",
        "timeout_sec": 60,
    }
    overdue = _record("sciagent-overdue", started_offset_sec=7200, timeout_sec=3600)
    monkeypatch.setattr(
        "sciagent.compute.reaper.list_tasks", lambda **kw: [garbage, overdue]
    )
    monkeypatch.setattr(
        "sciagent.compute.reaper.update_task_state",
        lambda *a, **kw: True,
    )
    cleanup = MagicMock(return_value=True)

    reaped = reap_overdue(cleanup=cleanup)
    assert reaped == ["sciagent-overdue"]


def test_reap_overdue_swallows_cleanup_failures(monkeypatch):
    """A single failing cleanup must not stop the sweep — sky.down is
    idempotent and we'd rather attempt every overdue cluster than bail
    halfway through."""
    overdue1 = _record("sciagent-a", started_offset_sec=9999, timeout_sec=60)
    overdue2 = _record("sciagent-b", started_offset_sec=9999, timeout_sec=60)
    monkeypatch.setattr(
        "sciagent.compute.reaper.list_tasks", lambda **kw: [overdue1, overdue2]
    )
    monkeypatch.setattr(
        "sciagent.compute.reaper.update_task_state",
        lambda *a, **kw: True,
    )

    cleanup = MagicMock(side_effect=[RuntimeError("sky offline"), True])
    reaped = reap_overdue(cleanup=cleanup)

    assert reaped == ["sciagent-a", "sciagent-b"]
    assert cleanup.call_count == 2


# ---- PR1 (consolidation): kind/state filter + cancelled-on-reap -------------


def test_reaper_only_reaps_running_compute_jobs(tmp_path, monkeypatch):
    """Reaper must filter list_tasks(kind='compute_job', state='running').
    A completed manifest whose started_at + timeout_sec is in the past must
    NOT be reaped — its lifecycle is over and the cluster is already gone."""
    from sciagent.compute import task_index

    fake_home = tmp_path / "home" / ".sciagent" / "tasks"
    monkeypatch.setattr(task_index, "manifest_dir", lambda: fake_home)

    started = (datetime.now(timezone.utc) - timedelta(seconds=7200)).isoformat()
    # Three manifests: running overdue, completed overdue, kindless overdue.
    task_index.write_task(
        {
            "job_id": "sciagent-running",
            "kind": "compute_job",
            "state": "running",
            "started_at": started,
            "timeout_sec": 3600,
        }
    )
    task_index.write_task(
        {
            "job_id": "sciagent-done",
            "kind": "compute_job",
            "state": "completed",
            "started_at": started,
            "timeout_sec": 3600,
        }
    )
    # Pre-PR1 manifest (no kind / state). Back-compat: defaults to
    # compute_job/running, so it SHOULD be reaped.
    fake_home.mkdir(parents=True, exist_ok=True)
    import json as _json

    (fake_home / "sciagent-legacy.json").write_text(
        _json.dumps(
            {
                "job_id": "sciagent-legacy",
                "started_at": started,
                "timeout_sec": 3600,
            }
        )
    )

    cleanup = MagicMock(return_value=True)
    reaped = reap_overdue(cleanup=cleanup)

    assert sorted(reaped) == ["sciagent-legacy", "sciagent-running"]
    assert "sciagent-done" not in reaped


def test_reaper_marks_state_cancelled_after_cleanup(tmp_path, monkeypatch):
    """After a successful cleanup, the manifest must read state='cancelled',
    completed_at populated, and result_summary set — so cross-session readers
    see the truth without needing to re-query sky."""
    from sciagent.compute import task_index

    fake_home = tmp_path / "home" / ".sciagent" / "tasks"
    monkeypatch.setattr(task_index, "manifest_dir", lambda: fake_home)

    started = (datetime.now(timezone.utc) - timedelta(seconds=9999)).isoformat()
    task_index.write_task(
        {
            "job_id": "sciagent-victim",
            "kind": "compute_job",
            "state": "running",
            "started_at": started,
            "timeout_sec": 60,
        }
    )

    cleanup = MagicMock(return_value=True)
    reaped = reap_overdue(cleanup=cleanup)
    assert reaped == ["sciagent-victim"]

    after = task_index.read_task("sciagent-victim")
    assert after["state"] == "cancelled"
    assert after["completed_at"]
    assert "reaped" in after["result_summary"].lower()
    assert "timeout" in after["result_summary"].lower()


def test_reaper_cleanup_failure_still_marks_cancelled(tmp_path, monkeypatch):
    """If cleanup raises, the reaper still records the cancellation on disk
    — the cluster is presumed already torn down or unreachable, and the
    manifest's state must not lie about a job we've stopped tracking."""
    from sciagent.compute import task_index

    fake_home = tmp_path / "home" / ".sciagent" / "tasks"
    monkeypatch.setattr(task_index, "manifest_dir", lambda: fake_home)

    started = (datetime.now(timezone.utc) - timedelta(seconds=9999)).isoformat()
    task_index.write_task(
        {
            "job_id": "sciagent-flaky",
            "kind": "compute_job",
            "state": "running",
            "started_at": started,
            "timeout_sec": 60,
        }
    )

    cleanup = MagicMock(side_effect=RuntimeError("sky offline"))
    reaped = reap_overdue(cleanup=cleanup)
    assert reaped == ["sciagent-flaky"]

    after = task_index.read_task("sciagent-flaky")
    assert after["state"] == "cancelled"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
