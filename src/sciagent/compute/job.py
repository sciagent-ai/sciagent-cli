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
    path: str                           # Mount path in container (e.g., /workspace)
    bucket: str                         # Bucket name (e.g., sciagent-workspace-abc)
    store: str = "s3"                   # s3, gcs, azure, r2
    mode: StorageMode = StorageMode.MOUNT
    source: Optional[str] = None        # Local path or s3://… URI to sync from (optional)
    persistent: bool = True             # Keep bucket after job ends
    # implicit=True means "we attached this mount automatically for output
    # retrieval, the caller didn't ask for it." The skypilot backend uses
    # this to decide whether to cd into the mount (explicit: yes — caller's
    # data lives there) or to keep workdir's CWD and just symlink the
    # mount's _outputs/ into the workdir (implicit: outputs persist via the
    # bucket but the user's local script still runs from ~/sky_workdir/).
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
    working_dir: str = "."
    requirements: ComputeRequirements = field(default_factory=ComputeRequirements)

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

    Carries the underlying message and the would-be cluster name so callers
    can (a) show a structured failure and (b) attempt cleanup on the
    partially-provisioned cluster — Sky may have brought an instance up
    before the setup phase failed (e.g. wrong image without /bin/bash).

    B4 fail-fast contract: a deliberately broken job must surface within
    the fail-fast budget instead of after a 10-min poll loop.
    """

    def __init__(self, message: str, cluster_name: Optional[str] = None) -> None:
        super().__init__(message)
        self.cluster_name = cluster_name


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
