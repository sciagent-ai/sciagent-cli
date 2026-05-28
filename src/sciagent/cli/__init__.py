"""
sciagent CLI dispatcher.

Public verbs (DESIGN_HARNESS.md §10.1):

  sciagent run [--task TXT] [--project-dir DIR] [--config PATH] [--set K=V]...
               [--model ID] [--verbose] [--resume SID] [--interactive] ...
  sciagent config show       prints the effective merged config as YAML
  sciagent config keys       lists OrchestratorConfig + AgentConfig fields

All existing top-level flags from the pre-H1 ``sciagent`` invocation are kept
on ``sciagent run`` so prior scripts keep working with one word added.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from ..defaults import DEFAULT_MODEL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sciagent",
        description="SciAgent - LLM-Agnostic Agent for Scientific and Engineering Workflows",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="verb", metavar="VERB")

    # ---- run ----
    run = subparsers.add_parser(
        "run",
        help="Execute a task",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  sciagent run --project-dir ~/proj --task "Fix the bug in main.py"
  sciagent run --project-dir ~/proj --interactive
  sciagent run --config ./run.yaml --set orchestrator.enable_data_gate=false --task "..."
  sciagent run --resume abc123def456
""",
    )

    # New ablation/config surface
    run.add_argument("--config", type=Path, help="Path to a sciagent config YAML")
    run.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VAL",
        help="Override a config key (dotted path; repeatable)",
    )

    # Existing flags (kept for backward compatibility)
    run.add_argument("--task", "-T", dest="task", help="Task to execute")
    run.add_argument(
        "task_positional",
        nargs="?",
        help=argparse.SUPPRESS,  # legacy positional; prefer --task
    )
    run.add_argument("-i", "--interactive", action="store_true", help="Run in interactive REPL mode")
    run.add_argument(
        "-m", "--model", default=None,
        help=f"Model id (default from config; built-in default: {DEFAULT_MODEL})"
    )
    run.add_argument("-p", "--project-dir", default=None, help="Project directory")
    run.add_argument("-t", "--load-tools", metavar="PATH", help="Load extra tools from a module")
    run.add_argument(
        "-s", "--subagents", action="store_true",
        help="Enable WorkflowTool for full DAG execution"
    )
    run.add_argument("--resume", metavar="SESSION_ID", help="Resume a previous session")
    run.add_argument("--list-sessions", action="store_true", help="List available sessions")
    run.add_argument("-v", "--verbose", action="store_true", default=True, help="Verbose output")
    run.add_argument("-q", "--quiet", action="store_true", help="Quiet mode")
    run.add_argument(
        "--max-iterations", type=int, default=None,
        help="Max agent loop iterations (overrides config)"
    )
    run.add_argument(
        "--temperature", type=float, default=None,
        help="LLM temperature (overrides config)"
    )
    run.add_argument("--system-prompt", metavar="PATH", help="Path to custom system prompt file")
    run.add_argument(
        "--skills-dir", metavar="PATH", type=Path, default=None,
        help="Directory containing skill definitions (SKILL.md files)"
    )

    # ---- config ----
    cfg = subparsers.add_parser("config", help="Inspect effective config")
    cfg_sub = cfg.add_subparsers(dest="config_verb", metavar="ACTION")

    show = cfg_sub.add_parser("show", help="Print the effective merged config as YAML")
    show.add_argument("--config", type=Path, help="Path to a sciagent config YAML")
    show.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VAL",
        help="Override a config key (dotted path; repeatable)",
    )
    show.add_argument("--project-dir", type=Path, default=None, help="Project directory")
    show.add_argument(
        "--format", choices=("yaml", "json"), default="yaml",
        help="Output format (default: yaml)"
    )

    keys = cfg_sub.add_parser(
        "keys",
        help="List supported orchestrator + agent config keys",
    )
    keys.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="Output format (default: text)"
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.verb == "config":
        from .config_cmd import handle_config
        return handle_config(args)
    if args.verb == "run":
        # Delegate to main.py's run logic. Imported lazily to keep
        # `sciagent config keys` fast and free of LiteLLM/SkyPilot side effects.
        from ..main import run_from_cli
        return run_from_cli(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
