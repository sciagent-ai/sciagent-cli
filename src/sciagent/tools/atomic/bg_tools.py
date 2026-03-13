"""
Background job management tools.

Tools for managing background processes launched via bash(background=True):
- bg_status: Check status of background jobs
- bg_output: Get output from a background job
- bg_wait: Wait for a background job to complete
- bg_kill: Terminate a background job
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any, Optional, List


@dataclass
class ToolResult:
    """Result from tool execution."""
    success: bool
    output: Any
    error: Optional[str] = None


class BgStatusTool:
    """Check status of background jobs."""

    name = "bg_status"
    description = "Check status of background jobs. Call with no args to list all jobs, or with job_id to check a specific job."

    parameters = {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "Specific job ID to check. If omitted, lists all jobs."
            },
            "running_only": {
                "type": "boolean",
                "description": "If true, only show running jobs.",
                "default": False
            }
        },
        "required": []
    }

    def __init__(self, working_dir: str = "."):
        self.working_dir = working_dir

    def execute(self, job_id: str = None, running_only: bool = False) -> ToolResult:
        """Get status of background jobs."""
        from sciagent.process_manager import ProcessManager, JobStatus

        try:
            pm = ProcessManager.get_instance()

            if job_id:
                # Get specific job status
                status = pm.get_status(job_id)
                if status is None:
                    return ToolResult(
                        success=False,
                        output=None,
                        error=f"Job '{job_id}' not found. Use bg_status() to list all jobs."
                    )

                output = self._format_job_status(status)
                return ToolResult(success=True, output=output, error=None)

            else:
                # List all jobs
                jobs = pm.list_jobs(include_completed=not running_only)

                if not jobs:
                    return ToolResult(
                        success=True,
                        output="No background jobs found.",
                        error=None
                    )

                output = self._format_job_list(jobs)
                return ToolResult(success=True, output=output, error=None)

        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def _format_job_status(self, status: Dict[str, Any]) -> str:
        """Format a single job status for display."""
        lines = [
            f"Job ID: {status['job_id']}",
            f"Status: {status['status']}",
            f"Command: {status['command'][:100]}{'...' if len(status['command']) > 100 else ''}",
            f"Working Dir: {status['working_dir']}",
            f"Started: {status['start_time']}",
        ]

        if status.get('end_time'):
            lines.append(f"Ended: {status['end_time']}")

        if status.get('exit_code') is not None:
            lines.append(f"Exit Code: {status['exit_code']}")

        if status.get('pid'):
            lines.append(f"PID: {status['pid']}")

        lines.extend([
            f"",
            f"Output files:",
            f"  stdout: {status['stdout_file']}",
            f"  stderr: {status['stderr_file']}",
        ])

        return '\n'.join(lines)

    def _format_job_list(self, jobs: List[Dict[str, Any]]) -> str:
        """Format a list of jobs for display."""
        lines = [f"Background Jobs ({len(jobs)} total):", ""]

        # Group by status
        running = [j for j in jobs if j['status'] == 'running']
        completed = [j for j in jobs if j['status'] == 'completed']
        failed = [j for j in jobs if j['status'] == 'failed']
        killed = [j for j in jobs if j['status'] == 'killed']

        if running:
            lines.append(f"RUNNING ({len(running)}):")
            for job in running:
                cmd_preview = job['command'][:50] + '...' if len(job['command']) > 50 else job['command']
                lines.append(f"  [{job['job_id']}] {cmd_preview}")
            lines.append("")

        if completed:
            lines.append(f"COMPLETED ({len(completed)}):")
            for job in completed[:5]:  # Show last 5
                cmd_preview = job['command'][:50] + '...' if len(job['command']) > 50 else job['command']
                lines.append(f"  [{job['job_id']}] exit={job['exit_code']} {cmd_preview}")
            if len(completed) > 5:
                lines.append(f"  ... and {len(completed) - 5} more")
            lines.append("")

        if failed:
            lines.append(f"FAILED ({len(failed)}):")
            for job in failed[:5]:
                cmd_preview = job['command'][:50] + '...' if len(job['command']) > 50 else job['command']
                lines.append(f"  [{job['job_id']}] exit={job['exit_code']} {cmd_preview}")
            if len(failed) > 5:
                lines.append(f"  ... and {len(failed) - 5} more")
            lines.append("")

        if killed:
            lines.append(f"KILLED ({len(killed)}):")
            for job in killed[:3]:
                cmd_preview = job['command'][:50] + '...' if len(job['command']) > 50 else job['command']
                lines.append(f"  [{job['job_id']}] {cmd_preview}")
            lines.append("")

        return '\n'.join(lines)

    def to_schema(self) -> Dict:
        """Convert to OpenAI-style tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


class BgOutputTool:
    """Get output from a background job."""

    name = "bg_output"
    description = "Get stdout/stderr output from a background job. Use tail_lines to get only recent output."

    parameters = {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "The job ID to get output from"
            },
            "stream": {
                "type": "string",
                "enum": ["stdout", "stderr", "both"],
                "description": "Which output stream to retrieve",
                "default": "stdout"
            },
            "tail_lines": {
                "type": "integer",
                "description": "Only return the last N lines. If omitted, returns all output.",
                "default": None
            }
        },
        "required": ["job_id"]
    }

    def __init__(self, working_dir: str = "."):
        self.working_dir = working_dir

    def execute(
        self,
        job_id: str = None,
        stream: str = "stdout",
        tail_lines: int = None
    ) -> ToolResult:
        """Get output from a background job."""
        from sciagent.process_manager import ProcessManager

        if not job_id:
            return ToolResult(
                success=False,
                output=None,
                error="job_id is required. Use bg_status() to list available jobs."
            )

        try:
            pm = ProcessManager.get_instance()

            # Check job exists
            status = pm.get_status(job_id)
            if status is None:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Job '{job_id}' not found."
                )

            # Get output
            if stream == "both":
                stdout = pm.get_output(job_id, "stdout", tail_lines)
                stderr = pm.get_output(job_id, "stderr", tail_lines)

                output_parts = []
                if stdout and stdout.strip():
                    output_parts.append(f"=== STDOUT ===\n{stdout}")
                if stderr and stderr.strip():
                    output_parts.append(f"=== STDERR ===\n{stderr}")

                if not output_parts:
                    output = "(no output yet)"
                else:
                    output = "\n\n".join(output_parts)
            else:
                output = pm.get_output(job_id, stream, tail_lines)
                if not output or not output.strip():
                    output = f"(no {stream} output yet)"

            # Add status info
            status_line = f"\n\n[Job {job_id}: {status['status']}]"
            if status['status'] == 'running':
                status_line += " (still running, output may be incomplete)"

            return ToolResult(
                success=True,
                output=output + status_line,
                error=None
            )

        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def to_schema(self) -> Dict:
        """Convert to OpenAI-style tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


class BgWaitTool:
    """Wait for a background job to complete."""

    name = "bg_wait"
    description = "Wait for a background job to complete. Returns final status and exit code."

    parameters = {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "The job ID to wait for"
            },
            "timeout": {
                "type": "integer",
                "description": "Maximum seconds to wait. If omitted, waits indefinitely.",
                "default": None
            }
        },
        "required": ["job_id"]
    }

    def __init__(self, working_dir: str = "."):
        self.working_dir = working_dir

    def execute(self, job_id: str = None, timeout: int = None) -> ToolResult:
        """Wait for a background job to complete."""
        from sciagent.process_manager import ProcessManager

        if not job_id:
            return ToolResult(
                success=False,
                output=None,
                error="job_id is required."
            )

        try:
            pm = ProcessManager.get_instance()

            # Check job exists
            initial_status = pm.get_status(job_id)
            if initial_status is None:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Job '{job_id}' not found."
                )

            # If already completed, return immediately
            if initial_status['status'] != 'running':
                output = self._format_completion(initial_status)
                return ToolResult(success=True, output=output, error=None)

            # Wait for completion
            final_status = pm.wait(job_id, timeout=timeout)

            if final_status['status'] == 'running':
                # Timeout occurred
                return ToolResult(
                    success=True,
                    output=f"Timeout after {timeout}s. Job {job_id} is still running.\n"
                           f"Use bg_wait(job_id=\"{job_id}\") to continue waiting.",
                    error=None
                )

            output = self._format_completion(final_status)
            return ToolResult(success=True, output=output, error=None)

        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def _format_completion(self, status: Dict[str, Any]) -> str:
        """Format job completion info."""
        lines = [
            f"Job {status['job_id']} {status['status']}",
            f"",
            f"Exit Code: {status.get('exit_code', 'unknown')}",
            f"Started: {status['start_time']}",
            f"Ended: {status.get('end_time', 'unknown')}",
            f"",
            f"Command: {status['command'][:100]}{'...' if len(status['command']) > 100 else ''}",
            f"",
            f"Use bg_output(job_id=\"{status['job_id']}\") to view full output.",
        ]
        return '\n'.join(lines)

    def to_schema(self) -> Dict:
        """Convert to OpenAI-style tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


class BgKillTool:
    """Terminate a background job."""

    name = "bg_kill"
    description = "Terminate a running background job. Use force=True for SIGKILL instead of SIGTERM."

    parameters = {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "The job ID to terminate"
            },
            "force": {
                "type": "boolean",
                "description": "Use SIGKILL instead of SIGTERM (immediate termination)",
                "default": False
            }
        },
        "required": ["job_id"]
    }

    def __init__(self, working_dir: str = "."):
        self.working_dir = working_dir

    def execute(self, job_id: str = None, force: bool = False) -> ToolResult:
        """Terminate a background job."""
        from sciagent.process_manager import ProcessManager

        if not job_id:
            return ToolResult(
                success=False,
                output=None,
                error="job_id is required."
            )

        try:
            pm = ProcessManager.get_instance()

            # Check job exists
            status = pm.get_status(job_id)
            if status is None:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Job '{job_id}' not found."
                )

            if status['status'] != 'running':
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Job '{job_id}' is not running (status: {status['status']})"
                )

            # Kill the job
            killed = pm.kill(job_id, force=force)

            if killed:
                signal_type = "SIGKILL" if force else "SIGTERM"
                output = (
                    f"Job {job_id} terminated with {signal_type}.\n"
                    f"\n"
                    f"Command: {status['command'][:100]}{'...' if len(status['command']) > 100 else ''}\n"
                    f"PID: {status.get('pid', 'unknown')}\n"
                    f"\n"
                    f"Use bg_output(job_id=\"{job_id}\") to view any output before termination."
                )
                return ToolResult(success=True, output=output, error=None)
            else:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Failed to kill job '{job_id}'. It may have already completed."
                )

        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def to_schema(self) -> Dict:
        """Convert to OpenAI-style tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


# Factory functions for tool discovery
def get_bg_status_tool(working_dir: str = ".") -> BgStatusTool:
    """Factory for bg_status tool."""
    return BgStatusTool(working_dir)


def get_bg_output_tool(working_dir: str = ".") -> BgOutputTool:
    """Factory for bg_output tool."""
    return BgOutputTool(working_dir)


def get_bg_wait_tool(working_dir: str = ".") -> BgWaitTool:
    """Factory for bg_wait tool."""
    return BgWaitTool(working_dir)


def get_bg_kill_tool(working_dir: str = ".") -> BgKillTool:
    """Factory for bg_kill tool."""
    return BgKillTool(working_dir)
