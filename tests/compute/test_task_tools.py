"""PR3 — kind-agnostic registry tools (task_list / task_get).

These tools wrap the task_index API as LLM-facing tools: task_list filters
across kinds/states/sessions, task_get inspects a single record. They
complement bg_* (which is the cloud-job-specific runtime surface) without
replacing it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sciagent.compute import task_index
from sciagent.tools.atomic.task_tools import TaskGetTool, TaskListTool


@pytest.fixture
def tmp_manifest_dir(monkeypatch, tmp_path: Path) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    target = fake_home / ".sciagent" / "tasks"
    monkeypatch.setattr(task_index, "manifest_dir", lambda: target)
    return target


def _seed(job_id: str, **fields):
    task_index.write_task({"job_id": job_id, **fields})


# ---- task_list --------------------------------------------------------------


def test_task_list_empty_registry(tmp_manifest_dir):
    tool = TaskListTool()
    result = tool.execute()
    assert result.success is True
    assert "no tasks" in result.output.lower()


def test_task_list_returns_all_tasks_when_unfiltered(tmp_manifest_dir):
    _seed("sciagent-a", kind="compute_job", state="running")
    _seed("sciagent-b", kind="compute_job", state="completed")

    tool = TaskListTool()
    result = tool.execute()
    assert result.success is True
    assert "sciagent-a" in result.output
    assert "sciagent-b" in result.output
    assert "2 task(s)" in result.output


def test_task_list_filters_by_kind(tmp_manifest_dir):
    _seed("sciagent-a", kind="compute_job", state="running")
    # Plant a kind=subagent manifest by hand — task_index doesn't write
    # subagent kinds yet, but task_list must still filter on it.
    (tmp_manifest_dir).mkdir(parents=True, exist_ok=True)
    (tmp_manifest_dir / "sciagent-sub.json").write_text(
        json.dumps(
            {"job_id": "sciagent-sub", "kind": "subagent", "state": "running"}
        )
    )

    tool = TaskListTool()
    result = tool.execute(kind="compute_job")
    assert "sciagent-a" in result.output
    assert "sciagent-sub" not in result.output

    result = tool.execute(kind="subagent")
    assert "sciagent-sub" in result.output
    assert "sciagent-a" not in result.output


def test_task_list_filters_by_state(tmp_manifest_dir):
    _seed("sciagent-r", kind="compute_job", state="running")
    _seed("sciagent-c", kind="compute_job", state="completed")

    result = TaskListTool().execute(state="running")
    assert "sciagent-r" in result.output
    assert "sciagent-c" not in result.output


def test_task_list_filters_by_session_id(tmp_manifest_dir):
    _seed("sciagent-a", kind="compute_job", state="running", session_id="alpha")
    _seed("sciagent-b", kind="compute_job", state="running", session_id="beta")

    result = TaskListTool().execute(session_id="alpha")
    assert "sciagent-a" in result.output
    assert "sciagent-b" not in result.output


def test_task_list_filters_compose(tmp_manifest_dir):
    _seed("sciagent-x", kind="compute_job", state="running", session_id="s1")
    _seed("sciagent-y", kind="compute_job", state="completed", session_id="s1")
    _seed("sciagent-z", kind="compute_job", state="running", session_id="s2")

    result = TaskListTool().execute(
        kind="compute_job", state="running", session_id="s1"
    )
    assert "sciagent-x" in result.output
    assert "sciagent-y" not in result.output
    assert "sciagent-z" not in result.output


def test_task_list_filter_qualifier_in_empty_message(tmp_manifest_dir):
    """When filtered to nothing, the message names the filters that were
    applied — agents debugging a wrong filter need that signal."""
    _seed("sciagent-x", kind="compute_job", state="running")

    result = TaskListTool().execute(state="failed")
    assert "no tasks" in result.output.lower()
    assert "state=failed" in result.output


def test_task_list_includes_intent_for_compute_job(tmp_manifest_dir):
    _seed(
        "sciagent-rich",
        kind="compute_job",
        state="running",
        intent={"paper": "X", "case": "y"},
    )

    result = TaskListTool().execute()
    assert "intent" in result.output
    assert "paper" in result.output


def test_task_list_back_compat_kindless_manifest(tmp_manifest_dir):
    """Pre-PR1 manifest (no kind / state) appears in unfiltered listings and
    when filtering on the default kind / state."""
    tmp_manifest_dir.mkdir(parents=True, exist_ok=True)
    (tmp_manifest_dir / "sciagent-old.json").write_text(
        json.dumps({"job_id": "sciagent-old"})  # no kind, no state
    )

    result = TaskListTool().execute()
    assert "sciagent-old" in result.output

    result = TaskListTool().execute(kind="compute_job", state="running")
    assert "sciagent-old" in result.output


# ---- task_get ---------------------------------------------------------------


def test_task_get_requires_id(tmp_manifest_dir):
    result = TaskGetTool().execute()
    assert result.success is False
    assert "id is required" in result.error


def test_task_get_returns_not_found(tmp_manifest_dir):
    result = TaskGetTool().execute(id="sciagent-ghost")
    assert result.success is False
    assert "no task" in result.error.lower()


def test_task_get_renders_normalized_record(tmp_manifest_dir):
    _seed(
        "sciagent-g1",
        kind="compute_job",
        state="running",
        session_id="ses-g",
        started_at="2026-04-30T10:00:00+00:00",
        owner_pid=4242,
        intent={"paper": "X", "case": "y"},
        managed_job_id=7,
    )

    result = TaskGetTool().execute(id="sciagent-g1")
    assert result.success is True
    out = result.output
    assert "sciagent-g1" in out
    assert "kind: compute_job" in out
    assert "state: running" in out
    assert "session: ses-g" in out
    assert "owner_pid: 4242" in out
    # Body fields surface under the body: section.
    assert "body:" in out
    assert "managed_job_id: 7" in out


def test_task_get_back_compat_kindless(tmp_manifest_dir):
    """task_get on a pre-PR1 manifest renders the defaulted kind/state."""
    tmp_manifest_dir.mkdir(parents=True, exist_ok=True)
    (tmp_manifest_dir / "sciagent-old.json").write_text(
        json.dumps(
            {
                "job_id": "sciagent-old",
                "intent": {"paper": "legacy"},
            }
        )
    )

    result = TaskGetTool().execute(id="sciagent-old")
    assert result.success is True
    assert "kind: compute_job" in result.output  # defaulted
    assert "state: running" in result.output  # defaulted


def test_task_get_includes_completed_at_and_result_summary(tmp_manifest_dir):
    """A terminal task surfaces its lifecycle timestamps + summary."""
    _seed(
        "sciagent-done",
        kind="compute_job",
        state="completed",
        completed_at="2026-04-30T11:00:00+00:00",
        result_summary="all green",
    )
    result = TaskGetTool().execute(id="sciagent-done")
    assert "state: completed" in result.output
    assert "completed: 2026-04-30T11:00:00+00:00" in result.output
    assert "result: all green" in result.output


# ---- registry integration ---------------------------------------------------


def test_task_tools_register_in_atomic_registry():
    from sciagent.tools.registry import create_atomic_registry

    registry = create_atomic_registry()
    names = registry.list_tools()
    assert "task_list" in names
    assert "task_get" in names
    # bg_* is unchanged — both surfaces coexist.
    assert "bg_status" in names
    assert "bg_kill" in names


def test_task_tools_have_to_schema():
    """Schema must be JSON-serializable and well-formed."""
    for tool in (TaskListTool(), TaskGetTool()):
        schema = tool.to_schema()
        assert "name" in schema
        assert "description" in schema
        assert "parameters" in schema
        # Parameters object must be JSON-encodable.
        json.dumps(schema)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
