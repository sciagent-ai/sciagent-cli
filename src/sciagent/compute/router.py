"""
Compute router for job routing to appropriate backends.

Supports:
- Local Docker (default)
- SkyPilot for GPU/cloud jobs
- Modal for serverless (future)
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

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
        """Route and run job, return job_id (the human-readable identifier).

        SkyPilot's backend now returns ``(name, managed_job_id)``; this
        method drops the integer because the router-level contract is "give
        me the LLM-facing job_id." Callers that need the integer
        (the manifest writer in ``compute_run``) use
        ``selected_backend.run(...)`` directly after ``select(...)``.
        """
        b, _reason = self.select(job.requirements, backend)
        result = b.run(job, background=background)
        if isinstance(result, tuple):
            return result[0]
        return result

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

    # ------------------------------------------------------------------
    # Cluster-mode pass-throughs (sky.launch + sky.exec).
    #
    # These delegate to the SkyPilot backend; cluster mode is a SkyPilot
    # concept and has no local-Docker analogue. Callers that try to use
    # them without SkyPilot get a clear error rather than a silent fallback.
    # ------------------------------------------------------------------

    def _require_skypilot(self) -> Any:
        if "skypilot" not in self._backends:
            available = list(self._backends.keys())
            raise RuntimeError(
                f"Cluster-mode operations require SkyPilot. "
                f"Available backends: {available}. "
                f"Install with: pip install 'skypilot[aws]' and configure credentials."
            )
        return self._backends["skypilot"]

    def launch_cluster(
        self,
        job: Job,
        cluster_name: str,
        autostop_minutes: int = 30,
        autostop_hook: Optional[str] = None,
        wait_for: str = "jobs",
    ) -> Tuple[str, Optional[int]]:
        """Provision (or reuse) a persistent cluster and run ``job``."""
        return self._require_skypilot().launch_cluster(
            cluster_name=cluster_name,
            job=job,
            autostop_minutes=autostop_minutes,
            autostop_hook=autostop_hook,
            wait_for=wait_for,
        )

    def exec_on_cluster(
        self,
        job: Job,
        cluster_name: str,
    ) -> Tuple[str, Optional[int]]:
        """Run ``job`` as a follow-up on an existing UP cluster."""
        return self._require_skypilot().exec_on_cluster(
            cluster_name=cluster_name,
            job=job,
        )

    def refresh_cluster_mounts(
        self,
        job: Job,
        cluster_name: str,
    ) -> Tuple[str, Optional[int]]:
        """Re-sync file_mounts on an existing cluster (sky --no-setup)."""
        return self._require_skypilot().refresh_cluster_mounts(
            cluster_name=cluster_name,
            job=job,
        )

    def cluster_status(self, cluster_name: str) -> Dict[str, Any]:
        """Return Sky's cluster status enriched with sciagent's manifest."""
        return self._require_skypilot().cluster_status(cluster_name)

    def wait_cluster_up(
        self,
        cluster_name: str,
        timeout: float = 300.0,
        poll_interval: float = 5.0,
    ) -> Dict[str, Any]:
        """Block until cluster reaches UP, terminal-bad, or timeout."""
        return self._require_skypilot().wait_cluster_up(
            cluster_name=cluster_name,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    def wait_cluster_job(
        self,
        cluster_name: str,
        cluster_job_id: int,
        timeout: float = 1800.0,
        poll_interval: float = 10.0,
    ) -> Dict[str, Any]:
        """Block until a per-cluster job reaches terminal state."""
        return self._require_skypilot().wait_cluster_job(
            cluster_name=cluster_name,
            cluster_job_id=cluster_job_id,
            timeout=timeout,
            poll_interval=poll_interval,
        )

    def tail_cluster_job_logs(
        self,
        cluster_name: str,
        cluster_job_id: int,
        tail_lines: int = 200,
    ) -> Dict[str, Any]:
        """Return the tail of a cluster-mode job's stdout, with cache fallback.

        Falls back to the cluster manifest's on-disk cache when the
        cluster has transitioned out of UP (autostop, manual down). See
        ``SkyPilotBackend.tail_cluster_job_logs`` for the source-selection
        contract and return shape.
        """
        return self._require_skypilot().tail_cluster_job_logs(
            cluster_name=cluster_name,
            cluster_job_id=int(cluster_job_id),
            tail_lines=int(tail_lines),
        )

    def cluster_down(self, cluster_name: str, graceful: bool = True) -> bool:
        """Tear down a cluster (graceful by default). Destructive — for
        end-of-task cleanup prefer ``cluster_stop`` so the disk and data
        tier survive."""
        return self._require_skypilot().cluster_down(cluster_name, graceful=graceful)

    def cluster_stop(self, cluster_name: str) -> bool:
        """Non-destructive stop — preserves disk and identity for fast
        restart. The default end-of-task action."""
        return self._require_skypilot().cluster_stop(cluster_name)

    def cluster_start(self, cluster_name: str) -> bool:
        """Restart a previously stopped cluster, reusing its disk."""
        return self._require_skypilot().cluster_start(cluster_name)

    def set_cluster_autostop(
        self,
        cluster_name: str,
        idle_minutes: int,
        wait_for: str = "jobs",
        hook: Optional[str] = None,
    ) -> bool:
        """Apply autostop config (idle minutes, wait_for, hook) to a cluster."""
        return self._require_skypilot()._set_cluster_autostop(
            cluster_name=cluster_name,
            idle_minutes=idle_minutes,
            wait_for=wait_for,
            hook=hook,
        )
