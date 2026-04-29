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
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def write_task(record: Dict[str, Any]) -> Path:
    """Write (or overwrite) a job's manifest. Returns the path written.

    Atomic via tempfile + os.replace so a crashed writer never leaves a
    half-written manifest that read_task would discard as corrupt JSON.
    """
    job_id = record.get("job_id")
    if not job_id or not isinstance(job_id, str):
        raise ValueError("manifest must contain a non-empty 'job_id' string")

    target_dir = manifest_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{job_id}.json"

    fd, tmp = tempfile.mkstemp(prefix=f".{job_id}.", suffix=".json", dir=str(target_dir))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f, indent=2, sort_keys=True)
        os.replace(tmp, target)
    except Exception:
        # Best-effort cleanup of the temp file on any failure.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def list_tasks() -> List[Dict[str, Any]]:
    """Return every well-formed manifest in the task directory.

    Skips unreadable / non-dict files silently — the caller is reaping or
    sweeping, and noisy raises here would mask the real failures we care
    about (cluster cleanup outcomes).
    """
    target_dir = manifest_dir()
    if not target_dir.exists():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(target_dir.glob("*.json")):
        if path.name.startswith("."):
            continue  # in-flight tempfiles
        try:
            with path.open("r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out


def delete_task(job_id: str) -> bool:
    """Remove a manifest. Returns True if a file was removed, False otherwise."""
    path = manifest_path(job_id)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# Manifest fields that join_status passes through verbatim when present.
# ``managed_job_id`` (M1A) is the integer Sky assigns to a managed job —
# pure passthrough; the join helper never synthesizes one if absent.
_LOCAL_PASSTHROUGH_FIELDS = (
    "intent",
    "expected_artifacts",
    "owner_pid",
    "started_at",
    "session_id",
    "managed_job_id",
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
