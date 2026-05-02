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

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, List, Optional, Union

import yaml


# Path-contract validation: agent-visible absolute paths. References to
# these in the user command must match a declared mount; otherwise the
# command is launched into an empty path and silently produces nothing.
# Strict whitelisting of every absolute path would reject /usr/bin/python
# and /tmp/foo — we only watch the paths the agent uses for inputs.
_WATCHED_INPUT_PATHS: tuple = ("/workspace", "/data", "/inputs")

# Always-allowed (output mount is auto-mounted; OUTPUTS_DIR env var is
# exported by the prologue).
_OUTPUTS_PATH: str = "/outputs"

# Forbidden — internal SkyPilot rsync target.
_FORBIDDEN_PATTERN = re.compile(r"~?/?sky_workdir(/|\s|\"|'|$|\\b)")

# Watched-path matcher. Captures the path so we can echo it in the error.
_WATCHED_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_/])(/(?:workspace|data|inputs)(?:/[^\s\"';|&]*)?)"
)


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

Path contract (image-agnostic, identical across every registry image):
  - Inputs (read): pass workspace_source= to mount buckets/dirs.
      Single source     -> "s3://bucket/prefix" (mounts at /workspace/)
      Multi-source      -> [{"path": "/workspace", "source": "s3://q/"},
                            {"path": "/data/nr",  "source": "gs://nr/"}]
      Any cloud SkyPilot supports works (s3, gs, az, r2, oci) — the
      agent never has to pick or hardcode.
  - Outputs (write): write to $OUTPUTS_DIR (= /outputs/<job_id>/) — always
      mounted, always isolated by job, auto-fetched by bg_wait on terminal
      status. Cross-job reads in the same session: /outputs/<other-job-id>/.
  - Local code: pass workdir=<path> to rsync a local dir to the cluster.
      CWD becomes ~/sky_workdir/. Without this, no rsync — image's WORKDIR
      is honored.
  - Never reference ~/sky_workdir/ — it's internal SkyPilot.

CWD precedence: input mount > workdir= rsync target > image WORKDIR.
The compute layer never invents a CWD.

Honor user intent on backend: if the user named a target ("on sky", "on
skypilot", "in the cloud", "on AWS"), pass backend="skypilot" explicitly —
do NOT leave it as "auto" and hope the router picks cloud. Same the other
way: "run locally" / "use Docker" → backend="local". Only fall back to
"auto" when the user didn't specify.

Examples:
  compute_run(service="scipy-base", command="python3 -c 'print(1+1)'")
  compute_run(service="openfoam", workspace_source="/local/case",
              command="bash Allrun && cp -r postProcessing $OUTPUTS_DIR/")
  compute_run(service="scipy-base",
              workspace_source=[{"path":"/workspace","source":"s3://q/"},
                                {"path":"/data/nr","source":"gs://nr/"}],
              command="blastn -query query.fa -db /data/nr/nr -out $OUTPUTS_DIR/hits.txt",
              backend="skypilot")

Returns job_id. Check status with bg_status(job_id).
For long jobs, use bg_wait(job_id) to block until complete.

For skypilot jobs, bg_wait auto-pulls /outputs/<job_id>/ from the cloud
to your local working dir on success — the file paths come back in
bg_wait's result. Don't launch extra cloud jobs to `cat` files back."""

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
                "type": ["boolean", "null"],
                "description": (
                    "Deprecated no-op; outputs are now always auto-mounted "
                    "at /outputs/<job_id>/ for skypilot jobs."
                ),
                "default": None
            },
            "workspace_source": {
                "description": (
                    "Input mount(s). Single string is a cloud URI or local "
                    "path mounted at /workspace/. List form mounts each entry "
                    "at its declared path: [{path, source}, ...]."
                )
            },
            "workdir": {
                "type": "string",
                "description": (
                    "Local directory to rsync to the cluster via SkyPilot. "
                    "When set, CWD becomes ~/sky_workdir/. Default: no rsync."
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
        outputs_uri: Optional[str] = None,
        outputs_prefix: Optional[str] = None,
        mounts: Optional[List[Dict[str, str]]] = None,
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
                # PR1 (consolidation): kind discriminator + lifecycle state.
                # kind=compute_job marks this as a cloud compute manifest in
                # the broader in-flight registry; state starts at "running"
                # because the launch has just succeeded by the time we get
                # here, and bg_wait/bg_kill drive transitions afterwards.
                "kind": "compute_job",
                "state": "running",
                "completed_at": None,
                "result_summary": None,
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
                # Workspace contract additions (cloud-agnostic). outputs_uri
                # carries the cloud identity through to fetch time so
                # compute_fetch.py can dispatch to the right CLI without
                # re-reading SkyPilot config. outputs_prefix is the bucket-
                # side path. mounts is the declared input mount list at
                # launch (used for validation read-back / debug).
                "outputs_uri": outputs_uri,
                "outputs_prefix": outputs_prefix,
                "mounts": list(mounts) if mounts else [],
            }
            write_task(record)
        except Exception:
            # Best-effort; never break the launch path on a manifest write.
            pass

    @staticmethod
    def _validate_path_contract(
        command: str,
        declared_input_paths: List[str],
    ) -> Optional[str]:
        """Return an error message when the command references a path that
        violates the input/output contract, else None.

        Two hard rules:
          1. ``~/sky_workdir/`` (or ``sky_workdir/`` anywhere) → forbidden;
             it's internal SkyPilot.
          2. Watched input paths (/workspace/, /data/, /inputs/) referenced
             without a covering declared mount → forbidden; the path is
             empty on the cluster and the command would silently produce
             nothing.

        ``/outputs/`` is always allowed (auto-mounted).
        """
        if _FORBIDDEN_PATTERN.search(command):
            return (
                "Command references ~/sky_workdir/ but that's internal "
                "SkyPilot. For inputs pass workspace_source=. For outputs "
                "write to $OUTPUTS_DIR or /outputs/<job_id>/."
            )

        # Build allowed-prefix set: declared mounts + the always-on output
        # mount. A path matches if any allowed prefix is its prefix.
        allowed_prefixes = list(declared_input_paths) + [_OUTPUTS_PATH]
        for match in _WATCHED_PATTERN.finditer(command):
            referenced = match.group(1).rstrip("/")
            covered = False
            for prefix in allowed_prefixes:
                norm_prefix = prefix.rstrip("/")
                if (
                    referenced == norm_prefix
                    or referenced.startswith(norm_prefix + "/")
                ):
                    covered = True
                    break
            if not covered:
                return (
                    f"Command references {referenced} but no matching input "
                    f"mount was declared. Pass workspace_source=[{{path: "
                    f"'{referenced.rsplit('/', 1)[0] or '/workspace'}', "
                    f"source: '...'}}, ...], or use $OUTPUTS_DIR for outputs."
                )
        return None

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
        workspace: Optional[bool] = None,
        workspace_source: Optional[Union[str, list, dict]] = None,
        workdir: Optional[str] = None,
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
            workspace: Mount a persistent workspace bucket. None (default) =>
                auto-on for skypilot jobs so bg_wait can auto-pull outputs
                back to local; True forces on; False forces off.
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

        # New mount layout (cloud-agnostic):
        #   - Output mount at /outputs/<job_id>/ — ALWAYS attached for
        #     skypilot jobs. Auto-fetched by bg_wait on terminal status.
        #   - Input mounts at caller-declared paths — built from
        #     workspace_source= (str or list[{path, source}]).
        # The legacy `workspace` boolean is now a no-op; outputs are always
        # mounted. Pass-through preserved for back-compat callers.
        actual_session_id: Optional[str] = None
        outputs_uri_for_manifest: Optional[str] = None
        outputs_prefix_for_manifest: Optional[str] = None
        mounts_for_manifest: List[Dict[str, str]] = []

        will_attach_mounts = backend == "skypilot" or (
            backend == "auto" and gpus > 0
        )

        # Resolve a relative workdir= to absolute against the agent's
        # project dir before handing it to SkyPilot. Sky rejects relative
        # paths with "Workdir must be a valid directory", and the agent
        # naturally reaches for relative ("_outputs", "."). Without this,
        # the agent burns a turn on a structured-error retry.
        # Use .absolute() (not .resolve()) so symlinks like /tmp -> /private/tmp
        # on macOS aren't followed — the caller's path stays as-written.
        if workdir is not None:
            workdir_path = Path(workdir)
            if not workdir_path.is_absolute():
                workdir_path = (Path(self._working_dir) / workdir_path).absolute()
            workdir = str(workdir_path)

        # Validate workspace_source shape early so a malformed list shows
        # a structured error instead of crashing inside the backend.
        try:
            from sciagent.compute.backends.skypilot import (
                _normalize_workspace_source as _normalize_ws,
            )
            normalized_inputs = _normalize_ws(workspace_source)
        except ValueError as e:
            return ToolResult(
                success=False,
                output={"error_kind": "path_contract", "field": "workspace_source"},
                error=f"Invalid workspace_source: {e}",
            )

        # Path-contract validation (fail-fast, before the backend launch).
        # Only enforced for skypilot — local Docker has its own filesystem
        # semantics that don't share /workspace, /outputs, or ~/sky_workdir.
        if will_attach_mounts:
            declared_paths = [entry["path"] for entry in normalized_inputs]
            err = self._validate_path_contract(command, declared_paths)
            if err is not None:
                return ToolResult(
                    success=False,
                    output={
                        "error_kind": "path_contract",
                        "command": command[:200],
                        "declared_inputs": declared_paths,
                    },
                    error=err,
                )

        if will_attach_mounts:
            actual_session_id = self._get_session_id(session_id)
            try:
                router = self._get_router()
                if "skypilot" in router.list_backends():
                    skypilot_backend = router._backends["skypilot"]
                    storage_list = []

                    # Always-on output mount.
                    outputs_mount = skypilot_backend.build_outputs_mount(
                        actual_session_id
                    )
                    if outputs_mount is not None:
                        storage_list.append(outputs_mount)

                    # Input mounts, if any.
                    input_mounts = skypilot_backend.build_input_mounts(
                        normalized_inputs,
                        session_id=actual_session_id,
                    )
                    storage_list.extend(input_mounts)

                    if storage_list:
                        requirements.storage = storage_list

                    # Build the manifest's outputs_uri/prefix now (the
                    # job_id isn't known yet — we patch it post-launch).
                    if outputs_mount is not None:
                        from sciagent.compute.backends.skypilot import (
                            _build_outputs_uri as _bld_uri,
                        )
                        outputs_prefix_for_manifest = "{job_id}/"
                        outputs_uri_for_manifest = _bld_uri(
                            outputs_mount.store,
                            outputs_mount.bucket,
                            "{job_id}/",
                        )

                    mounts_for_manifest = [
                        {"path": m.path, "source": m.source or ""}
                        for m in input_mounts
                    ]
                else:
                    actual_session_id = None
            except Exception:
                actual_session_id = None  # Fall back to no workspace

        # Build job. The backend cd's into the workspace mount (when one is
        # attached) before running the command — see SkyPilotBackend._build_task
        # for the rationale (M0 follow-up #1).
        #
        # M1B: session_id / intent / expected_artifacts are forwarded to Job
        # so the SkyPilot backend can emit a compute_job_launched event that
        # carries the v4.2 §C6 opaque payloads. They are recorded by the
        # backend verbatim. session_id falls back to ComputeTool's shared
        # session (set by the agent at startup); standalone callers without
        # an agent see None and the backend skips emission.
        session_for_job = actual_session_id or ComputeTool._shared_session_id
        job = Job(
            service=service or "custom",
            image=resolved_image,
            command=command,
            working_dir=self._working_dir,
            ship_workdir=workdir,
            requirements=requirements,
            session_id=session_for_job,
            intent=intent,
            expected_artifacts=expected_artifacts,
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
                # Substitute the launched job_id into the URI/prefix templates
                # built before launch (job_id is assigned by the backend).
                resolved_outputs_uri = (
                    outputs_uri_for_manifest.replace("{job_id}", job_id)
                    if outputs_uri_for_manifest else None
                )
                resolved_outputs_prefix = (
                    outputs_prefix_for_manifest.replace("{job_id}", job_id)
                    if outputs_prefix_for_manifest else None
                )
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
                    outputs_uri=resolved_outputs_uri,
                    outputs_prefix=resolved_outputs_prefix,
                    mounts=mounts_for_manifest,
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
                # Add workspace info — names every attached mount so the
                # caller can see what's at /workspace/, /outputs/, /data/...,
                # and which source URI populated each (cloud-agnostic).
                if actual_session_id and requirements.storage:
                    output_mounts: List[Dict[str, str]] = []
                    input_mounts_info: List[Dict[str, str]] = []
                    for m in requirements.storage:
                        info = {
                            "path": m.path,
                            "bucket": m.bucket,
                            "store": m.store,
                        }
                        if m.source:
                            info["source"] = m.source
                        if getattr(m, "kind", "input") == "output":
                            output_mounts.append(info)
                        else:
                            input_mounts_info.append(info)
                    workspace_info = {
                        "session_id": actual_session_id,
                        "outputs": output_mounts,
                        "inputs": input_mounts_info,
                        "outputs_dir_env": "$OUTPUTS_DIR",
                    }
                    if output_mounts:
                        workspace_info["cleanup_hint"] = (
                            f"sky storage delete {output_mounts[0]['bucket']}"
                        )
                    output["workspace"] = workspace_info
                    if input_mounts_info:
                        paths = ", ".join(m["path"] for m in input_mounts_info)
                        output["message"] += (
                            f" Inputs at {paths}; outputs at /outputs/{job_id}/ "
                            f"(session: {actual_session_id})"
                        )
                    else:
                        output["message"] += (
                            f" Outputs at /outputs/{job_id}/ "
                            f"(session: {actual_session_id})"
                        )
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
