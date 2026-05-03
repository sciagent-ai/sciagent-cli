"""
Job definitions for compute module.

Token-conscious design: JobResult contains summaries and previews,
not full output. Full logs are written to files.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, List
import uuid


class StorageMode(Enum):
    """SkyPilot storage mount modes."""
    MOUNT = "MOUNT"              # Stream from bucket, writes replicated
    COPY = "COPY"                # Download to local disk
    MOUNT_CACHED = "MOUNT_CACHED"  # Local cache + bucket persistence


@dataclass
class StorageMount:
    """Cloud storage mount configuration for SkyPilot jobs."""
    path: str                           # Mount path in container (e.g., /workspace, /outputs)
    bucket: str                         # Bucket name (e.g., sciagent-workspace-abc)
    store: str = "s3"                   # s3, gcs, azure, r2, oci
    mode: StorageMode = StorageMode.MOUNT
    source: Optional[str] = None        # Local path or s3://… URI to sync from (optional)
    persistent: bool = True             # Keep bucket after job ends
    # kind="input" or "output". The output mount is auto-attached at
    # /outputs/ and exposes $OUTPUTS_DIR=/outputs/<job_id>/ to the user
    # command; auto-fetched on terminal status. Input mounts (default)
    # come from the caller's workspace_source= and may have any path.
    # Only input mounts are eligible to be the run-CWD target.
    kind: str = "input"
    # Deprecated: was used to discriminate auto-attached output flow from
    # caller-asked input flow. Now subsumed by kind. Field retained as a
    # no-op so legacy callers constructing StorageMount(implicit=True) don't
    # crash, but it's no longer read.
    implicit: bool = False


class JobStatus(Enum):
    """Status of a compute job.

    M1A extension (per v4.1 §1): RECOVERING and CANCELLED were added so the
    agent can react differently when Sky's managed-job controller is mid-spot-
    recovery (output paused, not failed) vs. when a user/agent cancelled the
    job (terminal, but not a failure to retry).
    """
    PENDING = "pending"
    RUNNING = "running"
    RECOVERING = "recovering"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ComputeRequirements:
    """Resource requirements for a compute job."""
    memory_gb: float = 4.0
    cpus: int = 2
    gpus: int = 0
    gpu_type: Optional[str] = None
    timeout_sec: int = 3600
    storage: Optional[List[StorageMount]] = None  # Cloud storage mounts


@dataclass
class Job:
    """A compute job to be executed in a container."""
    id: str = field(default_factory=lambda: f"job-{uuid.uuid4().hex[:8]}")
    service: str = ""
    image: str = ""
    command: str = ""
    # Local-side bookkeeping only: where compute_fetch writes results to.
    # NOT shipped to the cluster.
    working_dir: str = "."
    requirements: ComputeRequirements = field(default_factory=ComputeRequirements)

    # ship_workdir: when set, SkyPilot rsyncs this local directory to
    # ~/sky_workdir/ on the cluster before running. CWD becomes
    # ~/sky_workdir/ unless an input mount overrides it. None (default)
    # means no rsync — the image's WORKDIR is honored.
    ship_workdir: Optional[str] = None

    # M1B provenance fields. Optional and non-load-bearing for execution —
    # SkyPilotBackend uses them to emit a compute_job_launched event that
    # carries the session id and the v4.2 §C6 opaque payloads. They mirror
    # what the manifest already records so a verifier reading either
    # surface sees consistent state.
    session_id: Optional[str] = None
    intent: Optional[Dict[str, Any]] = None
    expected_artifacts: Optional[List[str]] = None


class LaunchError(RuntimeError):
    """Raised when a Sky launch is rejected before the job can run.

    Carries the underlying message, the would-be cluster name, and the
    Sky request_id so callers can (a) show a structured failure, (b)
    attempt cleanup on the partially-provisioned cluster, and (c) point
    the agent at ``sky api logs <request_id>`` when the auto log-tail
    fetch comes back empty.

    B4 fail-fast contract: a deliberately broken job must surface within
    the fail-fast budget instead of after a 10-min poll loop.
    """

    def __init__(
        self,
        message: str,
        cluster_name: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.cluster_name = cluster_name
        self.request_id = request_id


@dataclass
class JobResult:
    """
    Token-light result from job execution.

    Contains summaries and previews, not full logs.
    Full output is written to output_file.
    """
    status: JobStatus
    exit_code: Optional[int] = None
    runtime_sec: float = 0.0
    summary: str = ""  # Short summary for agent
    output_preview: str = ""  # First 500 chars of stdout
    output_file: str = ""  # Full output written here
    error_preview: str = ""  # First 500 chars of stderr
