"""Kind-agnostic registry tools.

Three LLM-facing tools that view the in-flight registry across kinds:

  - task_list(kind=, state=, session_id=)   — enumerate registry entries
  - task_get(id)                            — inspect a single entry
  - task_wait(id, timeout=, poll_interval=) — block until terminal state

These complement the kind-specific bg_status / bg_output / bg_wait / bg_kill
tools rather than replacing them. bg_* still owns the cloud-job runtime
surface (status from sky, log streaming, compute-side terminal-state
polling). task_* owns the cross-kind registry surface: what's tracked, by
whom, in what lifecycle state, and waiting on lifecycle transitions for
any kind. PR4 added kind="subagent" as the first non-compute consumer of
task_wait; future kinds (watch, scheduled) inherit the same surface
without per-kind branching.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class ToolResult:
    """Result from tool execution."""

    success: bool
    output: Any
    error: Optional[str] = None


def _format_task_summary(record: Dict[str, Any]) -> List[str]:
    """One short block per task — the shape task_list emits per entry.

    Pulls only the cross-kind fields plus a short kind-specific hint so the
    output is scannable when many tasks are listed. Full per-record
    inspection is task_get's job.
    """
    job_id = record.get("job_id", "(unknown)")
    kind = record.get("kind", "compute_job")
    state = record.get("state", "running")
    lines = [f"- {job_id}", f"    kind: {kind}", f"    state: {state}"]
    if record.get("session_id"):
        lines.append(f"    session: {record['session_id']}")
    if record.get("started_at"):
        lines.append(f"    started: {record['started_at']}")
    if record.get("completed_at"):
        lines.append(f"    completed: {record['completed_at']}")
    if record.get("result_summary"):
        lines.append(f"    result: {record['result_summary']}")
    # Kind-specific hint (terse): one line that orients the reader without
    # duplicating task_get's full inspection.
    if kind == "compute_job":
        intent = record.get("intent")
        if intent:
            lines.append(f"    intent: {intent}")
    return lines


class TaskListTool:
    """Enumerate the in-flight registry."""

    name = "task_list"
    description = (
        "List entries in the in-flight task registry. Filters compose: "
        "kind ('compute_job' today; 'subagent' / 'watch' / 'scheduled' as "
        "they land), state ('running' / 'pending' / 'completed' / "
        "'failed' / 'cancelled'), session_id. With no args, lists every "
        "tracked task across kinds and states. For per-job runtime "
        "details on a cloud compute job specifically, use bg_status; "
        "task_list is the cross-kind registry view."
    )

    parameters = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "description": "Filter by kind (e.g. compute_job).",
            },
            "state": {
                "type": "string",
                "description": (
                    "Filter by state: running | pending | completed | "
                    "failed | cancelled."
                ),
            },
            "session_id": {
                "type": "string",
                "description": "Filter by session id.",
            },
        },
        "required": [],
    }

    def execute(
        self,
        kind: Optional[str] = None,
        state: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> ToolResult:
        try:
            from sciagent.compute.task_index import list_tasks

            records = list_tasks(kind=kind, state=state, session_id=session_id)
        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

        if not records:
            filters: List[str] = []
            if kind:
                filters.append(f"kind={kind}")
            if state:
                filters.append(f"state={state}")
            if session_id:
                filters.append(f"session_id={session_id}")
            qualifier = f" matching {', '.join(filters)}" if filters else ""
            return ToolResult(
                success=True,
                output=f"No tasks in the registry{qualifier}.",
                error=None,
            )

        # Sort by started_at descending (most recent first), falling back to
        # job_id when started_at is missing — old manifests don't always
        # have it.
        records = sorted(
            records,
            key=lambda r: (r.get("started_at") or "", r.get("job_id") or ""),
            reverse=True,
        )

        lines = [f"{len(records)} task(s) in registry:"]
        for record in records:
            lines.extend(_format_task_summary(record))
        return ToolResult(success=True, output="\n".join(lines), error=None)

    def to_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


def _format_full_record(record: Dict[str, Any], header: str) -> str:
    """Render a normalized manifest the way task_get / task_wait both do.

    Factored out of TaskGetTool.execute so task_wait's terminal-snapshot
    output looks identical to task_get's — same formatting reduces the
    noise an LLM has to cope with when alternating between the two.
    """
    from sciagent.compute.task_index import _normalize

    normalized = _normalize(record)
    lines = [
        header,
        f"  kind: {normalized['kind']}",
        f"  state: {normalized['state']}",
    ]
    for key, label in (
        ("session_id", "session"),
        ("started_at", "started"),
        ("completed_at", "completed"),
        ("owner_pid", "owner_pid"),
        ("result_summary", "result"),
    ):
        value = normalized.get(key)
        if value:
            lines.append(f"  {label}: {value}")

    body = normalized.get("body") or {}
    if body:
        lines.append("  body:")
        for key, value in body.items():
            if value is None or value == [] or value == {}:
                continue
            lines.append(f"    {key}: {value}")
    return "\n".join(lines)


class TaskGetTool:
    """Inspect a single registry entry."""

    name = "task_get"
    description = (
        "Inspect a single registry entry by id. Returns the full manifest "
        "including kind, state, lifecycle timestamps, session, and "
        "kind-specific body fields. Use this for the registry view; for "
        "the live runtime status of a cloud compute job (sky-side state, "
        "logs URL), use bg_status instead — it joins the manifest with "
        "the controller's view in one call."
    )

    parameters = {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "Task id (e.g. sciagent-abc123).",
            },
        },
        "required": ["id"],
    }

    def execute(self, id: Optional[str] = None) -> ToolResult:
        if not id:
            return ToolResult(
                success=False, output=None, error="id is required."
            )
        try:
            from sciagent.compute.task_index import get_task

            record = get_task(id)
        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

        if record is None:
            return ToolResult(
                success=False,
                output=None,
                error=f"No task with id {id!r} in the registry.",
            )

        header = f"Task: {record.get('job_id', id)}"
        return ToolResult(
            success=True,
            output=_format_full_record(record, header),
            error=None,
        )

    def to_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class TaskWaitTool:
    """Block until a registry task reaches a terminal state.

    Kind-agnostic: works on any task_index entry (compute_job, subagent,
    and any future kind) by polling the manifest's ``state`` field. For
    cloud-compute-specific waits with output streaming and cluster
    teardown, ``bg_wait`` remains the right tool — it joins the manifest
    with sky's controller view in one call. ``task_wait`` is the
    registry-only surface: it observes the file the manifest holds, and
    nothing else.
    """

    name = "task_wait"
    description = (
        "Block until a registry task (id) reaches a terminal state "
        "(completed / failed / cancelled). Works across all kinds — "
        "compute_job, subagent, etc. Returns the final manifest snapshot. "
        "On timeout, returns the still-running snapshot so the caller can "
        "decide whether to wait again or move on. For cloud-compute-"
        "specific waits with log fetching, prefer bg_wait."
    )

    parameters = {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "Task id to wait on.",
            },
            "timeout": {
                "type": "number",
                "description": (
                    "Max seconds to wait before returning the still-running "
                    "snapshot. Default 600."
                ),
            },
            "poll_interval": {
                "type": "number",
                "description": (
                    "Seconds between manifest polls. Default 1.0."
                ),
            },
        },
        "required": ["id"],
    }

    # Kept terse — bg_wait's poll cadence is comparable; faster polling
    # would hammer the disk for no signal benefit (manifest writes are
    # event-driven, not periodic).
    DEFAULT_TIMEOUT = 600.0
    DEFAULT_POLL_INTERVAL = 1.0

    def execute(
        self,
        id: Optional[str] = None,
        timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
    ) -> ToolResult:
        if not id:
            return ToolResult(
                success=False, output=None, error="id is required."
            )
        timeout = float(timeout) if timeout is not None else self.DEFAULT_TIMEOUT
        poll_interval = (
            float(poll_interval)
            if poll_interval is not None
            else self.DEFAULT_POLL_INTERVAL
        )
        if poll_interval <= 0:
            poll_interval = self.DEFAULT_POLL_INTERVAL

        try:
            from sciagent.compute.task_index import (
                TERMINAL_STATES,
                get_task,
            )
        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

        # Resolve the manifest once before sleeping — surfaces a clean
        # "no such task" error instead of waiting through the whole
        # timeout on a typo.
        record = get_task(id)
        if record is None:
            return ToolResult(
                success=False,
                output=None,
                error=f"No task with id {id!r} in the registry.",
            )

        # Honor the BaseTool interrupt event so a user Ctrl+C wakes the
        # poll wait immediately. Without this, task_wait blocks for the
        # full poll_interval before checking again — same trap bg_wait
        # had until it was made interrupt-aware.
        from sciagent.tools.registry import BaseTool
        interrupt_event = BaseTool._shared_interrupt_event

        deadline = time.time() + timeout
        timed_out = False
        while record.get("state", "running") not in TERMINAL_STATES:
            if interrupt_event is not None and interrupt_event.is_set():
                return ToolResult(
                    success=True,
                    output=(
                        f"task_wait on {id!r} interrupted by user. The task "
                        f"is still running — call task_get('{id}') to "
                        f"check, or task_wait again with a fresh budget."
                    ),
                    error=None,
                )
            if time.time() >= deadline:
                timed_out = True
                break
            if interrupt_event is not None:
                if interrupt_event.wait(poll_interval):
                    continue
            else:
                time.sleep(poll_interval)
            record = get_task(id)
            if record is None:
                # Manifest deleted while we were waiting — surface the
                # disappearance instead of looping forever.
                return ToolResult(
                    success=False,
                    output=None,
                    error=(
                        f"Manifest for {id!r} disappeared while waiting "
                        f"(deleted by a concurrent reaper?)."
                    ),
                )

        if timed_out:
            header = (
                f"Task: {record.get('job_id', id)} (still running after "
                f"{timeout:.0f}s — call task_wait again or task_get to "
                f"inspect)"
            )
            # Treat timeout as success=True with the snapshot — same shape
            # as bg_wait's snapshot return when block=False.
            return ToolResult(
                success=True,
                output=_format_full_record(record, header),
                error=None,
            )

        header = f"Task: {record.get('job_id', id)} (terminal)"
        # success of the wait reflects whether the task itself succeeded —
        # callers chain on this.
        ok = record.get("state") == "completed"
        return ToolResult(
            success=ok,
            output=_format_full_record(record, header),
            error=None
            if ok
            else f"Task ended in state {record.get('state')!r}.",
        )

    def to_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
