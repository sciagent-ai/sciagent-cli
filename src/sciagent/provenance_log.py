"""
Durable, append-only JSONL provenance log (M1B).

Writes session-scoped events to ~/.sciagent/sessions/<session_id>/provenance.jsonl
so a verifier — including one driven by a different LLM provider — can audit
a session after the fact using only the on-disk record.

Schema (schema_version="1") is documented in docs/provenance_log_schema.md.
The seven event kinds are:

  - tool_call               (an atomic-tool invocation began)
  - tool_result             (the invocation returned)
  - compute_job_launched    (a cloud job was submitted)
  - compute_job_status_changed (the mapped status moved; one per transition)
  - artifact_produced       (a file was observed on disk)
  - verification_result     (a DATA / EXEC / LLM gate produced a verdict)
  - correction              (an earlier event has been superseded)

Design constraints (carried from M1A's "three hard rules"):

  - Append-only: never rewrite a line. Corrections are appended.
  - Provider-neutral payloads: plain JSON, no SDK enums, no opaque blobs
    in fields the schema documents — except `intent` and `expected_artifacts`,
    which are opaque-by-design (v4.2 §C6).
  - Bounded growth: each line caps at 16 KB; truncatable fields (arguments,
    output_summary, evidence, claim, error) cap at 4 KB and get replaced
    with a {"_truncated": ..., "_sha256": ...} stub when they overrun.
  - Atomic-per-event: writes use fcntl.flock so concurrent writers (main
    thread + parallel orchestrator threads + a verify probe in another
    process) never interleave bytes.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = "1"

# Byte budgets. Documented in docs/provenance_log_schema.md alongside the
# truncation envelope shape. The line cap is advisory — if a load-bearing
# field (command_resolved, intent, expected_artifacts, path) overruns we
# emit slightly over rather than corrupt the field.
MAX_LINE_BYTES = 16_384
MAX_FIELD_BYTES = 4_096
TRUNCATION_PREVIEW_CHARS = 256

# Truncatable field names by event kind. Load-bearing fields are deliberately
# excluded so a verifier never sees a stub where a real command / path / intent
# is required.
_TRUNCATABLE_FIELDS = {
    "tool_call":            {"arguments"},
    "tool_result":          {"output_summary", "error"},
    "verification_result":  {"claim", "evidence"},
}


def _utc_now_iso() -> str:
    """Microsecond-precision UTC ISO 8601 string with explicit +00:00 suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _canonical_json(obj: Any) -> str:
    """Sort keys, no extra whitespace — used for hashing tool arguments."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _truncate_field(value: Any) -> Any:
    """Replace `value` with a truncation stub if its serialized form exceeds
    MAX_FIELD_BYTES. Strings are previewed in-place; structured values are
    serialized first so the stub records a faithful sha256 of the original."""
    if value is None:
        return None
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) <= MAX_FIELD_BYTES:
            return value
        preview = value[:TRUNCATION_PREVIEW_CHARS]
        return {
            "_truncated": True,
            "_original_size": len(encoded),
            "_preview": preview,
            "_sha256": _sha256_str(value),
        }
    serialized = _canonical_json(value)
    if len(serialized.encode("utf-8")) <= MAX_FIELD_BYTES:
        return value
    return {
        "_truncated": True,
        "_original_size": len(serialized.encode("utf-8")),
        "_preview": serialized[:TRUNCATION_PREVIEW_CHARS],
        "_sha256": _sha256_str(serialized),
    }


def _apply_field_truncation(event_kind: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Walk `body` and stub out any oversized truncatable fields."""
    fields = _TRUNCATABLE_FIELDS.get(event_kind, set())
    if not fields:
        return body
    out = dict(body)
    for name in fields:
        if name in out:
            out[name] = _truncate_field(out[name])
    return out


class ProvenanceLog:
    """Per-session append-only JSONL writer.

    One instance per session_id per process. Use ``get_provenance_log(session_id)``
    to obtain (or initialize) the singleton for a session.
    """

    def __init__(self, session_id: str, base_dir: Optional[Path] = None):
        if not session_id or not isinstance(session_id, str):
            raise ValueError("session_id must be a non-empty string")

        self.session_id = session_id
        self.base_dir = Path(base_dir) if base_dir else Path.home() / ".sciagent" / "sessions"
        self.session_dir = self.base_dir / session_id
        self.path = self.session_dir / "provenance.jsonl"
        self.lock_path = self.session_dir / ".provenance.lock"

        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self.lock_path.touch(exist_ok=True)

        self._thread_lock = threading.Lock()
        self._seq = self._count_existing_events()
        self._status_memo: Dict[str, str] = {}
        self._launched_at: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Internal: write path
    # ------------------------------------------------------------------

    def _count_existing_events(self) -> int:
        """Seed seq from on-disk state so resume across restart stays monotonic."""
        try:
            with open(self.path, "rb") as f:
                return sum(1 for _ in f if _.strip())
        except FileNotFoundError:
            return 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _build_envelope(self, event_kind: str, actor: Optional[str]) -> Dict[str, Any]:
        env: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "event_kind": event_kind,
            "session_id": self.session_id,
            "seq": self._next_seq(),
            "ts": _utc_now_iso(),
        }
        if actor:
            env["actor"] = actor
        return env

    def _write_event(
        self,
        event_kind: str,
        body: Dict[str, Any],
        actor: Optional[str] = None,
    ) -> str:
        """Build envelope + body, JSON-serialize, append under flock.

        Returns the event_id so callers can reference it from a future
        ``correction`` event.
        """
        body = _apply_field_truncation(event_kind, body)

        with self._thread_lock:
            envelope = self._build_envelope(event_kind, actor)
            event = {**envelope, **body}
            line = json.dumps(event, default=str, ensure_ascii=False) + "\n"

            with open(self.lock_path, "rb+") as lock_f:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
                try:
                    with open(self.path, "ab") as out:
                        out.write(line.encode("utf-8"))
                        out.flush()
                        os.fsync(out.fileno())
                finally:
                    fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

            return envelope["event_id"]

    # ------------------------------------------------------------------
    # Public emit_* methods (one per event kind)
    # ------------------------------------------------------------------

    def emit_tool_call(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        actor: Optional[str] = None,
    ) -> str:
        """Emit a tool_call event right before tool dispatch."""
        body = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "arguments": arguments if arguments is not None else {},
            "arguments_sha256": _sha256_str(_canonical_json(arguments or {})),
        }
        return self._write_event("tool_call", body, actor=actor)

    def emit_tool_result(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        success: bool,
        output_summary: Any,
        error: Optional[str],
        duration_ms: int,
        actor: Optional[str] = None,
    ) -> str:
        """Emit a tool_result event right after tool dispatch returns."""
        body = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "success": bool(success),
            "output_summary": output_summary,
            "error": error,
            "duration_ms": int(duration_ms),
        }
        return self._write_event("tool_result", body, actor=actor)

    def emit_compute_job_launched(
        self,
        *,
        job_id: str,
        managed_job_id: Optional[int],
        backend: str,
        service: Optional[str],
        image: Optional[str],
        command_original: str,
        command_resolved: str,
        mount_path: Optional[str],
        mount_bucket: Optional[str],
        requirements: Dict[str, Any],
        intent: Optional[Dict[str, Any]],
        expected_artifacts: Optional[List[str]],
    ) -> str:
        """Emit a compute_job_launched event after a successful launch.

        intent and expected_artifacts are recorded verbatim per v4.2 §C6 —
        the writer never validates or normalizes their shape.
        """
        body = {
            "job_id": job_id,
            "managed_job_id": managed_job_id,
            "backend": backend,
            "service": service,
            "image": image,
            "command_original": command_original,
            "command_resolved": command_resolved,
            "mount_path": mount_path,
            "mount_bucket": mount_bucket,
            "requirements": dict(requirements) if requirements else {},
            "intent": intent,
            "expected_artifacts": list(expected_artifacts) if expected_artifacts else [],
        }
        self._launched_at[job_id] = time.monotonic()
        return self._write_event("compute_job_launched", body)

    def emit_compute_job_status_changed(
        self,
        *,
        job_id: str,
        managed_job_id: Optional[int],
        status: str,
        sky_status_raw: Optional[str] = None,
        error_preview: Optional[str] = None,
        log_file: Optional[str] = None,
    ) -> Optional[str]:
        """Emit a status transition.

        Returns the event_id when a transition was recorded, or None when
        the status matches the last value emitted in this process for
        this job_id (dedup is process-local; restart will re-emit current
        status with status_previous=null per the schema).
        """
        prev = self._status_memo.get(job_id)
        if prev == status:
            return None
        body = {
            "job_id": job_id,
            "managed_job_id": managed_job_id,
            "status": status,
            "status_previous": prev,
            "sky_status_raw": sky_status_raw,
            "error_preview": error_preview,
            "log_file": log_file,
        }
        event_id = self._write_event("compute_job_status_changed", body)
        self._status_memo[job_id] = status
        return event_id

    def emit_artifact_produced(
        self,
        *,
        path: str,
        mount_path: Optional[str] = None,
        job_id: Optional[str] = None,
        size_bytes: Optional[int] = None,
        sha256: Optional[str] = None,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Emit an artifact_produced event.

        ``path`` is absolute (cluster-side for cluster artifacts, local
        otherwise). When ``mount_path`` is set, ``path_relative_to_mount``
        is derived for verifier convenience.
        """
        path_relative_to_mount: Optional[str] = None
        if mount_path and path.startswith(mount_path):
            stripped = path[len(mount_path):].lstrip("/")
            path_relative_to_mount = stripped or "."

        body = {
            "path": path,
            "mount_path": mount_path,
            "path_relative_to_mount": path_relative_to_mount,
            "job_id": job_id,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "content_type": content_type,
            "metadata": metadata,
        }
        return self._write_event("artifact_produced", body)

    def emit_verification_result(
        self,
        *,
        gate: str,
        task_id: Optional[str],
        claim: Dict[str, Any],
        verdict: str,
        confidence: Optional[float],
        evidence: Dict[str, Any],
        issues: List[Dict[str, Any]],
        verifier: str,
    ) -> str:
        """Emit a verification_result event.

        ``gate`` ∈ {"data", "exec", "llm"}.
        ``verdict`` ∈ {"verified", "refuted", "insufficient", "warning"}.
        """
        body = {
            "gate": gate,
            "task_id": task_id,
            "claim": claim,
            "verdict": verdict,
            "confidence": confidence,
            "evidence": evidence,
            "issues": list(issues) if issues else [],
            "verifier": verifier,
        }
        return self._write_event("verification_result", body)

    def emit_correction(
        self,
        *,
        corrects_event_id: str,
        reason: str,
        replacement: Dict[str, Any],
    ) -> str:
        """Emit a correction event referencing an earlier event_id.

        The writer never rewrites prior lines; corrections are how a session
        records that an earlier event has been superseded.
        """
        body = {
            "corrects_event_id": corrects_event_id,
            "reason": reason,
            "replacement": replacement,
        }
        return self._write_event("correction", body)

    # ------------------------------------------------------------------
    # Public read path (used by verify_session)
    # ------------------------------------------------------------------

    def read_events(self) -> List[Dict[str, Any]]:
        """Read all events from the log, in append order.

        Skips malformed lines defensively (records them as a synthetic
        ``_parse_error`` entry so a verifier can still see something
        happened). Acquires a shared flock during the read so a writer
        that's mid-line doesn't surface a torn record.
        """
        events: List[Dict[str, Any]] = []
        with open(self.lock_path, "rb+") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_SH)
            try:
                with open(self.path, "rb") as f:
                    for raw in f:
                        line = raw.strip()
                        if not line:
                            continue
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError as exc:
                            events.append({
                                "_parse_error": True,
                                "raw": line.decode("utf-8", errors="replace"),
                                "error": str(exc),
                            })
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
        return events


# ----------------------------------------------------------------------
# Per-session singleton accessor
# ----------------------------------------------------------------------

_logs_by_session: Dict[str, ProvenanceLog] = {}
_logs_lock = threading.Lock()
_active_session_id: Optional[str] = None


def get_provenance_log(session_id: str, base_dir: Optional[Path] = None) -> ProvenanceLog:
    """Return (and lazily create) the ProvenanceLog for ``session_id``.

    Multiple callers in the same process share a single ProvenanceLog
    instance per session — that's how the within-process status memo and
    sequence counter stay coherent.
    """
    with _logs_lock:
        log = _logs_by_session.get(session_id)
        if log is None:
            log = ProvenanceLog(session_id, base_dir=base_dir)
            _logs_by_session[session_id] = log
        return log


def set_active_session(session_id: Optional[str]) -> None:
    """Set the process-wide "active session" so layers that don't carry a
    session id explicitly (ProvenanceChecker, TaskOrchestrator gates, etc.)
    can resolve the right log via ``get_active_session_log()``.

    Called once by AgentLoop at startup. Pass ``None`` to clear.
    """
    global _active_session_id
    _active_session_id = session_id


def get_active_session_log() -> Optional[ProvenanceLog]:
    """Return the ProvenanceLog for the active session, or None.

    Layers that emit best-effort verification / artifact events use this
    to find the right log without threading session_id through their
    constructors. Returns None when no active session is set, in which
    case callers must skip emission silently.
    """
    sid = _active_session_id
    if not sid:
        return None
    try:
        return get_provenance_log(sid)
    except Exception:
        return None


def reset_provenance_logs() -> None:
    """Drop all cached ProvenanceLog instances (test helper)."""
    global _active_session_id
    with _logs_lock:
        _logs_by_session.clear()
    _active_session_id = None
