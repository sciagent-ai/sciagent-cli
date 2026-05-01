"""B12 — mocked joined-status test (v4.2 §N2).

Validates the M2A read path early: a job tracked in ``task_index`` plus a
``sky.queue``-derived JobResult must produce one coherent ``bg_status``
output.

All five cases run with mocks: no real Sky calls, no real manifest files
(``read_task`` is patched). Total cost: $0.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sciagent.compute.job import JobResult, JobStatus
from sciagent.compute.task_index import join_status


# ---------------------------------------------------------------------------
# join_status — pure unit tests over the five cases in v4.2 §N2.
# ---------------------------------------------------------------------------


def test_join_rich_intent_plus_running_sky():
    """Case 1 (v4.2 §N2): rich intent (paper/case/run) — joined output
    preserves all manifest fields and surfaces the cloud-side status."""
    local = {
        "job_id": "sciagent-rich1",
        "session_id": "abc12345",
        "intent": {
            "paper": "Boussinesq2024",
            "case": "typical_c",
            "run": "rep-1",
        },
        "expected_artifacts": [
            "postProcessing/probes/0/U",
            "log.icoFoam",
        ],
        "owner_pid": 4242,
        "started_at": "2026-04-27T18:32:11Z",
        "command": "bash Allrun",
        "metadata": {"notes": "first repro"},
    }
    sky = JobResult(
        status=JobStatus.RUNNING,
        summary="Job running on sciagent-rich1",
    )

    out = join_status(job_id="sciagent-rich1", local=local, sky_result=sky)

    assert out["job_id"] == "sciagent-rich1"
    assert out["backend"] == "skypilot"
    assert out["status"] == "running"
    assert out["summary"] == "Job running on sciagent-rich1"
    assert out["intent"] == local["intent"]
    assert out["expected_artifacts"] == local["expected_artifacts"]
    assert out["owner_pid"] == 4242
    assert out["started_at"] == "2026-04-27T18:32:11Z"
    assert out["session_id"] == "abc12345"
    assert out["command"] == "bash Allrun"
    assert out["metadata"] == {"notes": "first repro"}


def test_join_minimal_intent_does_not_synthesize_keys():
    """Case 2 (v4.2 §N2): minimal intent (just command/image) — joined
    output preserves what's there; never synthesises missing keys."""
    local = {
        "job_id": "sciagent-min1",
        "session_id": "ses-min",
        "intent": {"command": "python -c 'print(1+1)'", "image": "python:3.11"},
        "owner_pid": 99,
        # NOTE: no expected_artifacts, no started_at, no metadata.
    }
    sky = JobResult(status=JobStatus.RUNNING, summary="setting_up")

    out = join_status(job_id="sciagent-min1", local=local, sky_result=sky)

    assert out["intent"] == {"command": "python -c 'print(1+1)'", "image": "python:3.11"}
    assert out["owner_pid"] == 99
    assert out["session_id"] == "ses-min"
    # Keys that aren't in the manifest must NOT appear in the joined output.
    assert "expected_artifacts" not in out
    assert "started_at" not in out
    assert "metadata" not in out


@pytest.mark.parametrize("intent_value", [{}, None])
def test_join_empty_or_none_intent_does_not_crash(intent_value):
    """Case 3 (v4.2 §N2): empty `{}` or `None` intent — no crash; the field
    is reported as-is (empty/None) rather than fabricated."""
    local = {
        "job_id": "sciagent-empty",
        "intent": intent_value,
        "expected_artifacts": [],
    }
    sky = JobResult(status=JobStatus.COMPLETED, summary="done")

    out = join_status(job_id="sciagent-empty", local=local, sky_result=sky)

    assert out["status"] == "completed"
    # `intent` is passed through verbatim; an empty dict / None is honest.
    assert out["intent"] == intent_value
    # Empty artifacts list is preserved, not dropped.
    assert out["expected_artifacts"] == []


def test_join_no_local_manifest_falls_back_to_sky_only():
    """Case 4 (v4.2 §N2): no local manifest — joined output is the sky-only
    view (the legacy path for jobs launched before B7's writer existed)."""
    sky = JobResult(
        status=JobStatus.COMPLETED,
        summary="Job completed successfully on sciagent-legacy",
        output_file="_logs/sciagent-legacy.log",
    )

    out = join_status(job_id="sciagent-legacy", local=None, sky_result=sky)

    assert out["job_id"] == "sciagent-legacy"
    assert out["status"] == "completed"
    assert out["summary"] == "Job completed successfully on sciagent-legacy"
    assert out["output_file"] == "_logs/sciagent-legacy.log"
    # No manifest fields appear.
    for missing in ("intent", "expected_artifacts", "owner_pid", "session_id"):
        assert missing not in out


def test_join_local_present_sky_query_raises_yields_pending():
    """Case 5 (v4.2 §N2): manifest present, sky query failed entirely (raised
    rather than returning a JobResult). Output reports local intent + a
    transient PENDING — same recovery shape PR #1's B1 fix established."""
    local = {
        "job_id": "sciagent-flaky",
        "session_id": "ses-flaky",
        "intent": {"paper": "X", "case": "y", "run": "1"},
        "expected_artifacts": ["out.txt"],
        "owner_pid": 1234,
    }

    out = join_status(job_id="sciagent-flaky", local=local, sky_result=None)

    assert out["status"] == "pending"
    assert "querying" in out["summary"].lower()
    assert out["intent"] == local["intent"]
    assert out["expected_artifacts"] == ["out.txt"]
    assert out["owner_pid"] == 1234


def test_join_passes_through_managed_job_id():
    """M1A: managed_job_id (the integer Sky assigns) must flow from the
    manifest verbatim into the joined dict. None values are passed through
    so callers can distinguish "not yet captured" from "absent field."""
    local = {
        "job_id": "sciagent-mid1",
        "managed_job_id": 4242,
        "intent": None,
    }
    sky = JobResult(status=JobStatus.RUNNING, summary="running")

    out = join_status(job_id="sciagent-mid1", local=local, sky_result=sky)

    assert out["managed_job_id"] == 4242


def test_join_omits_managed_job_id_when_manifest_lacks_it():
    """M0-era manifests don't carry managed_job_id; the joined dict must
    not synthesize one (passthrough contract)."""
    local = {"job_id": "sciagent-old", "intent": None}
    sky = JobResult(status=JobStatus.RUNNING, summary="running")

    out = join_status(job_id="sciagent-old", local=local, sky_result=sky)

    assert "managed_job_id" not in out


def test_join_sky_error_preview_propagates():
    """A failed sky_result with an error_preview must propagate that into
    the joined dict — debug paths rely on it."""
    sky = JobResult(
        status=JobStatus.FAILED,
        summary="Job failed on sciagent-bad",
        error_preview="Error: no matching manifest for linux/amd64",
        output_file="_logs/sciagent-bad.log",
    )

    out = join_status(job_id="sciagent-bad", local=None, sky_result=sky)

    assert out["status"] == "failed"
    assert out["error_preview"].startswith("Error: no matching manifest")
    assert out["output_file"] == "_logs/sciagent-bad.log"


# ---------------------------------------------------------------------------
# BgStatusTool integration — verify the tool actually consults task_index
# and feeds the joined dict to the formatter. Mocked router + read_task,
# no real Sky and no real manifest files.
# ---------------------------------------------------------------------------


def test_bg_status_tool_uses_join_for_compute_jobs():
    """BgStatusTool._get_compute_status must read the local manifest, join it
    with the router's status, and surface manifest fields in the formatted
    output. Catches regressions where the tool reverts to the sky-only path."""
    from sciagent.tools.atomic.bg_tools import BgStatusTool

    fake_local = {
        "job_id": "sciagent-int1",
        "session_id": "ses-int",
        "intent": {"paper": "P", "case": "C", "run": "R"},
        "expected_artifacts": ["out.dat"],
        "owner_pid": 7777,
        "started_at": "2026-04-27T19:00:00Z",
    }
    fake_sky = JobResult(
        status=JobStatus.RUNNING,
        summary="Job running on sciagent-int1",
    )

    tool = BgStatusTool(working_dir="/tmp/work")
    with patch(
        "sciagent.compute.router.ComputeRouter.get_status",
        return_value=fake_sky,
    ), patch(
        "sciagent.compute.task_index.read_task",
        return_value=fake_local,
    ):
        result = tool.execute(job_id="sciagent-int1")

    assert result.success is True
    formatted = result.output
    # Sky-side fields:
    assert "Status: running" in formatted
    assert "Job running on sciagent-int1" in formatted
    # Local-side fields surfaced by the formatter:
    assert "Session: ses-int" in formatted
    assert "Owner PID: 7777" in formatted
    assert "Started: 2026-04-27T19:00:00Z" in formatted
    assert "P" in formatted and "C" in formatted and "R" in formatted
    assert "out.dat" in formatted


def test_bg_status_tool_legacy_no_manifest_still_works():
    """BgStatusTool falls back cleanly to sky-only output when no manifest
    exists for the job (legacy jobs launched before B7)."""
    from sciagent.tools.atomic.bg_tools import BgStatusTool

    fake_sky = JobResult(
        status=JobStatus.COMPLETED,
        summary="Job completed successfully on sciagent-legacy",
    )

    tool = BgStatusTool()
    with patch(
        "sciagent.compute.router.ComputeRouter.get_status",
        return_value=fake_sky,
    ), patch(
        "sciagent.compute.task_index.read_task",
        return_value=None,
    ):
        result = tool.execute(job_id="sciagent-legacy")

    assert result.success is True
    assert "Status: completed" in result.output
    # No manifest → no Session/Owner PID/Intent lines.
    assert "Session:" not in result.output
    assert "Owner PID:" not in result.output
    assert "Intent:" not in result.output


def test_bg_status_surfaces_kind_and_state_in_joined_output():
    """PR1 (consolidation): kind/state from the manifest must surface in the
    bg_status formatted output so the LLM sees lifecycle without a new tool.
    Pre-PR1 manifests (no kind field) fall through to compute_job/running
    via the back-compat defaults in join_status."""
    from sciagent.tools.atomic.bg_tools import BgStatusTool

    fake_local = {
        "job_id": "sciagent-ks1",
        "kind": "compute_job",
        "state": "running",
        "session_id": "ses-ks",
        "owner_pid": 8888,
    }
    fake_sky = JobResult(
        status=JobStatus.RUNNING,
        summary="Job running on sciagent-ks1",
    )

    tool = BgStatusTool()
    with patch(
        "sciagent.compute.router.ComputeRouter.get_status",
        return_value=fake_sky,
    ), patch(
        "sciagent.compute.task_index.read_task",
        return_value=fake_local,
    ):
        result = tool.execute(job_id="sciagent-ks1")

    assert result.success is True
    assert "Kind: compute_job" in result.output
    assert "State: running" in result.output


def test_bg_status_kindless_manifest_surfaces_default_kind_and_state():
    """A pre-PR1 manifest (no kind/state) reads as compute_job/running via
    join_status's back-compat setdefault — bg_status output reflects that."""
    from sciagent.tools.atomic.bg_tools import BgStatusTool

    fake_local = {
        "job_id": "sciagent-old1",
        "session_id": "ses-old",
        "owner_pid": 1234,
        # NOTE: no kind, no state.
    }
    fake_sky = JobResult(
        status=JobStatus.RUNNING,
        summary="still running",
    )

    tool = BgStatusTool()
    with patch(
        "sciagent.compute.router.ComputeRouter.get_status",
        return_value=fake_sky,
    ), patch(
        "sciagent.compute.task_index.read_task",
        return_value=fake_local,
    ):
        result = tool.execute(job_id="sciagent-old1")

    assert result.success is True
    assert "Kind: compute_job" in result.output
    assert "State: running" in result.output


def test_bg_status_routing_respects_kind_over_prefix(tmp_path, monkeypatch):
    """PR2: a manifest with kind=subagent that happens to share the sciagent-
    prefix must NOT be routed to the compute path. The manifest wins via
    task_index.kind_of, regardless of how the id starts.

    This is the load-bearing property that lets future non-compute kinds
    land without breaking the existing routing."""
    from pathlib import Path
    from sciagent.compute import task_index
    from sciagent.tools.atomic.bg_tools import BgStatusTool

    fake_home = Path(tmp_path) / "home" / ".sciagent" / "tasks"
    monkeypatch.setattr(task_index, "manifest_dir", lambda: fake_home)
    fake_home.mkdir(parents=True, exist_ok=True)
    # Plant a kind=subagent manifest with a sciagent-prefixed id.
    import json as _json

    (fake_home / "sciagent-sub1.json").write_text(
        _json.dumps(
            {
                "job_id": "sciagent-sub1",
                "kind": "subagent",
                "state": "running",
            }
        )
    )

    tool = BgStatusTool()
    # If the prefix sniff still ran, this would call into ComputeRouter and
    # the formatter would print "Compute Job:". With kind_of routing, the
    # subagent kind sends the lookup to ProcessManager (which doesn't know
    # this id) and bg_status reports not-found from the local path —
    # specifically NOT a compute_job result.
    result = tool.execute(job_id="sciagent-sub1")
    assert result.success is False
    assert "compute job" not in (result.output or "").lower()


def test_bg_status_tool_missing_both_returns_not_found():
    """Sky get_status raises AND no manifest exists → bg_status reports the
    job as not found rather than fabricating a status."""
    from sciagent.tools.atomic.bg_tools import BgStatusTool

    tool = BgStatusTool()
    with patch(
        "sciagent.compute.router.ComputeRouter.get_status",
        side_effect=RuntimeError("sky offline"),
    ), patch(
        "sciagent.compute.task_index.read_task",
        return_value=None,
    ):
        result = tool.execute(job_id="sciagent-ghost")

    assert result.success is False
    assert "not found" in (result.error or "").lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
