"""
Tool Registry - load and manage the atomic tool set.

This module provides the central registry for tools. It loads
only the 7 atomic tools by default, keeping context minimal.
"""

from __future__ import annotations

import inspect
from typing import Dict, Any, List, Optional, Union, Callable


class ToolResult:
    """Result from tool execution."""

    def __init__(self, success: bool, output: Any, error: Optional[str] = None):
        self.success = success
        self.output = output
        self.error = error

    def to_message(self) -> str:
        """Format for LLM consumption."""
        if self.success:
            if isinstance(self.output, dict):
                import json
                return json.dumps(self.output, indent=2)
            return str(self.output)
        else:
            return f"Error: {self.error}"


class BaseTool:
    """Base class for tools.

    Subclasses should define:
    - name: str - the tool name
    - description: str - what the tool does
    - parameters: dict - JSON schema for parameters
    - execute(**kwargs) -> ToolResult - the implementation

    Interrupt contract (for tools that may block longer than ~5s on a
    network call, subprocess, or polling loop):

      - The AgentLoop sets ``BaseTool._shared_interrupt_event`` to its
        own threading.Event at startup. The signal handler sets that
        event on Ctrl+C.
      - Tools that block in a poll loop should replace ``time.sleep(N)``
        with ``self._shared_interrupt_event.wait(N)`` so a Ctrl+C wakes
        the wait immediately.
      - Tools that do a single blocking RPC should check
        ``self._shared_interrupt_event.is_set()`` before the call and
        bail with a structured "interrupted" result if so.
      - Standalone callers (no AgentLoop) leave the event unset; the
        helpers below tolerate that (``None`` → skip the check).

    The shared event is class-level (not instance-level) so all tools
    across all subagents in the process see the same event. The
    AgentLoop wires it once at construction time.
    """

    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}

    # Wired by AgentLoop.__init__. Class-level so subclass instances
    # across the process share it without per-instance plumbing.
    _shared_interrupt_event = None  # type: ignore[var-annotated]

    @classmethod
    def set_shared_interrupt_event(cls, event):
        """Wire the AgentLoop's interrupt event into all tools."""
        cls._shared_interrupt_event = event

    @classmethod
    def is_interrupted(cls) -> bool:
        """True when a Ctrl+C has been signaled and the agent is asking
        tools to bail. False when no event is wired (standalone use)."""
        ev = cls._shared_interrupt_event
        return bool(ev is not None and ev.is_set())

    @classmethod
    def interruptible_sleep(cls, seconds: float) -> bool:
        """Sleep up to ``seconds``, but wake immediately on interrupt.

        Returns True when the wait was interrupted (caller should bail),
        False when the full duration elapsed (caller should continue).
        Falls back to plain time.sleep when no event is wired.
        """
        import time as _time
        ev = cls._shared_interrupt_event
        if ev is None:
            _time.sleep(seconds)
            return False
        return ev.wait(seconds)

    def execute(self, **kwargs) -> ToolResult:
        """Execute the tool. Override in subclasses."""
        raise NotImplementedError("Subclasses must implement execute()")

    def to_schema(self) -> Dict[str, Any]:
        """Convert to LLM tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters
        }


class FunctionTool(BaseTool):
    """Wrap a Python function as a tool."""

    def __init__(
        self,
        func: Callable,
        name: Optional[str] = None,
        description: Optional[str] = None,
        parameters: Optional[Dict] = None
    ):
        self.func = func
        self.name = name or func.__name__
        self.description = description or func.__doc__ or f"Execute {self.name}"
        self.parameters = parameters or self._infer_parameters()

    def _infer_parameters(self) -> Dict:
        """Infer parameter schema from function signature."""
        sig = inspect.signature(self.func)
        properties = {}
        required = []

        for param_name, param in sig.parameters.items():
            if param_name in ('self', 'cls'):
                continue

            prop = {"type": "string"}  # Default to string

            # Try to infer type from annotation
            if param.annotation != inspect.Parameter.empty:
                if param.annotation == int:
                    prop["type"] = "integer"
                elif param.annotation == float:
                    prop["type"] = "number"
                elif param.annotation == bool:
                    prop["type"] = "boolean"
                elif param.annotation == list:
                    prop["type"] = "array"
                elif param.annotation == dict:
                    prop["type"] = "object"

            properties[param_name] = prop

            # Required if no default
            if param.default == inspect.Parameter.empty:
                required.append(param_name)

        return {
            "type": "object",
            "properties": properties,
            "required": required
        }

    def execute(self, **kwargs) -> ToolResult:
        try:
            result = self.func(**kwargs)
            return ToolResult(success=True, output=result)
        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))


def tool(name: Optional[str] = None, description: Optional[str] = None):
    """Decorator to mark a function as a tool."""
    def decorator(func):
        func._is_tool = True
        func._tool_name = name
        func._tool_description = description
        return func
    return decorator


class ToolRegistry:
    """Central registry for tools."""

    def __init__(self):
        self._tools: Dict[str, Any] = {}

    def register(self, tool: Any) -> None:
        """Register a tool instance."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Remove a tool."""
        if name in self._tools:
            del self._tools[name]

    def get(self, name: str) -> Optional[Any]:
        """Get tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> List[str]:
        """List registered tool names."""
        return list(self._tools.keys())

    def get_schemas(self) -> List[Dict]:
        """Get all tool schemas for LLM."""
        return [tool.to_schema() for tool in self._tools.values()]

    def clone(self, exclude: Optional[set] = None) -> "ToolRegistry":
        """Return a new registry with the same tool instances, optionally
        omitting the names in ``exclude``.

        Used to give the main agent a different view of the toolset than
        the subagents (e.g., main agent doesn't see compute_*, those tools
        are reachable only via the `compute` subagent — which keeps cloud
        chatter inside its own context bubble per subagent.py:380-388).
        Tools are shared by reference; both registries see the same
        underlying instance, so a stateful tool (e.g., ComputeTool's
        shared session) stays consistent.
        """
        excluded = set(exclude or ())
        new_registry = ToolRegistry()
        for name, tool in self._tools.items():
            if name in excluded:
                continue
            new_registry.register(tool)
        return new_registry

    def execute(self, name: str, **kwargs) -> ToolResult:
        """Execute a tool by name."""
        tool = self.get(name)
        if not tool:
            return ToolResult(
                success=False,
                output=None,
                error=f"Tool '{name}' not found. Available: {self.list_tools()}"
            )

        # Some tools are legitimately callable with no args (bg_status to
        # list all jobs, todo to show the current graph). Others have
        # required args and a no-args call is a strong truncation signal.
        # Distinguish by introspecting tool.execute's signature: if every
        # non-self parameter has a default, no-args is fine.
        if not kwargs:
            import inspect
            try:
                sig = inspect.signature(tool.execute)
                required = [
                    p
                    for p in sig.parameters.values()
                    if p.name != "self"
                    and p.default is inspect.Parameter.empty
                    and p.kind not in (
                        inspect.Parameter.VAR_POSITIONAL,
                        inspect.Parameter.VAR_KEYWORD,
                    )
                ]
            except (TypeError, ValueError):
                required = []  # Couldn't introspect; let the call through.
            if required:
                return ToolResult(
                    success=False,
                    output=None,
                    error=(
                        f"Tool '{name}' called with no arguments but requires "
                        f"{[p.name for p in required]}. This may indicate "
                        f"response truncation."
                    ),
                )

        try:
            result = tool.execute(**kwargs)
        except TypeError as e:
            # Catch missing argument errors and provide helpful message
            return ToolResult(
                success=False,
                output=None,
                error=f"Tool '{name}' argument error: {e}. Received args: {list(kwargs.keys())}"
            )

        # Normalize result
        if isinstance(result, ToolResult):
            return result
        elif hasattr(result, 'success'):
            return ToolResult(result.success, result.output, getattr(result, 'error', None))
        else:
            return ToolResult(success=True, output=result)


def create_atomic_registry(working_dir: str = ".", skills_dir=None) -> ToolRegistry:
    """
    Create registry with the atomic tools.

    This is the minimal tool set for scientific/engineering tasks:
    - bash: Shell execution (including Docker commands for simulation services)
    - file_ops: Read/write/edit files (filesystem is memory)
    - search: Find files (glob) and content (grep)
    - web: Search and fetch web content
    - todo: Track task progress
    - ask_user: Request user input for decisions/clarifications
    - skill: Load specialized workflow skills (if skills exist)

    Background task management:
    - Use bash(command="...", background=True) for long-running commands
    - bg_status: Check status of background jobs
    - bg_output: Get output from a background job
    - bg_wait: Wait for a background job to complete
    - bg_kill: Terminate a background job

    Compute:
    - compute_run: Run containerized compute jobs (background by default)

    For simulation services (RCWA, MEEP, OpenFOAM, etc.), the agent uses
    compute_run with service= parameter. See services/registry.yaml for available images.

    Total: 11-12 tools (skill tool only added if skills exist)
    """
    from .atomic.shell import ShellTool
    from .atomic.file_ops import FileOpsTool
    from .atomic.search import SearchTool
    from .atomic.web import WebTool
    from .atomic.todo import TodoTool
    from .atomic.ask_user import AskUserTool
    from .atomic.bg_tools import BgStatusTool, BgOutputTool, BgWaitTool, BgKillTool
    from .atomic.task_tools import TaskListTool, TaskGetTool, TaskWaitTool
    from .atomic.compute import ComputeTool
    from .atomic.compute_exec import ComputeExecTool
    from .atomic.compute_cluster import ComputeClusterTool
    from .atomic.service_search import ServiceSearchTool
    from .atomic.monitor import MonitorTool, MonitorStopTool

    registry = ToolRegistry()

    # Core tools
    registry.register(ShellTool(working_dir))
    registry.register(FileOpsTool(working_dir))
    registry.register(SearchTool(working_dir))
    registry.register(WebTool())
    registry.register(TodoTool())
    registry.register(AskUserTool())

    # Background job management tools (kind-specific runtime surface).
    registry.register(BgStatusTool(working_dir))
    registry.register(BgOutputTool(working_dir))
    registry.register(BgWaitTool(working_dir))
    registry.register(BgKillTool(working_dir))

    # In-flight registry tools (kind-agnostic registry surface). These
    # complement bg_*: bg_* answers "what is THIS specific job doing right
    # now"; task_* answers "what's tracked across all kinds + sessions".
    # task_wait blocks until terminal state; works on any kind (PR4).
    registry.register(TaskListTool())
    registry.register(TaskGetTool())
    registry.register(TaskWaitTool())

    # Compute tool
    registry.register(ComputeTool(working_dir))

    # Cluster-mode follow-ups (sky.exec on a warm cluster) and lifecycle
    # surface (status / down / autostop / refresh_mounts). compute_run
    # picks managed-jobs vs cluster mode via the mode= kwarg; these two
    # tools cover everything else the agent needs for cluster-mode
    # iteration.
    registry.register(ComputeExecTool(working_dir))
    registry.register(ComputeClusterTool(working_dir))

    # Service registry discovery — keyword search across name, description,
    # packages, and capabilities. Cheaper than reading registry.yaml (which
    # file_ops truncates) and tolerant of case mismatch.
    registry.register(ServiceSearchTool())

    # Background monitors — push-style stdout-line events. Pairs with the
    # wait_until tools (Phase 1): wait_* blocks on ONE thing in one tool
    # call; monitor reacts to whichever happens first across MANY. Drain
    # hook in agent.py turns each line into a <system-reminder> on the
    # next turn — no LLM round-trip per event.
    registry.register(MonitorTool())
    registry.register(MonitorStopTool())

    # Add skill tool if skills exist
    try:
        from ..skills import SkillLoader
        from .atomic.skill import SkillTool

        loader = SkillLoader(skills_dir)
        if loader.skills:
            registry.register(SkillTool(loader))
    except ImportError:
        pass  # Skills module not available

    return registry


def create_default_registry(working_dir: str = ".", skills_dir=None) -> ToolRegistry:
    """Alias for create_atomic_registry - backward compatibility."""
    return create_atomic_registry(working_dir, skills_dir)


# For testing
if __name__ == "__main__":
    registry = create_atomic_registry()
    print(f"Registered tools: {registry.list_tools()}")
    print(f"\nSchemas:")
    for schema in registry.get_schemas():
        print(f"  - {schema['name']}: {schema['description'][:50]}...")
