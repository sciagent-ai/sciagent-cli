"""``materialize_workspace`` — fetch the durable session workspace bucket
to local.

Companion to ``materialize``. The session workspace is a Sky-provisioned
persistent bucket auto-mounted at ``/workspace/`` on every compute_run /
compute_exec in the session. This tool brings it back to local so the
agent can read, plot, or chain results downstream — same shape the
LLM uses for managed-jobs outputs (``materialize(job_id=...)``).

Bucket name and URI are derivable from session_id alone — no manifest
entry, no new state surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..registry import BaseTool, ToolResult
from .materialize import MaterializeTool


class MaterializeWorkspaceTool(BaseTool):
    name = "materialize_workspace"
    description = (
        "Fetch the durable session workspace bucket to local. The workspace "
        "is the persistent cloud bucket auto-mounted at /workspace/ on "
        "every compute_run / compute_exec — your cross-step durable data "
        "tier. This tool sync's it (or a subpath) to ./_outputs/workspace/ "
        "by default. Use this at the end of a multi-step cluster workflow "
        "to bring meshes, fields, derived analyses, etc. back to local. "
        "session_id defaults to the current session; pass dest=<path> to "
        "stage somewhere else, or subpath='run-001/fields/' to fetch only "
        "part of the workspace."
    )

    parameters = {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": (
                    "Session whose workspace to fetch. Defaults to the "
                    "current session (the same one /workspace/ is auto-"
                    "mounted from in this conversation)."
                ),
            },
            "dest": {
                "type": "string",
                "description": (
                    "Local target directory. Default: ./_outputs/workspace/. "
                    "Created if missing. Pass an absolute path to stage "
                    "somewhere else."
                ),
            },
            "subpath": {
                "type": "string",
                "description": (
                    "Optional subpath within the workspace to fetch (e.g. "
                    "'run-001/fields/'). When omitted, syncs the whole "
                    "bucket. Use this to keep transfers small when the "
                    "workspace has accumulated many runs."
                ),
            },
            "list_only": {
                "type": "boolean",
                "description": (
                    "If true, list the workspace contents without "
                    "downloading — your 'what's in the workspace?' probe."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": "Cloud-CLI sync timeout (seconds). Default 600.",
            },
        },
    }

    def __init__(self, working_dir: str = "."):
        self._working_dir = working_dir

    def _resolve_session_id(self, session_id: Optional[str]) -> Optional[str]:
        if session_id:
            return session_id
        # Pick up the agent-wide session id set by ComputeTool at startup.
        try:
            from .compute import ComputeTool
            return ComputeTool._shared_session_id
        except Exception:
            return None

    def _build_uri(self, session_id: str, subpath: Optional[str]) -> str:
        from sciagent.compute.backends.skypilot import (
            SkyPilotBackend,
            _build_workspace_uri as _bld_ws_uri,
        )
        store = SkyPilotBackend().resolve_workspace_store()
        base = _bld_ws_uri(store, session_id)  # ends with '/'
        if not subpath:
            return base
        return base + subpath.lstrip("/")

    def execute(
        self,
        session_id: str = "",
        dest: Optional[str] = None,
        subpath: Optional[str] = None,
        list_only: bool = False,
        timeout: int = 600,
        **kwargs,
    ) -> ToolResult:
        sid = self._resolve_session_id(session_id or None)
        if not sid:
            return ToolResult(
                success=False,
                output=None,
                error=(
                    "No session_id available. Pass session_id= explicitly, "
                    "or run a compute_run / compute_exec first so the agent's "
                    "session is initialized."
                ),
            )

        uri = self._build_uri(sid, subpath)

        # Default dest is project-relative ./_outputs/workspace/, or
        # ./_outputs/workspace/<subpath>/ when a subpath is given. Caller's
        # explicit dest= overrides verbatim.
        if dest is None:
            base = Path(self._working_dir) / "_outputs" / "workspace"
            if subpath:
                target = str(base / subpath.strip("/"))
            else:
                target = str(base)
        else:
            target = dest

        materialize = MaterializeTool(working_dir=self._working_dir)
        result = materialize.execute(
            uri=uri,
            target=target,
            list_only=list_only,
            timeout=timeout,
        )
        # Pass through whatever materialize returned, just enrich the output
        # with the session_id and workspace URI so the agent sees the
        # round-trip clearly.
        if result.output is not None and isinstance(result.output, dict):
            result.output.setdefault("session_id", sid)
            result.output.setdefault("workspace_uri", uri)
        elif result.success:
            result = ToolResult(
                success=True,
                output={
                    "session_id": sid,
                    "workspace_uri": uri,
                    "dest": target,
                },
            )
        return result


__all__ = ["MaterializeWorkspaceTool"]
