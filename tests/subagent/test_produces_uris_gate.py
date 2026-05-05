"""produces_uris validation gate — sync + background paths.

Catches the trajectory failure mode where a subagent claims success but
hasn't actually written its declared deliverable. Imports the
_FakeSubAgent + tmp_manifest_dir fixtures from test_background_spawn.py
so the deterministic-no-LLM pattern stays consistent across the gate
tests.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from sciagent.compute import task_index
from sciagent.subagent import (
    SubAgentOrchestrator,
    SubAgentResult,
    TaskTool,
)
from sciagent.tools.registry import ToolResult

# Reuse the existing fakes/fixtures so the test surface stays uniform.
from tests.subagent.test_background_spawn import (  # noqa: F401
    _FakeSubAgent,
    _make_orchestrator_with_fake,
    _wait_for_terminal,
    tmp_manifest_dir,
)


# ---- sync path -----------------------------------------------------------


def test_sync_no_produces_uris_unchanged_behavior(tmp_manifest_dir: Path, tmp_path: Path):
    """When produces_uris is omitted, the gate is a no-op — the subagent's
    own success result passes through verbatim."""
    orch = _make_orchestrator_with_fake(
        lambda name: _FakeSubAgent(name, output="ok")
    )
    orch.working_dir = str(tmp_path)
    result = orch.spawn("explore", "find a thing")  # no produces_uris
    assert result.success is True
    assert result.output == "ok"


def test_sync_local_pattern_resolves_passes(tmp_manifest_dir: Path, tmp_path: Path):
    """A pattern that resolves to a non-trivial file passes the gate."""
    target = tmp_path / "out.txt"
    target.write_bytes(b"x" * 300)

    orch = _make_orchestrator_with_fake(
        lambda name: _FakeSubAgent(name, output="ok")
    )
    orch.working_dir = str(tmp_path)
    result = orch.spawn(
        "explore", "find a thing",
        produces_uris=["./out.txt"],
    )
    assert result.success is True


def test_sync_local_pattern_missing_fails(tmp_manifest_dir: Path, tmp_path: Path):
    """A pattern that resolves to zero files downgrades the result to
    success=False with the missing pattern named in the error."""
    orch = _make_orchestrator_with_fake(
        lambda name: _FakeSubAgent(name, output="ok claimed")
    )
    orch.working_dir = str(tmp_path)
    result = orch.spawn(
        "explore", "find a thing",
        produces_uris=["./missing.txt"],
    )
    assert result.success is False
    assert "produces_uris validation FAILED" in result.error
    assert "./missing.txt" in result.error
    # Iterations / tokens / output preserved for cost attribution.
    assert result.iterations == 3
    assert result.tokens_used == 42
    assert result.output == "ok claimed"


def test_sync_local_pattern_below_min_bytes_fails(
    tmp_manifest_dir: Path, tmp_path: Path
):
    """A pattern that resolves but the file is below the byte floor fails."""
    (tmp_path / "tiny.txt").write_bytes(b"x" * 10)
    orch = _make_orchestrator_with_fake(
        lambda name: _FakeSubAgent(name, output="ok")
    )
    orch.working_dir = str(tmp_path)
    result = orch.spawn(
        "explore", "find a thing",
        produces_uris=["./tiny.txt"],
    )
    assert result.success is False
    assert "256 bytes" in result.error
    assert "./tiny.txt" in result.error


def test_sync_subagent_failure_unchanged_by_gate(
    tmp_manifest_dir: Path, tmp_path: Path
):
    """If the subagent itself failed, the gate doesn't run — the original
    failure is what the parent sees."""
    orch = _make_orchestrator_with_fake(
        lambda name: _FakeSubAgent(
            name, success=False, output="", error="real failure"
        )
    )
    orch.working_dir = str(tmp_path)
    result = orch.spawn(
        "explore", "find a thing",
        produces_uris=["./missing.txt"],
    )
    assert result.success is False
    assert result.error == "real failure"
    assert "produces_uris" not in (result.error or "")


def test_sync_glob_pattern_matches(tmp_manifest_dir: Path, tmp_path: Path):
    """Glob patterns resolve via glob.glob recursively."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "fig_a.png").write_bytes(b"x" * 1000)
    orch = _make_orchestrator_with_fake(
        lambda name: _FakeSubAgent(name, output="ok")
    )
    orch.working_dir = str(tmp_path)
    result = orch.spawn(
        "explore", "find a thing",
        produces_uris=["./sub/*.png"],
    )
    assert result.success is True


# ---- background path -----------------------------------------------------


def test_background_local_pattern_missing_lands_in_blocked_state(
    tmp_manifest_dir: Path, tmp_path: Path
):
    """A backgrounded subagent that claims success but hasn't written its
    declared artifact lands in terminal state 'blocked_produce_missing'.

    Distinct from 'failed' so a verifier can tell a contract gap apart
    from a real subagent failure (LLM crash, tool error). Both are
    terminal; only the discriminator differs.
    """
    orch = _make_orchestrator_with_fake(
        lambda name: _FakeSubAgent(name, output="ok claimed")
    )
    orch.working_dir = str(tmp_path)
    placeholder = orch.spawn(
        "explore", "find a thing",
        background=True,
        produces_uris=["./never_written.txt"],
    )
    assert placeholder.task_id is not None
    record = _wait_for_terminal(placeholder.task_id)
    assert record["state"] == "blocked_produce_missing", record
    body_result = record["body"]["result"]
    assert body_result["success"] is False
    assert "./never_written.txt" in body_result["error"]
    # Manifest body carries the declared contract so a later reader knows
    # what the gate was asked to check, not just the verdict.
    assert record["body"]["produces_uris"] == ["./never_written.txt"]
    assert record["body"]["produces_min_bytes"] == 256


def test_background_real_subagent_failure_still_lands_in_failed_state(
    tmp_manifest_dir: Path, tmp_path: Path
):
    """A real subagent failure (LLM crash, tool error) with produces_uris
    declared still terminates as 'failed', NOT 'blocked_produce_missing'.

    Guard against the gate widening past its scope — only contract gaps
    should land in the blocked state.
    """
    orch = _make_orchestrator_with_fake(
        lambda name: _FakeSubAgent(
            name, success=False, output="", error="LLM refused"
        )
    )
    orch.working_dir = str(tmp_path)
    placeholder = orch.spawn(
        "explore", "ignored",
        background=True,
        produces_uris=["./missing.txt"],
    )
    record = _wait_for_terminal(placeholder.task_id)
    assert record["state"] == "failed", record
    assert "LLM refused" in record["body"]["result"]["error"]


def test_background_local_pattern_present_lands_in_completed_state(
    tmp_manifest_dir: Path, tmp_path: Path
):
    """When the artifact is present, background terminal state stays
    'completed'."""
    (tmp_path / "out.bin").write_bytes(b"x" * 500)
    orch = _make_orchestrator_with_fake(
        lambda name: _FakeSubAgent(name, output="ok")
    )
    orch.working_dir = str(tmp_path)
    placeholder = orch.spawn(
        "explore", "find a thing",
        background=True,
        produces_uris=["./out.bin"],
    )
    record = _wait_for_terminal(placeholder.task_id)
    assert record["state"] == "completed", record


# ---- cloud-scheme path (mocked subprocess) -------------------------------


def _fake_materialize_result(file_count: int, bytes_each: int = 1024) -> ToolResult:
    """Synthesize a list_only=True ToolResult shape that MaterializeTool
    would emit for s3/gs/r2."""
    return ToolResult(
        success=True,
        output={
            "uri": "s3://bucket/prefix/",
            "scheme": "s3",
            "file_count": file_count,
            "bytes_total": file_count * bytes_each,
            "files": [{"path": f"k{i}", "bytes": bytes_each} for i in range(file_count)],
            "truncated": False,
            "list_only": True,
        },
    )


def test_sync_cloud_pattern_resolves_passes(tmp_manifest_dir: Path, tmp_path: Path):
    """An s3:// pattern that resolves via MaterializeTool list_only passes."""
    orch = _make_orchestrator_with_fake(
        lambda name: _FakeSubAgent(name, output="ok")
    )
    orch.working_dir = str(tmp_path)
    with patch(
        "sciagent.tools.atomic.materialize.MaterializeTool.execute",
        return_value=_fake_materialize_result(file_count=3),
    ):
        result = orch.spawn(
            "explore", "find a thing",
            produces_uris=["s3://bucket/prefix/"],
        )
    assert result.success is True


def test_sync_cloud_pattern_empty_fails(tmp_manifest_dir: Path, tmp_path: Path):
    """An s3:// pattern that lists zero files fails the gate."""
    orch = _make_orchestrator_with_fake(
        lambda name: _FakeSubAgent(name, output="ok")
    )
    orch.working_dir = str(tmp_path)
    with patch(
        "sciagent.tools.atomic.materialize.MaterializeTool.execute",
        return_value=_fake_materialize_result(file_count=0),
    ):
        result = orch.spawn(
            "explore", "find a thing",
            produces_uris=["s3://bucket/prefix/"],
        )
    assert result.success is False
    assert "s3://bucket/prefix/" in result.error


# ---- TaskTool wiring -----------------------------------------------------


def test_task_tool_threads_produces_uris_to_spawn(
    tmp_manifest_dir: Path, tmp_path: Path
):
    """TaskTool.execute must thread produces_uris/produces_min_bytes through
    to orchestrator.spawn so the gate fires from a real task() call shape."""
    orch = _make_orchestrator_with_fake(
        lambda name: _FakeSubAgent(name, output="ok")
    )
    orch.working_dir = str(tmp_path)
    tool = TaskTool(orch)
    r = tool.execute(
        agent_name="explore",
        task="find a thing",
        produces_uris=["./absent.csv"],
    )
    assert r.success is False
    assert "./absent.csv" in (r.error or "")


def test_task_tool_analyze_in_enum():
    """Regression: analyze must be a dispatchable agent_name (was missing
    despite being registered)."""
    enum = TaskTool.parameters["properties"]["agent_name"]["enum"]
    assert "analyze" in enum
