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

import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, List, Optional, Union

import yaml


_logger = logging.getLogger(__name__)


# Default ask_user commit threshold. Tool-layer gate (not a prompt rule):
# the LLM cannot bypass it because it lives below the tool boundary, in
# execute(). Configurable in ~/.sciagent/config.yaml under
# compute.commit_threshold_usd; env override
# SCIAGENT_COMPUTE_COMMIT_THRESHOLD_USD wins over the file.
_DEFAULT_COMMIT_THRESHOLD_USD: float = 5.0


def _load_commit_threshold_usd(cloud_config=None) -> float:
    """Resolve the ask_user commit threshold ($).

    Precedence: env > CloudConfig.commit_threshold_usd > yaml > $5 default.
    Any parse failure falls back silently — the gate is a safety rail, not
    a structural dependency.
    """
    env_val = os.environ.get("SCIAGENT_COMPUTE_COMMIT_THRESHOLD_USD")
    if env_val is not None and env_val != "":
        try:
            return float(env_val)
        except ValueError:
            pass
    if cloud_config is not None and getattr(cloud_config, "commit_threshold_usd", None) is not None:
        try:
            return float(cloud_config.commit_threshold_usd)
        except (TypeError, ValueError):
            pass
    cfg_path = Path.home() / ".sciagent" / "config.yaml"
    if cfg_path.exists():
        try:
            with open(cfg_path) as fh:
                data = yaml.safe_load(fh) or {}
            compute_cfg = data.get("compute") or {}
            val = compute_cfg.get("commit_threshold_usd")
            if val is not None:
                return float(val)
        except Exception:
            pass
    return _DEFAULT_COMMIT_THRESHOLD_USD


def _format_menu_for_prompt(menu: list, proposed_total_usd: float) -> str:
    """Render the menu as a small text table for the interactive gate.

    Tight on purpose: instance, cloud, region, spot, hourly, total — six
    columns the user actually decides on. ``available=False`` rows are
    appended verbatim with their reason so the user sees what didn't work.
    """
    lines: List[str] = []
    lines.append(
        f"  Proposed launch estimated total: ${proposed_total_usd:.2f}"
    )
    lines.append("")
    lines.append("  Sky optimizer menu:")
    if not menu:
        lines.append("    (menu unavailable — Sky optimizer returned no rows)")
        return "\n".join(lines)
    for idx, row in enumerate(menu):
        if not row.get("available", True):
            lines.append(
                f"    [x] {row.get('label', '?'):>13s}  unavailable: "
                f"{row.get('reason', 'unknown')}"
            )
            continue
        spot_str = "spot" if row.get("spot") else "on-dem"
        lines.append(
            f"    [{idx}] {row.get('label', '?'):>13s}  "
            f"{row.get('num_nodes', 1)}x{row.get('cpus', '?')}cpu"
            f"{('+' + str(row.get('gpus')) + 'gpu') if row.get('gpus') else ''}  "
            f"{row.get('instance', '?'):<14s} {row.get('cloud', '?'):<6s} "
            f"{row.get('region', '?'):<14s} {spot_str:<6s} "
            f"${row.get('estimated_hourly_usd', 0):.3f}/h "
            f"${row.get('estimated_total_usd', 0):.2f} total"
            f"{'  OVER BUDGET' if row.get('over_budget') else ''}"
        )
    return "\n".join(lines)


def _commit_gate_prompt(
    proposed_total_usd: float,
    threshold_usd: float,
    menu: list,
) -> Optional[str]:
    """Synchronous interactive ask_user. Returns:
        - "proceed"   : user confirmed the proposed launch as-is
        - "abort"     : user declined
        - None        : non-interactive shell — caller proceeds with original
                        args (gate falls back gracefully so batch runs don't
                        break, per the slim-plan acceptance bar)
    """
    if not (sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty()):
        _logger.warning(
            "compute_run: estimated total $%.2f exceeds commit threshold "
            "$%.2f but no interactive shell available; proceeding with the "
            "original args. Set SCIAGENT_COMPUTE_COMMIT_THRESHOLD_USD to "
            "change the threshold or run in an interactive shell to confirm.",
            proposed_total_usd,
            threshold_usd,
        )
        return None
    print("", file=sys.stderr)
    print(
        f"sciagent compute_run: estimated total ${proposed_total_usd:.2f} "
        f"exceeds commit threshold ${threshold_usd:.2f}.",
        file=sys.stderr,
    )
    print(_format_menu_for_prompt(menu, proposed_total_usd), file=sys.stderr)
    print("", file=sys.stderr)
    try:
        ans = input(
            "Proceed with the proposed launch? [y/N] (n aborts; "
            "to pick a different row, abort and re-run with the row's params) "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "abort"
    if ans in {"y", "yes"}:
        return "proceed"
    return "abort"


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


# Probe-shape heuristic: command tokens that strongly suggest the agent
# is doing a quick diagnostic check (env query, file listing, binary
# resolution) rather than running a real workload. Used to nudge toward
# mode="cluster" — every probe via mode="job" burns a 3-5 min cluster
# cycle. Conservative on purpose: false positives mean an unwarranted
# hint, not behavior change. Matches the FIRST shell token only so a
# real workload like `python -c "echo bla"` doesn't trip it.
_PROBE_TOKENS: frozenset = frozenset({
    "echo", "which", "whereis", "ls", "pwd", "find", "cat", "head",
    "tail", "env", "printenv", "whoami", "id", "uname", "df", "du",
    "wc", "stat", "file", "type", "command",
})


def _looks_like_probe(command: str) -> bool:
    """Heuristic: True when the command's first non-trivial token is a
    diagnostic probe (echo, which, ls, etc.) and the overall command is
    short. Returns False on any uncertainty so we never nudge a real
    workload. Visibility-only; never gates execution."""
    if not command or len(command) > 400:
        return False
    # Strip leading shell preamble (set -e, export FOO=bar, etc.) so we
    # look at the actual work-shaped first token.
    text = command.strip()
    # Take everything up to the first separator (&&, ||, ;, |, newline)
    # — the user's "real" command. Probes don't usually chain.
    head = re.split(r"&&|\|\||;|\n|\|", text, maxsplit=1)[0].strip()
    if not head:
        return False
    first_tok = head.split()[0] if head.split() else ""
    return first_tok in _PROBE_TOKENS


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
Use EITHER `service` (from registry; see service_search) OR `image`.

Path contract (image-agnostic):
  - Inputs: `workspace_source=` mounts a bucket/dir at /workspace/
    (single string) or any path (list of {path, source}). Cloud-agnostic
    (s3/gs/az/r2/oci).
  - Outputs: write to $OUTPUTS_DIR (= /outputs/<job_id>/). Auto-fetched
    by bg_wait on terminal status. Cross-job reads: /outputs/<other-job-id>/.
  - Anything outside $OUTPUTS_DIR is scratch (vanishes at teardown).
  - `workdir=<local>` rsyncs to ~/sky_workdir/ (don't reference that path).

Modes (Sky's two execution surfaces):
  - mode="job" (default): managed-jobs. Sky owns lifecycle. One-shot batch.
  - mode="cluster": persistent cluster + autostop. Idempotent on
    cluster_name. Iterate via compute_exec; manage via compute_cluster.

Honor user intent on backend: "on sky" → backend="skypilot"; "locally" →
"local". Only use "auto" when unspecified.

For sky work prefer delegating to the `compute` subagent — it carries the
full path-contract guidance and bounds compute chatter to its own context.

Returns job_id. Check status with bg_status(job_id); bg_wait(job_id) blocks
and auto-fetches /outputs/<job_id>/ to local on success."""

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
            "num_nodes": {
                "type": "integer",
                "description": (
                    "Number of cluster nodes (Sky Task.num_nodes). Default 1. "
                    ">1 provisions a multi-node Sky cluster; the user's command "
                    "is responsible for MPI/DDP coordination using SkyPilot's "
                    "native env vars (SKYPILOT_NUM_NODES, SKYPILOT_NODE_RANK, "
                    "SKYPILOT_NODE_IPS)."
                ),
                "default": 1,
            },
            "use_spot": {
                "type": "boolean",
                "description": (
                    "Use spot/preemptible instances (Sky Resources.use_spot). "
                    "3-5x cheaper but interruptible; the managed-jobs "
                    "controller handles re-launch on preemption. Default False."
                ),
                "default": False,
            },
            "background": {
                "type": "boolean",
                "description": "Run in background (default: true)",
                "default": True
            },
            "estimate_only": {
                "type": "boolean",
                "description": (
                    "Only return Sky's optimizer menu, don't run. Rows are at "
                    "scale points {1,2,4} x {spot, on-demand}, sorted by "
                    "total cost. Combine with duration_hours / budget_usd / "
                    "target_total_cores / target_gpus to shape the menu."
                ),
                "default": False
            },
            "duration_hours": {
                "type": "number",
                "description": (
                    "Estimated runtime in hours. Drives estimated_total_usd, "
                    "the commit-gate threshold check, AND the routing "
                    "decision: short jobs (<5 min) prefer local when local "
                    "fits, since cloud provisioning would dominate. Be "
                    "honest — under-estimating skips a gate that should "
                    "have fired AND mis-routes short jobs to cloud. "
                    "Default 1.0."
                ),
                "default": 1.0,
            },
            "target_total_cores": {
                "type": "integer",
                "description": (
                    "Optional. A specific total core count the user wants "
                    "matched (a published reference, a prior baseline, an "
                    "internal SLA). The menu adds an extra row sized to "
                    "this target so you can see what matching it would cost."
                )
            },
            "target_gpus": {
                "type": "integer",
                "description": (
                    "Optional. A specific GPU count the user wants matched. "
                    "The menu adds an extra row sized to this target so you "
                    "can compare it against the standard scale points."
                )
            },
            "budget_usd": {
                "type": "number",
                "description": (
                    "Optional spend cap in USD. Each menu row is flagged "
                    "over_budget=True if estimated_total_usd exceeds this; "
                    "rows are still returned so you can see the shape of the "
                    "tradeoff."
                )
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
            "mode": {
                "type": "string",
                "enum": ["job", "cluster"],
                "description": (
                    "Execution model. 'job' (default): managed-jobs — Sky "
                    "owns lifecycle, fresh cluster per call. Best for "
                    "one-shot batch / scale-out. 'cluster': persistent "
                    "cluster + autostop — idempotent on cluster_name, "
                    "follow-ups via compute_exec run on the warm cluster "
                    "in seconds. Best for iteration."
                ),
                "default": "job",
            },
            "cluster_name": {
                "type": "string",
                "description": (
                    "mode='cluster' only. Persistent cluster identifier; "
                    "passing the same name in subsequent compute_run / "
                    "compute_exec calls reuses the warm cluster. Defaults "
                    "to sciagent-<session_id>-i if omitted."
                ),
            },
            "autostop_minutes": {
                "type": "integer",
                "description": (
                    "mode='cluster' only. Idle minutes before Sky auto-"
                    "stops the cluster. Reset on each new job submit. "
                    "Default 30."
                ),
                "default": 30,
            },
            "autostop_hook": {
                "type": "string",
                "description": (
                    "mode='cluster' only. Optional shell snippet that "
                    "runs on the cluster before autostop fires (e.g., to "
                    "flush /scratch to S3 before teardown)."
                ),
            },
        },
        "required": ["command"]
    }

    # Class-level session ID for workspace sharing across jobs
    _shared_session_id: str = None

    def __init__(self, working_dir: str = ".", session_id: str = None, cloud_config=None):
        self._working_dir = working_dir
        self._router = None  # Lazy init
        self._session_id = session_id
        # Optional CloudConfig (sciagent.compute.CloudConfig). Used by the
        # cost-gate resolver to honor a Python-API-set commit threshold
        # when env / yaml don't override.
        self._cloud_config = cloud_config

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
        num_nodes: int = 1,
        use_spot: bool = False,
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
        mode: str = "job",
        cluster_name: Optional[str] = None,
        autostop_minutes: int = 30,
        autostop_hook: Optional[str] = None,
        duration_hours: float = 1.0,
        target_total_cores: Optional[int] = None,
        target_gpus: Optional[int] = None,
        budget_usd: Optional[float] = None,
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

        # Build compute requirements. timeout_sec resolution:
        #   per-call timeout_sec → CloudConfig.default_timeout_sec →
        #   ComputeRequirements default (3600s).
        # Passing 0 disables the on-VM timeout wrapper (B6 / v4.2 §C2).
        requirements_kwargs: Dict[str, Any] = {
            "cpus": cpus,
            "memory_gb": memory_gb,
            "gpus": gpus,
            "gpu_type": gpu_type if gpus > 0 else None,
            "num_nodes": int(num_nodes) if num_nodes else 1,
            "use_spot": bool(use_spot),
        }
        if timeout_sec is not None:
            requirements_kwargs["timeout_sec"] = int(timeout_sec)
        elif (
            self._cloud_config is not None
            and getattr(self._cloud_config, "default_timeout_sec", None) is not None
        ):
            requirements_kwargs["timeout_sec"] = int(self._cloud_config.default_timeout_sec)
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
        # workspace_uri surfaces the durable session bucket to the LLM so
        # it can declare it in produces_uris and pass it to materialize.
        # Stays None when an explicit workspace_source override is in
        # effect (per the auto-mount contract).
        workspace_uri: Optional[str] = None

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
                _DEFAULT_INPUT_MOUNT_PATH,
                _normalize_workspace_source as _normalize_ws,
            )
            normalized_inputs = _normalize_ws(workspace_source)
        except ValueError as e:
            return ToolResult(
                success=False,
                output={"error_kind": "path_contract", "field": "workspace_source"},
                error=f"Invalid workspace_source: {e}",
            )

        # Auto-mount the session workspace at /workspace/ when the caller
        # didn't pass workspace_source explicitly. The session workspace is
        # a persistent cloud bucket that survives cluster stop/down/crash —
        # the durable cross-step data tier. Honors "compute is too-thin
        # wrapper around sky": uses sky.Storage directly, no abstraction.
        # Explicit workspace_source is preserved verbatim — caller knows
        # what they want and the auto-mount must not override.
        auto_workspace_mount = (
            will_attach_mounts and workspace_source is None
        )

        # Path-contract validation (fail-fast, before the backend launch).
        # Only enforced for skypilot — local Docker has its own filesystem
        # semantics that don't share /workspace, /outputs, or ~/sky_workdir.
        if will_attach_mounts:
            declared_paths = [entry["path"] for entry in normalized_inputs]
            # When auto-workspace fires, /workspace/ is implicitly declared.
            # Add it to the allowed-prefix set so the validator doesn't
            # reject the (very natural) command that writes to /workspace/
            # without an explicit workspace_source.
            if auto_workspace_mount and _DEFAULT_INPUT_MOUNT_PATH not in declared_paths:
                effective_paths = declared_paths + [_DEFAULT_INPUT_MOUNT_PATH]
            else:
                effective_paths = declared_paths
            err = self._validate_path_contract(command, effective_paths)
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

                    # Auto-mount the durable session workspace at /workspace/
                    # when no explicit workspace_source was given. The bucket
                    # is persistent (sky.Storage(persistent=True)), so writes
                    # survive cluster stop/down/crash and are visible to the
                    # next compute_exec / compute_run in the session.
                    if auto_workspace_mount:
                        ws_mount = skypilot_backend.build_session_workspace_mount(
                            actual_session_id
                        )
                        storage_list.append(ws_mount)
                        from sciagent.compute.backends.skypilot import (
                            _build_workspace_uri as _bld_ws_uri,
                        )
                        workspace_uri = _bld_ws_uri(
                            ws_mount.store, actual_session_id
                        )

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

            # Select backend and get cost estimate. duration_hours flows
            # into the router as a wall-time hint: short jobs prefer local
            # when local fits (skips the cloud provisioning tax).
            preferred = backend if backend != "auto" else None
            selected_backend, routing_reason = router.select(
                job.requirements,
                preferred=preferred,
                duration_hours=duration_hours,
            )
            estimate_duration = float(duration_hours or 1.0)
            cost_estimate = router.estimate_cost(
                job, duration_hours=estimate_duration
            )

            # If estimate_only, return Sky's optimizer menu (multi-scale).
            # The single-row cost_estimate is preserved so existing callers
            # see the same key, but the menu is the new primary surface.
            if estimate_only:
                menu = router.estimate_menu(
                    job,
                    duration_hours=float(duration_hours or 1.0),
                    target_total_cores=target_total_cores,
                    target_gpus=target_gpus,
                    budget_usd=budget_usd,
                )
                output = {
                    "backend": selected_backend.name,
                    "routing_reason": routing_reason,
                    "cost_estimate": cost_estimate,  # cheapest single row, back-compat
                    "options": menu,
                    "resources": {
                        "cpus": cpus,
                        "memory_gb": memory_gb,
                        "gpus": gpus,
                        "gpu_type": gpu_type if gpus > 0 else None,
                        "num_nodes": int(num_nodes) if num_nodes else 1,
                        "use_spot": bool(use_spot),
                    },
                    "duration_hours": float(duration_hours or 1.0),
                    "budget_usd": budget_usd,
                    "image": resolved_image,
                }
                if gpu_hint:
                    output["gpu_hint"] = gpu_hint
                    if gpu_hint == "gpu_beneficial":
                        output["gpu_note"] = f"Service '{service}' benefits from GPU (5-13x speedup). Add gpus=1 for better performance."
                return ToolResult(success=True, output=output)

            # ask_user commit gate. Tool-layer (not prompt-layer) so the LLM
            # can't silently bypass it. Only meaningful for cloud launches —
            # local Docker has no $-cost. estimated_total_usd comes from
            # router.estimate_cost (Sky's optimizer when skypilot is the
            # selected backend; otherwise a no-op zero).
            estimated_total_usd = 0.0
            if isinstance(cost_estimate, dict):
                # estimate_cost returns "estimated_total" (existing key);
                # fall back to hourly * duration if absent.
                estimated_total_usd = float(
                    cost_estimate.get("estimated_total")
                    or (cost_estimate.get("estimated_hourly", 0.0) or 0.0)
                    * estimate_duration
                )
            threshold_usd = _load_commit_threshold_usd(self._cloud_config)
            if (
                selected_backend.name == "skypilot"
                and estimated_total_usd > threshold_usd
            ):
                menu = router.estimate_menu(
                    job,
                    duration_hours=estimate_duration,
                    target_total_cores=target_total_cores,
                    target_gpus=target_gpus,
                    budget_usd=budget_usd,
                )
                decision = _commit_gate_prompt(
                    proposed_total_usd=estimated_total_usd,
                    threshold_usd=threshold_usd,
                    menu=menu,
                )
                if decision == "abort":
                    return ToolResult(
                        success=False,
                        output={
                            "failure_type": "commit_gate_aborted",
                            "estimated_total_usd": estimated_total_usd,
                            "threshold_usd": threshold_usd,
                            "options": menu,
                        },
                        error=(
                            f"Aborted by user at commit gate "
                            f"(${estimated_total_usd:.2f} > "
                            f"${threshold_usd:.2f}). Re-run with explicit "
                            f"params from the options list, or raise the "
                            f"threshold via "
                            f"SCIAGENT_COMPUTE_COMMIT_THRESHOLD_USD."
                        ),
                    )
                # decision in {"proceed", None}: fall through to launch.

            # Mode dispatch (Sky's two execution surfaces):
            #   - "job"     → managed-jobs (sky.jobs.launch). Sky owns
            #                 lifecycle, fresh cluster per call. Default.
            #   - "cluster" → persistent cluster (sky.launch + autostop).
            #                 Idempotent on cluster_name; second call with
            #                 the same name reuses the warm cluster.
            # Cluster mode requires SkyPilot backend; reject auto / local.
            if mode == "cluster":
                if selected_backend.name != "skypilot":
                    return ToolResult(
                        success=False,
                        output={
                            "mode": "cluster",
                            "selected_backend": selected_backend.name,
                            "failure_type": "mode_backend_mismatch",
                        },
                        error=(
                            "mode='cluster' requires backend='skypilot'. "
                            "Cluster mode is a SkyPilot concept and has no "
                            "local-Docker analogue."
                        ),
                    )
                resolved_cluster_name = (
                    cluster_name
                    or f"sciagent-{actual_session_id or 'session'}-i"
                )
                try:
                    cluster, cluster_job_id = router.launch_cluster(
                        job=job,
                        cluster_name=resolved_cluster_name,
                        autostop_minutes=autostop_minutes,
                        autostop_hook=autostop_hook,
                    )
                except LaunchError as launch_exc:
                    rejected_output = {
                        "service": service,
                        "image": resolved_image,
                        "command": command[:100],
                        "mode": "cluster",
                        "cluster_name": resolved_cluster_name,
                        "failure_type": "launch_rejected",
                        "request_id": getattr(launch_exc, "request_id", None),
                        "next_step": (
                            f"Run `sky api logs {launch_exc.request_id}` via "
                            f"bash to see the actual rejection reason "
                            f"(image pull failure, capacity, auth, etc.)."
                            if getattr(launch_exc, "request_id", None)
                            else "Check `sky check` for cloud credentials."
                        ),
                    }
                    return ToolResult(
                        success=False,
                        output=rejected_output,
                        error=f"sky.launch rejected: {launch_exc}",
                    )

                output = {
                    "cluster_name": cluster,
                    "cluster_job_id": cluster_job_id,
                    "status": "running",
                    "backend": "skypilot",
                    "mode": "cluster",
                    "autostop_minutes": autostop_minutes,
                    "routing_reason": routing_reason,
                    "cost_estimate": cost_estimate,
                    "image": resolved_image,
                    "resources_used": {
                        "cpus": cpus,
                        "memory_gb": memory_gb,
                        "gpus": gpus,
                    },
                    # Durable session tier — persists across cluster
                    # stop/down so compute_exec follow-ups (and the next
                    # compute_run on a fresh cluster in this session) can
                    # read prior writes at /workspace/. Sky-provisioned
                    # via sky.Storage(persistent=True, mode=MOUNT).
                    "workspace_uri": workspace_uri,
                    "message": (
                        f"Cluster {cluster} launched (or reused if UP); "
                        f"per-cluster job_id {cluster_job_id}. Subsequent "
                        f"follow-ups: compute_exec(cluster_name='{cluster}', "
                        f"command=...). Status: compute_cluster("
                        f"action='status', cluster_name='{cluster}'). "
                        f"Down: compute_cluster(action='down', "
                        f"cluster_name='{cluster}'). "
                        f"NOTE: cluster_name is NOT a job_id — do not pass "
                        f"it to bg_status / bg_output / bg_wait."
                    ),
                }
                return ToolResult(success=True, output=output)

            # Run the job (managed-jobs mode). A LaunchError surfaced from
            # the backend's fail-fast poll (B4) means Sky rejected the
            # launch outright — return a structured failure now instead of
            # letting the agent burn a 10-min status-poll loop. We call
            # the selected backend directly (not router.run) so
            # SkyPilotBackend's tuple return — which carries the integer
            # managed_job_id when the controller acknowledged the launch
            # inside the fail-fast budget — flows into the manifest write.
            managed_job_id: Optional[int] = None
            try:
                run_result = selected_backend.run(job, background=background)
            except LaunchError as launch_exc:
                # cluster_name is set when the failure came from the SkyPilot
                # backend; propagate it so callers (and our paid AWS tests)
                # can clean up a partially-provisioned cluster instead of
                # leaving it billing on the cloud.
                #
                # request_id is the Sky-side handle for the rejected request.
                # When the auto log-tail fetch in _await_launch_or_fail came
                # back empty (sky api logs takes a moment to populate; or
                # the CLI isn't on PATH from the python process), the agent
                # can still recover the actual cause by running the
                # next_step bash command. Surfacing both fields structurally
                # — not just embedded in a truncated error string — means
                # the agent doesn't have to parse a request_id out of the
                # truncated display.
                rejected_output = {
                    "service": service,
                    "image": resolved_image,
                    "command": command[:100],
                    "backend_attempted": backend,
                    "failure_type": "launch_rejected",
                    "request_id": getattr(launch_exc, "request_id", None),
                    "next_step": (
                        f"Run `sky api logs {launch_exc.request_id}` via "
                        f"bash to see the actual rejection reason "
                        f"(image pull failure, capacity, auth, etc.)."
                        if getattr(launch_exc, "request_id", None)
                        else "Check `sky check` for cloud credentials."
                    ),
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
                    # Sky-provisioned durable session tier (persistent bucket
                    # mounted at /workspace/). Stays None when the caller
                    # passed an explicit workspace_source override.
                    "workspace_uri": workspace_uri,
                }
                # Probe-shape nudge: when the agent is using mode="job"
                # (managed-jobs default) on a command that looks like a
                # diagnostic probe (echo / which / ls / pwd / find / cat
                # / head / tail / env / printenv / whoami), point at
                # mode="cluster" — each probe via mode="job" burns a
                # fresh 3-5 min cluster cycle that mode="cluster" + a
                # warm cluster + compute_exec would have done in seconds.
                # Visibility-only; no auto-switch.
                if (
                    selected_backend.name == "skypilot"
                    and mode == "job"
                    and _looks_like_probe(command)
                ):
                    output["mode_hint"] = (
                        "This command looks like a diagnostic probe. "
                        "mode='cluster' + compute_exec would have run "
                        "this in seconds instead of 3–5 min provisioning. "
                        "If you'll iterate (probe → fix → retry), "
                        "compute_run(mode='cluster', cluster_name='...') "
                        "first, then compute_exec for follow-ups."
                    )
                # Add GPU hint if applicable
                if gpu_hint == "gpu_beneficial":
                    output["gpu_hint"] = f"Service '{service}' benefits from GPU. Consider adding gpus=1 for 5-13x speedup."
                # Add workspace info — names every attached mount so the
                # caller can see what's at /workspace/, /outputs/, /data/...,
                # and which source URI populated each (cloud-agnostic).
                if actual_session_id and requirements.storage:
                    output_mounts: List[Dict[str, str]] = []
                    input_mounts_info: List[Dict[str, str]] = []
                    durable_mounts_info: List[Dict[str, str]] = []
                    for m in requirements.storage:
                        info = {
                            "path": m.path,
                            "bucket": m.bucket,
                            "store": m.store,
                        }
                        if m.source:
                            info["source"] = m.source
                        kind = getattr(m, "kind", "input")
                        if kind == "output":
                            output_mounts.append(info)
                        elif kind == "durable":
                            durable_mounts_info.append(info)
                        else:
                            input_mounts_info.append(info)
                    workspace_info = {
                        "session_id": actual_session_id,
                        "outputs": output_mounts,
                        "inputs": input_mounts_info,
                        "durable": durable_mounts_info,
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
                    if durable_mounts_info and workspace_uri:
                        output["message"] += (
                            f". Durable workspace at /workspace/ "
                            f"(persistent across stop/down): {workspace_uri}"
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


def session_context_block() -> str:
    """One-paragraph 'session context' header for system prompts.

    Pinpoints the concrete workspace URI for the current session so the
    orchestrator (and every sub-agent) can name it directly in
    `produces_uris` for cluster-internal handoff. Without this, the
    orchestrator dispatches with a wildcard URI and the validator's
    cloud-CLI list rejects it (AWS doesn't accept `*` in bucket names).

    Empty when no session has been initialized yet, or when SkyPilot
    isn't installed / configured (top-of-prompt composition skips it
    cleanly in either case).
    """
    sid = ComputeTool._shared_session_id
    if not sid:
        return ""
    try:
        from sciagent.compute.backends.skypilot import (
            SkyPilotBackend,
            _build_workspace_uri,
        )
        store = SkyPilotBackend().resolve_workspace_store()
        uri = _build_workspace_uri(store, sid)
    except Exception:
        return ""
    return (
        "## Session context\n\n"
        f"This session's workspace URI: `{uri}`\n\n"
        "The bucket auto-mounts at `/workspace/` on every cluster compute "
        "job in this session, persists across `sky stop` / `sky down`, and "
        "is shared by every sub-agent you dispatch. Use this exact URI "
        "(not a wildcard, not a placeholder) in `produces_uris` for "
        "cluster-internal artifact handoff between sub-agents — the "
        "validator lists the bucket directly, no local sync needed for the "
        "gate to pass. Local paths in `produces_uris` are only for the "
        "final user-facing deliverables.\n"
    )
