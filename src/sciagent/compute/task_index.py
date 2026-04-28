"""Local task index for SkyPilot compute jobs (M0 stub).

Holds the per-job session manifest at ``~/.sciagent/tasks/<job_id>.json``.
M0 only needs the read side and the join helper; PR #4 (B7) lands the writer.
M2A promotes this module to ``sciagent/task_index.py`` and broadens its scope
beyond compute jobs (see v4 §6 / v4.2 §C3).

Manifest schema (M0 — opaque-by-design, v4.2 §C6):

    {
        "job_id":             "sciagent-abc123",   # cluster name from sky.launch
        "session_id":         "abc12345",
        "intent":             dict | None,         # opaque blob; not validated
        "expected_artifacts": list,                # opaque list; possibly empty
        "owner_pid":          int,                 # agent process pid
        "started_at":         "2026-04-27T18:32:11Z",
        "command":            "bash Allrun",       # optional, for display
        "metadata":           dict                 # optional, for free-form notes
    }

The schema is intentionally permissive: ``intent`` and ``expected_artifacts``
are passthrough fields (v4.2 §C6). The OpenFOAM repro happens to populate
``intent={"paper":..., "case":..., "run":...}``; an arbitrary
``compute_run(image="python:3.11", command="...")`` will populate
``{"command":..., "image":...}`` or ``{}`` or ``None`` — all valid.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from .job import JobResult, JobStatus


def manifest_dir() -> Path:
    """Directory where per-job manifests live.

    Hardcoded to ``~/.sciagent/tasks`` (v4.2 §N1) — never CWD. The repository
    contains an untracked project-local ``.sciagent/`` that would otherwise
    shadow the user-global one.
    """
    return Path.home() / ".sciagent" / "tasks"


def manifest_path(job_id: str) -> Path:
    return manifest_dir() / f"{job_id}.json"


def read_task(job_id: str) -> Optional[Dict[str, Any]]:
    """Read a job's manifest, or return None if it doesn't exist / is unreadable.

    Unreadable manifests (corrupt JSON, permission errors) deliberately return
    None rather than raising — the caller's contract is "best-effort local
    augmentation of the cloud-side status," and a corrupt manifest must not
    take down ``bg_status``.
    """
    path = manifest_path(job_id)
    try:
        with path.open("r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


# Manifest fields that join_status passes through verbatim when present.
_LOCAL_PASSTHROUGH_FIELDS = (
    "intent",
    "expected_artifacts",
    "owner_pid",
    "started_at",
    "session_id",
    "metadata",
)


def join_status(
    job_id: str,
    local: Optional[Dict[str, Any]],
    sky_result: Optional[JobResult],
) -> Dict[str, Any]:
    """Merge a local manifest and a SkyPilot status into one bg_status payload.

    The four cases we have to handle (v4.2 §N2 enumerates these as the B12
    test matrix):

      - both present                → intent/artifacts/etc. from local;
                                      status/summary from sky.
      - local only (sky raised)     → local fields + transient PENDING.
      - sky only (no manifest)      → today's sky-only behaviour (legacy
                                      jobs launched before B7 manifests
                                      existed).
      - neither                     → caller treats as "not found"; we still
                                      return a dict so the formatter doesn't
                                      crash.

    Both inputs are passthrough — ``intent`` is opaque, ``expected_artifacts``
    is opaque (v4.2 §C6). No schema enforcement.
    """
    out: Dict[str, Any] = {"job_id": job_id, "backend": "skypilot"}

    if sky_result is not None:
        out["status"] = sky_result.status.value
        out["summary"] = sky_result.summary or ""
        if sky_result.error_preview:
            out["error_preview"] = sky_result.error_preview
        if sky_result.output_file:
            out["output_file"] = sky_result.output_file
    else:
        # Sky query failed entirely (raised) — surface as transient PENDING,
        # matching the recovery shape PR #1's B1 fix established at the
        # backend layer.
        out["status"] = JobStatus.PENDING.value
        out["summary"] = f"querying job {job_id}"

    if local is not None:
        for key in _LOCAL_PASSTHROUGH_FIELDS:
            if key in local:
                out[key] = local[key]
        # `command` is useful for formatters that today hardcode "(compute job)".
        if "command" in local:
            out["command"] = local["command"]

    return out
