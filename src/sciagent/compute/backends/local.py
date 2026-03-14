"""
Local Docker compute backend.

Uses ProcessManager for background execution, Docker for containerization.
Designed for token-light output - summaries instead of full logs.
"""

from __future__ import annotations

import subprocess
import shutil
from typing import TYPE_CHECKING

from ..job import Job, JobResult, JobStatus, ComputeRequirements

if TYPE_CHECKING:
    from sciagent.process_manager import ProcessManager


class LocalBackend:
    """Local Docker compute via ProcessManager."""

    name = "local"

    def __init__(self):
        self._pm = None  # Lazy init

    def _get_pm(self) -> "ProcessManager":
        """Get ProcessManager instance (lazy init)."""
        if self._pm is None:
            from sciagent.process_manager import ProcessManager
            self._pm = ProcessManager.get_instance()
        return self._pm

    def is_available(self) -> bool:
        """Check if Docker is available."""
        return shutil.which("docker") is not None

    def can_run(self, req: ComputeRequirements) -> bool:
        """Check if this backend can handle the requirements.

        Local Docker can't do GPU (Mac Docker limitation) or very large memory.
        """
        if req.gpus > 0:
            return False
        # Check available memory (rough estimate)
        try:
            import psutil
            available_gb = psutil.virtual_memory().available / (1024**3)
            return req.memory_gb < available_gb * 0.8
        except ImportError:
            # Assume 16GB available if psutil not installed
            return req.memory_gb <= 16

    def run(self, job: Job, background: bool = True) -> str:
        """Run job, return job_id.

        Args:
            job: The job to run
            background: If True, run in background via ProcessManager

        Returns:
            job_id for tracking (can be used with bg_status, bg_wait, etc.)
        """
        # Build docker command
        cmd = self._build_docker_cmd(job)

        if background:
            # Use ProcessManager for background execution
            pm = self._get_pm()
            job_id = pm.launch(cmd, working_dir=job.working_dir)
            return job_id
        else:
            # Foreground - block until complete
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                cwd=job.working_dir,
                timeout=job.requirements.timeout_sec
            )
            # Return synthetic job_id for foreground jobs
            return f"fg-{job.id}"

    def _build_docker_cmd(self, job: Job) -> str:
        """Build Docker run command.

        Mounts current directory as /workspace and sets it as working dir.
        """
        image = job.image or f"ghcr.io/sciagent-ai/{job.service}:latest"

        # Build docker run command
        # - --rm: Remove container after completion
        # - -v "$(pwd)":/workspace: Mount current dir
        # - -w /workspace: Set working directory
        cmd = f'docker run --rm -v "$(pwd)":/workspace -w /workspace {image} {job.command}'
        return cmd

    def get_status(self, job_id: str) -> JobResult:
        """Get job status - token-light output.

        Returns JobResult with summaries and previews, not full logs.
        """
        pm = self._get_pm()
        status = pm.get_status(job_id)

        if status is None:
            return JobResult(status=JobStatus.FAILED, summary="Job not found")

        # Map ProcessManager status to JobStatus
        pm_status = status.get("status", "")

        if pm_status == "running":
            # Calculate runtime
            from datetime import datetime
            start_time = status.get("start_time", "")
            runtime = 0.0
            if start_time:
                try:
                    start_dt = datetime.fromisoformat(start_time)
                    runtime = (datetime.now() - start_dt).total_seconds()
                except (ValueError, TypeError):
                    pass

            return JobResult(
                status=JobStatus.RUNNING,
                runtime_sec=runtime,
                summary=f"Running for {runtime:.0f}s",
            )

        elif pm_status == "completed":
            # Get output preview (token-light)
            output = pm.get_output(job_id) or ""
            runtime = 0.0

            # Calculate runtime
            start_time = status.get("start_time", "")
            end_time = status.get("end_time", "")
            if start_time and end_time:
                try:
                    from datetime import datetime
                    start_dt = datetime.fromisoformat(start_time)
                    end_dt = datetime.fromisoformat(end_time)
                    runtime = (end_dt - start_dt).total_seconds()
                except (ValueError, TypeError):
                    pass

            return JobResult(
                status=JobStatus.COMPLETED,
                exit_code=status.get("exit_code", 0),
                runtime_sec=runtime,
                summary=f"Completed in {runtime:.0f}s",
                output_preview=output[:500] if output else "",
                output_file=status.get("stdout_file", ""),
            )

        else:
            # Failed or killed
            stderr = pm.get_output(job_id, stream="stderr") or ""
            return JobResult(
                status=JobStatus.FAILED,
                exit_code=status.get("exit_code", 1),
                summary=f"Failed with exit code {status.get('exit_code', 1)}",
                error_preview=stderr[:500] if stderr else "",
            )
