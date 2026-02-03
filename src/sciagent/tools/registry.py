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
    """

    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}

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

    def execute(self, name: str, **kwargs) -> ToolResult:
        """Execute a tool by name."""
        tool = self.get(name)
        if not tool:
            return ToolResult(
                success=False,
                output=None,
                error=f"Tool '{name}' not found. Available: {self.list_tools()}"
            )

        # Warn if no arguments provided (likely LLM response truncation)
        if not kwargs:
            return ToolResult(
                success=False,
                output=None,
                error=f"Tool '{name}' called with no arguments. This may indicate response truncation."
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


def create_atomic_registry(working_dir: str = ".") -> ToolRegistry:
    """
    Create registry with the 7 atomic tools.

    This is the minimal tool set for scientific/engineering tasks:
    - bash: Shell execution
    - file_ops: Read/write/edit files (filesystem is memory)
    - search: Find files (glob) and content (grep)
    - web: Search and fetch web content
    - todo: Track task progress
    - service: Run code in containerized simulation environments (RCWA, MEEP, etc.)
    - ask_user: Request user input for decisions/clarifications

    Total: 7 tools
    """
    from .atomic.shell import ShellTool
    from .atomic.file_ops import FileOpsTool
    from .atomic.search import SearchTool
    from .atomic.web import WebTool
    from .atomic.todo import TodoTool
    from .atomic.service import ServiceTool
    from .atomic.ask_user import AskUserTool

    registry = ToolRegistry()

    registry.register(ShellTool(working_dir))
    registry.register(FileOpsTool(working_dir))
    registry.register(SearchTool(working_dir))
    registry.register(WebTool())
    registry.register(TodoTool())
    registry.register(ServiceTool(working_dir))
    registry.register(AskUserTool())

    return registry


def create_default_registry(working_dir: str = ".") -> ToolRegistry:
    """Alias for create_atomic_registry - backward compatibility."""
    return create_atomic_registry(working_dir)


# For testing
if __name__ == "__main__":
    registry = create_atomic_registry()
    print(f"Registered tools: {registry.list_tools()}")
    print(f"\nSchemas:")
    for schema in registry.get_schemas():
        print(f"  - {schema['name']}: {schema['description'][:50]}...")
