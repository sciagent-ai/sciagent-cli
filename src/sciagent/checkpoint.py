"""Subagent iteration checkpoints — append-only JSONL per backgrounded task.

Mirrors ``provenance_log`` conventions (schema_version, fcntl.flock, best-effort
writes that never fail the run). Stored at::

    ~/.sciagent/sessions/<session_id>/subagents/<task_id>/checkpoint.jsonl

with a sibling ``agent_state.json`` snapshot (full ``AgentState.to_dict()``) so
a resume path can rebuild the conversation exactly. Both writes are wrapped in
``try/except`` — a bad disk must never take down a running subagent.

Why this exists: a background subagent that has done dozens of tool calls can
lose every one to a transient ``Server disconnected`` mid-flight. Replaying
from an iteration-level record is the only way to keep that work, since the
LLM context is itself ephemeral and the prompt cache decays in minutes.

The schema is a sibling of provenance_log's, NOT a replacement. provenance_log
records *what happened in the session as a whole* (auditable). This file
records *what the subagent's loop did per iteration* (resumable). The two
overlap in spirit but answer different questions, so they live in adjacent
files rather than fighting over one schema.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = "1"

# Match provenance_log's per-field byte budget. The two files don't share a
# writer, but a verifier reading both should see consistent truncation
# semantics.
MAX_FIELD_BYTES = 4_096
TRUNCATION_PREVIEW_CHARS = 256


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def task_description_hash(task: str) -> str:
    """Stable sha256 of the user's task string. Used to match a fresh spawn
    against a prior crashed entry — same task text, same hash, resume
    candidate. Trimmed of leading/trailing whitespace so trivial differences
    don't fragment the match."""
    return _sha256_str((task or "").strip())


def _hash_value(value: Any) -> str:
    """sha256 of any JSON-serializable value, after canonical serialization.

    Used for tool args / tool result hashes — the checkpoint records the
    *hash* (cheap, bounded) and a small preview (debugging), not the full
    payload (the provenance log already has the full record).
    """
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8", errors="replace")
        except Exception:
            value = repr(value)
    if value is None:
        return _sha256_str("")
    if isinstance(value, str):
        return _sha256_str(value)
    return _sha256_str(_canonical_json(value))


def _truncated_preview(value: Any) -> str:
    """Return a short string preview of ``value`` for in-line debugging.

    Truncated at ``TRUNCATION_PREVIEW_CHARS`` so the checkpoint file stays
    bounded even when a tool result is megabytes long. The full payload is
    on disk in provenance_log.jsonl; the preview here is a hint, not a
    record.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        s = value
    else:
        try:
            s = _canonical_json(value)
        except Exception:
            s = repr(value)
    return s[:TRUNCATION_PREVIEW_CHARS]


def session_subagents_dir(session_id: str, base_dir: Optional[Path] = None) -> Path:
    """Per-session parent dir for all subagent checkpoint dirs."""
    base = Path(base_dir) if base_dir else Path.home() / ".sciagent" / "sessions"
    return base / session_id / "subagents"


class SubagentCheckpoint:
    """Append-only JSONL writer for one backgrounded subagent task.

    One instance per ``task_id`` per process. Writes are atomic per line via
    ``fcntl.flock``; the file is opened in ``ab`` mode each time so a crash
    mid-write never corrupts an earlier line.

    The full ``AgentState`` (system prompt, message history, todos, summary
    block) is snapshotted to a sibling ``agent_state.json`` on every
    checkpoint write. That's how the resume path rebuilds the subagent's
    context — the JSONL is the iteration ledger, the JSON is the state
    payload to replay.
    """

    def __init__(
        self,
        session_id: str,
        task_id: str,
        base_dir: Optional[Path] = None,
    ):
        if not session_id or not isinstance(session_id, str):
            raise ValueError("session_id must be a non-empty string")
        if not task_id or not isinstance(task_id, str):
            raise ValueError("task_id must be a non-empty string")

        self.session_id = session_id
        self.task_id = task_id

        self.dir = session_subagents_dir(session_id, base_dir=base_dir) / task_id
        self.path = self.dir / "checkpoint.jsonl"
        self.state_path = self.dir / "agent_state.json"
        self.lock_path = self.dir / ".checkpoint.lock"
        self.meta_path = self.dir / "meta.json"

        self.dir.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self.lock_path.touch(exist_ok=True)

        self._thread_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def write_meta(
        self,
        *,
        agent_name: str,
        task: str,
        parent_session_id: Optional[str] = None,
        child_session_id: Optional[str] = None,
    ) -> None:
        """Write a one-shot meta file describing what this checkpoint covers.

        Best-effort. The meta file is the cheap path resume detection uses
        to match a fresh spawn against an old crashed task without parsing
        the JSONL. ``task_hash`` lets the matcher answer "is this the same
        task?" without comparing free-form strings byte-for-byte.
        """
        meta = {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "agent_name": agent_name,
            "task": task,
            "task_hash": task_description_hash(task),
            "parent_session_id": parent_session_id,
            "child_session_id": child_session_id,
            "created_at": _utc_now_iso(),
        }
        try:
            tmp = self.meta_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(meta, indent=2, sort_keys=True))
            os.replace(tmp, self.meta_path)
        except Exception:
            pass  # best-effort; resume can fall back to JSONL last line

    def record_iteration(
        self,
        *,
        iteration: int,
        tool_name: str,
        tool_args: Any,
        tool_result: Any,
        todo_state: Optional[List[Dict[str, Any]]] = None,
        message_count: int = 0,
        success: bool = True,
    ) -> Optional[str]:
        """Append one checkpoint line. Returns the event_id, or None on failure.

        The line records hashes of args and result (so a verifier can match
        against the provenance_log's fuller record) plus short previews
        (so a human reader doesn't have to cross-reference for context).
        Best-effort: if the disk is unwritable, the subagent run continues
        unaffected.
        """
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "ts": _utc_now_iso(),
            "iteration": int(iteration),
            "tool_name": str(tool_name) if tool_name else "",
            "tool_args_hash": _hash_value(tool_args),
            "tool_args_preview": _truncated_preview(tool_args),
            "tool_result_hash": _hash_value(tool_result),
            "tool_result_preview": _truncated_preview(tool_result),
            "todo_state_snapshot": list(todo_state) if todo_state else [],
            "message_count": int(message_count),
            "success": bool(success),
        }
        line = json.dumps(event, default=str, ensure_ascii=False) + "\n"
        try:
            with self._thread_lock, open(self.lock_path, "rb+") as lock_f:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
                try:
                    with open(self.path, "ab") as out:
                        out.write(line.encode("utf-8"))
                        out.flush()
                        os.fsync(out.fileno())
                finally:
                    fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
        except Exception:
            return None
        return event["event_id"]

    def save_agent_state(self, state_dict: Dict[str, Any]) -> bool:
        """Write the full ``AgentState.to_dict()`` to ``agent_state.json``.

        Atomic via tempfile + os.replace. Best-effort — failure here means
        warm resume falls back to cold (replay from JSONL preview only).
        """
        try:
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state_dict, default=str))
            os.replace(tmp, self.state_path)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def read_records(self) -> List[Dict[str, Any]]:
        """Read all valid checkpoint lines.

        A truncated last line (writer crashed mid-flush) is silently dropped
        — that's the corrupt-tail tolerance the spec calls out. Mid-file
        corruption is unusual (writes go through flock + fsync), but if it
        happens we still return earlier valid lines.
        """
        records: List[Dict[str, Any]] = []
        try:
            with open(self.lock_path, "rb+") as lock_f:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_SH)
                try:
                    with open(self.path, "rb") as f:
                        raw_lines = f.readlines()
                finally:
                    fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
        except FileNotFoundError:
            return []
        except OSError:
            return []

        for raw in raw_lines:
            line = raw.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # Truncated / corrupt line — skip and keep reading.
                # The spec says "read up to last valid line, ignore tail";
                # we tolerate mid-file too because it costs nothing.
                continue
        return records

    def last_record_mtime(self) -> Optional[datetime]:
        """Return the mtime of the checkpoint file, or None if empty.

        Used to decide warm-vs-cold resume — if the last write was within
        the warm window, the prompt cache may still be valid and the parent
        can replay full message history; otherwise summarize.
        """
        try:
            st = self.path.stat()
        except OSError:
            return None
        if st.st_size == 0:
            return None
        return datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)

    def load_agent_state(self) -> Optional[Dict[str, Any]]:
        """Return the saved ``AgentState`` dict, or None if missing/corrupt."""
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def load_meta(self) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(self.meta_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None


# ----------------------------------------------------------------------
# Resume detection helpers
# ----------------------------------------------------------------------


def find_resumable_subagents(
    session_id: str,
    *,
    base_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Return checkpoint summaries for every subagent dir under this session.

    The orchestrator's spawn() filters this list against task_index state
    (only ``crashed`` / ``blocked_resume`` entries are resumable) plus the
    new task's description hash. Directories without a meta.json are
    skipped — those are pre-checkpoint relics, not resume candidates.
    """
    parent = session_subagents_dir(session_id, base_dir=base_dir)
    if not parent.exists():
        return []
    out: List[Dict[str, Any]] = []
    for child in sorted(parent.iterdir()):
        if not child.is_dir():
            continue
        meta_path = child / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        cp_path = child / "checkpoint.jsonl"
        try:
            mtime = (
                datetime.fromtimestamp(cp_path.stat().st_mtime, tz=timezone.utc)
                if cp_path.exists()
                else None
            )
        except OSError:
            mtime = None
        out.append(
            {
                "task_id": meta.get("task_id") or child.name,
                "task_hash": meta.get("task_hash"),
                "agent_name": meta.get("agent_name"),
                "task": meta.get("task"),
                "checkpoint_path": str(cp_path),
                "checkpoint_mtime": mtime,
                "dir": str(child),
            }
        )
    return out


# ----------------------------------------------------------------------
# Warm-resume window config
# ----------------------------------------------------------------------


_DEFAULT_WARM_RESUME_SECONDS = 300


def warm_resume_window_seconds() -> int:
    """Resolve the warm-resume window. Env wins, then config file, else 300s.

    Env: ``SCIAGENT_SUBAGENT_WARM_RESUME_SECONDS``.
    Config: ``~/.sciagent/config.yaml`` →
        ``subagent.warm_resume_window_seconds``.

    The 300s default is *intentionally* not anchored to any one provider's
    prompt-cache TTL — it's a heuristic: short enough that "still warm" is
    a reasonable assumption, long enough that small operational pauses
    (a 90s reconnect) don't force a cold replay. Per-deployment tuning
    via the config knob.
    """
    env = os.environ.get("SCIAGENT_SUBAGENT_WARM_RESUME_SECONDS")
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            pass
    cfg_path = Path.home() / ".sciagent" / "config.yaml"
    if cfg_path.exists():
        try:
            import yaml  # noqa: WPS433 (lazy import — yaml is heavy)

            data = yaml.safe_load(cfg_path.read_text()) or {}
            sub_cfg = (data.get("subagent") or {}) if isinstance(data, dict) else {}
            val = sub_cfg.get("warm_resume_window_seconds")
            if val is not None:
                return max(0, int(val))
        except Exception:
            pass
    return _DEFAULT_WARM_RESUME_SECONDS
