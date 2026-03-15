"""
Compute router for job routing to appropriate backends.

Supports:
- Local Docker (default)
- SkyPilot for GPU/cloud jobs
- Modal for serverless (future)
"""

from __future__ import annotations

from typing import Dict, Any, Tuple, Optional

from .job import Job, JobResult, JobStatus, ComputeRequirements
from .backends.local import LocalBackend


class ComputeRouter:
    """Route jobs to appropriate backend based on requirements."""

    # Thresholds for routing to cloud backends
    LARGE_MEMORY_THRESHOLD_GB = 16
    LARGE_CPU_THRESHOLD = 8  # More than 8 CPUs → cloud

    def __init__(self):
        self._backends: Dict[str, Any] = {}
        self._init_backends()

    def _init_backends(self):
        """Initialize available backends."""
        # Local Docker backend
        local = LocalBackend()
        if local.is_available():
            self._backends["local"] = local

        # SkyPilot backend (lazy import - don't break if not installed)
        try:
            from .backends.skypilot import SkyPilotBackend
            skypilot = SkyPilotBackend()
            if skypilot.is_available():
                self._backends["skypilot"] = skypilot
        except Exception:
            pass  # SkyPilot not installed or not configured

    def list_backends(self) -> list:
        """List available backends."""
        return list(self._backends.keys())

    def select(
        self,
        req: ComputeRequirements,
        preferred: Optional[str] = None
    ) -> Tuple[Any, str]:
        """Select backend for job requirements.

        Routing logic:
        1. If preferred backend specified and available, use it
        2. Route GPU jobs (gpus > 0) to SkyPilot
        3. Route large memory jobs (> 16GB) to SkyPilot
        4. Route high CPU jobs (> 8 cores) to SkyPilot
        5. Default to local Docker

        Args:
            req: Compute requirements
            preferred: Preferred backend name (optional)

        Returns:
            Tuple of (backend, reason_string)

        Raises:
            RuntimeError: If no backend available
        """
        # If preferred backend specified
        if preferred:
            if preferred in self._backends:
                return self._backends[preferred], f"Using requested backend: {preferred}"
            else:
                available = list(self._backends.keys())
                raise RuntimeError(
                    f"Requested backend '{preferred}' not available. "
                    f"Available backends: {available}. "
                    f"For SkyPilot: pip install 'skypilot[aws]' and configure credentials."
                )

        # Route GPU jobs to SkyPilot (local Docker can't do GPU on Mac)
        if req.gpus > 0:
            if "skypilot" in self._backends:
                return self._backends["skypilot"], f"GPU job ({req.gpus}x {req.gpu_type or 'GPU'}) routed to SkyPilot"
            else:
                raise RuntimeError(
                    "GPU requested but SkyPilot not available. "
                    "Install with: pip install 'skypilot[aws]' and configure cloud credentials."
                )

        # Route large memory jobs to SkyPilot
        if req.memory_gb > self.LARGE_MEMORY_THRESHOLD_GB:
            if "skypilot" in self._backends:
                return self._backends["skypilot"], f"Large memory job ({req.memory_gb}GB) routed to SkyPilot"
            # Fall through to local if SkyPilot not available

        # Route high CPU jobs to SkyPilot
        if req.cpus > self.LARGE_CPU_THRESHOLD:
            if "skypilot" in self._backends:
                return self._backends["skypilot"], f"High CPU job ({req.cpus} cores) routed to cloud"
            # Fall through to local if SkyPilot not available

        # Default to local Docker
        if "local" in self._backends:
            local = self._backends["local"]
            if local.can_run(req):
                return local, "Using local Docker"
            else:
                # Local can't handle it, try SkyPilot
                if "skypilot" in self._backends:
                    return self._backends["skypilot"], "Local Docker constrained, using SkyPilot"
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

        Checks SkyPilot for cluster-prefixed jobs, local otherwise.
        """
        # SkyPilot jobs have sciagent- prefix
        if job_id.startswith("sciagent-") and "skypilot" in self._backends:
            return self._backends["skypilot"].get_status(job_id)

        # Local backend
        if "local" in self._backends:
            return self._backends["local"].get_status(job_id)

        return JobResult(status=JobStatus.FAILED, summary="No backend available")

    def estimate_cost(self, job: Job, duration_hours: float = 1.0) -> dict:
        """Estimate cost for a job.

        Args:
            job: The job to estimate
            duration_hours: Expected duration

        Returns:
            Cost estimation dict
        """
        if "skypilot" in self._backends:
            return self._backends["skypilot"].estimate_cost(job, duration_hours)
        return {"estimated_hourly": 0, "note": "Local execution - no cloud cost"}

    def cleanup(self, job_id: str) -> bool:
        """Cleanup/terminate a job's resources.

        Args:
            job_id: The job ID to cleanup

        Returns:
            True if cleanup succeeded
        """
        if job_id.startswith("sciagent-") and "skypilot" in self._backends:
            return self._backends["skypilot"].cleanup(job_id)
        # Local jobs don't need cleanup (containers are --rm)
        return True
