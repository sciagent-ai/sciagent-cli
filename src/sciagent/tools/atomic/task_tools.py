"""Kind-agnostic registry tools.

Two LLM-facing tools that view the in-flight registry across kinds:

  - task_list(kind=, state=, session_id=)   — enumerate registry entries
  - task_get(id)                            — inspect a single entry

These complement the kind-specific bg_status / bg_output / bg_wait / bg_kill
tools rather than replacing them. bg_* still owns the cloud-job runtime
surface (status from sky, log streaming, terminal-state polling); the
task_* tools own the cross-kind registry surface (what's tracked, by whom,
in what lifecycle state).

When non-compute kinds (subagent, watch, scheduled) eventually land, they
appear in task_list / task_get without any tool-shape change — that's the
whole point of the kind discriminator. bg_* will continue to be the
compute-specific runtime surface; task_* will be how the agent (or a human
reading the session) sees "everything in flight."
"""

from __future__ import annotations

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
            from sciagent.compute.task_index import _normalize, get_task

            record = get_task(id)
        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

        if record is None:
            return ToolResult(
                success=False,
                output=None,
                error=f"No task with id {id!r} in the registry.",
            )

        normalized = _normalize(record)
        lines = [
            f"Task: {normalized.get('job_id', id)}",
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

        return ToolResult(success=True, output="\n".join(lines), error=None)

    def to_schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
