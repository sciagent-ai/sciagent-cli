"""PR4 step 3 — task_wait kind-agnostic atomic tool.

task_wait polls the in-flight task_index for a terminal state on any kind
(compute_job, subagent, future kinds). Tests cover:

  - immediate return on already-terminal manifests
  - blocking until a concurrent writer flips state
  - timeout returns the still-running snapshot (success=True, kind=bg_wait
    block=False shape)
  - kind-agnostic: works on subagent AND compute_job
  - missing-id and disappearing-manifest error paths
  - registry integration: tool registered alongside task_list / task_get
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import List

import pytest

from sciagent.compute import task_index
from sciagent.tools.atomic.task_tools import TaskWaitTool


@pytest.fixture
def tmp_manifest_dir(monkeypatch, tmp_path: Path) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    target = fake_home / ".sciagent" / "tasks"
    monkeypatch.setattr(task_index, "manifest_dir", lambda: target)
    return target


# ---- terminal-state observation --------------------------------------------


def test_task_wait_returns_immediately_on_completed(tmp_manifest_dir: Path):
    task_index.write_task(
        {
            "job_id": "sciagent-done",
            "kind": "compute_job",
            "state": "completed",
            "completed_at": "2026-04-30T10:00:00+00:00",
            "result_summary": "all green",
        }
    )
    started = time.time()
    result = TaskWaitTool().execute(id="sciagent-done", timeout=10.0)
    elapsed = time.time() - started
    assert result.success is True
    assert "completed" in result.output.lower()
    assert "all green" in result.output
    # Must NOT have polled — already terminal.
    assert elapsed < 0.2


def test_task_wait_returns_failure_on_failed_terminal(tmp_manifest_dir: Path):
    task_index.write_task(
        {
            "job_id": "sciagent-fail",
            "kind": "compute_job",
            "state": "failed",
            "completed_at": "2026-04-30T10:00:00+00:00",
            "result_summary": "OOM at step 4",
        }
    )
    result = TaskWaitTool().execute(id="sciagent-fail", timeout=10.0)
    # Wait succeeded as a *call* but the task itself failed — success=False
    # so the LLM/caller can chain on it cleanly.
    assert result.success is False
    assert "failed" in (result.error or "").lower()
    assert "OOM at step 4" in result.output


def test_task_wait_returns_failure_on_cancelled(tmp_manifest_dir: Path):
    task_index.write_task(
        {
            "job_id": "sciagent-can",
            "kind": "compute_job",
            "state": "cancelled",
            "completed_at": "2026-04-30T10:00:00+00:00",
        }
    )
    result = TaskWaitTool().execute(id="sciagent-can", timeout=10.0)
    assert result.success is False
    assert "cancelled" in (result.error or "").lower()


# ---- blocking until a writer flips state ------------------------------------


def test_task_wait_blocks_until_state_transition(tmp_manifest_dir: Path):
    """A concurrent writer flips the manifest from running→completed; the
    waiter observes the transition and returns the terminal snapshot."""
    task_index.write_task(
        {
            "job_id": "sciagent-flip",
            "kind": "subagent",
            "state": "running",
            "body": {"name": "explore"},
        }
    )

    def flipper() -> None:
        time.sleep(0.15)
        task_index.update_task_state(
            "sciagent-flip", "completed", result_summary="found the function"
        )

    t = threading.Thread(target=flipper)
    t.start()
    try:
        result = TaskWaitTool().execute(
            id="sciagent-flip", timeout=5.0, poll_interval=0.05
        )
    finally:
        t.join()

    assert result.success is True
    assert "completed" in result.output.lower()
    assert "found the function" in result.output


def test_task_wait_kind_agnostic_works_on_subagent(tmp_manifest_dir: Path):
    """Same poll path works for kind=subagent — registry-side, no compute
    coupling."""
    task_index.write_task(
        {
            "job_id": "sciagent-sub-w",
            "kind": "subagent",
            "state": "completed",
            "completed_at": "2026-04-30T10:00:00+00:00",
            "result_summary": "subagent done",
            "body": {
                "name": "explore",
                "task_preview": "find auth",
                "result": {"success": True, "summary": "found at line 42"},
            },
        }
    )
    result = TaskWaitTool().execute(id="sciagent-sub-w", timeout=10.0)
    assert result.success is True
    assert "kind: subagent" in result.output
    # Body fields surfaced (the shape task_get also produces).
    assert "task_preview" in result.output


# ---- timeout ---------------------------------------------------------------


def test_task_wait_timeout_returns_running_snapshot(tmp_manifest_dir: Path):
    """When the task is still running at deadline, return the snapshot —
    don't raise, don't return an error string. success=True so the LLM can
    branch on the snapshot's state field, mirroring bg_wait(block=False)."""
    task_index.write_task(
        {
            "job_id": "sciagent-stuck",
            "kind": "subagent",
            "state": "running",
            "body": {"name": "explore"},
        }
    )
    started = time.time()
    result = TaskWaitTool().execute(
        id="sciagent-stuck", timeout=0.2, poll_interval=0.05
    )
    elapsed = time.time() - started
    assert result.success is True
    assert "still running" in result.output.lower()
    # Actually waited for the timeout (not instant).
    assert 0.15 <= elapsed < 1.5


# ---- error paths -----------------------------------------------------------


def test_task_wait_missing_id_returns_error(tmp_manifest_dir: Path):
    """Surface the typo immediately instead of polling for the full timeout."""
    started = time.time()
    result = TaskWaitTool().execute(id="sciagent-ghost", timeout=10.0)
    elapsed = time.time() - started
    assert result.success is False
    assert "no task" in (result.error or "").lower()
    assert elapsed < 0.2


def test_task_wait_no_id_returns_error(tmp_manifest_dir: Path):
    result = TaskWaitTool().execute()
    assert result.success is False
    assert "id is required" in (result.error or "").lower()


def test_task_wait_manifest_deleted_during_wait(
    tmp_manifest_dir: Path, monkeypatch
):
    """A concurrent reaper that deletes the manifest while we're polling
    surfaces a clear error rather than spinning forever.

    Deterministic via patched get_task: first call (initial existence
    check) returns the manifest; subsequent calls (the polling loop)
    return None, simulating a delete that happened between the initial
    check and the first poll. Avoids relying on threading scheduler
    timing — a real concurrent reaper would race here, but the behavior
    we care about is the same: get_task returning None inside the loop
    triggers the disappeared-manifest error path.
    """
    task_index.write_task(
        {
            "job_id": "sciagent-disappear",
            "kind": "subagent",
            "state": "running",
            "body": {"name": "explore"},
        }
    )
    call_count = {"n": 0}
    real_get_task = task_index.get_task

    def fake_get_task(id: str, strict: bool = False):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            return None
        return real_get_task(id, strict=strict)

    monkeypatch.setattr(
        "sciagent.compute.task_index.get_task", fake_get_task
    )

    result = TaskWaitTool().execute(
        id="sciagent-disappear", timeout=5.0, poll_interval=0.02
    )
    assert result.success is False
    assert "disappeared" in (result.error or "").lower()


# ---- schema / registry integration -----------------------------------------


def test_task_wait_registers_in_atomic_registry():
    from sciagent.tools.registry import create_atomic_registry

    registry = create_atomic_registry()
    assert "task_wait" in registry.list_tools()
    # bg_wait is unchanged — both surfaces coexist.
    assert "bg_wait" in registry.list_tools()


def test_task_wait_has_to_schema():
    schema = TaskWaitTool().to_schema()
    assert schema["name"] == "task_wait"
    assert "parameters" in schema
    # JSON-encodable.
    import json

    json.dumps(schema)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
