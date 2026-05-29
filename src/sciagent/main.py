#!/usr/bin/env python3
"""
SciAgent - A Software Engineering Agent Framework

Usage:
    sciagent "Your task here"
    sciagent --interactive
    sciagent --model openai/gpt-4o "Your task"
    sciagent --load-tools ./my_tools.py "Your task"
"""
# CRITICAL: Suppress pydantic serialization warnings BEFORE any imports
# These warnings occur when litellm's pydantic models serialize LLM responses
# and some fields (like thinking_blocks) don't match expected schema
import signal
import warnings

# Filter by message content - catches the actual warning text
warnings.filterwarnings("ignore", message=".*Pydantic serializer warnings.*")
warnings.filterwarnings("ignore", message=".*PydanticSerializationUnexpectedValue.*")
warnings.filterwarnings("ignore", message=".*Expected.*fields but got.*")
warnings.filterwarnings("ignore", message=".*serialized value may not be as expected.*")

# Filter by category and module - broad catch for pydantic warnings
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic.main")
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic.*")

import os
import sys

# Strip macOS MallocStackLogging* env vars before any subprocess spawns.
# These are often left in the user's shell by Xcode / Instruments / Activity
# Monitor's "Sample Process", or injected by launchd. When inherited by
# child Pythons (ours and SkyPilot's internal aws/ray/fork spawns), each
# child prints
#   Python(NNN) MallocStackLogging: can't turn off malloc stack logging
#   because it was not enabled.
# which spams `bg_status` and `compute_run` output.
#
# `pop()` alone wasn't enough in practice — SkyPilot's optimizer fork-spawns
# helpers and on some macOS configs the var resurfaces. Belt-and-suspenders:
# `pop()` to remove from our env (children inherit the cleaned set), then
# leave nothing behind. The full family of these vars all trigger the same
# warning, so strip them together.
for _var in (
    "MallocStackLogging",
    "MallocStackLoggingNoCompact",
    "MallocStackLoggingDirectory",
    "MallocStackLoggingFile",
    "MallocScribble",
):
    os.environ.pop(_var, None)

# Some macOS Python builds emit the "MallocStackLogging: can't turn off ..."
# warning regardless of env state — it comes from libc on subprocess Pythons
# that SkyPilot's internal optimizer/queue paths fork-spawn, and there is
# no env var that suppresses it cleanly. Wrapping every sky.* call site in
# the backend is brittle (a future call site will forget). Instead, install
# a line-level filter on sys.stdout/stderr that drops the known noise
# patterns. Tiny per-write cost, one place to maintain.
import re as _re

_NOISE_PATTERNS = (
    _re.compile(r"^Python\(\d+\) MallocStackLogging:"),
    # SkyPilot rich-console payload markers when stdout isn't a TTY.
    _re.compile(r"^<sky-payload[^>]*>.*</sky-payload>\s*$"),
)


class _NoiseFilteredStream:
    """Wraps a real stream and drops lines that match _NOISE_PATTERNS.

    Stateful across writes because Python frequently calls write() with
    partial lines. Buffer non-newline writes; on a newline, decide whether
    to emit. Anything that isn't a "complete line" (e.g. interactive
    prompts) is flushed verbatim on close/flush so we don't swallow it.
    """

    def __init__(self, real):
        self._real = real
        self._buf = ""

    def write(self, s):
        if not s:
            return 0
        self._buf += s
        # Emit complete lines; keep any trailing partial line in the buffer.
        if "\n" in self._buf:
            *complete, trailing = self._buf.split("\n")
            self._buf = trailing
            for line in complete:
                if not any(p.search(line) for p in _NOISE_PATTERNS):
                    self._real.write(line + "\n")
        return len(s)

    def flush(self):
        # Flush any pending partial line — don't drop on flush since we
        # can't pattern-match an incomplete line reliably.
        if self._buf:
            self._real.write(self._buf)
            self._buf = ""
        self._real.flush()

    def __getattr__(self, name):
        # Pass through everything else (isatty, fileno, etc.) to the real
        # stream — important for libraries that probe these.
        return getattr(self._real, name)


sys.stdout = _NoiseFilteredStream(sys.stdout)
sys.stderr = _NoiseFilteredStream(sys.stderr)

from pathlib import Path
from typing import Optional

from .display import create_display
from .agent import AgentLoop, AgentConfig, create_agent
from .tools import ToolRegistry, create_default_registry
from .subagent import SubAgentOrchestrator, TaskTool, WorkflowTool
from .prompts import build_system_prompt
from .state import StateManager
from .defaults import DEFAULT_MODEL
from .startup import show_startup_banner, check_configuration_ready, check_optional_keys


def get_package_dir() -> Path:
    """Get the directory where the sciagent package is installed."""
    return Path(__file__).parent.resolve()


def validate_project_dir(project_dir: str) -> Path:
    """
    Validate and resolve the project directory.
    Prevents the agent from operating in its own package directory.
    """
    package_dir = get_package_dir()
    project_path = Path(project_dir).resolve()

    # Block if project dir is the package directory
    if project_path == package_dir:
        print(f"Error: Cannot use sciagent's own directory as project directory.")
        print(f"  Package location: {package_dir}")
        print(f"  Requested project dir: {project_path}")
        print()
        print("Please specify a different directory with --project-dir:")
        print("  sciagent --project-dir ~/my-project \"your task\"")
        sys.exit(1)

    # Warn if package is inside project dir (but allow it)
    if package_dir.is_relative_to(project_path) and project_path != Path.home():
        print(f"Warning: Project directory contains sciagent package at {package_dir}")
        print("         Be careful not to modify agent source files.")
        print()

    return project_path


def main():
    """Package entry-point. Delegates to the cli dispatcher (sciagent.cli)."""
    from .cli import main as cli_main
    sys.exit(cli_main())


def run_from_cli(args):
    """Execute a `sciagent run` invocation from the cli dispatcher.

    ``args`` is the argparse Namespace produced by sciagent.cli's ``run``
    subparser. Resolves the layered SciagentConfig, then applies the same
    setup the pre-H1 main() did (project dir validation, banner, tools,
    SubAgentOrchestrator + TaskTool, optional WorkflowTool, AgentLoop, signal
    handlers, session resume).
    """
    from .config import ConfigError, load_config

    # Handle list sessions
    if getattr(args, "list_sessions", False):
        manager = StateManager()
        sessions = manager.list_sessions()
        if not sessions:
            print("No saved sessions found.")
        else:
            print("\nAvailable Sessions:")
            print("-" * 60)
            for s in sessions:
                print(f"  {s['session_id']}  |  {s['updated_at'][:19]}  |  {s['task_count']} tasks")
        return 0

    # Legacy: support both `--task TXT` and the bare positional form.
    task = args.task or getattr(args, "task_positional", None)

    if not task and not args.interactive and not args.resume:
        print("Error: Must provide --task, --interactive, or --resume")
        return 1

    try:
        sci_cfg = load_config(
            explicit_path=args.config,
            project_dir=Path(args.project_dir) if args.project_dir else None,
            overrides=args.overrides,
        )
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    # Legacy CLI flags layer on top of the resolved config (None means
    # "leave the config value alone"). Matches the documented contract that
    # --config / --set are the new ablation surface; legacy flags still win
    # when explicitly passed because they're the user's most-recent intent.
    if args.model is not None:
        sci_cfg.agent.model = args.model
    if args.max_iterations is not None:
        sci_cfg.agent.max_iterations = args.max_iterations
    if args.temperature is not None:
        sci_cfg.agent.temperature = args.temperature

    verbose = not args.quiet and args.verbose
    sci_cfg.agent.verbose = verbose

    # Resolve and validate project directory
    project_dir_arg = args.project_dir or os.getcwd()
    project_dir = validate_project_dir(project_dir_arg)
    project_dir.mkdir(parents=True, exist_ok=True)
    sci_cfg.agent.working_dir = str(project_dir)

    # Load custom system prompt if provided
    system_prompt = None
    if args.system_prompt:
        system_prompt = Path(args.system_prompt).read_text()

    skills_dir = args.skills_dir
    if skills_dir and not skills_dir.exists():
        print(f"Warning: Skills directory not found: {skills_dir}")
        skills_dir = None

    tools = create_default_registry(str(project_dir), skills_dir=skills_dir)
    if args.load_tools:
        tools.load_from_module(args.load_tools)

    show_startup_banner(
        model=sci_cfg.agent.model,
        project_dir=project_dir,
        interactive=args.interactive,
        verbose=verbose,
        tools_loaded=tools.list_tools(),
        subagents=args.subagents,
    )

    is_ready, issues = check_configuration_ready(sci_cfg.agent.model)
    if not is_ready and not args.quiet:
        for issue in issues:
            print(f"Error: {issue}")
        print()

    if verbose and not args.quiet:
        check_optional_keys()

    orchestrator = SubAgentOrchestrator(
        tools=tools,
        working_dir=str(project_dir),
    )

    # Apply the verifier-model override to the SubAgentRegistry. The
    # TaskOrchestrator (used by WorkflowTool) does the same mutation when it
    # constructs, but the verifier subagent can also be spawned via TaskTool
    # without a TaskOrchestrator in the loop — so mirror the mutation here so
    # both code paths honor the configured verifier_model.
    if sci_cfg.orchestrator.verifier_model:
        verifier_cfg = orchestrator.registry.get("verifier")
        if verifier_cfg is not None:
            verifier_cfg.model = sci_cfg.orchestrator.verifier_model

    tools.register(TaskTool(orchestrator))

    if args.subagents:
        tools.register(
            WorkflowTool(
                orchestrator,
                str(project_dir),
                orchestrator_config=sci_cfg.orchestrator,
            )
        )

    final_system_prompt = system_prompt or build_system_prompt(
        working_dir=str(project_dir),
        registry_path=str(Path(__file__).parent / "services" / "registry.yaml"),
    )

    # Main agent gets a trimmed view: compute_* tools are reachable only
    # via the `compute` subagent (which has them in its allowed_tools).
    # Keeps cloud chatter (install logs, status polls, large bg_output) out
    # of the main agent's context per the subagent.py:380-388 rationale.
    # The orchestrator/subagents still see the full registry — they filter
    # via allowed_tools at SubAgent construction time.
    main_tools = tools.clone(
        exclude={"compute_run", "compute_exec", "compute_cluster"}
    )

    # Thread the OrchestratorConfig + SubAgentOrchestrator through so
    # AgentLoop can fire the LLM verification gate at session close on the
    # single-task run path (DESIGN_BENCH.md §5.4.b / DESIGN_HARNESS.md §3.7).
    # verifier_model is already applied to the registry above; passing the
    # orchestrator here makes that override reachable from the gate.
    agent = AgentLoop(
        config=sci_cfg.agent,
        tools=main_tools,
        system_prompt=final_system_prompt,
        orchestrator_config=sci_cfg.orchestrator,
        subagent_orchestrator=orchestrator,
    )

    def _on_sigterm(signum, frame):
        try:
            agent.cleanup_session_clusters()
        finally:
            sys.exit(128 + int(signum))
    signal.signal(signal.SIGTERM, _on_sigterm)

    orchestrator.parent_interrupt_event = agent._interrupt_event

    if args.resume:
        if agent.load_session(args.resume):
            if verbose:
                print(f"Resumed session: {args.resume}")
        else:
            print(f"Error: Session not found: {args.resume}")
            return 1

    if args.interactive:
        agent.run_interactive()
    else:
        try:
            result = agent.run(task)
            print("\nResult:")
            print(result)
        finally:
            agent.cleanup_session_clusters()
    return 0


if __name__ == "__main__":
    main()
