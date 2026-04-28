"""Orphan sweep for sciagent-launched SkyPilot clusters (B11).

When a sciagent process exits ungracefully (kill -9, OOM, panic), its
clusters keep billing on the cloud side even though no one is watching them
locally. The orphan sweep walks ``~/.sciagent/tasks/*.json``, finds the
records whose ``owner_pid`` is no longer alive, and calls cleanup (sky.down)
on each. Manifests for swept jobs are removed so a re-running agent doesn't
keep trying to look them up.

Per v4.2 §C4 this is a *function*, not a CLI subcommand. M0's scope rules
keep the CLI surface untouched; a ``sciagent compute sweep`` subcommand can
land later as a small standalone PR or in M2A's unified-task-model work.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from .task_index import delete_task, list_tasks


def _pid_is_alive(pid: int) -> bool:
    """Return True if a process with this pid currently exists.

    Uses ``os.kill(pid, 0)`` — no signal sent, just a permission/existence
    probe. This is best-effort: pid recycling on a long-lived host could in
    principle make a dead manifest's pid alias a fresh process. Accepting
    that false-positive (we'd leave the cluster running) over a false-
    negative (we'd kill a still-owned job) is the right trade-off here.

    Returns False on any non-EPERM error so a flaky probe doesn't keep
    orphans alive. EPERM means the pid exists but belongs to another user;
    that still counts as "alive" for sweep purposes.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # the process exists, we just can't signal it
    except OSError:
        return False


def _is_orphaned(record: Dict[str, Any], self_pid: int) -> bool:
    """Decide whether a manifest's owner is gone.

    Skips records that:
      - have no ``owner_pid`` (pre-B7 manifests, or callers who declined to
        record one — sweeping them blind would risk killing live jobs).
      - belong to *this* sciagent process (we're not orphaned).
    """
    owner_pid = record.get("owner_pid")
    if not isinstance(owner_pid, int) or owner_pid <= 0:
        return False
    if owner_pid == self_pid:
        return False
    return not _pid_is_alive(owner_pid)


def sweep(
    cleanup: Callable[[str], bool],
    self_pid: Optional[int] = None,
) -> List[str]:
    """Cancel clusters whose ``owner_pid`` is no longer alive.

    Args:
        cleanup: callable accepting a job_id and terminating the cluster.
            Bind ``SkyPilotBackend(...).cleanup`` at the call site so this
            module never imports the backend (keeps tests cheap).
        self_pid: pid of the running sciagent process; defaults to
            ``os.getpid()``. Manifests owned by this pid are skipped.

    Returns:
        List of job_ids that were swept (cleanup attempted + manifest
        removed). The cleanup callable's return value is not propagated;
        sky.down is idempotent, so a single failure must not abort the
        sweep.
    """
    if self_pid is None:
        self_pid = os.getpid()

    swept: List[str] = []
    for record in list_tasks():
        if not _is_orphaned(record, self_pid):
            continue
        job_id = record.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            continue
        try:
            cleanup(job_id)
        except Exception:
            # Best-effort: a single failed cleanup must not stop the sweep.
            pass
        # Remove the manifest regardless so subsequent sweeps don't re-attempt
        # a cluster we've already declared orphaned.
        try:
            delete_task(job_id)
        except Exception:
            pass
        swept.append(job_id)
    return swept
