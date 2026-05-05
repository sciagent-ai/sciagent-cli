"""produces_uris validation — formal ProvenanceLog emit + manifest body.

Distinct from test_produces_uris_gate.py (which exercises the gate's
verdict and the manifest's terminal state). This file pins:

  - The pass / fail event lands via the formal emit_produces_validation_*
    methods (event_kind matches, payload carries the load-bearing fields).
  - The subagent manifest body persists the declared produces_uris /
    produces_min_bytes for later readers (lineage, post-mortem).

Reuses _FakeSubAgent + tmp_manifest_dir from test_background_spawn.py so
the deterministic-no-LLM pattern stays uniform across the gate tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from sciagent.compute import task_index
from sciagent.provenance_log import (
    ProvenanceLog,
    get_provenance_log,
    reset_provenance_logs,
    set_active_session,
)
from sciagent.subagent import SubAgentOrchestrator
from sciagent.tools.registry import ToolResult

from tests.subagent.test_background_spawn import (  # noqa: F401
    _FakeSubAgent,
    _make_orchestrator_with_fake,
    _wait_for_terminal,
    tmp_manifest_dir,
)


@pytest.fixture(autouse=True)
def _reset_provenance():
    reset_provenance_logs()
    set_active_session(None)
    yield
    reset_provenance_logs()
    set_active_session(None)


@pytest.fixture
def session_log(tmp_path: Path) -> ProvenanceLog:
    log = get_provenance_log("produces-emit-test", base_dir=tmp_path)
    set_active_session("produces-emit-test")
    return log


def _events_of_kind(log: ProvenanceLog, kind: str) -> list[dict]:
    return [
        json.loads(line)
        for line in log.path.read_text().splitlines()
        if line.strip() and json.loads(line).get("event_kind") == kind
    ]


# ---- pass event ----------------------------------------------------------


def test_pass_emits_formal_produces_validation_passed(
    tmp_manifest_dir: Path, tmp_path: Path, session_log: ProvenanceLog
):
    """A successful gate run lands a produces_validation_passed event with
    the resolved files + sizes, via the formal emit_* method."""
    target = tmp_path / "result.bin"
    target.write_bytes(b"x" * 600)

    orch = _make_orchestrator_with_fake(
        lambda name: _FakeSubAgent(name, output="ok")
    )
    orch.working_dir = str(tmp_path)
    result = orch.spawn(
        "explore", "produce result.bin",
        produces_uris=["./result.bin"],
    )
    assert result.success is True

    passed = _events_of_kind(session_log, "produces_validation_passed")
    failed = _events_of_kind(session_log, "produces_validation_failed")
    assert len(passed) == 1
    assert failed == []

    ev = passed[0]
    assert ev["subagent_name"] == "explore"
    assert ev["patterns"] == ["./result.bin"]
    assert ev["verdict"] == "passed"
    assert ev["actor"] == "subagent:explore"
    # Resolved entries name the actual file the gate observed.
    assert len(ev["resolved"]) == 1
    resolved = ev["resolved"][0]
    assert resolved["pattern"] == "./result.bin"
    assert resolved["scheme"] == "file"
    assert resolved["files"][0]["bytes"] == 600


def test_fail_emits_formal_produces_validation_failed(
    tmp_manifest_dir: Path, tmp_path: Path, session_log: ProvenanceLog
):
    """A gate failure lands a produces_validation_failed event with each
    missing pattern + reason, via the formal emit_* method."""
    orch = _make_orchestrator_with_fake(
        lambda name: _FakeSubAgent(name, output="ok claimed")
    )
    orch.working_dir = str(tmp_path)
    result = orch.spawn(
        "explore", "produce a thing",
        produces_uris=["./never_written.txt"],
    )
    assert result.success is False

    passed = _events_of_kind(session_log, "produces_validation_passed")
    failed = _events_of_kind(session_log, "produces_validation_failed")
    assert passed == []
    assert len(failed) == 1

    ev = failed[0]
    assert ev["subagent_name"] == "explore"
    assert ev["patterns"] == ["./never_written.txt"]
    assert ev["verdict"] == "failed"
    assert ev["missing"][0]["pattern"] == "./never_written.txt"
    assert "0 bytes" in ev["missing"][0]["reason"] or "none ≥" in ev["missing"][0]["reason"]


def test_no_event_when_subagent_failed_independently(
    tmp_manifest_dir: Path, tmp_path: Path, session_log: ProvenanceLog
):
    """If the subagent itself failed, no validation event is emitted —
    the gate didn't run, so it has no verdict to record."""
    orch = _make_orchestrator_with_fake(
        lambda name: _FakeSubAgent(
            name, success=False, output="", error="LLM refused"
        )
    )
    orch.working_dir = str(tmp_path)
    orch.spawn(
        "explore", "ignored",
        produces_uris=["./missing.txt"],
    )
    assert _events_of_kind(session_log, "produces_validation_passed") == []
    assert _events_of_kind(session_log, "produces_validation_failed") == []


def test_no_event_when_no_active_session(
    tmp_manifest_dir: Path, tmp_path: Path
):
    """No active session → silently skip emission. The validator still
    runs and the result still reflects the verdict; only the audit
    event is suppressed."""
    set_active_session(None)
    orch = _make_orchestrator_with_fake(
        lambda name: _FakeSubAgent(name, output="ok")
    )
    orch.working_dir = str(tmp_path)
    result = orch.spawn(
        "explore", "ignored",
        produces_uris=["./missing.txt"],
    )
    # Verdict still applied — gate is independent of provenance logging.
    assert result.success is False


# ---- manifest body -------------------------------------------------------


def test_background_manifest_body_carries_produces_contract(
    tmp_manifest_dir: Path, tmp_path: Path
):
    """The subagent manifest body persists the declared produces_uris and
    produces_min_bytes so a later reader (lineage, post-mortem) sees what
    the gate was asked to check."""
    (tmp_path / "out.bin").write_bytes(b"x" * 500)
    orch = _make_orchestrator_with_fake(
        lambda name: _FakeSubAgent(name, output="ok")
    )
    orch.working_dir = str(tmp_path)
    placeholder = orch.spawn(
        "explore", "produce out.bin",
        background=True,
        produces_uris=["./out.bin", "s3://bucket/prefix/"],
        produces_min_bytes=128,
    )
    record = _wait_for_terminal(placeholder.task_id)
    body = record["body"]
    assert body["produces_uris"] == ["./out.bin", "s3://bucket/prefix/"]
    assert body["produces_min_bytes"] == 128


def test_background_manifest_body_default_produces_when_omitted(
    tmp_manifest_dir: Path,
):
    """No produces_uris on dispatch → empty list in the manifest body
    (not missing field). Keeps the body shape stable across read-only
    and artifact-producing tasks."""
    orch = _make_orchestrator_with_fake(
        lambda name: _FakeSubAgent(name, output="ok")
    )
    placeholder = orch.spawn("explore", "read-only task", background=True)
    record = _wait_for_terminal(placeholder.task_id)
    body = record["body"]
    assert body["produces_uris"] == []
    assert body["produces_min_bytes"] == 256
