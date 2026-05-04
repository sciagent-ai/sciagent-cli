"""monitor + monitor_stop atomic tools — push-style stdout-line events.

Thin wrappers over ``MonitorRegistry``. The agent calls ``monitor`` to
spawn a background subprocess; each stdout line becomes a
``<system-reminder>`` event injected on the next turn (drain hook is
in ``agent.py``'s main loop).

Pairs naturally with the wait_until tools (Phase 1):
  - wait_until_up / wait_for_job / bg_wait(block=True) — block on
    ONE thing inside one tool call.
  - monitor — react to whichever happens first across MANY things,
    or surface intermediate state without blocking.

The agent's pipeline does the filtering. Examples:

  monitor("sky api logs <rid> 2>&1 | grep --line-buffered -E 'FAIL|SUCC'", "sky job")
  monitor("tail -f log.solver | grep --line-buffered 'Time = '", "solver")
  monitor("pytest -v 2>&1 | grep --line-buffered -E 'PASS|FAIL'", "tests")
"""

from __future__ import annotations

from typing import Any, Optional

from ..registry import BaseTool, ToolResult


class MonitorTool(BaseTool):
    name = "monitor"
    description = (
        "Spawn a background subprocess; each stdout line becomes a "
        "<system-reminder> event injected into your next turn — push-"
        "style notifications without burning an LLM turn per check. "
        "Returns immediately with a watcher_id. Use to watch any long-"
        "running observable: sky job state, solver heartbeats, test "
        "progress, custom polls. The harness emits every stdout line; "
        "the agent's pipeline does the filtering — pipe through "
        "`grep --line-buffered` (or awk/jq) so only meaningful lines "
        "hit stdout. To stop a watcher, call monitor_stop(watcher_id). "
        "Hard cap: 20 active watchers per agent process."
    )

    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "Shell command to run. Use --line-buffered (grep), "
                    "stdbuf, or unbuffered tools so output streams "
                    "instead of block-buffering. Pipe through grep/awk "
                    "to filter to only meaningful lines."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "Short label shown in the system-reminder so you "
                    "can tell watchers apart (e.g., 'sky job status', "
                    "'solver heartbeat')."
                ),
            },
            "timeout_ms": {
                "type": "integer",
                "description": (
                    "Advisory soft timeout (ms) for the watcher. Watcher "
                    "currently runs until the subprocess exits or "
                    "monitor_stop is called; future revisions may "
                    "enforce. Default 300000 (5 min)."
                ),
                "default": 300_000,
            },
            "persistent": {
                "type": "boolean",
                "description": (
                    "Reserved for future use (cross-restart watcher "
                    "persistence). Today watchers always die with the "
                    "agent process. Default false."
                ),
                "default": False,
            },
        },
        "required": ["command"],
    }

    # Common alias kwarg names the model reaches for.
    _COMMAND_ALIASES = ("command", "cmd", "shell")
    _DESCRIPTION_ALIASES = ("description", "desc", "label", "name")

    def execute(
        self,
        command: str = "",
        description: str = "",
        timeout_ms: int = 300_000,
        persistent: bool = False,
        **kwargs,
    ) -> ToolResult:
        if not command:
            for alias in self._COMMAND_ALIASES[1:]:
                value = kwargs.get(alias)
                if isinstance(value, str) and value.strip():
                    command = value
                    break
        if not description:
            for alias in self._DESCRIPTION_ALIASES[1:]:
                value = kwargs.get(alias)
                if isinstance(value, str) and value.strip():
                    description = value
                    break

        if not command:
            return ToolResult(
                success=False,
                output=None,
                error=(
                    "command is required. Pass a shell command whose "
                    "stdout lines you want as system-reminder events "
                    "on subsequent turns."
                ),
            )
        if not description:
            description = "(unlabeled)"

        from sciagent.monitoring import MonitorRegistry

        try:
            watcher_id = MonitorRegistry.instance().spawn(
                command=command,
                description=description,
                timeout_ms=int(timeout_ms),
                persistent=bool(persistent),
            )
        except RuntimeError as exc:
            return ToolResult(
                success=False,
                output={"failure_type": "watcher_cap_reached"},
                error=str(exc),
            )

        return ToolResult(
            success=True,
            output={
                "watcher_id": watcher_id,
                "command": command,
                "description": description,
                "message": (
                    f"Watcher {watcher_id} started. Each stdout line "
                    f"will arrive as a system-reminder on a subsequent "
                    f"turn. Stop it via monitor_stop(watcher_id="
                    f"'{watcher_id}')."
                ),
            },
        )


class MonitorStopTool(BaseTool):
    name = "monitor_stop"
    description = (
        "Stop a watcher started by monitor(). Sends SIGTERM, then "
        "SIGKILL after 2s if the subprocess is still alive. Returns "
        "the exit code on success. Idempotent: stopping an already-"
        "exited watcher returns the cached exit code; stopping an "
        "unknown watcher_id returns stopped=false."
    )

    parameters = {
        "type": "object",
        "properties": {
            "watcher_id": {
                "type": "string",
                "description": "The watcher_id returned by monitor(...).",
            },
        },
        "required": ["watcher_id"],
    }

    _ALIASES = ("watcher_id", "id", "watcher")

    def execute(self, watcher_id: str = "", **kwargs) -> ToolResult:
        if not watcher_id:
            for alias in self._ALIASES[1:]:
                value = kwargs.get(alias)
                if isinstance(value, str) and value.strip():
                    watcher_id = value
                    break

        if not watcher_id:
            return ToolResult(
                success=False,
                output=None,
                error="watcher_id is required.",
            )

        from sciagent.monitoring import MonitorRegistry

        result = MonitorRegistry.instance().stop(watcher_id)
        return ToolResult(
            success=bool(result.get("stopped")),
            output=result,
            error=(
                None
                if result.get("stopped")
                else f"watcher {watcher_id!r} not found."
            ),
        )
