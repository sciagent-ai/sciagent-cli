"""
Compute module for sciagent.

Provides container-based compute job execution with:
- Background execution by default (token-light)
- Local Docker backend (MVP)
- Integration with existing ProcessManager and bg_* tools
"""

from dataclasses import dataclass
from typing import Optional

from .job import Job, JobResult, JobStatus, ComputeRequirements, StorageMount, StorageMode
from .router import ComputeRouter


@dataclass
class CloudConfig:
    """Cloud-side runtime configuration.

    Separate from ``AgentConfig`` (agent-loop concerns: tokens, model,
    iterations). ``CloudConfig`` carries cloud / compute concerns: cost
    gates, workspace storage backend, cluster lifecycle defaults, subagent
    warm-resume window.

    All fields are ``Optional`` and default to ``None``. ``None`` means
    "fall through to env / yaml / built-in default" — the caller did not
    specify a value at the Python-API layer.

    Precedence for each knob: env var > CloudConfig field > yaml
    (``~/.sciagent/config.yaml``) > built-in default.
    """

    # ask_user commit threshold ($) for cloud launches.
    # Env: ``SCIAGENT_COMPUTE_COMMIT_THRESHOLD_USD``.
    # YAML: ``compute.commit_threshold_usd``. Default: $5.00.
    commit_threshold_usd: Optional[float] = None

    # Cloud provider for the per-session workspace bucket:
    # ``s3`` / ``gcs`` / ``az`` / ``r2`` / ``oci``.
    # Env: ``SCIAGENT_WORKSPACE_STORE``. Default: auto-detect from
    # available cloud creds.
    workspace_store: Optional[str] = None

    # Default ``idle_minutes`` for cluster autostop. Per-cluster overrides
    # via ``compute_cluster(action="autostop", idle_minutes=...)`` win.
    # Default: SkyPilot provider default.
    default_autostop_minutes: Optional[int] = None

    # Default wall-clock budget per compute job (seconds). The reaper kills
    # clusters whose runtime exceeds this. Per-call ``compute_run(timeout_sec=...)``
    # wins. Pass 0 to disable the on-VM timeout wrapper.
    # Default: ``ComputeRequirements.timeout_sec`` built-in (3600).
    default_timeout_sec: Optional[int] = None

    # Window during which a subagent that crashed can be warm-resumed
    # without prompting the parent.
    # Env: ``SCIAGENT_SUBAGENT_WARM_RESUME_SECONDS``.
    # YAML: ``subagent.warm_resume_seconds``.
    subagent_warm_resume_seconds: Optional[int] = None


__all__ = [
    "CloudConfig",
    "Job",
    "JobResult",
    "JobStatus",
    "ComputeRequirements",
    "ComputeRouter",
    "StorageMount",
    "StorageMode",
]
