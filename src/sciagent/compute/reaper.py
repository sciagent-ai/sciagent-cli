"""Driver-side reaper for the on-VM ``timeout`` wrapper (B6).

The on-VM wrapper kills the user command when the runtime exceeds
``timeout_sec``, but does nothing about a hung VM, a controller in a stuck
provisioning state, or a process that the kernel never delivered SIGTERM to.
v4 §7 OQ2 calls for *both* mechanisms: the on-VM wrapper for the common case
plus a driver-side reaper for the edge cases the wrapper can't cover.

The reaper is intentionally simple in M0: scan manifests, find ones whose
``started_at + timeout_sec`` is in the past, and terminate the cluster.
Manifest deletion is left to the orphan sweep / explicit cleanup so a
reaped record can still be inspected post-hoc.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .task_index import list_tasks, update_task_state


def _parse_started_at(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp from the manifest. Returns None on garbage.

    Accepts both naive and aware timestamps. Naive timestamps are treated as
    UTC (the writer uses ``datetime.now(timezone.utc).isoformat()``).
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_overdue(record: Dict[str, Any], now: datetime) -> bool:
    timeout_sec = record.get("timeout_sec") or 0
    try:
        timeout_sec = int(timeout_sec)
    except (TypeError, ValueError):
        return False
    if timeout_sec <= 0:
        return False
    started = _parse_started_at(record.get("started_at"))
    if started is None:
        return False
    return (now - started).total_seconds() > timeout_sec


def reap_overdue(
    cleanup: Callable[[str], bool],
    now: Optional[datetime] = None,
) -> List[str]:
    """Cancel clusters whose runtime exceeded their manifest's ``timeout_sec``.

    Args:
        cleanup: callable accepting a job_id (cluster name) and terminating
            the cluster. Typically ``SkyPilotBackend(...).cleanup`` bound at
            the call site so we don't import the backend here (keeps this
            module test-friendly).
        now: timestamp to compare against; defaults to ``datetime.now(UTC)``.

    Returns:
        List of job_ids that were over budget. The cleanup callable's return
        value is not propagated — the manifest scan is best-effort and any
        sky.down failure is acceptable since sky.down is idempotent.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    reaped: List[str] = []
    # PR1 (consolidation): only reap manifests whose kind is compute_job and
    # whose lifecycle state is still running. Pre-PR1 manifests (no kind /
    # state fields) match both filters via task_index back-compat defaults.
    # Once bg_wait/bg_kill drive state transitions, terminal jobs whose
    # started_at + timeout_sec is in the past will be skipped here — they're
    # already done, the cluster is already gone.
    for record in list_tasks(kind="compute_job", state="running"):
        if not _is_overdue(record, now):
            continue
        job_id = record.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            continue
        try:
            cleanup(job_id)
        except Exception:
            # Best-effort: a single failed cleanup must not stop the scan.
            pass
        # Mark the manifest cancelled so future reads see the truth — the
        # cluster has been (best-effort) torn down, the lifecycle is over.
        # update_task_state is itself best-effort: a write failure here must
        # not stop the sweep, hence no error propagation.
        update_task_state(
            job_id,
            "cancelled",
            result_summary="reaped: timeout exceeded",
        )
        reaped.append(job_id)
    return reaped
