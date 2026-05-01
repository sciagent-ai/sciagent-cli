"""
Output sync helper for SkyPilot jobs.

Why this exists: sciagent's compute_run wrapper uses SkyPilot managed jobs,
which run on worker nodes the user can't ssh/rsync into. Outputs land in a
per-session workspace bucket (s3://sciagent-workspace-<session_id>/...).
Without an automatic fetch, the agent thrashes — observed in real
transcripts to guess non-existent `sky storage download` commands and
launch extra cloud jobs to `cat` files.

This module is NOT a standalone tool. It's a helper called by `bg_wait`
when a cloud job hits COMPLETED, so the agent gets local file paths back
in the same call that observed completion. Folding the fetch into the
existing wait flow keeps the agent's mental model simple ("I run a job and
get files") and avoids adding another tool call to every workflow.

Hardcoded to AWS S3 for the first iteration. The user-base is AWS-only
and `get_enabled_store()` already returns "s3" as the fallback. Multi-cloud
(GCS, Azure) is a follow-up — when needed, persist the bucket's store
type in the manifest at write time and dispatch here.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional


def fetch_workspace_outputs(
    job_id: str,
    working_dir: str = ".",
    dest: Optional[str] = None,
    prefix: Optional[str] = None,
    timeout: int = 300,
) -> Dict[str, Any]:
    """Sync a SkyPilot job's workspace bucket prefix to a local directory.

    Default ``prefix`` is the job's per-job prefix ``_outputs/<job_id>/`` —
    matches the implicit-mount symlink target (skypilot.py:resolve_command),
    so each job's outputs land in their own isolated subdir both in the
    bucket and locally. Parallel sweeps don't collide. Caller can override
    `prefix` to read another job's outputs (cross-tool sharing pattern:
    Job 2 fetches Job 1's prefix explicitly).

    Returns a dict describing what was fetched (or why it couldn't be).
    Always returns a dict — never raises — so callers (bg_wait) can fold
    the result into their own ToolResult without try/except plumbing.

    Result shape on success::

        {
            "ok": True,
            "bucket": "sciagent-workspace-<session>",
            "prefix": "_outputs/<job_id>/",
            "dest": "/abs/path",
            "files": [{"path": "...", "bytes": N}, ...],
            "file_count": N,
            "bytes_total": N,
        }

    Result shape on skip / failure::

        {"ok": False, "reason": "<one-line>", "bucket": "..." (when known)}
    """
    from sciagent.compute.task_index import read_task

    manifest = read_task(job_id)
    if manifest is None:
        return {"ok": False, "reason": f"no manifest for job_id={job_id!r}"}

    session_id = manifest.get("session_id")
    if not session_id:
        return {
            "ok": False,
            "reason": (
                "job has no session_id — likely launched without a workspace "
                "mount (workspace=False or non-skypilot backend)"
            ),
        }

    bucket = f"sciagent-workspace-{session_id}"

    # Default prefix is the per-job key. Explicit override stays as-passed
    # so cross-tool sharing (Job 2 reads Job 1's prefix) works.
    if prefix is None:
        prefix = f"_outputs/{job_id}/"

    if shutil.which("aws") is None:
        return {
            "ok": False,
            "bucket": bucket,
            "reason": (
                "aws CLI not found on PATH — install AWS CLI v2 or pull "
                f"manually: aws s3 sync s3://{bucket}/{prefix} ./{prefix}"
            ),
        }

    dest_root = Path(dest) if dest else Path(working_dir)
    if not dest_root.is_absolute():
        dest_root = Path(working_dir) / dest_root

    prefix_clean = prefix.strip("/")
    local_target = dest_root / prefix_clean if prefix_clean else dest_root
    local_target.mkdir(parents=True, exist_ok=True)

    s3_uri = f"s3://{bucket}/{prefix}"
    cmd = ["aws", "s3", "sync", s3_uri, str(local_target)]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "bucket": bucket, "reason": f"aws s3 sync timed out after {timeout}s"}
    except OSError as e:
        return {"ok": False, "bucket": bucket, "reason": f"failed to invoke aws CLI: {e}"}

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "NoSuchBucket" in stderr:
            return {
                "ok": False,
                "bucket": bucket,
                "reason": (
                    "bucket does not exist — job likely didn't mount a "
                    "workspace, or finished before the bucket was created"
                ),
            }
        return {
            "ok": False,
            "bucket": bucket,
            "reason": f"aws s3 sync exit {result.returncode}: {stderr[:300]}",
        }

    files = []
    bytes_total = 0
    if local_target.exists():
        for p in sorted(local_target.rglob("*")):
            if p.is_file():
                size = p.stat().st_size
                files.append({"path": str(p.relative_to(dest_root)), "bytes": size})
                bytes_total += size

    return {
        "ok": True,
        "bucket": bucket,
        "prefix": prefix,
        "dest": str(dest_root),
        "files": files,
        "file_count": len(files),
        "bytes_total": bytes_total,
    }
