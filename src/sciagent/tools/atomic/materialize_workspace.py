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


_CLOUD_SCHEME_PREFIXES: tuple = ("s3://", "gs://", "az://", "r2://", "oci://")
# Lazy capture of <sid> in a sciagent-workspace-<sid>[-input-N] bucket URI.
# Lazy `+?` plus the optional `-input-N` peer is what peels the suffix off
# clean (so .../sciagent-workspace-abc-input-0/data/ → "abc" not
# "abc-input-0"). Permissive on the session id itself (any non-slash char
# allowed) because production has seen 8-hex (uuid4().hex[:8]),
# 12-hex (longer slices), and externally-set ids; bucket-name DNS rules
# allow lowercase alnum + hyphen + dot, so we don't pre-restrict here.
_WORKSPACE_BUCKET_RE = r"sciagent-workspace-([^/]+?)(?:-input-\d+)?(?:/|$)"


def _looks_like_uri(value: Optional[str]) -> bool:
    if not value:
        return False
    return any(value.startswith(p) for p in _CLOUD_SCHEME_PREFIXES)


def _extract_session_from_uri(uri: str) -> Optional[str]:
    """Best-effort: pull <sid> out of a sciagent-workspace-<sid>[-input-N] URI.

    Used purely to enrich the result payload (so the agent sees which
    session it just fetched from). Failure returns None — the materialize
    call itself doesn't depend on this."""
    import re

    m = re.search(_WORKSPACE_BUCKET_RE, uri)
    return m.group(1) if m else None


class MaterializeWorkspaceTool(BaseTool):
    name = "materialize_workspace"
    description = (
        "Fetch a durable session workspace (or a slice of one) to local. "
        "The workspace is the persistent cloud bucket auto-mounted at "
        "/workspace/ on every compute_run / compute_exec — your cross-step "
        "durable data tier. Three call shapes:\n"
        "  (a) materialize_workspace() — fetch the CURRENT session's "
        "      workspace. Fails if no compute_run has run yet in this session.\n"
        "  (b) materialize_workspace(session_id='abc12345') — fetch a "
        "      PRIOR session's workspace by id (cross-session resume).\n"
        "  (c) materialize_workspace(uri='s3://sciagent-workspace-<sid>/path/') "
        "      — fetch by full URI when you have one (e.g. from a prior run's "
        "      tool result, the manuscript, or a teammate's hand-off). Most "
        "      flexible; works for any workspace bucket regardless of session.\n"
        "All three syncs to ./_outputs/workspace/ by default; pass dest=<path> "
        "to stage somewhere else, subpath='run-001/fields/' to fetch only "
        "part of the workspace, or list_only=True to inspect contents first."
    )

    parameters = {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": (
                    "Session whose workspace to fetch. Defaults to the "
                    "current session (set when compute_run first fires). "
                    "For cross-session resume — fetching a PRIOR session's "
                    "data — pass the session_id explicitly OR use uri=."
                ),
            },
            "uri": {
                "type": "string",
                "description": (
                    "Full cloud URI of a workspace bucket (or subpath). "
                    "Use this when you have the URI from a prior tool "
                    "result, a hand-off from another session, or a "
                    "manuscript. Wins over session_id and subpath when set. "
                    "Examples: 's3://sciagent-workspace-abc12345/', "
                    "'s3://sciagent-workspace-abc12345/run-001/fields/'."
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
                    "workspace has accumulated many runs. Ignored when "
                    "uri= is set (the URI carries its own subpath)."
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

    @staticmethod
    def _looks_like_no_such_bucket(error: Optional[str]) -> bool:
        if not error:
            return False
        low = error.lower()
        return (
            "nosuchbucket" in low
            or "no such bucket" in low
            or "bucket does not exist" in low
            or "404" in low and "bucket" in low
        )

    def execute(
        self,
        session_id: str = "",
        uri: str = "",
        dest: Optional[str] = None,
        subpath: Optional[str] = None,
        list_only: bool = False,
        timeout: int = 600,
        **kwargs,
    ) -> ToolResult:
        # Three input shapes resolved into a single (uri, sid_for_payload)
        # pair. Precedence: explicit uri > subpath-as-uri > session_id +
        # subpath > current-session + subpath.
        sid_for_payload: Optional[str] = None
        resolved_uri: Optional[str] = None

        if _looks_like_uri(uri):
            resolved_uri = uri
            sid_for_payload = _extract_session_from_uri(uri) or (session_id or None)
        elif _looks_like_uri(subpath):
            # Common LLM mistake: passing the URI as `subpath`. Recover
            # gracefully so the user doesn't lose a turn.
            resolved_uri = subpath
            sid_for_payload = _extract_session_from_uri(subpath) or (session_id or None)
            subpath = None  # don't double-append
        else:
            sid = self._resolve_session_id(session_id or None)
            if not sid:
                return ToolResult(
                    success=False,
                    output=None,
                    error=(
                        "No session_id available and no uri= passed. Either "
                        "(a) pass session_id='<sid>' to fetch a prior session's "
                        "workspace, (b) pass uri='s3://sciagent-workspace-<sid>/...' "
                        "for a full-URI fetch, or (c) run a compute_run / "
                        "compute_exec first so the current session has a "
                        "workspace to fetch."
                    ),
                )
            resolved_uri = self._build_uri(sid, subpath)
            sid_for_payload = sid

        # Default dest layout: project-relative ./_outputs/workspace/[subpath/].
        # Caller's explicit dest= overrides verbatim.
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
            uri=resolved_uri,
            target=target,
            list_only=list_only,
            timeout=timeout,
        )

        # NoSuchBucket on the resolved URI is the dominant first-turn
        # failure: the LLM called materialize_workspace() with no args
        # in a fresh session whose bucket hasn't been created yet, OR
        # the session_id passed doesn't correspond to a real workspace.
        # Rewrite the error so the recovery path is obvious instead of
        # making the agent burn a turn on cloud-CLI semantics.
        if not result.success and self._looks_like_no_such_bucket(result.error):
            recovery = (
                f"No bucket at {resolved_uri}. Likely causes:\n"
                f"  - Current session has no workspace yet (no compute_run "
                f"has fired); run one or pass session_id= / uri= for a "
                f"prior session.\n"
                f"  - The session_id you passed doesn't have a workspace "
                f"on this cloud / account / region.\n"
                f"To list known sciagent workspaces: "
                f"`bash sky storage ls | grep sciagent-workspace`."
            )
            return ToolResult(
                success=False,
                output={
                    "workspace_uri": resolved_uri,
                    "session_id": sid_for_payload,
                    "failure_type": "no_such_bucket",
                },
                error=recovery,
            )

        # Enrich successful payload with the session id and resolved URI so
        # the LLM sees the round-trip cleanly.
        if result.output is not None and isinstance(result.output, dict):
            if sid_for_payload:
                result.output.setdefault("session_id", sid_for_payload)
            result.output.setdefault("workspace_uri", resolved_uri)
        elif result.success:
            result = ToolResult(
                success=True,
                output={
                    "session_id": sid_for_payload,
                    "workspace_uri": resolved_uri,
                    "dest": target,
                },
            )
        return result


__all__ = ["MaterializeWorkspaceTool"]
