"""
Compute router for job routing to appropriate backends.

MVP: Local Docker only.
Future: SkyPilot for GPU/cloud, Modal for serverless.
"""

from __future__ import annotations

from typing import Dict, Any, Tuple, Optional

from .job import Job, JobResult, JobStatus, ComputeRequirements
from .backends.local import LocalBackend


class ComputeRouter:
    """Route jobs to appropriate backend. MVP: local only."""

    def __init__(self):
        self._backends: Dict[str, Any] = {}
        self._init_backends()

    def _init_backends(self):
        """Initialize available backends."""
        local = LocalBackend()
        if local.is_available():
            self._backends["local"] = local

    def list_backends(self) -> list:
        """List available backends."""
        return list(self._backends.keys())

    def select(
        self,
        req: ComputeRequirements,
        preferred: Optional[str] = None
    ) -> Tuple[Any, str]:
        """Select backend for job requirements.

        Args:
            req: Compute requirements
            preferred: Preferred backend name (optional)

        Returns:
            Tuple of (backend, reason_string)

        Raises:
            RuntimeError: If no backend available
        """
        # If preferred and available
        if preferred and preferred in self._backends:
            return self._backends[preferred], f"Using requested backend: {preferred}"

        # MVP: just use local
        if "local" in self._backends:
            local = self._backends["local"]
            if local.can_run(req):
                return local, "Using local Docker"
            else:
                return local, "Using local Docker (may be resource constrained)"

        raise RuntimeError("No compute backend available. Is Docker installed?")

    def run(
        self,
        job: Job,
        backend: Optional[str] = None,
        background: bool = True
    ) -> str:
        """Route and run job, return job_id.

        Args:
            job: The job to run
            backend: Preferred backend name (optional)
            background: Run in background (default: True)

        Returns:
            job_id for tracking
        """
        b, reason = self.select(job.requirements, backend)
        return b.run(job, background=background)

    def get_status(self, job_id: str) -> JobResult:
        """Get job status from appropriate backend.

        MVP: Only checks local backend.
        """
        # MVP: only local backend
        if "local" in self._backends:
            return self._backends["local"].get_status(job_id)
        return JobResult(status=JobStatus.FAILED, summary="No backend available")
