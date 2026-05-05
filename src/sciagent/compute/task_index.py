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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .job import JobResult, JobStatus


# ---- Kind / state taxonomy (consolidation refactor PR1) ---------------------
#
# The manifest is being broadened from "compute job record" to "in-flight
# registry entry typed by kind." PR1 shipped compute_job; PR4 adds subagent
# (the first non-compute kind, with backgrounded SubAgent runs as the
# consumer). Future kinds (watch, scheduled) will land additively. Both
# fields are read-side defaulted: a kind-less manifest is interpreted as
# compute_job, a state-less manifest is interpreted as running (the only
# state pre-PR1 manifests could have been in when written).

KNOWN_KINDS = ("compute_job", "subagent")

VALID_STATES = (
    "pending", "running", "completed", "failed", "cancelled",
    # Subagent-only terminal: subagent claimed success but its declared
    # produces_uris pattern(s) didn't resolve to a non-trivial artifact.
    # Distinct from "failed" so the parent / a verifier can tell a
    # contract gap apart from a real subagent failure (LLM crash, tool
    # error, etc.) without parsing result_summary.
    "blocked_produce_missing",
    # Subagent-only resumable: the run raised an exception (server
    # disconnect, network drop, transient LLM error) BEFORE reaching a
    # natural terminal. Distinct from "failed" because a failed run has
    # produced its verdict; a crashed run hasn't and may still be
    # finishable from its last checkpoint. Not in TERMINAL_STATES — a
    # later spawn can match this entry by task description hash and
    # offer the parent a 3-way resume decision.
    "crashed",
    # Subagent-only resumable: the agent itself decided the work can't
    # complete in the current process and asked to be picked up later
    # (e.g., parent budget hit, explicit pause). Sibling of "crashed";
    # both are matched by the resume detector.
    "blocked_resume",
)

TERMINAL_STATES = (
    "completed", "failed", "cancelled", "blocked_produce_missing",
)

# States from which a subagent can be resumed by a fresh spawn whose task
# description hash matches an existing entry. Distinct from TERMINAL_STATES
# because resumable entries are NOT done — they paused and can finish.
RESUMABLE_STATES = ("crashed", "blocked_resume")

DEFAULT_KIND = "compute_job"
DEFAULT_STATE = "running"


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


def list_tasks(
    kind: Optional[str] = None,
    state: Optional[str] = None,
    session_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return well-formed manifests, optionally filtered by kind/state/session.

    With no args (the pre-PR1 signature), returns every well-formed manifest in
    the task directory — same behavior as before. Filters compose with AND.

    Filtering is back-compat: a manifest missing ``kind`` is interpreted as
    ``compute_job``; a manifest missing ``state`` is interpreted as
    ``running``. So a kind-less pre-PR1 manifest matches
    ``list_tasks(kind="compute_job", state="running")`` without rewrite.

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
        if not isinstance(data, dict):
            continue
        if kind is not None and data.get("kind", DEFAULT_KIND) != kind:
            continue
        if state is not None and data.get("state", DEFAULT_STATE) != state:
            continue
        if session_id is not None and data.get("session_id") != session_id:
            continue
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
# ``kind``/``state``/``completed_at``/``result_summary`` (PR1) are the new
# discriminators; bg_status surfaces them so the LLM sees lifecycle without
# a new tool.
_LOCAL_PASSTHROUGH_FIELDS = (
    "intent",
    "expected_artifacts",
    "owner_pid",
    "started_at",
    "session_id",
    "managed_job_id",
    "metadata",
    "kind",
    "state",
    "completed_at",
    "result_summary",
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
        # Back-compat: pre-PR1 manifests have no kind/state field. Surface the
        # defaults so bg_status formatters and downstream consumers see a
        # consistent shape regardless of when the manifest was written.
        out.setdefault("kind", DEFAULT_KIND)
        out.setdefault("state", DEFAULT_STATE)

    return out


# ---- Generic registry API (consolidation refactor PR1) ----------------------


def _normalize(record: Dict[str, Any]) -> Dict[str, Any]:
    """Return a canonical view of a manifest record.

    Defaults ``kind`` to ``compute_job`` and ``state`` to ``running`` when
    absent (back-compat for pre-PR1 manifests written without those fields).
    Does NOT mutate the input.

    For compute_job, the kind-specific flat fields are surfaced under a
    derived ``body`` view (read-side convenience; the on-disk shape stays
    flat for back-compat). For non-compute kinds (subagent and later
    additions), the manifest writes ``body`` authoritatively at the top
    level and ``setdefault`` below leaves the authored body untouched —
    that's the precedent new kinds follow so kind-specific keys don't
    multiply across the flat namespace.
    """
    if not isinstance(record, dict):
        return {}
    out = dict(record)
    out.setdefault("kind", DEFAULT_KIND)
    out.setdefault("state", DEFAULT_STATE)
    out.setdefault("completed_at", None)
    out.setdefault("result_summary", None)

    body: Dict[str, Any] = {}
    if out["kind"] == "compute_job":
        for key in (
            "managed_job_id",
            "intent",
            "expected_artifacts",
            "command",
            "image",
            "service",
            "timeout_sec",
        ):
            if key in record:
                body[key] = record[key]
    out.setdefault("body", body)
    return out


def kind_of(task_id: str) -> str:
    """Return the kind of a task id. Manifest wins; prefix is a fallback.

    Resolution order:

      1. If a manifest exists for ``task_id``, return its ``kind`` field
         (defaulting to ``compute_job`` for pre-PR1 manifests via the
         normalized read).
      2. Otherwise, if the id starts with ``"sciagent-"``, return
         ``compute_job`` — covers the legacy/no-manifest path that the B7
         writer didn't yet cover, plus jobs in the brief window between
         sky.launch and manifest write.
      3. Otherwise, return ``"local"`` — the catch-all for ProcessManager-
         tracked bash background jobs and anything else.

    Callers route on this result instead of prefix-sniffing directly. That
    way, when a non-compute kind (e.g. ``subagent``) lands with an id that
    happens to share the ``sciagent-`` prefix, the manifest's ``kind`` wins
    and the routing goes to the correct per-kind handler — no silent
    misroute to the compute_job path.
    """
    if not task_id or not isinstance(task_id, str):
        return "local"
    record = read_task(task_id)
    if record is not None:
        return record.get("kind") or DEFAULT_KIND
    if task_id.startswith("sciagent-"):
        return "compute_job"
    return "local"


def get_task(task_id: str, strict: bool = False) -> Optional[Dict[str, Any]]:
    """Read a manifest by id (alias for read_task with broader naming).

    Returns the on-disk verbatim shape (not normalized) so existing callers
    don't break. Set ``strict=True`` to raise ``ValueError`` on an unknown
    ``kind`` value — useful when a caller is about to dispatch to a per-kind
    handler and a typo in the on-disk manifest would otherwise route silently
    to the default path.
    """
    record = read_task(task_id)
    if record is None:
        return None
    if strict:
        kind = record.get("kind", DEFAULT_KIND)
        if kind not in KNOWN_KINDS:
            raise ValueError(
                f"unknown kind {kind!r} in manifest {task_id!r}; "
                f"known kinds: {KNOWN_KINDS}"
            )
    return record


def update_task_state(
    task_id: str,
    state: str,
    completed_at: Optional[str] = None,
    result_summary: Optional[str] = None,
) -> bool:
    """Read+merge+write the lifecycle state for a task.

    Returns True if the manifest was updated, False otherwise (manifest
    missing, invalid state, or write failure). Atomic via the existing
    ``write_task`` tempfile+replace path.

    When ``state`` is a terminal state and ``completed_at`` is None, fills it
    with ``datetime.now(UTC).isoformat()`` automatically. ``result_summary``
    is opaque; callers are expected to keep it terse (≤120 chars) so it can
    be displayed across sessions without truncation gymnastics.
    """
    if state not in VALID_STATES:
        return False
    record = read_task(task_id)
    if record is None:
        return False
    record = dict(record)
    record["state"] = state
    if state in TERMINAL_STATES:
        if completed_at is None:
            completed_at = datetime.now(timezone.utc).isoformat()
        record["completed_at"] = completed_at
    elif completed_at is not None:
        record["completed_at"] = completed_at
    if result_summary is not None:
        record["result_summary"] = result_summary
    try:
        write_task(record)
    except Exception:
        return False
    return True
