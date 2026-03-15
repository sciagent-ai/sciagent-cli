"""
SkyPilot compute backend for cloud GPU/large jobs.

Requires: pip install skypilot
Cloud credentials must be configured (aws configure, gcloud auth, etc.)
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional, Dict, Any

from ..job import Job, JobResult, JobStatus, ComputeRequirements


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

    def run(self, job: Job, background: bool = True) -> str:
        """Launch job on cloud via SkyPilot.

        Args:
            job: The job to run
            background: If True, returns immediately after launch starts
                       If False, waits for job completion

        Returns:
            cluster_name as job_id for tracking
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

        if not background:
            # Wait for completion using sky.stream_and_get
            # This blocks and streams logs
            try:
                sky.stream_and_get(request_id)
            except Exception:
                pass  # Job may have completed or failed

        return cluster_name

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

        # Create task
        task = sky.Task(
            name=job.id,
            run=job.command,
        )
        task.set_resources(resources)

        return task

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
        # SkyPilot now uses async API - status() returns RequestId
        request_id = sky.status(cluster_names=[job_id], refresh='NONE')
        # stream_and_get() returns the actual result
        return sky.stream_and_get(request_id)

    def _get_queue(self, job_id: str) -> list:
        """Get job queue, handling async API."""
        sky = self._get_sky()
        # SkyPilot now uses async API - queue() returns RequestId
        request_id = sky.queue(cluster_name=job_id, refresh='NONE')
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

        except Exception as e:
            # Fall back to cluster status
            return self.get_status(job_id)

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
