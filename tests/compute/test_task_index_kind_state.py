"""PR1 — kind/state discriminators on task_index.

Covers the consolidation refactor's read-side foundation: list_tasks filters
by kind/state/session_id with back-compat defaults (kind-less manifest →
compute_job, state-less manifest → running), get_task with strict-mode
unknown-kind detection, and update_task_state lifecycle transitions.

All tests redirect ~/.sciagent/tasks/ to a tmp dir; nothing touches the real
user-global manifest store.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from sciagent.compute import task_index


@pytest.fixture
def tmp_manifest_dir(monkeypatch, tmp_path: Path) -> Path:
    """Redirect ~/.sciagent/tasks/ to a tmp dir for the duration of the test."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    target = fake_home / ".sciagent" / "tasks"
    monkeypatch.setattr(task_index, "manifest_dir", lambda: target)
    return target


def _write_raw(target_dir: Path, job_id: str, payload: dict) -> Path:
    """Write a manifest by hand without going through write_task.

    Used to plant pre-PR1 (kind-less) manifests or deliberately malformed
    records that exercise back-compat / strict-mode paths.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{job_id}.json"
    path.write_text(json.dumps(payload))
    return path


# ---- kind/state round-trip --------------------------------------------------


def test_kind_and_state_round_trip(tmp_manifest_dir: Path):
    record = {
        "job_id": "sciagent-rt-ks",
        "kind": "compute_job",
        "state": "running",
        "completed_at": None,
        "result_summary": None,
        "intent": {"paper": "X"},
    }
    task_index.write_task(record)
    got = task_index.read_task("sciagent-rt-ks")
    assert got is not None
    assert got["kind"] == "compute_job"
    assert got["state"] == "running"
    assert got["completed_at"] is None
    assert got["result_summary"] is None


# ---- list_tasks back-compat -------------------------------------------------


def test_back_compat_kind_absent_treated_as_compute_job(tmp_manifest_dir: Path):
    """A pre-PR1 manifest (no kind field) must filter as kind=compute_job."""
    _write_raw(
        tmp_manifest_dir,
        "sciagent-pre-pr1",
        {"job_id": "sciagent-pre-pr1", "intent": {"paper": "old"}},
    )
    rows = task_index.list_tasks(kind="compute_job")
    assert len(rows) == 1
    assert rows[0]["job_id"] == "sciagent-pre-pr1"


def test_back_compat_state_absent_treated_as_running(tmp_manifest_dir: Path):
    """A manifest without state must filter as state=running."""
    _write_raw(
        tmp_manifest_dir,
        "sciagent-no-state",
        {"job_id": "sciagent-no-state", "kind": "compute_job"},
    )
    rows = task_index.list_tasks(state="running")
    assert [r["job_id"] for r in rows] == ["sciagent-no-state"]
    rows = task_index.list_tasks(state="completed")
    assert rows == []


def test_list_tasks_filter_kind_excludes_other(tmp_manifest_dir: Path):
    """Three manifests, three kinds: list_tasks(kind=compute_job) returns
    the two that resolve to compute_job (one explicit, one kind-less)."""
    task_index.write_task(
        {"job_id": "sciagent-cj", "kind": "compute_job", "state": "running"}
    )
    _write_raw(
        tmp_manifest_dir,
        "sciagent-old",
        {"job_id": "sciagent-old"},  # kind absent → defaults to compute_job
    )
    _write_raw(
        tmp_manifest_dir,
        "sciagent-sub",
        {"job_id": "sciagent-sub", "kind": "subagent", "state": "running"},
    )

    rows = task_index.list_tasks(kind="compute_job")
    job_ids = sorted(r["job_id"] for r in rows)
    assert job_ids == ["sciagent-cj", "sciagent-old"]

    rows = task_index.list_tasks(kind="subagent")
    assert [r["job_id"] for r in rows] == ["sciagent-sub"]


def test_list_tasks_filter_state_terminal_excluded_from_running(
    tmp_manifest_dir: Path,
):
    task_index.write_task(
        {"job_id": "sciagent-r", "kind": "compute_job", "state": "running"}
    )
    task_index.write_task(
        {"job_id": "sciagent-c", "kind": "compute_job", "state": "completed"}
    )
    task_index.write_task(
        {"job_id": "sciagent-f", "kind": "compute_job", "state": "failed"}
    )

    running = sorted(r["job_id"] for r in task_index.list_tasks(state="running"))
    assert running == ["sciagent-r"]
    terminal = sorted(
        r["job_id"]
        for s in ("completed", "failed")
        for r in task_index.list_tasks(state=s)
    )
    assert terminal == ["sciagent-c", "sciagent-f"]


def test_list_tasks_filter_session_id(tmp_manifest_dir: Path):
    task_index.write_task(
        {
            "job_id": "sciagent-a",
            "kind": "compute_job",
            "state": "running",
            "session_id": "alpha",
        }
    )
    task_index.write_task(
        {
            "job_id": "sciagent-b",
            "kind": "compute_job",
            "state": "running",
            "session_id": "beta",
        }
    )
    rows = task_index.list_tasks(session_id="alpha")
    assert [r["job_id"] for r in rows] == ["sciagent-a"]


def test_list_tasks_filters_compose(tmp_manifest_dir: Path):
    task_index.write_task(
        {
            "job_id": "sciagent-x",
            "kind": "compute_job",
            "state": "running",
            "session_id": "s1",
        }
    )
    task_index.write_task(
        {
            "job_id": "sciagent-y",
            "kind": "compute_job",
            "state": "completed",
            "session_id": "s1",
        }
    )
    rows = task_index.list_tasks(
        kind="compute_job", state="running", session_id="s1"
    )
    assert [r["job_id"] for r in rows] == ["sciagent-x"]


def test_list_tasks_no_args_still_returns_all(tmp_manifest_dir: Path):
    """Pre-PR1 callers (e.g. reaper before commit 3) called list_tasks() with
    no args expecting every manifest. That contract must keep working."""
    task_index.write_task(
        {"job_id": "sciagent-p", "kind": "compute_job", "state": "running"}
    )
    _write_raw(
        tmp_manifest_dir,
        "sciagent-q",
        {"job_id": "sciagent-q"},  # kind/state both absent
    )
    rows = task_index.list_tasks()
    assert sorted(r["job_id"] for r in rows) == ["sciagent-p", "sciagent-q"]


# ---- get_task strict mode ---------------------------------------------------


def test_get_task_returns_record(tmp_manifest_dir: Path):
    task_index.write_task(
        {"job_id": "sciagent-g1", "kind": "compute_job", "state": "running"}
    )
    got = task_index.get_task("sciagent-g1")
    assert got is not None
    assert got["job_id"] == "sciagent-g1"


def test_get_task_returns_none_for_missing(tmp_manifest_dir: Path):
    assert task_index.get_task("sciagent-ghost") is None


def test_get_task_strict_raises_on_unknown_kind(tmp_manifest_dir: Path):
    _write_raw(
        tmp_manifest_dir,
        "sciagent-alien",
        {"job_id": "sciagent-alien", "kind": "alien"},
    )
    # Permissive (default) — returns the record verbatim.
    got = task_index.get_task("sciagent-alien")
    assert got is not None
    assert got["kind"] == "alien"
    # Strict — surfaces the unknown kind so a dispatcher doesn't silently
    # route to the default per-kind handler.
    with pytest.raises(ValueError, match="alien"):
        task_index.get_task("sciagent-alien", strict=True)


def test_get_task_strict_accepts_known_kind(tmp_manifest_dir: Path):
    task_index.write_task(
        {"job_id": "sciagent-k1", "kind": "compute_job", "state": "running"}
    )
    got = task_index.get_task("sciagent-k1", strict=True)
    assert got is not None


def test_get_task_strict_accepts_kindless_as_compute_job(tmp_manifest_dir: Path):
    """A kind-less manifest defaults to compute_job in strict mode too."""
    _write_raw(
        tmp_manifest_dir,
        "sciagent-old",
        {"job_id": "sciagent-old"},
    )
    got = task_index.get_task("sciagent-old", strict=True)
    assert got is not None


# ---- update_task_state ------------------------------------------------------


def test_update_task_state_to_completed_sets_completed_at(tmp_manifest_dir: Path):
    task_index.write_task(
        {"job_id": "sciagent-u1", "kind": "compute_job", "state": "running"}
    )
    ok = task_index.update_task_state(
        "sciagent-u1", "completed", result_summary="all good"
    )
    assert ok is True
    got = task_index.read_task("sciagent-u1")
    assert got["state"] == "completed"
    assert got["completed_at"]  # auto-filled
    assert got["result_summary"] == "all good"


def test_update_task_state_to_failed_sets_completed_at(tmp_manifest_dir: Path):
    task_index.write_task(
        {"job_id": "sciagent-u2", "kind": "compute_job", "state": "running"}
    )
    assert task_index.update_task_state(
        "sciagent-u2", "failed", result_summary="OOM at step 4"
    )
    got = task_index.read_task("sciagent-u2")
    assert got["state"] == "failed"
    assert got["completed_at"]
    assert "OOM" in got["result_summary"]


def test_update_task_state_to_cancelled_sets_completed_at(tmp_manifest_dir: Path):
    task_index.write_task(
        {"job_id": "sciagent-u3", "kind": "compute_job", "state": "running"}
    )
    assert task_index.update_task_state(
        "sciagent-u3", "cancelled", result_summary="user-cancelled via bg_kill"
    )
    got = task_index.read_task("sciagent-u3")
    assert got["state"] == "cancelled"
    assert got["completed_at"]


def test_update_task_state_to_running_does_not_set_completed_at(
    tmp_manifest_dir: Path,
):
    task_index.write_task(
        {"job_id": "sciagent-u4", "kind": "compute_job", "state": "pending"}
    )
    assert task_index.update_task_state("sciagent-u4", "running")
    got = task_index.read_task("sciagent-u4")
    assert got["state"] == "running"
    # Non-terminal transition leaves completed_at alone (or unset).
    assert got.get("completed_at") in (None, "")


def test_update_task_state_invalid_state_returns_false(tmp_manifest_dir: Path):
    task_index.write_task(
        {"job_id": "sciagent-u5", "kind": "compute_job", "state": "running"}
    )
    assert task_index.update_task_state("sciagent-u5", "bogus") is False
    got = task_index.read_task("sciagent-u5")
    assert got["state"] == "running"  # unchanged


def test_update_task_state_missing_manifest_returns_false(tmp_manifest_dir: Path):
    assert task_index.update_task_state("sciagent-ghost", "completed") is False


def test_update_task_state_caller_supplied_completed_at_honored(
    tmp_manifest_dir: Path,
):
    task_index.write_task(
        {"job_id": "sciagent-u6", "kind": "compute_job", "state": "running"}
    )
    when = "2026-04-30T12:00:00+00:00"
    assert task_index.update_task_state(
        "sciagent-u6", "completed", completed_at=when
    )
    got = task_index.read_task("sciagent-u6")
    assert got["completed_at"] == when


def test_update_task_state_atomic_on_write_failure(
    tmp_manifest_dir: Path, monkeypatch
):
    """A write failure during update must NOT leave a half-written manifest
    nor mutate the on-disk record. Returns False instead."""
    task_index.write_task(
        {"job_id": "sciagent-u7", "kind": "compute_job", "state": "running"}
    )
    # Patch write_task to raise; update_task_state must swallow and return False.
    with patch.object(
        task_index, "write_task", side_effect=OSError("disk full")
    ):
        ok = task_index.update_task_state("sciagent-u7", "completed")
    assert ok is False
    got = task_index.read_task("sciagent-u7")
    # Original state preserved.
    assert got["state"] == "running"


# ---- _normalize -------------------------------------------------------------


def test_normalize_defaults_kind_and_state(tmp_manifest_dir: Path):
    record = {"job_id": "sciagent-n1", "intent": {"a": 1}}
    out = task_index._normalize(record)
    assert out["kind"] == "compute_job"
    assert out["state"] == "running"
    assert out["completed_at"] is None
    assert out["result_summary"] is None
    # Input must not be mutated.
    assert "kind" not in record
    assert "state" not in record


def test_normalize_preserves_explicit_fields(tmp_manifest_dir: Path):
    record = {
        "job_id": "sciagent-n2",
        "kind": "compute_job",
        "state": "completed",
        "completed_at": "2026-04-30T10:00:00+00:00",
        "result_summary": "done",
    }
    out = task_index._normalize(record)
    assert out["kind"] == "compute_job"
    assert out["state"] == "completed"
    assert out["completed_at"] == "2026-04-30T10:00:00+00:00"
    assert out["result_summary"] == "done"


def test_normalize_derives_body_for_compute_job(tmp_manifest_dir: Path):
    record = {
        "job_id": "sciagent-n3",
        "managed_job_id": 7,
        "intent": {"paper": "X"},
        "expected_artifacts": ["out.dat"],
        "command": "bash Allrun",
        "image": "python:3.11",
        "service": "openfoam",
        "timeout_sec": 1800,
    }
    out = task_index._normalize(record)
    body = out["body"]
    assert body["managed_job_id"] == 7
    assert body["intent"] == {"paper": "X"}
    assert body["expected_artifacts"] == ["out.dat"]
    assert body["command"] == "bash Allrun"
    assert body["image"] == "python:3.11"
    assert body["service"] == "openfoam"
    assert body["timeout_sec"] == 1800


def test_normalize_handles_non_dict(tmp_manifest_dir: Path):
    assert task_index._normalize(None) == {}
    assert task_index._normalize("not a dict") == {}
    assert task_index._normalize(42) == {}


# ---- kind_of ----------------------------------------------------------------


def test_kind_of_returns_kind_from_manifest(tmp_manifest_dir: Path):
    task_index.write_task(
        {"job_id": "sciagent-k1", "kind": "compute_job", "state": "running"}
    )
    assert task_index.kind_of("sciagent-k1") == "compute_job"


def test_kind_of_returns_kind_for_non_compute_manifest(tmp_manifest_dir: Path):
    """A manifest with kind=subagent that happens to share the sciagent-
    prefix must NOT be misrouted to compute_job. Manifest wins over prefix —
    this is the load-bearing property the prefix-sniff replacement enables."""
    _write_raw(
        tmp_manifest_dir,
        "sciagent-sub1",
        {"job_id": "sciagent-sub1", "kind": "subagent", "state": "running"},
    )
    assert task_index.kind_of("sciagent-sub1") == "subagent"


def test_kind_of_falls_back_to_prefix_for_no_manifest(tmp_manifest_dir: Path):
    """Legacy / brief-window-after-sky.launch / pre-B7 jobs have no manifest.
    The prefix is the only signal — fall back to compute_job so existing
    routing still works."""
    assert task_index.kind_of("sciagent-legacy") == "compute_job"


def test_kind_of_no_manifest_no_prefix_is_local(tmp_manifest_dir: Path):
    """ProcessManager-tracked bash jobs have no manifest and no sciagent-
    prefix. Default is local."""
    assert task_index.kind_of("bash-job-42") == "local"


def test_kind_of_kindless_manifest_defaults_to_compute_job(tmp_manifest_dir: Path):
    """A pre-PR1 manifest (no kind field) defaults to compute_job per
    DEFAULT_KIND. Same behavior as list_tasks/get_task."""
    _write_raw(
        tmp_manifest_dir,
        "sciagent-old",
        {"job_id": "sciagent-old"},  # no kind, no state
    )
    assert task_index.kind_of("sciagent-old") == "compute_job"


def test_kind_of_handles_empty_or_none(tmp_manifest_dir: Path):
    assert task_index.kind_of("") == "local"
    assert task_index.kind_of(None) == "local"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
