"""
Compute module for sciagent.

Provides container-based compute job execution with:
- Background execution by default (token-light)
- Local Docker backend (MVP)
- Integration with existing ProcessManager and bg_* tools
"""

from .job import Job, JobResult, JobStatus, ComputeRequirements
from .router import ComputeRouter

__all__ = [
    "Job",
    "JobResult",
    "JobStatus",
    "ComputeRequirements",
    "ComputeRouter",
]
