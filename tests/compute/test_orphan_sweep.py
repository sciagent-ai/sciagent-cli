"""B11 — orphan sweep (mocked, $0).

v4.1 §2 acceptance: write a session manifest, kill its ``owner_pid``, run
the sweep, confirm the cluster is cancelled. v4.2 §C4 reshaped this from a
``sciagent compute sweep`` CLI subcommand to a function in
``compute/orphan.py`` — the test calls the function directly.

Cancellation uses ``sky.down`` (NOT ``sky.jobs.cancel`` — that's M1A,
v4.2 §C1).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sciagent.compute import orphan, task_index


@pytest.fixture
def tmp_manifest_dir(monkeypatch, tmp_path: Path) -> Path:
    """Redirect ~/.sciagent/tasks to a tmp dir so tests never touch real $HOME."""
    fake_dir = tmp_path / "home" / ".sciagent" / "tasks"
    monkeypatch.setattr(task_index, "manifest_dir", lambda: fake_dir)
    return fake_dir


@pytest.fixture
def fixed_self_pid() -> int:
    return 99999


def _write_manifest(job_id: str, owner_pid: int, **extras) -> None:
    record = {
        "job_id": job_id,
        "session_id": "ses-test",
        "owner_pid": owner_pid,
        "started_at": "2026-04-28T10:00:00+00:00",
        "command": "echo test",
        "timeout_sec": 0,
    }
    record.update(extras)
    task_index.write_task(record)


# ---------------------------------------------------------------------------


def test_sweep_cancels_orphans_and_removes_manifest(
    tmp_manifest_dir, monkeypatch, fixed_self_pid
):
    """B11 acceptance: dead-pid manifest → cleanup called + manifest removed."""
    _write_manifest("sciagent-orphan", owner_pid=11111)

    # Pretend pid 11111 doesn't exist.
    monkeypatch.setattr(orphan, "_pid_is_alive", lambda pid: False)
    cleanup = MagicMock(return_value=True)

    swept = orphan.sweep(cleanup=cleanup, self_pid=fixed_self_pid)

    assert swept == ["sciagent-orphan"]
    cleanup.assert_called_once_with("sciagent-orphan")
    # Manifest is removed so subsequent sweeps don't re-attempt it.
    assert task_index.read_task("sciagent-orphan") is None


def test_sweep_skips_live_owner(tmp_manifest_dir, monkeypatch, fixed_self_pid):
    _write_manifest("sciagent-alive", owner_pid=22222)

    monkeypatch.setattr(orphan, "_pid_is_alive", lambda pid: True)
    cleanup = MagicMock()

    swept = orphan.sweep(cleanup=cleanup, self_pid=fixed_self_pid)

    assert swept == []
    cleanup.assert_not_called()
    assert task_index.read_task("sciagent-alive") is not None


def test_sweep_skips_self_owned_manifest(tmp_manifest_dir, fixed_self_pid):
    """A manifest owned by the *current* sciagent process is not orphaned —
    even if some pid-alive probe is unreliable, never sweep our own jobs."""
    _write_manifest("sciagent-mine", owner_pid=fixed_self_pid)
    cleanup = MagicMock()

    swept = orphan.sweep(cleanup=cleanup, self_pid=fixed_self_pid)

    assert swept == []
    cleanup.assert_not_called()
    assert task_index.read_task("sciagent-mine") is not None


def test_sweep_skips_records_without_owner_pid(
    tmp_manifest_dir, monkeypatch, fixed_self_pid
):
    """Pre-B7 manifests (or callers that declined to record an owner_pid)
    must not be swept blind — that would risk killing live jobs whose
    ownership we can't verify."""
    _write_manifest("sciagent-noowner", owner_pid=0)
    monkeypatch.setattr(orphan, "_pid_is_alive", lambda pid: False)
    cleanup = MagicMock()

    swept = orphan.sweep(cleanup=cleanup, self_pid=fixed_self_pid)

    assert swept == []
    cleanup.assert_not_called()


def test_sweep_handles_mixed_live_and_dead_owners(
    tmp_manifest_dir, monkeypatch, fixed_self_pid
):
    _write_manifest("sciagent-dead-1", owner_pid=11111)
    _write_manifest("sciagent-alive", owner_pid=22222)
    _write_manifest("sciagent-dead-2", owner_pid=33333)

    alive_pids = {22222}
    monkeypatch.setattr(orphan, "_pid_is_alive", lambda pid: pid in alive_pids)

    cleanup = MagicMock(return_value=True)
    swept = orphan.sweep(cleanup=cleanup, self_pid=fixed_self_pid)

    assert sorted(swept) == ["sciagent-dead-1", "sciagent-dead-2"]
    assert sorted(c.args[0] for c in cleanup.call_args_list) == [
        "sciagent-dead-1",
        "sciagent-dead-2",
    ]
    # Live one still has its manifest.
    assert task_index.read_task("sciagent-alive") is not None


def test_sweep_continues_after_cleanup_failure(
    tmp_manifest_dir, monkeypatch, fixed_self_pid
):
    """A single failing sky.down must not halt the sweep — sky.down is
    idempotent and we'd rather try every orphan than stop halfway."""
    _write_manifest("sciagent-a", owner_pid=11111)
    _write_manifest("sciagent-b", owner_pid=22222)
    monkeypatch.setattr(orphan, "_pid_is_alive", lambda pid: False)

    cleanup = MagicMock(side_effect=[RuntimeError("sky offline"), True])
    swept = orphan.sweep(cleanup=cleanup, self_pid=fixed_self_pid)

    assert sorted(swept) == ["sciagent-a", "sciagent-b"]
    assert cleanup.call_count == 2
    # Both manifests removed regardless — leaving the failed one would cause
    # subsequent sweeps to keep retrying a cluster the user has already
    # been told about.
    assert task_index.read_task("sciagent-a") is None
    assert task_index.read_task("sciagent-b") is None


def test_pid_is_alive_returns_false_for_invalid_pids():
    """The pid probe must reject obviously-bogus pids without raising."""
    assert orphan._pid_is_alive(0) is False
    assert orphan._pid_is_alive(-1) is False
    assert orphan._pid_is_alive(None) is False  # type: ignore[arg-type]
    assert orphan._pid_is_alive("123") is False  # type: ignore[arg-type]


def test_pid_is_alive_returns_true_for_current_process():
    """Sanity check: the live-process probe is wired correctly to os.kill."""
    import os

    assert orphan._pid_is_alive(os.getpid()) is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
