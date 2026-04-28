"""
SkyPilot compute backend for cloud GPU/large jobs.

Requires: pip install skypilot
Cloud credentials must be configured (aws configure, gcloud auth, etc.)
"""

from __future__ import annotations

import shlex
import tempfile
import time
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

from ..job import Job, JobResult, JobStatus, ComputeRequirements, LaunchError


# Default wall-clock budget for the fail-fast launch poll (B4). Picked to
# match v4.1 §2 B9's "structured error within 60 s" acceptance bar.
_LAUNCH_FAIL_FAST_BUDGET_SEC: float = 60.0
_LAUNCH_FAIL_FAST_POLL_SEC: float = 2.0


# Cloud URI prefix → sciagent store name. Restricted to schemes whose first
# path segment is unambiguously the bucket name. https:// (Azure blob) is
# excluded — extracting an Azure container from a URL is not a one-liner.
_CLOUD_URI_PREFIXES: Dict[str, str] = {
    "s3://": "s3",
    "gs://": "gcs",
    "r2://": "r2",
}


def _parse_cloud_uri(uri: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return (store, bucket) for a recognized cloud URI, else (None, None).

    Examples:
        s3://my-bucket           -> ("s3", "my-bucket")
        s3://my-bucket/case/foo  -> ("s3", "my-bucket")
        /local/path              -> (None, None)
        None                     -> (None, None)
    """
    if not uri or not isinstance(uri, str):
        return None, None
    for prefix, store in _CLOUD_URI_PREFIXES.items():
        if uri.startswith(prefix):
            rest = uri[len(prefix):]
            bucket = rest.split("/", 1)[0]
            if bucket:
                return store, bucket
            return None, None
    return None, None


class SkyPilotBackend:
    """Cloud compute via SkyPilot."""

    name = "skypilot"

    def __init__(self):
        self._sky = None  # Lazy import

    def _get_sky(self):
        """Lazy import of skypilot to avoid breaking if not installed."""
        if self._sky is None:
            try:
                import sky
                self._sky = sky
            except ImportError:
                raise RuntimeError(
                    "SkyPilot not installed. Run: pip install 'skypilot[aws]' "
                    "or 'skypilot[gcp]' or 'skypilot[azure]'"
                )
        return self._sky

    def is_available(self) -> bool:
        """Check if SkyPilot is installed and configured with cloud credentials."""
        try:
            sky = self._get_sky()
            # Check if any cloud is enabled for compute
            from sky.clouds import CloudCapability
            enabled_clouds = sky.check.get_cached_enabled_clouds_or_refresh(
                CloudCapability.COMPUTE
            )
            return len(enabled_clouds) > 0
        except Exception:
            return False

    def can_run(self, req: ComputeRequirements) -> bool:
        """SkyPilot can run anything if available."""
        return self.is_available()

    def get_enabled_store(self) -> str:
        """Get the storage type for the first enabled cloud."""
        try:
            sky = self._get_sky()
            from sky.clouds import CloudCapability
            enabled = sky.check.get_cached_enabled_clouds_or_refresh(
                CloudCapability.STORAGE
            )
            if not enabled:
                # Fall back to compute-enabled clouds
                enabled = sky.check.get_cached_enabled_clouds_or_refresh(
                    CloudCapability.COMPUTE
                )

            # Map cloud to store type
            cloud_to_store = {
                "AWS": "s3",
                "GCP": "gcs",
                "Azure": "azure",
                "Cloudflare": "r2",
            }
            for cloud in enabled:
                cloud_name = str(cloud).upper()
                for key, store in cloud_to_store.items():
                    if key.upper() in cloud_name:
                        return store
            return "s3"  # Default
        except Exception:
            return "s3"

    def get_workspace_mount(
        self,
        session_id: str,
        workspace_source: Optional[str] = None,
    ) -> "StorageMount":
        """Get a StorageMount for the session workspace bucket.

        Args:
            session_id: agent session id; used to derive the default bucket name.
            workspace_source: optional URI or local path passed to sky.Storage as
                `source`. When it is a recognized cloud URI (s3://bucket[/...]),
                the bucket name is taken from the URI so sky.Storage doesn't try
                to upload into a different bucket. Local paths fall back to the
                session-derived bucket name and get synced up by Sky on launch.
        """
        from ..job import StorageMount, StorageMode

        store_from_uri, bucket_from_uri = _parse_cloud_uri(workspace_source)
        if bucket_from_uri:
            bucket_name = bucket_from_uri
            store = store_from_uri
        else:
            bucket_name = f"sciagent-workspace-{session_id}"
            store = self.get_enabled_store()

        return StorageMount(
            path="/workspace",
            bucket=bucket_name,
            store=store,
            mode=StorageMode.MOUNT,
            source=workspace_source,
            persistent=True,
        )

    def run(self, job: Job, background: bool = True) -> str:
        """Launch job on cloud via SkyPilot.

        Args:
            job: The job to run
            background: If True, returns immediately after launch starts
                       If False, waits for job completion

        Returns:
            cluster_name as job_id for tracking

        Raises:
            LaunchError: when sky reports the launch FAILED/CANCELLED inside
                the fail-fast budget. Surfaced verbatim so callers can show
                a structured error rather than letting the agent burn a 10
                minute poll loop on a launch Sky already gave up on (B4).
        """
        sky = self._get_sky()

        # Build SkyPilot Task programmatically
        task = self._build_task(job)

        # Cluster name used as job_id
        cluster_name = f"sciagent-{job.id}"

        # Launch returns RequestId - async by default
        # Use down=True to auto-terminate after job completes (saves cost)
        request_id = sky.launch(
            task,
            cluster_name=cluster_name,
            down=not background,  # Auto-terminate if running foreground
            idle_minutes_to_autostop=10 if background else None,
        )

        # B4 fail-fast: poll the launch's request status briefly so a
        # controller-side rejection (bad image_id, missing creds, no
        # capacity) surfaces as a LaunchError within the budget rather
        # than disappearing into the next status-poll cycle.
        self._await_launch_or_fail(
            request_id=request_id,
            cluster_name=cluster_name,
            budget_sec=_LAUNCH_FAIL_FAST_BUDGET_SEC,
        )

        if not background:
            # Wait for completion using sky.stream_and_get
            # This blocks and streams logs
            try:
                sky.stream_and_get(request_id)
            except Exception:
                pass  # Job may have completed or failed

        return cluster_name

    def _await_launch_or_fail(
        self,
        request_id,
        cluster_name: str,
        budget_sec: float,
        poll_interval_sec: float = _LAUNCH_FAIL_FAST_POLL_SEC,
    ) -> None:
        """Poll sky.api_status briefly; raise LaunchError on FAILED/CANCELLED.

        B4 fail-fast (v4.2 §C5): sky.stream_and_get has no timeout kwarg, so
        the audit-described mechanism doesn't exist. We use sky.api_status
        polling instead — non-blocking, returns the request's pre-execution
        state, and lets us bail out fast on rejection.

        Returns silently when the launch:
          - has SUCCEEDED inside the budget (cluster is up), or
          - is still PENDING/RUNNING when the budget elapses (legitimate
            long provisioning; caller proceeds with normal status polling).

        Raises LaunchError when sky reports FAILED or CANCELLED.
        """
        sky = self._get_sky()
        try:
            from sky.server.requests.requests import RequestStatus
        except Exception:
            # If the import surface drifts on a future Sky upgrade, fall back
            # to compare-by-name on the status enum. Better than crashing.
            RequestStatus = None  # type: ignore

        deadline = time.monotonic() + budget_sec
        while time.monotonic() < deadline:
            try:
                payloads = sky.api_status(request_ids=[request_id])
            except Exception:
                # Transient API hiccup — retry within the budget. Don't let
                # an api_status flake convert into a phantom LaunchError.
                time.sleep(poll_interval_sec)
                continue

            if payloads:
                payload = payloads[0]
                status = getattr(payload, "status", None)
                status_name = getattr(status, "name", None) or str(status)

                if status_name in ("FAILED", "CANCELLED"):
                    msg = (
                        getattr(payload, "status_msg", None)
                        or getattr(payload, "error", None)
                        or f"sky.launch {status_name.lower()} for cluster {cluster_name}"
                    )
                    raise LaunchError(str(msg))

                if status_name == "SUCCEEDED":
                    return

            time.sleep(poll_interval_sec)
        # Budget exceeded; treat as a still-launching cluster.
        return

    def _build_task(self, job: Job):
        """Build SkyPilot Task object."""
        sky = self._get_sky()

        # Build resources
        resources_kwargs = {
            "cpus": f"{job.requirements.cpus}+",
            "memory": f"{job.requirements.memory_gb}+",
        }

        # Add GPU if requested
        if job.requirements.gpus > 0:
            gpu_type = job.requirements.gpu_type or "A10G"
            resources_kwargs["accelerators"] = {gpu_type: job.requirements.gpus}

        # Add Docker image if specified
        if job.image:
            resources_kwargs["image_id"] = f"docker:{job.image}"

        resources = sky.Resources(**resources_kwargs)

        # B6: enforce ComputeRequirements.timeout_sec on-VM by wrapping the
        # user command with the GNU `timeout` utility. shlex.quote handles
        # arbitrary command shapes (multi-line scripts, embedded quotes).
        # A timeout_sec <= 0 disables the wrapper for callers who have a
        # legitimate need to run unbounded.
        run_command = job.command
        timeout_sec = getattr(job.requirements, "timeout_sec", 0) or 0
        if timeout_sec > 0:
            run_command = (
                f"timeout {int(timeout_sec)} bash -c {shlex.quote(job.command)}"
            )

        # Create task
        task = sky.Task(
            name=job.id,
            run=run_command,
        )
        task.set_resources(resources)

        # Add storage mounts if specified
        if job.requirements.storage:
            storage_mounts = self._build_storage_mounts(job.requirements.storage)
            task.set_storage_mounts(storage_mounts)

        return task

    def _build_storage_mounts(self, storage_mounts) -> Dict[str, Any]:
        """Build SkyPilot storage_mounts dict from StorageMount list."""
        sky = self._get_sky()
        file_mounts = {}

        for mount in storage_mounts:
            # Map mode
            mode_map = {
                "MOUNT": sky.StorageMode.MOUNT,
                "COPY": sky.StorageMode.COPY,
                "MOUNT_CACHED": sky.StorageMode.MOUNT_CACHED,
            }
            mode = mode_map.get(mount.mode.value, sky.StorageMode.MOUNT)

            # Map store type to StoreType enum
            stores = None
            if mount.store:
                store_type = getattr(sky.StoreType, mount.store.upper(), None)
                if store_type:
                    stores = [store_type]

            # Create SkyPilot Storage object with all params in constructor.
            # persistent=True keeps the bucket after the cluster is torn down,
            # which is what users expect for a workspace mount.
            storage = sky.Storage(
                name=mount.bucket,
                source=mount.source,
                stores=stores,
                mode=mode,
                persistent=mount.persistent,
            )

            file_mounts[mount.path] = storage

        return file_mounts

    def _build_task_yaml(self, job: Job) -> Dict[str, Any]:
        """Build SkyPilot task definition as YAML dict (for debugging/export)."""
        task = {
            "name": job.id,
            "resources": {
                "cpus": f"{job.requirements.cpus}+",
                "memory": f"{job.requirements.memory_gb}+",
            },
            "run": job.command,
        }

        # Add GPU if requested
        if job.requirements.gpus > 0:
            gpu_type = job.requirements.gpu_type or "A10G"
            task["resources"]["accelerators"] = f"{gpu_type}:{job.requirements.gpus}"

        # Add Docker image
        if job.image:
            task["resources"]["image_id"] = f"docker:{job.image}"

        return task

    def _get_clusters(self, job_id: str) -> list:
        """Get cluster status, handling async API."""
        sky = self._get_sky()
        from sky.utils.common import StatusRefreshMode
        # sky.status takes cluster_names: List[str] and refresh: StatusRefreshMode (enum, not str).
        # AUTO lets Sky refresh stale records (preemption / autostop) without forcing a full refresh.
        request_id = sky.status(
            cluster_names=[job_id],
            refresh=StatusRefreshMode.AUTO,
        )
        return sky.stream_and_get(request_id)

    def _get_queue(self, job_id: str) -> list:
        """Get job queue, handling async API."""
        sky = self._get_sky()
        # sky.queue in 0.12 has no `refresh` kwarg; signature is (cluster_name, skip_finished, all_users).
        request_id = sky.queue(cluster_name=job_id)
        return sky.stream_and_get(request_id)

    def get_status(self, job_id: str) -> JobResult:
        """Get job status from SkyPilot cluster.

        Checks both cluster status and job queue for detailed status.
        """
        sky = self._get_sky()

        try:
            # Get cluster status using async API
            clusters = self._get_clusters(job_id)

            if not clusters:
                return JobResult(
                    status=JobStatus.FAILED,
                    summary=f"Cluster {job_id} not found"
                )

            cluster_info = clusters[0]
            cluster_status = cluster_info.get("status")

            # SkyPilot ClusterStatus enum values
            if cluster_status is not None:
                status_name = cluster_status.name if hasattr(cluster_status, 'name') else str(cluster_status)

                if status_name == "UP":
                    # Cluster is UP - check actual job status for more detail
                    return self.get_job_status(job_id)
                elif status_name == "STOPPED":
                    # Cluster stopped - check if job completed or failed
                    job_result = self.get_job_status(job_id)
                    if job_result.status == JobStatus.FAILED:
                        return job_result  # Return with error details
                    return JobResult(
                        status=JobStatus.COMPLETED,
                        summary=f"Cluster {job_id} stopped (job completed)"
                    )
                elif status_name == "INIT":
                    return JobResult(
                        status=JobStatus.PENDING,
                        summary=f"Cluster {job_id} initializing"
                    )
                else:
                    return JobResult(
                        status=JobStatus.RUNNING,
                        summary=f"Cluster status: {status_name}"
                    )

            return JobResult(
                status=JobStatus.FAILED,
                summary=f"Unknown cluster status for {job_id}"
            )

        except Exception as e:
            return JobResult(
                status=JobStatus.FAILED,
                summary=f"Error getting status: {str(e)}"
            )

    def get_job_status(self, job_id: str) -> JobResult:
        """Get detailed job status including queue info."""
        sky = self._get_sky()

        try:
            # Get job queue using async API
            jobs = self._get_queue(job_id)

            if not jobs:
                # No jobs in queue - cluster is up but no job submitted yet
                return JobResult(
                    status=JobStatus.PENDING,
                    summary=f"Cluster {job_id} is up, waiting for job"
                )

            # Get most recent job
            latest_job = jobs[0]
            job_status = latest_job.get("status")

            if job_status is not None:
                status_name = job_status.name if hasattr(job_status, 'name') else str(job_status)

                if status_name in ("RUNNING", "SETTING_UP"):
                    return JobResult(
                        status=JobStatus.RUNNING,
                        summary=f"Job {status_name.lower()} on {job_id}"
                    )
                elif status_name == "SUCCEEDED":
                    return JobResult(
                        status=JobStatus.COMPLETED,
                        summary=f"Job completed successfully on {job_id}"
                    )
                elif status_name in ("FAILED", "FAILED_SETUP"):
                    # Fetch logs and write to file for agent to read
                    error_logs = self.get_logs(job_id, tail=200)
                    log_file = self._write_logs_to_file(job_id, error_logs)
                    # Extract key error line for preview (token efficient)
                    error_preview = self._extract_error_line(error_logs)
                    return JobResult(
                        status=JobStatus.FAILED,
                        summary=f"Job failed on {job_id}",
                        error_preview=error_preview,
                        output_file=log_file,
                    )
                elif status_name == "PENDING":
                    return JobResult(
                        status=JobStatus.PENDING,
                        summary=f"Job pending on {job_id}"
                    )

            return self.get_status(job_id)

        except Exception:
            # get_status() calls back into get_job_status() — falling back to it here
            # produces unbounded recursion when both queries fail. Surface a transient
            # PENDING result and let the next poll retry.
            return JobResult(
                status=JobStatus.PENDING,
                summary=f"querying job {job_id}",
            )

    def estimate_cost(self, job: Job, duration_hours: float = 1.0) -> Dict[str, Any]:
        """Estimate cost for running job.

        Args:
            job: The job to estimate cost for
            duration_hours: Estimated duration in hours

        Returns:
            Dict with cost estimation details
        """
        sky = self._get_sky()

        try:
            # Build resources for cost lookup
            task = self._build_task(job)

            # Use optimizer to find cheapest resources
            # This returns (best_resources, cheapest_resources) per cloud
            dag = sky.Dag()
            dag.add(task)

            # Get cost estimate from optimizer
            optimizer = sky.Optimizer()
            optimized = optimizer.optimize(dag)

            # Extract cost info from optimized task
            if optimized and optimized.tasks:
                opt_task = optimized.tasks[0]
                resources = opt_task.best_resources
                if resources:
                    # get_cost() returns cost per SECOND, multiply by 3600 for hourly
                    cost_per_sec = resources.get_cost(1.0)
                    hourly = cost_per_sec * 3600
                    return {
                        "estimated_hourly": round(hourly, 2),
                        "estimated_total": round(hourly * duration_hours, 2),
                        "duration_hours": duration_hours,
                        "cloud": str(resources.cloud),
                        "instance_type": resources.instance_type,
                        "accelerators": str(resources.accelerators) if resources.accelerators else None,
                        "region": resources.region,
                    }

        except Exception as e:
            pass

        # Fallback: rough estimates based on requirements
        base_hourly = 0.10  # Base CPU cost
        if job.requirements.gpus > 0:
            gpu_costs = {
                "A10G": 1.00,
                "A100": 3.50,
                "V100": 2.50,
                "T4": 0.50,
                "L4": 0.80,
            }
            gpu_type = job.requirements.gpu_type or "A10G"
            base_hourly = gpu_costs.get(gpu_type, 1.50) * job.requirements.gpus

        return {
            "estimated_hourly": round(base_hourly, 4),
            "estimated_total": round(base_hourly * duration_hours, 4),
            "duration_hours": duration_hours,
            "cloud": "unknown",
            "gpu_type": job.requirements.gpu_type or "A10G" if job.requirements.gpus else None,
            "note": "Rough estimate - install skypilot for accurate pricing",
        }

    def cleanup(self, job_id: str, purge: bool = False) -> bool:
        """Terminate cluster to stop billing.

        Args:
            job_id: Cluster name to terminate
            purge: If True, also delete cluster record

        Returns:
            True if cleanup succeeded
        """
        sky = self._get_sky()
        try:
            sky.down(job_id, purge=purge)
            return True
        except Exception:
            return False

    def stop(self, job_id: str) -> bool:
        """Stop cluster (can be restarted later, still incurs storage cost).

        Args:
            job_id: Cluster name to stop

        Returns:
            True if stop succeeded
        """
        sky = self._get_sky()
        try:
            sky.stop(job_id)
            return True
        except Exception:
            return False

    def get_logs(self, job_id: str, tail: int = 100) -> str:
        """Get job logs from cluster.

        Args:
            job_id: Cluster name
            tail: Number of lines to retrieve

        Returns:
            Log output as string
        """
        sky = self._get_sky()
        try:
            # Use sky logs to get output
            import subprocess
            result = subprocess.run(
                ["sky", "logs", job_id, "--tail", str(tail)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.stdout if result.returncode == 0 else result.stderr
        except Exception as e:
            return f"Error fetching logs: {str(e)}"

    def _write_logs_to_file(self, job_id: str, logs: str) -> str:
        """Write logs to file for agent to read later.

        Args:
            job_id: Job/cluster identifier
            logs: Log content to write

        Returns:
            Path to the log file
        """
        log_dir = Path("_logs")
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"{job_id}.log"
        log_file.write_text(logs)
        return str(log_file)

    def _extract_error_line(self, logs: str, max_chars: int = 500) -> str:
        """Extract key error information from logs for preview.

        Looks for common error patterns and extracts relevant lines.
        Keeps it small for token efficiency.

        Args:
            logs: Full log content
            max_chars: Maximum characters for preview

        Returns:
            Extracted error preview
        """
        if not logs:
            return ""

        lines = logs.strip().split("\n")

        # Look for lines containing error indicators
        error_keywords = [
            "error:", "Error:", "ERROR:",
            "failed", "Failed", "FAILED",
            "exception", "Exception", "EXCEPTION",
            "fatal", "Fatal", "FATAL",
            "no matching manifest",  # Docker architecture errors
            "permission denied",
            "not found",
            "cannot",
        ]

        error_lines = []
        for line in lines:
            for keyword in error_keywords:
                if keyword in line:
                    error_lines.append(line.strip())
                    break

        if error_lines:
            # Return unique error lines, up to max_chars
            seen = set()
            unique_errors = []
            for line in error_lines:
                if line not in seen:
                    seen.add(line)
                    unique_errors.append(line)
            result = "\n".join(unique_errors)
            return result[:max_chars]

        # No error keywords found - return last few lines
        tail_lines = lines[-10:]
        result = "\n".join(tail_lines)
        return result[:max_chars]
