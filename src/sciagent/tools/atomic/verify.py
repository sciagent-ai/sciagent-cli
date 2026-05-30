"""
verify_session — read the durable provenance log and produce a structured
verification report (M1B).

Three hard rules carried from M1A apply:

  1. Non-blocking, one-shot. Reads the JSONL once and returns. No wait=,
     until=, or block=True kwarg.
  2. No convenience helper that hides a wait. The whole tool is a snapshot
     read; there is no follow-up polling.
  3. Snapshot, not stream. A second invocation on the same session
     produces a new snapshot of whatever the log looked like at read time.

The report is intended to be consumed by an LLM from any provider — the
schema mirrors docs/provenance_log_schema.md and uses provider-neutral
language and types throughout.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...provenance_log import ProvenanceLog, get_provenance_log


@dataclass
class ToolResult:
    """Result from tool execution. Mirrors the shape used by other
    atomic tools so the registry can dispatch uniformly."""
    success: bool
    output: Any
    error: Optional[str] = None


# Subset of event kinds the report explicitly accounts for. Unknown
# kinds (added in a future schema bump) flow through into events_by_kind
# without breaking the report.
_KNOWN_EVENT_KINDS = {
    "tool_call",
    "tool_result",
    "compute_job_launched",
    "compute_job_status_changed",
    "artifact_produced",
    "verification_result",
    "correction",
}


def verify_session(session_id: str, base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Build a structured verification report for ``session_id``.

    The report is plain JSON — no SDK enums or opaque blobs except inside
    fields the schema permits (intent, expected_artifacts). A different
    LLM provider can read this report and decide whether to trust the
    session's outcome.

    Returns a dict (not a dataclass) so the result is trivially
    serializable — important because verify_session is itself an atomic
    tool and the result rides through the agent's tool-result channel.
    """
    log = get_provenance_log(session_id, base_dir=base_dir)
    events = log.read_events()
    return _build_report(session_id, log.path, events)


def _build_report(
    session_id: str,
    log_path: Path,
    events: List[Dict[str, Any]],
) -> Dict[str, Any]:
    parse_errors = [e for e in events if e.get("_parse_error")]
    real = [e for e in events if not e.get("_parse_error")]

    events_by_kind: Dict[str, int] = {}
    for ev in real:
        kind = ev.get("event_kind", "unknown")
        events_by_kind[kind] = events_by_kind.get(kind, 0) + 1

    tool_calls = [e for e in real if e.get("event_kind") == "tool_call"]
    tool_results = [e for e in real if e.get("event_kind") == "tool_result"]
    result_ids = {e.get("tool_call_id") for e in tool_results}
    unmatched_calls = [
        {"tool_call_id": e.get("tool_call_id"), "tool_name": e.get("tool_name"), "seq": e.get("seq")}
        for e in tool_calls
        if e.get("tool_call_id") not in result_ids
    ]

    compute_jobs = _summarize_compute_jobs(real)
    artifacts = _summarize_artifacts(real)
    verifications = _summarize_verifications(real)
    corrections = _summarize_corrections(real)

    summary_issues = _derive_summary_issues(
        unmatched_calls=unmatched_calls,
        compute_jobs=compute_jobs,
        verifications=verifications,
        parse_errors=parse_errors,
    )

    return {
        "schema_version": "1",
        "session_id": session_id,
        "log_path": str(log_path),
        "events_total": len(real),
        "events_by_kind": events_by_kind,
        "parse_errors": len(parse_errors),
        "tool_calls": {
            "total": len(tool_calls),
            "results_total": len(tool_results),
            "unmatched": unmatched_calls,
        },
        "compute_jobs": compute_jobs,
        "artifacts": artifacts,
        "verifications": verifications,
        "corrections": corrections,
        "summary_issues": summary_issues,
    }


def _summarize_compute_jobs(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One entry per launched job, joining its status transitions and
    artifacts. Jobs that have status transitions but no launched event
    (e.g. a session that resumed mid-run) appear with launched=null so
    a verifier sees them rather than silently dropping them."""
    jobs: Dict[str, Dict[str, Any]] = {}

    for ev in events:
        kind = ev.get("event_kind")
        job_id = ev.get("job_id")
        if not job_id:
            continue
        entry = jobs.setdefault(job_id, {
            "job_id": job_id,
            "launched": None,
            "status_transitions": [],
            "current_status": None,
            "artifacts": [],
        })
        if kind == "compute_job_launched":
            entry["launched"] = {
                "managed_job_id": ev.get("managed_job_id"),
                "backend": ev.get("backend"),
                "service": ev.get("service"),
                "image": ev.get("image"),
                "command_original": ev.get("command_original"),
                "command_resolved": ev.get("command_resolved"),
                "mount_path": ev.get("mount_path"),
                "mount_bucket": ev.get("mount_bucket"),
                "requirements": ev.get("requirements"),
                "intent": ev.get("intent"),
                "expected_artifacts": ev.get("expected_artifacts"),
                "ts": ev.get("ts"),
                "seq": ev.get("seq"),
            }
        elif kind == "compute_job_status_changed":
            # cost_usd is schema-v2 (H3): None on v1 logs, populated on v2
            # transitions where the backend reported realized cloud cost.
            # ev.get returns None when the field is absent — v1 logs pass
            # through cleanly with no shape change for downstream readers.
            entry["status_transitions"].append({
                "status": ev.get("status"),
                "status_previous": ev.get("status_previous"),
                "sky_status_raw": ev.get("sky_status_raw"),
                "error_preview": ev.get("error_preview"),
                "log_file": ev.get("log_file"),
                "cost_usd": ev.get("cost_usd"),
                "ts": ev.get("ts"),
                "seq": ev.get("seq"),
            })
            entry["current_status"] = ev.get("status")
        elif kind == "artifact_produced":
            entry["artifacts"].append({
                "path": ev.get("path"),
                "mount_path": ev.get("mount_path"),
                "path_relative_to_mount": ev.get("path_relative_to_mount"),
                "size_bytes": ev.get("size_bytes"),
                "content_type": ev.get("content_type"),
                "seq": ev.get("seq"),
            })

    return list(jobs.values())


def _summarize_artifacts(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """All artifact_produced events, including ones not tied to a job."""
    return [
        {
            "path": e.get("path"),
            "mount_path": e.get("mount_path"),
            "path_relative_to_mount": e.get("path_relative_to_mount"),
            "job_id": e.get("job_id"),
            "size_bytes": e.get("size_bytes"),
            "sha256": e.get("sha256"),
            "content_type": e.get("content_type"),
            "metadata": e.get("metadata"),
            "ts": e.get("ts"),
            "seq": e.get("seq"),
        }
        for e in events
        if e.get("event_kind") == "artifact_produced"
    ]


def _summarize_verifications(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Group verification_result events by gate; surface counts + entries."""
    by_gate: Dict[str, List[Dict[str, Any]]] = {"data": [], "exec": [], "llm": []}
    for ev in events:
        if ev.get("event_kind") != "verification_result":
            continue
        gate = ev.get("gate")
        bucket = by_gate.setdefault(gate or "unknown", [])
        bucket.append({
            "task_id": ev.get("task_id"),
            "claim": ev.get("claim"),
            "verdict": ev.get("verdict"),
            "confidence": ev.get("confidence"),
            "issues": ev.get("issues"),
            "verifier": ev.get("verifier"),
            "ts": ev.get("ts"),
            "seq": ev.get("seq"),
        })

    summary = {}
    for gate, entries in by_gate.items():
        verdicts: Dict[str, int] = {}
        for entry in entries:
            v = entry.get("verdict") or "unknown"
            verdicts[v] = verdicts.get(v, 0) + 1
        summary[gate] = {
            "total": len(entries),
            "verdicts": verdicts,
            "entries": entries,
        }
    return summary


def _summarize_corrections(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "corrects_event_id": e.get("corrects_event_id"),
            "reason": e.get("reason"),
            "replacement": e.get("replacement"),
            "ts": e.get("ts"),
            "seq": e.get("seq"),
        }
        for e in events
        if e.get("event_kind") == "correction"
    ]


def _derive_summary_issues(
    *,
    unmatched_calls: List[Dict[str, Any]],
    compute_jobs: List[Dict[str, Any]],
    verifications: Dict[str, Any],
    parse_errors: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Surface things a verifier should flag without re-walking the whole
    report. Each issue is a structured record so the consumer LLM can
    react to specific categories."""
    issues: List[Dict[str, Any]] = []

    if unmatched_calls:
        issues.append({
            "category": "unmatched_tool_calls",
            "severity": "warning",
            "count": len(unmatched_calls),
            "message": (
                f"{len(unmatched_calls)} tool_call event(s) have no matching "
                "tool_result. The session may have been interrupted mid-call."
            ),
        })

    failed_jobs = [
        j for j in compute_jobs if j.get("current_status") == "failed"
    ]
    if failed_jobs:
        issues.append({
            "category": "failed_compute_jobs",
            "severity": "error",
            "count": len(failed_jobs),
            "job_ids": [j["job_id"] for j in failed_jobs],
            "message": f"{len(failed_jobs)} compute job(s) ended in status 'failed'.",
        })

    cancelled_jobs = [
        j for j in compute_jobs if j.get("current_status") == "cancelled"
    ]
    if cancelled_jobs:
        issues.append({
            "category": "cancelled_compute_jobs",
            "severity": "warning",
            "count": len(cancelled_jobs),
            "job_ids": [j["job_id"] for j in cancelled_jobs],
            "message": f"{len(cancelled_jobs)} compute job(s) were cancelled.",
        })

    refuted = []
    for gate_name, gate_data in verifications.items():
        n_refuted = (gate_data or {}).get("verdicts", {}).get("refuted", 0)
        if n_refuted:
            refuted.append((gate_name, n_refuted))
    if refuted:
        issues.append({
            "category": "refuted_verifications",
            "severity": "error",
            "details": [{"gate": g, "count": n} for g, n in refuted],
            "message": (
                "Earlier verification gate(s) returned 'refuted' on at least one claim. "
                "A verifier should consult the corresponding entries before trusting the session outcome."
            ),
        })

    if parse_errors:
        issues.append({
            "category": "log_parse_errors",
            "severity": "warning",
            "count": len(parse_errors),
            "message": (
                f"{len(parse_errors)} log line(s) could not be parsed as JSON. "
                "Other events are still trustworthy."
            ),
        })

    return issues


# NOTE — `VerifySessionTool` was retired 2026-05-29.
#
# The class was defined here but never registered in `create_default_registry`
# (grep confirms no `register(VerifySessionTool())` call exists), so the agent
# never had it in its toolset. It was forensic/replay plumbing wrapped as a
# tool, but for live audit-grade verification the right mechanism is the
# fresh-context `verifier` subagent kind invoked by
# `TaskOrchestrator._run_llm_verification_gate` (orchestrator.py:365) — that's
# the only verifier that runs adversarially with a fresh context.
#
# The pure function `verify_session(session_id, base_dir)` above is preserved
# because it's a useful read helper. H2's planned `sciagent verify <log>` CLI
# subcommand should NOT call it as a tool from inside an agent loop; instead
# H2 should invoke the same gate code (and therefore the same `verifier`
# subagent) that single-task runs will trigger after the §5.4.b prereq lands.
# This keeps "live verification" and "replay verification" using one
# mechanism, which is what audit-grade requires.
