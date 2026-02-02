#!/usr/bin/env python3
"""
SWE Agent - A Software Engineering Agent Framework

Usage:
    python main.py "Your task here"
    python main.py --interactive
    python main.py --model openai/gpt-4o "Your task"
    python main.py --load-tools ./my_tools.py "Your task"
"""
import os
import sys
import argparse
from pathlib import Path
from typing import Optional

from .display import create_display
from .agent import AgentLoop, AgentConfig, create_agent
from .tools import ToolRegistry, create_default_registry
from .subagent import create_agent_with_subagents
from .state import StateManager

# Suppress warnings early before any other imports trigger them
_display = create_display(verbose=False)
_display.setup()


def parse_args():
    parser = argparse.ArgumentParser(
        description="SWE Agent - Software Engineering Agent Framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Run a task in a specific project directory
    python main.py --project-dir ~/my-project "Create a Python script that fetches weather data"

    # Run in current directory (if not sweagent's own directory)
    python main.py "Fix the bug in main.py"

    # Interactive mode
    python main.py --project-dir ~/my-project --interactive

    # Use a different model
    python main.py --project-dir ~/my-project --model openai/gpt-4o "Analyze this code"

    # Load custom tools
    python main.py --project-dir ~/my-project --load-tools ./my_tools.py "Use my custom tool"

    # Enable sub-agents
    python main.py --project-dir ~/my-project --subagents "Research this codebase and write tests"

    # Resume a session
    python main.py --resume abc123def456
"""
    )
    
    parser.add_argument(
        "task",
        nargs="?",
        help="Task to execute (required unless --interactive or --resume)"
    )
    
    parser.add_argument(
        "-i", "--interactive",
        action="store_true",
        help="Run in interactive REPL mode"
    )
    
    parser.add_argument(
        "-m", "--model",
        default="anthropic/claude-sonnet-4-20250514",
        help="Model to use (default: anthropic/claude-sonnet-4-20250514)"
    )
    
    parser.add_argument(
        "-p", "--project-dir",
        default=None,
        help="Project directory where generated code will be placed (required, or use current dir if safe)"
    )
    
    parser.add_argument(
        "-t", "--load-tools",
        metavar="PATH",
        help="Load additional tools from a Python module"
    )
    
    parser.add_argument(
        "-s", "--subagents",
        action="store_true",
        help="Enable sub-agent spawning capability"
    )
    
    parser.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="Resume a previous session"
    )
    
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="List available sessions to resume"
    )
    
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=True,
        help="Verbose output (default: True)"
    )
    
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Quiet mode (minimal output)"
    )
    
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=30,
        help="Maximum agent loop iterations (default: 30)"
    )
    
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="LLM temperature (default: 0.0)"
    )
    
    parser.add_argument(
        "--system-prompt",
        metavar="PATH",
        help="Path to custom system prompt file"
    )
    
    return parser.parse_args()


def get_package_dir() -> Path:
    """Get the directory where the sweagent package is installed."""
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
        print(f"Error: Cannot use sweagent's own directory as project directory.")
        print(f"  Package location: {package_dir}")
        print(f"  Requested project dir: {project_path}")
        print()
        print("Please specify a different directory with --project-dir:")
        print("  sweagent --project-dir ~/my-project \"your task\"")
        sys.exit(1)

    # Warn if package is inside project dir (but allow it)
    if package_dir.is_relative_to(project_path) and project_path != Path.home():
        print(f"Warning: Project directory contains sweagent package at {package_dir}")
        print("         Be careful not to modify agent source files.")
        print()

    return project_path


def main():
    args = parse_args()

    # Handle list sessions
    if args.list_sessions:
        manager = StateManager()
        sessions = manager.list_sessions()
        if not sessions:
            print("No saved sessions found.")
        else:
            print("\nAvailable Sessions:")
            print("-" * 60)
            for s in sessions:
                print(f"  {s['session_id']}  |  {s['updated_at'][:19]}  |  {s['task_count']} tasks")
        return
    
    # Validate args
    if not args.task and not args.interactive and not args.resume:
        print("Error: Must provide a task, use --interactive, or --resume")
        sys.exit(1)
    
    # Verbose setting
    verbose = not args.quiet and args.verbose

    # Resolve and validate project directory
    project_dir_arg = args.project_dir or os.getcwd()
    project_dir = validate_project_dir(project_dir_arg)

    # Create project directory if it doesn't exist
    project_dir.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(f"Project directory: {project_dir}")

    # Load custom system prompt if provided
    system_prompt = None
    if args.system_prompt:
        system_prompt = Path(args.system_prompt).read_text()
    
    # Create tool registry
    tools = create_default_registry(str(project_dir))
    
    # Load additional tools if specified
    if args.load_tools:
        tools.load_from_module(args.load_tools)
        if verbose:
            print(f"Loaded tools from: {args.load_tools}")
    
    # Create the agent
    if args.subagents:
        agent = create_agent_with_subagents(
            model=args.model,
            working_dir=str(project_dir),
            verbose=verbose
        )
    else:
        config = AgentConfig(
            model=args.model,
            working_dir=str(project_dir),
            verbose=verbose,
            max_iterations=args.max_iterations,
            temperature=args.temperature
        )
        agent = AgentLoop(config=config, tools=tools, system_prompt=system_prompt)
    
    # Resume session if specified
    if args.resume:
        if agent.load_session(args.resume):
            if verbose:
                print(f"Resumed session: {args.resume}")
        else:
            print(f"Error: Session not found: {args.resume}")
            sys.exit(1)
    
    # Run the agent
    if args.interactive:
        agent.run_interactive()
    else:
        result = agent.run(args.task)
        print("\nResult:")
        print(result)


if __name__ == "__main__":
    main()
