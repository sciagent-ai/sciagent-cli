"""Session-start project-dir snapshot.

Captures the set of files present in the project directory at session start
(path + size + mtime), so a verifier can later distinguish files that
**predated** the session — likely reference data, prior runs, or
manuscript-bundled artifacts — from files genuinely produced by this
session's work.

Why this exists: agents have been observed claiming "I found existing
simulation results" by reading a years-old reference file in the project
folder, then declaring the task complete without ever producing new
output. The snapshot makes the heuristic cheap: any file the agent
"discovers" whose path is in the snapshot, with mtime <= snapshot time,
is PRE_EXISTING and should not satisfy a "produced by this run" claim.

Storage: ``~/.sciagent/sessions/<session_id>/project_snapshot.json``.
Single write at session start; never updated. A small helper is exposed
for callers that need to ask "was this path here when the session
started?" without re-reading the JSON every time.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, Optional


# Bounded scan: defensive against scanning gigantic node_modules / .git /
# data dirs into RAM. The snapshot is for "did this file predate the
# session?", not a full FS index — these caps are well above any real
# scientific-computing project's source tree.
_MAX_FILES = 20_000
_MAX_FILE_BYTES_TRACKED = 50 * 1024 * 1024  # don't checksum large files; size+mtime is enough

# Directories to skip during the scan. Conservative — we err toward
# including more, since false positives in "predated" are recoverable
# (the verifier can ignore) but false negatives (missing a real
# pre-existing file) defeat the point of the snapshot.
_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "__pycache__", ".pytest_cache",
    ".venv", "venv", ".env",
    ".idea", ".vscode", ".DS_Store",
    ".sciagent",  # don't snapshot our own session state
})


def _scan(project_dir: Path) -> Dict[str, Dict[str, float]]:
    """Walk project_dir and return ``{rel_path: {size, mtime}}``.

    Entries are keyed by POSIX-style relative paths so the snapshot is
    portable across hosts. mtime uses the FS-reported value (Unix epoch
    seconds, float).
    """
    found: Dict[str, Dict[str, float]] = {}
    project_dir = project_dir.resolve()

    for root, dirs, files in os.walk(project_dir, followlinks=False):
        # Mutate dirs in place so os.walk skips them.
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]

        root_path = Path(root)
        for fname in files:
            if fname in _SKIP_DIRS:
                continue
            fpath = root_path / fname
            try:
                stat = fpath.stat()
            except (OSError, PermissionError):
                continue
            try:
                rel = str(fpath.relative_to(project_dir).as_posix())
            except ValueError:
                continue
            found[rel] = {"size": stat.st_size, "mtime": stat.st_mtime}
            if len(found) >= _MAX_FILES:
                return found
    return found


def write_session_snapshot(
    *,
    session_id: str,
    project_dir: str,
    base_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Write the project snapshot for a new session. Best-effort: returns
    None if the project dir doesn't exist or scanning fails. Callers
    should not block on this."""
    p = Path(project_dir)
    if not p.exists() or not p.is_dir():
        return None

    if base_dir is None:
        base_dir = Path.home() / ".sciagent" / "sessions" / session_id
    base_dir.mkdir(parents=True, exist_ok=True)

    snapshot_path = base_dir / "project_snapshot.json"
    if snapshot_path.exists():
        return snapshot_path

    try:
        files = _scan(p)
    except Exception:
        return None

    payload = {
        "schema_version": "1",
        "session_id": session_id,
        "project_dir": str(p.resolve()),
        "snapshot_at": time.time(),
        "snapshot_at_iso": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        ),
        "file_count": len(files),
        "truncated": len(files) >= _MAX_FILES,
        "files": files,
    }
    try:
        snapshot_path.write_text(json.dumps(payload, separators=(",", ":")))
    except OSError:
        return None
    return snapshot_path


def load_snapshot(
    session_id: str, base_dir: Optional[Path] = None
) -> Optional[Dict]:
    if base_dir is None:
        base_dir = Path.home() / ".sciagent" / "sessions" / session_id
    snapshot_path = base_dir / "project_snapshot.json"
    if not snapshot_path.exists():
        return None
    try:
        return json.loads(snapshot_path.read_text())
    except Exception:
        return None


def is_pre_existing(
    path: str,
    *,
    snapshot: Dict,
) -> bool:
    """Return True if ``path`` was present in the project at session start
    and its mtime hasn't moved past the snapshot time. False if absent,
    or present-but-modified-since.

    ``path`` may be absolute or project-relative. snapshot must be the
    dict returned by ``load_snapshot``.
    """
    if not snapshot:
        return False
    project_dir = snapshot.get("project_dir") or ""
    files = snapshot.get("files") or {}
    snapshot_at = snapshot.get("snapshot_at") or 0.0

    p = Path(path)
    if p.is_absolute():
        try:
            rel = str(p.resolve().relative_to(project_dir).as_posix())
        except (ValueError, OSError):
            return False
    else:
        rel = p.as_posix()

    entry = files.get(rel)
    if not entry:
        return False
    mtime = entry.get("mtime")
    if mtime is None:
        return False
    # If the file was modified after the snapshot, it's no longer the
    # original pre-existing version. We are conservative: only call it
    # PRE_EXISTING if the on-disk mtime hasn't moved.
    try:
        current_mtime = (Path(project_dir) / rel).stat().st_mtime
    except (OSError, ValueError):
        return False
    return current_mtime <= snapshot_at + 1e-3
