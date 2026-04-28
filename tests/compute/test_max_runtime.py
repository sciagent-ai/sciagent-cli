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
        lambda: [overdue, healthy],
    )
    cleanup = MagicMock(return_value=True)

    reaped = reap_overdue(cleanup=cleanup)

    assert reaped == ["sciagent-overdue"]
    cleanup.assert_called_once_with("sciagent-overdue")


def test_reap_overdue_skips_records_without_timeout(monkeypatch):
    """timeout_sec=0 / missing means "no enforcement" — never reap."""
    no_timeout = _record("sciagent-untimed", started_offset_sec=99999, timeout_sec=0)
    monkeypatch.setattr(
        "sciagent.compute.reaper.list_tasks", lambda: [no_timeout]
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
        "sciagent.compute.reaper.list_tasks", lambda: [garbage, overdue]
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
        "sciagent.compute.reaper.list_tasks", lambda: [overdue1, overdue2]
    )

    cleanup = MagicMock(side_effect=[RuntimeError("sky offline"), True])
    reaped = reap_overdue(cleanup=cleanup)

    assert reaped == ["sciagent-a", "sciagent-b"]
    assert cleanup.call_count == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
