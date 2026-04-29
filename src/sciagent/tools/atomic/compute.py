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
from pathlib import Path
from typing import Dict, Any, Optional

import yaml


@dataclass
class ToolResult:
    """Result from tool execution."""
    success: bool
    output: Any
    error: Optional[str] = None


# Cache for registry to avoid repeated YAML parsing
_registry_cache: Dict[str, Any] = {}


def _load_service_registry() -> Dict[str, Any]:
    """Load the service registry.yaml and cache it."""
    if _registry_cache:
        return _registry_cache

    # Find registry.yaml relative to this file
    registry_path = Path(__file__).parent.parent.parent / "services" / "registry.yaml"
    if not registry_path.exists():
        return {}

    try:
        with open(registry_path) as f:
            data = yaml.safe_load(f)
            _registry_cache.update(data)
            return _registry_cache
    except Exception:
        return {}


def _get_service_resources(service: str) -> Dict[str, Any]:
    """Get resource hints for a service from the registry.

    Walks the ``extends:`` chain so a leaf that doesn't declare its own
    ``resources:`` inherits the nearest ancestor's hints. Without this, a
    bare ``compute_run(service="openfoam-swak4foam-2012")`` lands on a
    c6i.large because only the root ``openfoam`` declares the OpenFOAM-class
    memory/CPU floor (M0 follow-up #2).

    Merge order (later wins): defaults → root parent → … → immediate parent
    → leaf service. Leaf-level keys override inherited values; missing keys
    fall through to the nearest ancestor that defines them.
    """
    registry = _load_service_registry()

    defaults = registry.get("defaults", {})
    default_resources = defaults.get("resources", {
        "min_memory_gb": 4,
        "recommended_memory_gb": 8,
        "min_cpus": 2,
        "gpu_beneficial": False,
        "gpu_required": False,
    })

    services = registry.get("services", {})

    # Walk extends:-chain from the leaf upward. Stop on missing parent or
    # cycle (registry is hand-edited; trust nothing). chain[0] is the leaf.
    chain: list = []
    seen: set = set()
    cursor = service
    while cursor and cursor not in seen and cursor in services:
        seen.add(cursor)
        chain.append(services[cursor])
        cursor = services[cursor].get("extends")

    merged = dict(default_resources)
    for entry in reversed(chain):
        merged.update(entry.get("resources") or {})
    return merged


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
                "description": (
                    "Number of CPUs. Omit to defer to the service's registry "
                    "hint (or 2 for image-only calls). Pass an explicit value "
                    "to override the hint — including the literal 2 if that's "
                    "what you want."
                )
            },
            "memory_gb": {
                "type": "number",
                "description": (
                    "Memory in GB. Omit to defer to the service's registry "
                    "hint (or 4 for image-only calls). Pass an explicit value "
                    "to override. >16 routes to cloud."
                )
            },
            "gpus": {
                "type": "integer",
                "description": (
                    "Number of GPUs. Omit to defer to the service's registry "
                    "hint (auto-enables 1 GPU for gpu_required services; 0 "
                    "otherwise). Pass 0 to explicitly request CPU-only and "
                    "skip the auto-enable."
                )
            },
            "gpu_type": {
                "type": "string",
                "description": (
                    "GPU type (e.g., 'T4', 'A10G', 'V100', 'A100'). Defaults "
                    "to T4 when GPUs are requested."
                )
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
            "workspace": {
                "type": "boolean",
                "description": "Mount shared workspace bucket (for multi-job workflows on skypilot)",
                "default": False
            },
            "workspace_source": {
                "type": "string",
                "description": (
                    "Source for the workspace mount: a cloud URI like 's3://bucket[/prefix]' "
                    "(reuses that bucket directly) or a local path Sky should sync up. "
                    "Setting this auto-enables the workspace mount on skypilot."
                )
            },
            "session_id": {
                "type": "string",
                "description": "Session ID for workspace bucket (auto-generated if not provided)"
            },
            "intent": {
                "type": "object",
                "description": (
                    "Opaque intent blob recorded in the task manifest verbatim "
                    "(e.g. {paper, case, run} for a reproduction; {} or omitted "
                    "for ad-hoc jobs). Not validated."
                )
            },
            "expected_artifacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Opaque list of expected output paths recorded in the manifest. "
                    "Used by downstream verification; never validated here."
                )
            },
        },
        "required": ["command"]
    }

    # Class-level session ID for workspace sharing across jobs
    _shared_session_id: str = None

    def __init__(self, working_dir: str = ".", session_id: str = None):
        self._working_dir = working_dir
        self._router = None  # Lazy init
        self._session_id = session_id

    def _get_router(self):
        """Lazy init router to avoid import at module load."""
        if self._router is None:
            from sciagent.compute.router import ComputeRouter
            self._router = ComputeRouter()
        return self._router

    def _get_session_id(self, session_id: str = None) -> str:
        """Get or create session ID for workspace sharing."""
        import uuid
        if session_id:
            return session_id
        if self._session_id:
            return self._session_id
        if ComputeTool._shared_session_id:
            return ComputeTool._shared_session_id
        # Generate new session ID
        new_id = uuid.uuid4().hex[:8]
        ComputeTool._shared_session_id = new_id
        return new_id

    @classmethod
    def set_shared_session(cls, session_id: str) -> None:
        """Set the agent-wide session id used for workspace bucket naming."""
        cls._shared_session_id = session_id

    @staticmethod
    def _write_session_manifest(
        job_id: str,
        session_id: Optional[str],
        intent: Optional[Dict[str, Any]],
        expected_artifacts: Optional[list],
        command: str,
        image: Optional[str],
        service: Optional[str],
        timeout_sec: int,
        managed_job_id: Optional[int] = None,
    ) -> None:
        """B7: write the per-job manifest to ~/.sciagent/tasks/<job_id>.json.

        ``managed_job_id`` is the integer Sky assigns to a managed job, when
        we captured it at launch (M1A). When absent (still in-flight after
        the fail-fast budget elapsed), the manifest writes ``null``; later
        status queries can resolve the integer by name and re-write.

        Best-effort: a write failure is logged but not raised. The job is
        already running on Sky; losing the local manifest only means the
        resume + reaper paths won't see it, which is preferable to failing
        the user-visible compute_run on a manifest write error.
        """
        try:
            import os
            from datetime import datetime, timezone
            from sciagent.compute.task_index import write_task

            record: Dict[str, Any] = {
                "job_id": job_id,
                "managed_job_id": managed_job_id,
                "session_id": session_id,
                # intent / expected_artifacts are opaque-by-design (v4.2 §C6).
                "intent": intent,
                "expected_artifacts": list(expected_artifacts) if expected_artifacts else [],
                "owner_pid": os.getpid(),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "command": command,
                "image": image,
                "service": service,
                "timeout_sec": int(timeout_sec) if timeout_sec else 0,
            }
            write_task(record)
        except Exception:
            # Best-effort; never break the launch path on a manifest write.
            pass

    def execute(
        self,
        command: str,
        service: str = None,
        image: str = None,
        cpus: Optional[int] = None,
        memory_gb: Optional[float] = None,
        gpus: Optional[int] = None,
        gpu_type: Optional[str] = None,
        background: bool = True,
        estimate_only: bool = False,
        backend: str = "auto",
        workspace: bool = False,
        workspace_source: str = None,
        session_id: str = None,
        intent: Dict[str, Any] = None,
        expected_artifacts: list = None,
        timeout_sec: int = None,
    ) -> ToolResult:
        """Execute compute job.

        Resource args (cpus / memory_gb / gpus / gpu_type) default to None,
        meaning "no caller preference — use the service's registry hint or
        the ultimate fallback." Pass an explicit value (including the
        literal default-shaped value, e.g. ``cpus=2``) to override the
        registry hint. The earlier value-equality detection conflated
        "didn't specify" with "specified the default value" and silently
        clobbered legitimate explicit calls — fixed in M1A.

        Args:
            command: Command to run in container
            service: Service name from registry (e.g., 'scipy-base', 'openfoam')
            image: Direct Docker image (e.g., 'python:3.11')
            cpus: Number of CPUs. None -> registry hint (or 2). Explicit value
                wins over the hint.
            memory_gb: Memory in GB. None -> registry hint (or 4). Explicit
                value wins; >16 still routes to cloud.
            gpus: Number of GPUs. None -> hint (auto-enables 1 for
                gpu_required services). Explicit 0 means "CPU-only, skip
                the auto-enable."
            gpu_type: GPU type for cloud. None -> 'T4' when gpus > 0.
            background: Run in background (default: True)
            estimate_only: Only show cost estimate (default: False)
            backend: 'auto' (default), 'local', or 'skypilot'
            workspace: Mount shared workspace bucket (default: False)
            workspace_source: Optional source URI/path for the workspace mount
                (e.g. 's3://bucket/prefix' or a local path). When set, the
                workspace mount is auto-enabled on skypilot.
            session_id: Session ID for workspace (auto-generated if not provided)
            intent: Opaque dict recorded in the manifest verbatim (B7).
            expected_artifacts: Opaque list of expected outputs (B7).
            timeout_sec: Max runtime in seconds (B6). Defaults to the
                ComputeRequirements default (3600). Pass 0 to disable the
                on-VM timeout wrapper.

        Returns:
            ToolResult with job_id, status, and cost estimate for cloud jobs
        """
        from sciagent.compute.job import Job, ComputeRequirements, JobStatus, LaunchError

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

        # Resolve image from service and apply resource hints. Hint
        # application uses ``is None`` (caller didn't specify) instead of
        # value-equality against a sentinel default — the M0 code clobbered
        # an explicit ``cpus=2`` / ``memory_gb=4`` / ``gpus=0`` because
        # those happened to match the python-default values the LLM passes
        # by default. Optional defaults make the contract honest.
        gpu_hint = None
        if service:
            resolved_image = f"ghcr.io/sciagent-ai/{service}:latest"
            hints = _get_service_resources(service)

            if memory_gb is None:
                memory_gb = hints.get("recommended_memory_gb", 4)
            if cpus is None:
                cpus = hints.get("min_cpus", 2)
            if gpus is None:
                if hints.get("gpu_required"):
                    gpus = 1
                    gpu_hint = "gpu_required"
                elif hints.get("gpu_beneficial"):
                    gpus = 0  # advisory only, don't auto-enable
                    gpu_hint = "gpu_beneficial"
                else:
                    gpus = 0
        else:
            resolved_image = image
            # No service hints — fall back to the ultimate defaults.
            if memory_gb is None:
                memory_gb = 4
            if cpus is None:
                cpus = 2
            if gpus is None:
                gpus = 0

        # gpu_type only matters when GPUs are actually requested.
        if gpus > 0 and gpu_type is None:
            gpu_type = "T4"

        # Build compute requirements. timeout_sec keeps its existing default
        # (3600s) when caller doesn't override; passing 0 disables the on-VM
        # timeout wrapper (B6 / v4.2 §C2).
        requirements_kwargs: Dict[str, Any] = {
            "cpus": cpus,
            "memory_gb": memory_gb,
            "gpus": gpus,
            "gpu_type": gpu_type if gpus > 0 else None,
        }
        if timeout_sec is not None:
            requirements_kwargs["timeout_sec"] = int(timeout_sec)
        requirements = ComputeRequirements(**requirements_kwargs)

        # Add workspace storage mount if requested (skypilot only).
        # workspace_source implies workspace=True so callers can pass a single
        # arg (`workspace_source="s3://…"`) without also setting `workspace=True`.
        wants_workspace = workspace or bool(workspace_source)
        actual_session_id = None
        if wants_workspace and (backend == "skypilot" or (backend == "auto" and gpus > 0)):
            actual_session_id = self._get_session_id(session_id)
            try:
                router = self._get_router()
                if "skypilot" in router.list_backends():
                    skypilot_backend = router._backends["skypilot"]
                    workspace_mount = skypilot_backend.get_workspace_mount(
                        actual_session_id,
                        workspace_source=workspace_source,
                    )
                    requirements.storage = [workspace_mount]
            except Exception:
                pass  # Fall back to no workspace if unavailable

        # Build job
        job = Job(
            service=service or "custom",
            image=resolved_image,
            command=command,
            working_dir=self._working_dir,
            requirements=requirements,
        )

        try:
            router = self._get_router()

            # Select backend and get cost estimate
            preferred = backend if backend != "auto" else None
            selected_backend, routing_reason = router.select(job.requirements, preferred=preferred)
            cost_estimate = router.estimate_cost(job, duration_hours=1.0)

            # If estimate_only, return cost without running
            if estimate_only:
                output = {
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
                if gpu_hint:
                    output["gpu_hint"] = gpu_hint
                    if gpu_hint == "gpu_beneficial":
                        output["gpu_note"] = f"Service '{service}' benefits from GPU (5-13x speedup). Add gpus=1 for better performance."
                return ToolResult(success=True, output=output)

            # Run the job. A LaunchError surfaced from the backend's fail-fast
            # poll (B4) means Sky rejected the launch outright — return a
            # structured failure now instead of letting the agent burn a
            # 10-min status-poll loop. We call the selected backend directly
            # (not router.run) so SkyPilotBackend's tuple return — which
            # carries the integer managed_job_id when the controller
            # acknowledged the launch inside the fail-fast budget — flows
            # into the manifest write.
            managed_job_id: Optional[int] = None
            try:
                run_result = selected_backend.run(job, background=background)
            except LaunchError as launch_exc:
                # cluster_name is set when the failure came from the SkyPilot
                # backend; propagate it so callers (and our paid AWS tests)
                # can clean up a partially-provisioned cluster instead of
                # leaving it billing on the cloud.
                rejected_output = {
                    "service": service,
                    "image": resolved_image,
                    "command": command[:100],
                    "backend_attempted": backend,
                    "failure_type": "launch_rejected",
                }
                if launch_exc.cluster_name:
                    rejected_output["job_id"] = launch_exc.cluster_name
                return ToolResult(
                    success=False,
                    output=rejected_output,
                    error=f"sky.launch rejected: {launch_exc}",
                )

            # SkyPilotBackend.run returns (name, managed_job_id); local
            # backends return a bare str. Unify here so the rest of execute
            # is backend-agnostic.
            if isinstance(run_result, tuple):
                job_id = run_result[0]
                if len(run_result) >= 2:
                    managed_job_id = run_result[1]
            else:
                job_id = run_result

            # B7: after a successful skypilot launch, write a session manifest
            # so bg_status (PR #3 join) and the orphan sweep / reaper can find
            # the job after a process restart. Local jobs are tracked by
            # ProcessManager already; no double-bookkeeping for them.
            if selected_backend.name == "skypilot":
                self._write_session_manifest(
                    job_id=job_id,
                    session_id=actual_session_id,
                    intent=intent,
                    expected_artifacts=expected_artifacts,
                    command=command,
                    image=resolved_image,
                    service=service,
                    timeout_sec=requirements.timeout_sec,
                    managed_job_id=managed_job_id,
                )

            if background:
                # Token-light response for background jobs
                output = {
                    "job_id": job_id,
                    "status": "running",
                    "backend": selected_backend.name,
                    "routing_reason": routing_reason,
                    "cost_estimate": cost_estimate,
                    "image": resolved_image,
                    "resources_used": {
                        "cpus": cpus,
                        "memory_gb": memory_gb,
                        "gpus": gpus,
                    },
                    "message": f"Job {job_id} started. Check with bg_status('{job_id}')",
                }
                # Add GPU hint if applicable
                if gpu_hint == "gpu_beneficial":
                    output["gpu_hint"] = f"Service '{service}' benefits from GPU. Consider adding gpus=1 for 5-13x speedup."
                # Add workspace info if enabled. Read bucket/mount_path from the
                # actual StorageMount we attached so cloud-URI workspace_source
                # values (which override the synthesized bucket name) report
                # honestly.
                if actual_session_id and requirements.storage:
                    mount = requirements.storage[0]
                    workspace_info = {
                        "session_id": actual_session_id,
                        "bucket": mount.bucket,
                        "mount_path": mount.path,
                        "cleanup_hint": f"sky storage delete {mount.bucket}",
                    }
                    if workspace_source:
                        workspace_info["source"] = workspace_source
                    output["workspace"] = workspace_info
                    output["message"] += f" Workspace mounted at {mount.path} (session: {actual_session_id})"
                return ToolResult(success=True, output=output)
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


def get_tool(working_dir: str = ".", session_id: str = None) -> ComputeTool:
    """Factory function for tool discovery."""
    return ComputeTool(working_dir, session_id=session_id)
