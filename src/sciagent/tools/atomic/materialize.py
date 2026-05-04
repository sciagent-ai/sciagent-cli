"""``materialize`` — fetch a remote artifact (URI or job-id) to a local path.

Why this exists: cluster-mode jobs leave outputs in the persistent
cloud storage mount (``s3://...``, ``gs://...``, etc.). The data tier is
the source of truth; the local filesystem is just one possible
materialization target. Without an agent-callable fetch primitive, the
agent has been observed reaching for raw cloud CLIs (``aws s3 cp ...``)
and inventing bucket paths, which is fragile and provider-specific.

This tool is the cloud-agnostic primitive: pass a URI (or a job_id) and
optional local target; the underlying ``compute_fetch.fetch_workspace_outputs``
helper handles the cloud-specific CLI dispatch.

What this tool does NOT do (yet): direct rsync of an arbitrary path on
a *running* cluster's local filesystem. That's a follow-up; today the
durable layer is the persistent storage mount, and any output the agent
cares about should be there.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from ..registry import BaseTool, ToolResult
from .compute_fetch import (
    _FETCH_DISPATCH,
    _REQUIRED_CLI,
    _split_uri,
    _supported_schemes_message,
    fetch_workspace_outputs,
)


class MaterializeTool(BaseTool):
    name = "materialize"
    description = (
        "Fetch a remote artifact to a local path. The data tier (S3/GCS/"
        "Azure/R2/OCI persistent mount) is sciagent's source of truth for "
        "outputs — use this tool to bring a specific URI or job-id's "
        "outputs onto the local filesystem when you need to read or plot "
        "them. Two call shapes: (a) materialize(job_id=...) to fetch all "
        "outputs of a managed-jobs job (same path bg_wait uses on auto-"
        "fetch); (b) materialize(uri='s3://bucket/path/') to sync any "
        "URI under a cloud the user is configured for. Default target is "
        "a sciagent cache (./_outputs/<...>); pass target=<path> to stage "
        "into the project dir. Cloud CLIs (aws/gsutil/az/oci) must be on "
        "PATH for the chosen scheme. Prefer this over inventing cloud-CLI "
        "commands in bash."
    )

    parameters = {
        "type": "object",
        "properties": {
            "uri": {
                "type": "string",
                "description": (
                    "Cloud URI to fetch (e.g. 's3://bucket/case/10000/T'). "
                    "Either uri OR job_id is required. URI scheme picks "
                    "the cloud CLI: s3, gs, az, r2, oci."
                ),
            },
            "job_id": {
                "type": "string",
                "description": (
                    "Job id (managed-jobs) to fetch outputs for. Looks up "
                    "the job's outputs_uri from the local task index and "
                    "dispatches the right cloud CLI. Either uri OR job_id "
                    "is required."
                ),
            },
            "target": {
                "type": "string",
                "description": (
                    "Local target directory. Default: project-relative "
                    "_outputs/<...>/. Created if missing. Pass an absolute "
                    "path to stage somewhere else."
                ),
            },
            "list_only": {
                "type": "boolean",
                "description": (
                    "If true, list the URI's contents (a manifest of "
                    "{path, size}) without downloading. The data tier is "
                    "sciagent's source of truth — `list_only` lets you "
                    "inspect what a job produced before deciding what to "
                    "materialize locally. URI form only."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": (
                    "Maximum seconds to wait for the cloud-CLI sync. "
                    "Default 300."
                ),
            },
        },
    }

    def __init__(self, working_dir: str = "."):
        self._working_dir = working_dir

    def execute(
        self,
        uri: str = "",
        job_id: str = "",
        target: Optional[str] = None,
        list_only: bool = False,
        timeout: int = 300,
        **kwargs,
    ) -> ToolResult:
        if not uri and not job_id:
            return ToolResult(
                success=False,
                output=None,
                error="Either uri or job_id is required.",
            )
        if uri and job_id:
            return ToolResult(
                success=False,
                output=None,
                error="Pass uri OR job_id, not both.",
            )

        if list_only:
            if not uri:
                return ToolResult(
                    success=False,
                    output=None,
                    error="list_only requires uri (no list-only for job_id yet).",
                )
            return self._do_list(uri, timeout=timeout)

        if job_id:
            try:
                result = fetch_workspace_outputs(
                    job_id=job_id,
                    working_dir=self._working_dir,
                    dest=target,
                    timeout=timeout,
                )
            except Exception as exc:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"materialize(job_id={job_id!r}) failed: {exc}",
                )
            ok = bool(result.get("ok"))
            return ToolResult(
                success=ok,
                output=result if ok else None,
                error=None if ok else result.get("reason") or "fetch failed",
            )

        # URI path. Validate scheme + CLI availability, then dispatch.
        parsed = urlparse(uri)
        scheme = parsed.scheme
        if scheme not in _FETCH_DISPATCH:
            return ToolResult(
                success=False,
                output=None,
                error=(
                    f"Unsupported scheme {scheme!r} in uri={uri!r}; "
                    + _supported_schemes_message()
                ),
            )

        required_cli = _REQUIRED_CLI[scheme]
        if shutil.which(required_cli) is None:
            return ToolResult(
                success=False,
                output=None,
                error=(
                    f"{required_cli} CLI not found on PATH — install it or "
                    f"pull manually: {required_cli} <download> {uri}"
                ),
            )

        _, bucket, prefix = _split_uri(uri)
        dest_root = Path(target) if target else Path(self._working_dir)
        if not dest_root.is_absolute():
            dest_root = Path(self._working_dir) / dest_root
        # If caller passed a target, treat that as the literal local
        # target (no nested _outputs/<...>). If no target, use the
        # project-relative cache convention from compute_fetch.
        if target is None:
            prefix_clean = prefix.strip("/")
            if prefix_clean:
                dest_root = dest_root / "_outputs" / prefix_clean
            else:
                dest_root = dest_root / "_outputs"
        dest_root.mkdir(parents=True, exist_ok=True)

        cmd_builder = _FETCH_DISPATCH[scheme]
        cmd = cmd_builder(uri, str(dest_root))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                output=None,
                error=f"{required_cli} sync timed out after {timeout}s",
            )
        except OSError as exc:
            return ToolResult(
                success=False,
                output=None,
                error=f"failed to invoke {required_cli} CLI: {exc}",
            )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            return ToolResult(
                success=False,
                output={"uri": uri, "scheme": scheme, "bucket": bucket},
                error=(
                    f"{required_cli} sync exit {result.returncode}: "
                    f"{stderr[:400]}"
                ),
            )

        files: list = []
        bytes_total = 0
        if dest_root.exists():
            for p in sorted(dest_root.rglob("*")):
                if p.is_file():
                    size = p.stat().st_size
                    files.append({"path": str(p), "bytes": size})
                    bytes_total += size

        return ToolResult(
            success=True,
            output={
                "uri": uri,
                "scheme": scheme,
                "bucket": bucket,
                "prefix": prefix,
                "dest": str(dest_root),
                "files": files[:200],
                "file_count": len(files),
                "bytes_total": bytes_total,
            },
        )


    # Cloud-CLI list dispatch. Each returns argv that prints one line per
    # object — the parser below handles the per-cloud line shapes.
    _LIST_CMDS = {
        "s3": lambda u: ["aws", "s3", "ls", "--recursive", u],
        "gs": lambda u: ["gsutil", "ls", "-r", u],
        "r2": lambda u: ["aws", "s3", "ls", "--recursive",
                          f"s3://{_split_uri(u)[1]}/{_split_uri(u)[2]}"],
    }

    def _do_list(self, uri: str, *, timeout: int) -> ToolResult:
        """List objects under a URI without downloading them. The data tier
        is the source of truth; this is the cheap "what's here?" probe.
        """
        parsed = urlparse(uri)
        scheme = parsed.scheme
        if scheme not in self._LIST_CMDS:
            return ToolResult(
                success=False,
                output=None,
                error=(
                    f"list_only not implemented for scheme {scheme!r}. "
                    f"Supported: {', '.join(sorted(self._LIST_CMDS))}."
                ),
            )
        required_cli = _REQUIRED_CLI[scheme]
        if shutil.which(required_cli) is None:
            return ToolResult(
                success=False,
                output=None,
                error=f"{required_cli} CLI not on PATH.",
            )

        cmd = self._LIST_CMDS[scheme](uri)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                output=None,
                error=f"{required_cli} ls timed out after {timeout}s",
            )
        except OSError as exc:
            return ToolResult(
                success=False,
                output=None,
                error=f"failed to invoke {required_cli}: {exc}",
            )
        if result.returncode != 0:
            return ToolResult(
                success=False,
                output={"uri": uri},
                error=(
                    f"{required_cli} ls exit {result.returncode}: "
                    f"{(result.stderr or '').strip()[:300]}"
                ),
            )

        files: list = []
        bytes_total = 0
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            # AWS CLI: "DATE TIME  SIZE  KEY"
            # gsutil:  "gs://bucket/path"
            if scheme in ("s3", "r2"):
                parts = line.split()
                if len(parts) >= 4 and parts[2].isdigit():
                    size = int(parts[2])
                    key = " ".join(parts[3:])
                    files.append({"path": key, "bytes": size})
                    bytes_total += size
            else:
                if line.endswith("/") or line.endswith(":"):
                    continue
                files.append({"path": line, "bytes": None})

        return ToolResult(
            success=True,
            output={
                "uri": uri,
                "scheme": scheme,
                "file_count": len(files),
                "bytes_total": bytes_total,
                "files": files[:500],
                "truncated": len(files) > 500,
                "list_only": True,
            },
        )


__all__ = ["MaterializeTool"]
