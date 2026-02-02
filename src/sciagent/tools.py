"""
Tool System - Registry, execution, and dynamic loading of tools
"""
import os
import json
import subprocess
import inspect
from typing import Dict, Any, List, Optional, Callable, Union
from dataclasses import dataclass, field
from abc import ABC, abstractmethod
from pathlib import Path


@dataclass
class ToolResult:
    """Result from tool execution"""
    success: bool
    output: Any
    error: Optional[str] = None
    
    def to_message(self) -> str:
        """Format for LLM consumption"""
        if self.success:
            if isinstance(self.output, dict):
                return json.dumps(self.output, indent=2)
            return str(self.output)
        else:
            return f"Error: {self.error}"


class BaseTool(ABC):
    """Base class for all tools"""
    
    name: str
    description: str
    parameters: Dict[str, Any]
    
    @abstractmethod
    def execute(self, **kwargs) -> ToolResult:
        """Execute the tool with given arguments"""
        pass
    
    def to_schema(self) -> Dict:
        """Convert to OpenAI-style tool schema"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


class FunctionTool(BaseTool):
    """Wrap a Python function as a tool"""
    
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
        """Infer parameter schema from function signature"""
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


class ToolRegistry:
    """
    Central registry for all available tools
    
    Supports:
    - Registering tools programmatically
    - Loading tools from Python modules
    - Loading tools from JSON schemas
    """
    
    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}
        
    def register(self, tool: Union[BaseTool, Callable], **kwargs) -> None:
        """Register a tool"""
        if callable(tool) and not isinstance(tool, BaseTool):
            tool = FunctionTool(tool, **kwargs)
        self._tools[tool.name] = tool
        
    def unregister(self, name: str) -> None:
        """Remove a tool"""
        if name in self._tools:
            del self._tools[name]
    
    def get(self, name: str) -> Optional[BaseTool]:
        """Get a tool by name"""
        return self._tools.get(name)
    
    def list_tools(self) -> List[str]:
        """List all registered tool names"""
        return list(self._tools.keys())
    
    def get_schemas(self) -> List[Dict]:
        """Get all tool schemas for LLM"""
        return [tool.to_schema() for tool in self._tools.values()]
    
    def execute(self, name: str, **kwargs) -> ToolResult:
        """Execute a tool by name"""
        tool = self.get(name)
        if not tool:
            return ToolResult(
                success=False,
                output=None,
                error=f"Tool '{name}' not found. Available: {self.list_tools()}"
            )
        return tool.execute(**kwargs)
    
    def load_from_module(self, module_path: str) -> None:
        """
        Load tools from a Python module
        
        The module should have a `register_tools(registry)` function
        or a `TOOLS` list of tool instances
        """
        import importlib.util
        
        spec = importlib.util.spec_from_file_location("tools_module", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        
        # Try register_tools function first
        if hasattr(module, 'register_tools'):
            module.register_tools(self)
        # Fall back to TOOLS list
        elif hasattr(module, 'TOOLS'):
            for tool in module.TOOLS:
                self.register(tool)
        else:
            # Auto-discover functions with @tool decorator
            for name, obj in inspect.getmembers(module):
                if hasattr(obj, '_is_tool') and obj._is_tool:
                    self.register(obj)


def tool(name: Optional[str] = None, description: Optional[str] = None):
    """Decorator to mark a function as a tool"""
    def decorator(func):
        func._is_tool = True
        func._tool_name = name
        func._tool_description = description
        return func
    return decorator


# =============================================================================
# Built-in Core Tools
# =============================================================================

class BashTool(BaseTool):
    """Execute bash commands with adaptive retry on timeout"""

    name = "bash"
    description = """Execute shell commands with automatic retry on timeout.

BEHAVIOR:
- Auto-retries up to 3 times with exponential backoff
- Timeouts: 30s → 60s → 120s → 240s (or starts higher for known slow commands)
- On final failure, suggests alternative approach

WHEN TO USE: scripts, builds, tests, installs, git operations
WHEN NOT TO USE: reading files (use view), writing files (use write_file)"""

    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute"
            },
            "timeout": {
                "type": "integer",
                "description": "Initial timeout in seconds (default: 30, auto-extends on retry)"
            }
        },
        "required": ["command"]
    }

    # Max retries for timeout
    MAX_RETRIES = 3

    def __init__(self, working_dir: str = "."):
        self.working_dir = working_dir

    def _get_initial_timeout(self, command: str) -> int:
        """Estimate initial timeout based on command type."""
        cmd_lower = command.lower()

        # These commands are known to be slow - start with higher timeout
        if any(x in cmd_lower for x in ['create-react-app', 'create-next-app', 'ng new', 'vue create']):
            return 300  # 5 minutes for scaffolding tools
        if any(x in cmd_lower for x in ['npm install', 'yarn install', 'pip install', 'cargo build']):
            return 180  # 3 minutes for package installs
        if any(x in cmd_lower for x in ['git clone', 'docker build', 'docker pull']):
            return 180  # 3 minutes for network-heavy ops
        if any(x in cmd_lower for x in ['npm run build', 'make', 'cargo build --release']):
            return 120  # 2 minutes for builds
        if any(x in cmd_lower for x in ['pytest', 'npm test', 'cargo test']):
            return 120  # 2 minutes for tests

        return 30  # Default: 30 seconds

    def execute(self, command: str, timeout: int = None) -> ToolResult:
        """Execute with automatic retry on timeout."""

        # Use smart default if not specified
        if timeout is None:
            timeout = self._get_initial_timeout(command)

        last_error = None

        for attempt in range(self.MAX_RETRIES + 1):
            current_timeout = timeout * (2 ** attempt)  # Exponential backoff

            if attempt > 0:
                print(f"  ⏱️  Retry {attempt}/{self.MAX_RETRIES}: timeout {current_timeout}s")

            try:
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=current_timeout,
                    cwd=self.working_dir
                )

                output = ""
                if result.stdout:
                    output += result.stdout
                if result.stderr:
                    output += f"\n[stderr]\n{result.stderr}" if output else result.stderr

                return ToolResult(
                    success=result.returncode == 0,
                    output=output.strip() or "(no output)",
                    error=None if result.returncode == 0 else f"Exit code: {result.returncode}"
                )

            except subprocess.TimeoutExpired:
                last_error = f"Timeout after {current_timeout}s"
                if attempt < self.MAX_RETRIES:
                    continue  # Retry with longer timeout

            except Exception as e:
                # Non-timeout errors don't retry
                return ToolResult(success=False, output=None, error=str(e))

        # All retries exhausted
        return ToolResult(
            success=False,
            output=None,
            error=f"{last_error} (after {self.MAX_RETRIES} retries). "
                  f"Suggestion: Try alternative approach - create files manually instead of using scaffolding tools."
        )


class ViewTool(BaseTool):
    """View file contents or directory listings"""
    
    name = "view"
    description = "View the contents of a file or list directory contents. For files, shows line numbers."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to file or directory"
            },
            "start_line": {
                "type": "integer",
                "description": "Start line number (1-indexed, for files only)"
            },
            "end_line": {
                "type": "integer",
                "description": "End line number (1-indexed, -1 for end of file)"
            }
        },
        "required": ["path"]
    }
    
    def execute(self, path: str, start_line: int = None, end_line: int = None) -> ToolResult:
        try:
            p = Path(path).expanduser()
            
            if not p.exists():
                return ToolResult(success=False, output=None, error=f"Path not found: {path}")
            
            if p.is_dir():
                # List directory
                items = []
                for item in sorted(p.iterdir()):
                    prefix = "📁 " if item.is_dir() else "📄 "
                    items.append(f"{prefix}{item.name}")
                return ToolResult(success=True, output="\n".join(items) or "(empty directory)")
            
            else:
                # Read file
                content = p.read_text()
                lines = content.splitlines()
                
                # Apply line range
                if start_line is not None:
                    start_idx = max(0, start_line - 1)
                    end_idx = len(lines) if end_line == -1 or end_line is None else end_line
                    lines = lines[start_idx:end_idx]
                    line_offset = start_idx
                else:
                    line_offset = 0
                
                # Add line numbers
                numbered = []
                for i, line in enumerate(lines):
                    line_num = i + line_offset + 1
                    numbered.append(f"{line_num:4d} │ {line}")
                
                return ToolResult(success=True, output="\n".join(numbered))
                
        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))


class WriteFileTool(BaseTool):
    """Create or overwrite a file"""

    name = "write_file"
    description = "Create a new file or overwrite existing file with content"
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to create/write"
            },
            "content": {
                "type": "string",
                "description": "Content to write to the file"
            }
        },
        "required": ["path", "content"]
    }

    # Package directory - protected from writes
    _package_dir: Path = Path(__file__).parent.resolve()

    def __init__(self, working_dir: str = "."):
        self.working_dir = Path(working_dir).resolve()

    def _is_protected_path(self, path: Path) -> bool:
        """Check if path is within the package directory (protected)."""
        resolved = path.resolve()
        return resolved == self._package_dir or self._package_dir in resolved.parents

    def execute(self, path: str, content: str) -> ToolResult:
        try:
            p = Path(path).expanduser()

            # If relative path, resolve relative to working_dir
            if not p.is_absolute():
                p = self.working_dir / p

            # Block writes to package directory
            if self._is_protected_path(p):
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Cannot write to sweagent package directory: {p}"
                )

            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return ToolResult(success=True, output=f"✓ Wrote {len(content)} chars to {path}")
        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))


class StrReplaceTool(BaseTool):
    """Replace text in a file (must be unique match)"""

    name = "str_replace"
    description = "Replace a unique string in a file. The old_str must appear exactly once in the file."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to edit"
            },
            "old_str": {
                "type": "string",
                "description": "The exact string to replace (must be unique in file)"
            },
            "new_str": {
                "type": "string",
                "description": "The string to replace it with"
            }
        },
        "required": ["path", "old_str", "new_str"]
    }

    # Package directory - protected from edits
    _package_dir: Path = Path(__file__).parent.resolve()

    def __init__(self, working_dir: str = "."):
        self.working_dir = Path(working_dir).resolve()

    def _is_protected_path(self, path: Path) -> bool:
        """Check if path is within the package directory (protected)."""
        resolved = path.resolve()
        return resolved == self._package_dir or self._package_dir in resolved.parents

    def execute(self, path: str, old_str: str, new_str: str) -> ToolResult:
        try:
            p = Path(path).expanduser()

            # If relative path, resolve relative to working_dir
            if not p.is_absolute():
                p = self.working_dir / p

            # Block edits to package directory
            if self._is_protected_path(p):
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Cannot edit files in sweagent package directory: {p}"
                )

            if not p.exists():
                return ToolResult(success=False, output=None, error=f"File not found: {path}")

            content = p.read_text()
            count = content.count(old_str)

            if count == 0:
                return ToolResult(success=False, output=None, error="String not found in file")
            if count > 1:
                return ToolResult(success=False, output=None, error=f"String appears {count} times (must be unique)")

            new_content = content.replace(old_str, new_str)
            p.write_text(new_content)

            return ToolResult(success=True, output=f"✓ Replaced in {p}")

        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))


def create_default_registry(working_dir: str = ".") -> ToolRegistry:
    """Create a registry with default tools.

    Uses the atomic tool set (5 tools) which includes:
    - bash: Shell execution with smart timeouts
    - file_ops: Read/write/edit/list files
    - search: Glob and grep for files and content
    - web: Search the web and fetch URLs
    - todo: Task tracking with dependencies
    """
    # Try to use the atomic tools (preferred)
    try:
        from tools.atomic.shell import ShellTool
        from tools.atomic.file_ops import FileOpsTool
        from tools.atomic.search import SearchTool
        from tools.atomic.web import WebTool
        from tools.atomic.todo import TodoTool

        registry = ToolRegistry()
        registry.register(ShellTool(working_dir))
        registry.register(FileOpsTool(working_dir))
        registry.register(SearchTool(working_dir))
        registry.register(WebTool())
        registry.register(TodoTool())
        return registry
    except ImportError:
        # Fallback to basic tools if atomic tools not available
        registry = ToolRegistry()
        registry.register(BashTool(working_dir))
        registry.register(ViewTool())
        registry.register(WriteFileTool(working_dir))
        registry.register(StrReplaceTool(working_dir))
        return registry
