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
            "cpus": {
                "type": "integer",
                "description": "Number of CPUs",
                "default": 2
            },
            "memory_gb": {
                "type": "number",
                "description": "Memory in GB (>16 routes to cloud)",
                "default": 4
            },
            "gpus": {
                "type": "integer",
                "description": "Number of GPUs (0 for CPU only)",
                "default": 0
            },
            "gpu_type": {
                "type": "string",
                "description": "GPU type (e.g., 'T4', 'A10G', 'V100', 'A100')",
                "default": "T4"
            },
            "background": {
                "type": "boolean",
                "description": "Run in background (default: true)",
                "default": True
            },
            "estimate_only": {
                "type": "boolean",
                "description": "Only estimate cost, don't run job",
                "default": False
            },
            "backend": {
                "type": "string",
                "enum": ["local", "skypilot", "auto"],
                "description": "Backend: 'auto' (default) routes based on resources, 'local' for Docker, 'skypilot' for cloud",
                "default": "auto"
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
        cpus: int = 2,
        memory_gb: float = 4,
        gpus: int = 0,
        gpu_type: str = "T4",
        background: bool = True,
        estimate_only: bool = False,
        backend: str = "auto",
    ) -> ToolResult:
        """Execute compute job.

        Args:
            command: Command to run in container
            service: Service name from registry (e.g., 'scipy-base', 'openfoam')
            image: Direct Docker image (e.g., 'python:3.11')
            cpus: Number of CPUs (default: 2, >8 routes to cloud)
            memory_gb: Memory in GB (default: 4, >16 routes to cloud)
            gpus: Number of GPUs (default: 0, >0 routes to cloud)
            gpu_type: GPU type for cloud (default: T4)
            background: Run in background (default: True)
            estimate_only: Only show cost estimate (default: False)
            backend: 'auto' (default), 'local', or 'skypilot'

        Returns:
            ToolResult with job_id, status, and cost estimate for cloud jobs
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
                cpus=cpus,
                memory_gb=memory_gb,
                gpus=gpus,
                gpu_type=gpu_type if gpus > 0 else None,
            ),
        )

        try:
            router = self._get_router()

            # Select backend and get cost estimate
            preferred = backend if backend != "auto" else None
            selected_backend, routing_reason = router.select(job.requirements, preferred=preferred)
            cost_estimate = router.estimate_cost(job, duration_hours=1.0)

            # If estimate_only, return cost without running
            if estimate_only:
                return ToolResult(
                    success=True,
                    output={
                        "backend": selected_backend.name,
                        "routing_reason": routing_reason,
                        "cost_estimate": cost_estimate,
                        "resources": {
                            "cpus": cpus,
                            "memory_gb": memory_gb,
                            "gpus": gpus,
                            "gpu_type": gpu_type if gpus > 0 else None,
                        },
                        "image": resolved_image,
                    }
                )

            # Run the job
            job_id = router.run(job, backend=preferred, background=background)

            if background:
                # Token-light response for background jobs
                return ToolResult(
                    success=True,
                    output={
                        "job_id": job_id,
                        "status": "running",
                        "backend": selected_backend.name,
                        "routing_reason": routing_reason,
                        "cost_estimate": cost_estimate,
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
                        "job_id": job_id,
                        "status": result.status.value,
                        "backend": selected_backend.name,
                        "cost_estimate": cost_estimate,
                        "summary": result.summary,
                        "output_preview": result.output_preview,
                        "output_file": result.output_file,
                    },
                    error=result.error_preview if result.status == JobStatus.FAILED else None,
                )

        except Exception as e:
            error_msg = str(e) if str(e) else f"{type(e).__name__}: (no message)"
            return ToolResult(
                success=False,
                output={
                    "service": service,
                    "image": resolved_image,
                    "command": command[:100],
                    "backend_attempted": backend,
                },
                error=f"Compute job failed: {error_msg}"
            )

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
