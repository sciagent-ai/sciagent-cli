"""
Compute tool for container-based job execution.

Token-conscious design:
1. Returns job_id immediately (background by default)
2. Summary instead of full output
3. Output written to file, path returned
4. Structured JSON, not prose

Use existing bg_status, bg_wait, bg_output, bg_kill for job management.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any, Optional


@dataclass
class ToolResult:
    """Result from tool execution."""
    success: bool
    output: Any
    error: Optional[str] = None


class ComputeTool:
    """
    Token-conscious compute tool.

    Runs containerized compute jobs. Background by default.
    Uses EITHER service (from registry) OR image (direct Docker image).

    Examples:
        compute_run(service="scipy-base", command="python3 -c 'print(1+1)'")
        compute_run(image="python:3.11", command="python -c 'import sys; print(sys.version)'")

    Returns job_id immediately. Check status with bg_status(job_id).
    For long jobs, use bg_wait(job_id) to block until complete.
    """

    name = "compute_run"
    description = """Run a compute job in a container. Background by default.

Use EITHER service (from registry) OR image (direct Docker image).

Examples:
  compute_run(service="scipy-base", command="python3 -c 'print(1+1)'")
  compute_run(image="python:3.11", command="python -c 'import sys; print(sys.version)'")

Returns job_id. Check status with bg_status(job_id).
For long jobs, use bg_wait(job_id) to block until complete."""

    parameters = {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": "Service from registry (e.g., 'openfoam', 'scipy-base')"
            },
            "image": {
                "type": "string",
                "description": "Direct Docker image (e.g., 'python:3.11')"
            },
            "command": {
                "type": "string",
                "description": "Command to run in container"
            },
            "memory_gb": {
                "type": "number",
                "description": "Memory requirement in GB",
                "default": 4
            },
            "gpus": {
                "type": "integer",
                "description": "Number of GPUs (0 for CPU only)",
                "default": 0
            },
            "background": {
                "type": "boolean",
                "description": "Run in background (default: true)",
                "default": True
            },
        },
        "required": ["command"]
    }

    def __init__(self, working_dir: str = "."):
        self._working_dir = working_dir
        self._router = None  # Lazy init

    def _get_router(self):
        """Lazy init router to avoid import at module load."""
        if self._router is None:
            from sciagent.compute.router import ComputeRouter
            self._router = ComputeRouter()
        return self._router

    def execute(
        self,
        command: str,
        service: str = None,
        image: str = None,
        memory_gb: float = 4,
        gpus: int = 0,
        background: bool = True,
    ) -> ToolResult:
        """Execute compute job.

        Args:
            command: Command to run in container
            service: Service name from registry (e.g., 'scipy-base', 'openfoam')
            image: Direct Docker image (e.g., 'python:3.11')
            memory_gb: Memory requirement in GB (default: 4)
            gpus: Number of GPUs (default: 0)
            background: Run in background (default: True)

        Returns:
            ToolResult with job_id and status
        """
        from sciagent.compute.job import Job, ComputeRequirements, JobStatus

        # Validate: need service OR image
        if not service and not image:
            return ToolResult(
                success=False,
                output=None,
                error="Must specify either 'service' or 'image'"
            )
        if service and image:
            return ToolResult(
                success=False,
                output=None,
                error="Specify 'service' OR 'image', not both"
            )

        # Resolve image from service
        if service:
            resolved_image = f"ghcr.io/sciagent-ai/{service}:latest"
        else:
            resolved_image = image

        # Build job
        job = Job(
            service=service or "custom",
            image=resolved_image,
            command=command,
            working_dir=self._working_dir,
            requirements=ComputeRequirements(
                memory_gb=memory_gb,
                gpus=gpus,
            ),
        )

        try:
            router = self._get_router()
            job_id = router.run(job, background=background)

            if background:
                # Token-light response for background jobs
                return ToolResult(
                    success=True,
                    output={
                        "job_id": job_id,
                        "status": "running",
                        "image": resolved_image,
                        "message": f"Job {job_id} started. Check with bg_status('{job_id}')",
                    }
                )
            else:
                # Foreground - wait and return result
                result = router.get_status(job_id)
                return ToolResult(
                    success=result.status == JobStatus.COMPLETED,
                    output={
                        "status": result.status.value,
                        "summary": result.summary,
                        "output_preview": result.output_preview,
                        "output_file": result.output_file,
                    },
                    error=result.error_preview if result.status == JobStatus.FAILED else None,
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


def get_tool(working_dir: str = ".") -> ComputeTool:
    """Factory function for tool discovery."""
    return ComputeTool(working_dir)
