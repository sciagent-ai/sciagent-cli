"""Per-cluster manifest for sciagent's persistent (cluster-mode) sky clusters.

Mirrors the per-job manifest at ``~/.sciagent/tasks/<job_id>.json`` but is
keyed by cluster_name. Tracks the bits sciagent needs to manage warm
clusters across calls without re-querying Sky every time:

  - created_at:        ISO-8601 timestamp of first launch
  - last_used_at:      ISO-8601 timestamp of most recent launch/exec
  - autostop_minutes:  what we asked Sky to enforce
  - session_id:        agent session that owns the cluster (cleanup hook)
  - service / image:   what was launched
  - last_job_ids:      list of int Sky job_ids run on this cluster
  - autostop_hook:     shell snippet attached to autostop, if any

Best-effort: a write failure never breaks the launch path. The cluster is
already up on Sky; losing the local manifest only means subsequent
``compute_cluster(action='status')`` falls back to Sky's bare response.

The manifest is NOT load-bearing for correctness — every method that uses
it tolerates absence. It exists to enrich agent-facing replies (e.g.,
"this cluster has been UP for 23 minutes, last used 4 min ago, will
autostop at 30 min idle") and to enable a future orphaned-cluster reaper.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _manifest_dir() -> Path:
    """Directory holding cluster manifests. Created lazily on first write."""
    return Path.home() / ".sciagent" / "clusters"


def _manifest_path(cluster_name: str) -> Path:
    """Per-cluster manifest path. cluster_name is sanitized minimally —
    callers control the cluster_name and Sky already restricts it to
    DNS-safe characters."""
    safe = cluster_name.replace("/", "_")
    return _manifest_dir() / f"{safe}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_cluster(
    cluster_name: str,
    *,
    autostop_minutes: Optional[int] = None,
    autostop_hook: Optional[str] = None,
    session_id: Optional[str] = None,
    service: Optional[str] = None,
    image: Optional[str] = None,
    last_job_id: Optional[int] = None,
) -> None:
    """Create or update a cluster manifest. Best-effort — never raises.

    On first call (no existing file): writes a fresh record with
    created_at = now. On subsequent calls: merges fields, updates
    last_used_at = now, and appends last_job_id (if given) to the
    last_job_ids list (deduped, capped at the most recent 20).
    """
    try:
        path = _manifest_path(cluster_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: Dict[str, Any] = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except Exception:
                existing = {}

        record: Dict[str, Any] = {
            "cluster_name": cluster_name,
            "kind": "compute_cluster",
            "created_at": existing.get("created_at") or _now_iso(),
            "last_used_at": _now_iso(),
        }

        # Merge in optional fields. Don't clobber non-None existing values
        # with None on a partial update.
        for key, value in (
            ("autostop_minutes", autostop_minutes),
            ("autostop_hook", autostop_hook),
            ("session_id", session_id),
            ("service", service),
            ("image", image),
        ):
            if value is not None:
                record[key] = value
            elif key in existing:
                record[key] = existing[key]

        # last_job_ids: append + dedupe + cap. Existing list wins on
        # ordering, new id appended at the end.
        prior_ids: List[int] = list(existing.get("last_job_ids") or [])
        if last_job_id is not None:
            try:
                int_id = int(last_job_id)
                if int_id in prior_ids:
                    prior_ids = [i for i in prior_ids if i != int_id]
                prior_ids.append(int_id)
            except (TypeError, ValueError):
                pass
        record["last_job_ids"] = prior_ids[-20:]

        path.write_text(json.dumps(record, indent=2))
    except Exception:
        # Manifest is best-effort. The cluster is up on Sky; a write
        # failure must not propagate.
        pass


def read_cluster(cluster_name: str) -> Optional[Dict[str, Any]]:
    """Read a cluster manifest. Returns None if missing or unreadable."""
    try:
        path = _manifest_path(cluster_name)
        if not path.exists():
            return None
        return json.loads(path.read_text())
    except Exception:
        return None


def delete_cluster(cluster_name: str) -> bool:
    """Remove a cluster manifest. Returns True on success or if absent."""
    try:
        path = _manifest_path(cluster_name)
        if path.exists():
            path.unlink()
        return True
    except Exception:
        return False


def _log_cache_dir() -> Path:
    """Sibling dir to manifests for cached per-job logs."""
    return _manifest_dir() / "logs"


def _log_cache_path(cluster_name: str, cluster_job_id: int) -> Path:
    safe = cluster_name.replace("/", "_")
    return _log_cache_dir() / f"{safe}__{int(cluster_job_id)}.log"


def cache_job_log(
    cluster_name: str,
    cluster_job_id: int,
    log_text: str,
    *,
    max_lines: int = 1000,
) -> bool:
    """Cache the last ``max_lines`` of a per-cluster job's stdout to disk.

    Solves the autostop race: ``sky logs`` raises ``ClusterNotUpError`` once
    the cluster transitions out of UP, which can happen within minutes of a
    FAILED job. Caching at terminal status (or on the first successful
    fetch) means failure forensics still works after the cluster is gone.

    Returns True on success, False on any I/O failure. Best-effort —
    callers must not rely on it for correctness.
    """
    try:
        d = _log_cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        lines = (log_text or "").splitlines()
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        _log_cache_path(cluster_name, cluster_job_id).write_text("\n".join(lines))
        return True
    except Exception:
        return False


def read_cached_job_log(
    cluster_name: str,
    cluster_job_id: int,
) -> Optional[str]:
    """Read a cached per-job log. Returns None when no cache exists."""
    try:
        path = _log_cache_path(cluster_name, cluster_job_id)
        if not path.exists():
            return None
        return path.read_text()
    except Exception:
        return None


def list_clusters(session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all cluster manifests, optionally filtered by session_id.

    Returns a list of records sorted by ``last_used_at`` descending.
    Empty list when the manifest dir doesn't exist yet.
    """
    try:
        d = _manifest_dir()
        if not d.exists():
            return []
        records: List[Dict[str, Any]] = []
        for entry in d.glob("*.json"):
            try:
                rec = json.loads(entry.read_text())
            except Exception:
                continue
            if session_id and rec.get("session_id") != session_id:
                continue
            records.append(rec)
        records.sort(key=lambda r: r.get("last_used_at") or "", reverse=True)
        return records
    except Exception:
        return []
