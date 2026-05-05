"""
Lineage reader on top of the append-only provenance log.

A consumer that wants to ask "what produced ``s3://.../fields/U``?" or "what
consumed ``./_outputs/run-42/``?" should call ``produced_by`` / ``consumed_by``
here instead of re-implementing JSONL scanning ad-hoc.

Design constraints (carried from the plan):

  - Library function, not a service. Reads the on-disk JSONL directly.
  - Best-effort, mirroring how provenance writes themselves are wrapped:
    a missing / unreadable / corrupt log returns ``[]`` and a logged
    warning, never an exception into the caller.
  - URI matching is exact-or-prefix (in either direction), not glob.
    Globs are caller-side; pass ``glob.glob()`` output as individual URIs.
  - In-memory parse with a (path, mtime_ns) memo. ~100 LOC fits the
    plan's v1 envelope; SQLite escalation lives in P1.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class LineageEdge:
    """One side of a producer/consumer relationship for ``uri``.

    ``event`` is the raw provenance dict so callers can inspect any field
    the schema carries (sha256, size, derived_from, expected_artifacts, …)
    without this module having to model each one.
    """
    uri: str
    event: Dict[str, Any]
    direction: Literal["produced_by", "consumed_by"]
    job_id: Optional[str] = None
    subagent_id: Optional[str] = None
    timestamp: str = ""


# ---------------------------------------------------------------------------
# Log loading + memo
# ---------------------------------------------------------------------------

_memo_lock = threading.Lock()
_memo: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}


def _resolve_log_path(
    session_id: Optional[str], log_path: Optional[Path]
) -> Optional[Path]:
    if log_path is not None:
        return Path(log_path)
    if session_id:
        return Path.home() / ".sciagent" / "sessions" / session_id / "provenance.jsonl"
    try:
        from .provenance_log import get_active_session_log
        plog = get_active_session_log()
        if plog is not None:
            return Path(plog.path)
    except Exception:
        pass
    return None


def _load_events(log_path: Path) -> List[Dict[str, Any]]:
    """Return parsed events for ``log_path``, memoized on (path, mtime_ns)."""
    try:
        st = log_path.stat()
    except OSError as exc:
        logger.warning(
            "provenance_lineage: log not accessible at %s: %s", log_path, exc
        )
        return []

    key = (str(log_path), st.st_mtime_ns)
    with _memo_lock:
        cached = _memo.get(key)
    if cached is not None:
        return cached

    events: List[Dict[str, Any]] = []
    try:
        with open(log_path, "rb") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        logger.warning(
            "provenance_lineage: failed reading %s: %s", log_path, exc
        )
        return []

    with _memo_lock:
        _memo[key] = events
    return events


def reset_memo() -> None:
    """Drop the parsed-log cache (test helper; production callers don't need this)."""
    with _memo_lock:
        _memo.clear()


# ---------------------------------------------------------------------------
# Matching primitives
# ---------------------------------------------------------------------------


def _matches_uri(query: str, candidate: Optional[str]) -> bool:
    """Exact-or-prefix match between two URIs, in either direction.

    Either: ``candidate == query``, OR ``query`` is a directory prefix of
    ``candidate`` (the producer wrote a file under the queried directory),
    OR ``candidate`` is a directory prefix of ``query`` (the producer
    declared a parent directory and the query points inside it).
    """
    if not query or not candidate or not isinstance(candidate, str):
        return False
    if candidate == query:
        return True
    q = query.rstrip("/") + "/"
    c = candidate.rstrip("/") + "/"
    return candidate.startswith(q) or query.startswith(c)


def _substring_match(query: str, value: Any) -> bool:
    """Recursive substring match across str / dict / list — used for tool_call
    arguments, where input URIs land in any of half a dozen keys
    (``workspace_source``, ``source``, ``path``, ``cmd``, …) the tool's
    schema defines. v1 doesn't model those schemas; substring is enough."""
    if not query or value is None:
        return False
    if isinstance(value, str):
        return query in value
    if isinstance(value, dict):
        return any(_substring_match(query, v) for v in value.values())
    if isinstance(value, list):
        return any(_substring_match(query, v) for v in value)
    return False


def _build_edge(
    uri: str, event: Dict[str, Any], direction: str
) -> LineageEdge:
    actor = event.get("actor") or ""
    subagent_id = event.get("subagent_name")
    if not subagent_id and isinstance(actor, str) and actor.startswith("subagent:"):
        subagent_id = actor.split(":", 1)[1] or None
    return LineageEdge(
        uri=uri,
        event=event,
        direction=direction,  # type: ignore[arg-type]
        job_id=event.get("job_id"),
        subagent_id=subagent_id,
        timestamp=event.get("ts", ""),
    )


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------


def produced_by(
    uri: str,
    *,
    session_id: Optional[str] = None,
    log_path: Optional[Path] = None,
) -> List[LineageEdge]:
    """Events that wrote to (or claimed to land artifacts at) ``uri``.

    Matches:
      - ``artifact_produced`` events whose ``path`` overlaps ``uri``
        (exact, or one is a directory prefix of the other).
      - ``produces_validation_passed`` events whose declared ``patterns``
        or per-pattern resolved ``files[].path`` overlap ``uri``.
      - ``compute_job_launched`` events whose ``outputs_uri`` overlaps
        ``uri`` (forward-compat with a field not yet emitted in v1
        compute_job events; still safe — non-matches are skipped).
    """
    log = _resolve_log_path(session_id, log_path)
    if log is None:
        return []
    events = _load_events(log)
    out: List[LineageEdge] = []
    for ev in events:
        kind = ev.get("event_kind")
        if kind == "artifact_produced":
            if _matches_uri(uri, ev.get("path")):
                out.append(_build_edge(uri, ev, "produced_by"))
        elif kind == "produces_validation_passed":
            if _validation_resolves_uri(uri, ev):
                out.append(_build_edge(uri, ev, "produced_by"))
        elif kind == "compute_job_launched":
            if _matches_uri(uri, ev.get("outputs_uri")):
                out.append(_build_edge(uri, ev, "produced_by"))
    return out


def consumed_by(
    uri: str,
    *,
    session_id: Optional[str] = None,
    log_path: Optional[Path] = None,
) -> List[LineageEdge]:
    """Events that read from (or were dispatched against) ``uri``.

    Matches:
      - ``tool_call`` events whose ``arguments`` mention ``uri`` as a
        substring — the v1 stand-in for a structured per-tool-schema parser.
      - ``subagent_spawned`` events whose ``task_preview`` mentions ``uri``.
      - ``subagent_spawned`` events whose declared ``produces_uris``
        patterns overlap ``uri`` (consumer of upstream is producer of
        downstream — useful for chain walks).
    """
    log = _resolve_log_path(session_id, log_path)
    if log is None:
        return []
    events = _load_events(log)
    out: List[LineageEdge] = []
    for ev in events:
        kind = ev.get("event_kind")
        if kind == "tool_call":
            if _substring_match(uri, ev.get("arguments")):
                out.append(_build_edge(uri, ev, "consumed_by"))
        elif kind == "subagent_spawned":
            if _substring_match(uri, ev.get("task_preview")):
                out.append(_build_edge(uri, ev, "consumed_by"))
                continue
            patterns = ev.get("produces_uris") or []
            if any(
                isinstance(p, str) and _matches_uri(uri, p) for p in patterns
            ):
                out.append(_build_edge(uri, ev, "consumed_by"))
    return out


def chain(
    uri: str,
    *,
    max_depth: int = 5,
    session_id: Optional[str] = None,
    log_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Walk both directions to build a small lineage subtree.

    Ancestors: from each producer's ``derived_from`` list (the canonical
    upstream-input field on ``artifact_produced``).
    Descendants: from each consumer subagent's declared ``produces_uris``.
    Cycle-safe: a URI already on the walk path is not re-expanded.
    """
    return _chain(uri, max_depth, session_id, log_path, frozenset())


def _chain(
    uri: str,
    max_depth: int,
    session_id: Optional[str],
    log_path: Optional[Path],
    seen: frozenset,
) -> Dict[str, Any]:
    p = produced_by(uri, session_id=session_id, log_path=log_path)
    c = consumed_by(uri, session_id=session_id, log_path=log_path)
    node: Dict[str, Any] = {
        "uri": uri,
        "produced_by": p,
        "consumed_by": c,
        "ancestors": [],
        "descendants": [],
    }
    if max_depth <= 0:
        return node
    next_seen = seen | {uri}
    for edge in p:
        for parent in edge.event.get("derived_from") or []:
            if isinstance(parent, str) and parent not in next_seen:
                node["ancestors"].append(
                    _chain(parent, max_depth - 1, session_id, log_path, next_seen)
                )
    for edge in c:
        if edge.event.get("event_kind") != "subagent_spawned":
            continue
        for child in edge.event.get("produces_uris") or []:
            if isinstance(child, str) and child not in next_seen:
                node["descendants"].append(
                    _chain(child, max_depth - 1, session_id, log_path, next_seen)
                )
    return node


def _validation_resolves_uri(uri: str, event: Dict[str, Any]) -> bool:
    """True if a produces_validation_passed event's resolved set covers ``uri``.

    The event shape carries ``resolved[].pattern`` (the declared glob/URI)
    and ``resolved[].files[].path`` (concrete files when the scheme had a
    cheap listing). Either is a legitimate producer signal.
    """
    for entry in event.get("resolved") or []:
        if not isinstance(entry, dict):
            continue
        if _matches_uri(uri, entry.get("pattern")):
            return True
        for f in entry.get("files") or []:
            if isinstance(f, dict) and _matches_uri(uri, f.get("path")):
                return True
    for pat in event.get("patterns") or []:
        if isinstance(pat, str) and _matches_uri(uri, pat):
            return True
    return False
