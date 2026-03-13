"""
Process Manager for background task execution.

Manages background processes launched via Popen, providing:
- Job ID tracking and metadata storage
- Output capture to temp files
- Process polling and status checking
- Graceful cleanup on exit
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
import atexit
import signal
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List, Any
from enum import Enum


class JobStatus(Enum):
    """Status of a background job."""
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


@dataclass
class BackgroundJob:
    """Represents a background job with its process and metadata."""
    job_id: str
    command: str
    process: subprocess.Popen
    working_dir: str
    start_time: datetime
    stdout_file: Path
    stderr_file: Path
    status: JobStatus = JobStatus.RUNNING
    exit_code: Optional[int] = None
    end_time: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "job_id": self.job_id,
            "command": self.command,
            "working_dir": self.working_dir,
            "start_time": self.start_time.isoformat(),
            "status": self.status.value,
            "exit_code": self.exit_code,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "stdout_file": str(self.stdout_file),
            "stderr_file": str(self.stderr_file),
            "pid": self.process.pid if self.process else None,
        }


class ProcessManager:
    """
    Manages background processes for long-running tasks.

    Features:
    - Launch commands in background with Popen
    - Track jobs by unique ID
    - Capture stdout/stderr to temp files
    - Poll for completion status
    - Graceful cleanup on exit

    Usage:
        pm = ProcessManager.get_instance()
        job_id = pm.launch("sleep 60 && echo done", working_dir="/tmp")
        status = pm.get_status(job_id)
        output = pm.get_output(job_id)
        pm.kill(job_id)
    """

    _instance: Optional["ProcessManager"] = None
    _lock = threading.Lock()

    def __init__(self, output_dir: Optional[str] = None):
        """Initialize ProcessManager with output directory for logs."""
        if output_dir is None:
            output_dir = os.path.join(os.getcwd(), "_logs", "background_jobs")

        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        self._jobs: Dict[str, BackgroundJob] = {}
        self._jobs_lock = threading.Lock()

        # Register cleanup on exit
        atexit.register(self._cleanup_all)

    @classmethod
    def get_instance(cls, output_dir: Optional[str] = None) -> "ProcessManager":
        """Get or create the singleton ProcessManager instance."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(output_dir)
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance (for testing)."""
        with cls._lock:
            if cls._instance is not None:
                cls._instance._cleanup_all()
            cls._instance = None

    def _generate_job_id(self) -> str:
        """Generate a short, unique job ID."""
        return uuid.uuid4().hex[:8]

    def launch(
        self,
        command: str,
        working_dir: str = ".",
        env: Optional[Dict[str, str]] = None
    ) -> str:
        """
        Launch a command in the background.

        Args:
            command: Shell command to execute
            working_dir: Working directory for the command
            env: Optional environment variables (merged with current env)

        Returns:
            job_id: Unique identifier for this background job
        """
        job_id = self._generate_job_id()

        # Create output files
        stdout_file = self._output_dir / f"{job_id}.stdout"
        stderr_file = self._output_dir / f"{job_id}.stderr"

        # Merge environment
        process_env = os.environ.copy()
        if env:
            process_env.update(env)

        # Open output files
        stdout_handle = open(stdout_file, "w")
        stderr_handle = open(stderr_file, "w")

        try:
            # Launch process with Popen
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=stdout_handle,
                stderr=stderr_handle,
                cwd=working_dir,
                env=process_env,
                # Start new process group for clean termination
                preexec_fn=os.setsid if os.name != 'nt' else None,
            )

            # Create job record
            job = BackgroundJob(
                job_id=job_id,
                command=command,
                process=process,
                working_dir=working_dir,
                start_time=datetime.now(),
                stdout_file=stdout_file,
                stderr_file=stderr_file,
            )

            with self._jobs_lock:
                self._jobs[job_id] = job

            return job_id

        except Exception as e:
            # Clean up on failure
            stdout_handle.close()
            stderr_handle.close()
            stdout_file.unlink(missing_ok=True)
            stderr_file.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to launch background job: {e}")

    def _update_job_status(self, job: BackgroundJob) -> None:
        """Update job status by polling the process."""
        if job.status != JobStatus.RUNNING:
            return

        exit_code = job.process.poll()
        if exit_code is not None:
            job.exit_code = exit_code
            job.end_time = datetime.now()
            job.status = JobStatus.COMPLETED if exit_code == 0 else JobStatus.FAILED

    def get_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the current status of a background job.

        Args:
            job_id: The job identifier

        Returns:
            Dictionary with job status and metadata, or None if not found
        """
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None

            self._update_job_status(job)
            return job.to_dict()

    def get_output(
        self,
        job_id: str,
        stream: str = "stdout",
        tail_lines: Optional[int] = None,
        follow: bool = False
    ) -> Optional[str]:
        """
        Get output from a background job.

        Args:
            job_id: The job identifier
            stream: "stdout" or "stderr"
            tail_lines: If set, return only the last N lines
            follow: If True and job is running, wait for more output (not implemented yet)

        Returns:
            Output string, or None if job not found
        """
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None

            output_file = job.stdout_file if stream == "stdout" else job.stderr_file

        if not output_file.exists():
            return ""

        try:
            content = output_file.read_text()

            if tail_lines is not None:
                lines = content.split('\n')
                content = '\n'.join(lines[-tail_lines:])

            return content
        except Exception as e:
            return f"Error reading output: {e}"

    def wait(self, job_id: str, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """
        Wait for a background job to complete.

        Args:
            job_id: The job identifier
            timeout: Maximum seconds to wait (None = wait forever)

        Returns:
            Final job status dictionary, or None if not found
        """
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None

        try:
            job.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass

        return self.get_status(job_id)

    def kill(self, job_id: str, force: bool = False) -> bool:
        """
        Kill a background job.

        Args:
            job_id: The job identifier
            force: If True, use SIGKILL instead of SIGTERM

        Returns:
            True if job was killed, False if not found or already completed
        """
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False

            if job.status != JobStatus.RUNNING:
                return False

            try:
                if os.name != 'nt':
                    # Kill entire process group on Unix
                    pgid = os.getpgid(job.process.pid)
                    sig = signal.SIGKILL if force else signal.SIGTERM
                    os.killpg(pgid, sig)
                else:
                    # On Windows, just terminate the process
                    if force:
                        job.process.kill()
                    else:
                        job.process.terminate()

                job.status = JobStatus.KILLED
                job.end_time = datetime.now()
                return True

            except (ProcessLookupError, OSError):
                # Process already dead
                self._update_job_status(job)
                return False

    def list_jobs(
        self,
        status_filter: Optional[JobStatus] = None,
        include_completed: bool = True
    ) -> List[Dict[str, Any]]:
        """
        List all background jobs.

        Args:
            status_filter: If set, only return jobs with this status
            include_completed: If False, exclude completed/failed/killed jobs

        Returns:
            List of job status dictionaries
        """
        result = []

        with self._jobs_lock:
            for job in self._jobs.values():
                self._update_job_status(job)

                if status_filter and job.status != status_filter:
                    continue

                if not include_completed and job.status != JobStatus.RUNNING:
                    continue

                result.append(job.to_dict())

        # Sort by start time (newest first)
        result.sort(key=lambda x: x["start_time"], reverse=True)
        return result

    def get_running_count(self) -> int:
        """Get the number of currently running jobs."""
        with self._jobs_lock:
            count = 0
            for job in self._jobs.values():
                self._update_job_status(job)
                if job.status == JobStatus.RUNNING:
                    count += 1
            return count

    def _cleanup_all(self) -> None:
        """Clean up all running processes on exit."""
        with self._jobs_lock:
            for job in self._jobs.values():
                if job.status == JobStatus.RUNNING:
                    try:
                        if os.name != 'nt':
                            pgid = os.getpgid(job.process.pid)
                            os.killpg(pgid, signal.SIGTERM)
                        else:
                            job.process.terminate()
                    except (ProcessLookupError, OSError):
                        pass

    def cleanup_completed(self, older_than_seconds: int = 3600) -> int:
        """
        Remove completed jobs older than specified age.

        Args:
            older_than_seconds: Remove jobs completed more than this many seconds ago

        Returns:
            Number of jobs cleaned up
        """
        cutoff = datetime.now()
        removed = 0

        with self._jobs_lock:
            to_remove = []
            for job_id, job in self._jobs.items():
                if job.status != JobStatus.RUNNING and job.end_time:
                    age = (cutoff - job.end_time).total_seconds()
                    if age > older_than_seconds:
                        to_remove.append(job_id)

            for job_id in to_remove:
                job = self._jobs.pop(job_id)
                # Optionally remove output files
                job.stdout_file.unlink(missing_ok=True)
                job.stderr_file.unlink(missing_ok=True)
                removed += 1

        return removed
