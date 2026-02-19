"""
Todo tool - Dependency-aware task tracking for long-horizon work.

FEATURES:
1. Task IDs for referencing
2. Dependency tracking (depends_on)
3. Result storage and passing
4. Task types (research, code, validate, review)
5. Parallel execution hints
6. Automatic dependency resolution
"""

from __future__ import annotations

import csv
import os
import re
from typing import Dict, Any, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime
import uuid


# =============================================================================
# CONTENT VALIDATION - Detect fabricated/error data
# =============================================================================

class ContentValidator:
    """
    Validates file content to detect fabrication or error pages.

    This provides external evidence that cannot be fabricated by the model.
    Checks include:
    - HTML/error page detection (suggests 404 or wrong content)
    - CSV structure validation
    - Row count verification
    - Common error patterns
    """

    # Error page indicators
    ERROR_PATTERNS = [
        r"404\s*not\s*found",
        r"page\s*not\s*found",
        r"file\s*not\s*found",
        r"access\s*denied",
        r"403\s*forbidden",
        r"500\s*internal\s*server",
        r"502\s*bad\s*gateway",
        r"503\s*service\s*unavailable",
        r"error\s*loading",
        r"failed\s*to\s*load",
        r"could\s*not\s*be\s*found",
        r"the\s*requested\s*url\s*was\s*not\s*found",
    ]

    # HTML structure indicators
    HTML_PATTERNS = [
        r"<!doctype\s+html",
        r"<html[\s>]",
        r"<head[\s>]",
        r"<body[\s>]",
        r"<script[\s>]",
        r"<style[\s>]",
        r"<meta[\s>]",
        r"<link[\s>]",
    ]

    @classmethod
    def is_error_content(cls, content: str) -> Tuple[bool, List[str]]:
        """
        Check if content appears to be an error page.

        Returns (is_error, list of matched patterns).
        """
        content_lower = content.lower()[:5000]  # Check first 5KB
        matched = []

        for pattern in cls.ERROR_PATTERNS:
            if re.search(pattern, content_lower, re.IGNORECASE):
                matched.append(pattern)

        return len(matched) > 0, matched

    @classmethod
    def is_html_content(cls, content: str, expected_type: str = None) -> Tuple[bool, List[str]]:
        """
        Check if content is HTML when it shouldn't be.

        Args:
            content: File content
            expected_type: Expected content type (e.g., 'csv', 'json', 'data')

        Returns (is_html, list of matched patterns).
        """
        content_lower = content.lower()[:2000]
        matched = []

        for pattern in cls.HTML_PATTERNS:
            if re.search(pattern, content_lower, re.IGNORECASE):
                matched.append(pattern)

        # Only flag if we expected non-HTML content
        if expected_type and expected_type.lower() in ('csv', 'json', 'data', 'txt', 'xml'):
            return len(matched) > 0, matched

        # If no expected type, be conservative - only flag if multiple HTML indicators
        return len(matched) >= 3, matched

    @classmethod
    def validate_csv_file(
        cls,
        file_path: str,
        min_rows: int = None,
        max_rows: int = None,
        expected_rows: int = None,
        required_columns: List[str] = None
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        Validate a CSV file structure and content.

        Returns (is_valid, error_message, metadata).
        """
        metadata = {
            "row_count": 0,
            "column_count": 0,
            "columns": [],
            "has_header": False,
        }

        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                # Read first 1000 chars to check for HTML
                sample = f.read(1000)
                f.seek(0)

                # Check for HTML content
                is_html, html_patterns = cls.is_html_content(sample, expected_type='csv')
                if is_html:
                    return False, f"CSV file contains HTML content (patterns: {html_patterns[:3]})", metadata

                # Check for error page content
                is_error, error_patterns = cls.is_error_content(sample)
                if is_error:
                    return False, f"CSV file contains error page content (patterns: {error_patterns[:3]})", metadata

                # Parse CSV
                f.seek(0)
                reader = csv.reader(f)
                rows = list(reader)

                if not rows:
                    return False, "CSV file is empty", metadata

                # Assume first row is header
                header = rows[0]
                data_rows = rows[1:]

                metadata["has_header"] = True
                metadata["columns"] = header
                metadata["column_count"] = len(header)
                metadata["row_count"] = len(data_rows)

                # Validate required columns
                if required_columns:
                    missing = [col for col in required_columns if col not in header]
                    if missing:
                        return False, f"Missing required columns: {missing}", metadata

                # Validate row count
                if expected_rows is not None:
                    if metadata["row_count"] != expected_rows:
                        return False, f"Expected {expected_rows} rows, found {metadata['row_count']}", metadata

                if min_rows is not None:
                    if metadata["row_count"] < min_rows:
                        return False, f"Expected at least {min_rows} rows, found {metadata['row_count']}", metadata

                if max_rows is not None:
                    if metadata["row_count"] > max_rows:
                        return False, f"Expected at most {max_rows} rows, found {metadata['row_count']}", metadata

                return True, None, metadata

        except csv.Error as e:
            return False, f"CSV parsing error: {e}", metadata
        except Exception as e:
            return False, f"Error reading CSV file: {e}", metadata

    @classmethod
    def validate_json_file(cls, file_path: str) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """Validate a JSON file."""
        import json
        metadata = {"type": None, "size": 0}

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                sample = f.read(1000)
                f.seek(0)

                # Check for HTML
                is_html, _ = cls.is_html_content(sample, expected_type='json')
                if is_html:
                    return False, "JSON file contains HTML content", metadata

                # Check for error page
                is_error, _ = cls.is_error_content(sample)
                if is_error:
                    return False, "JSON file contains error page content", metadata

                # Parse JSON
                f.seek(0)
                data = json.load(f)
                metadata["type"] = type(data).__name__
                metadata["size"] = len(data) if hasattr(data, '__len__') else 1

                return True, None, metadata

        except json.JSONDecodeError as e:
            return False, f"Invalid JSON: {e}", metadata
        except Exception as e:
            return False, f"Error reading JSON file: {e}", metadata

    @classmethod
    def validate_file_content(
        cls,
        file_path: str,
        expected_type: str = None,
        **kwargs
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """
        Validate file content based on type.

        Args:
            file_path: Path to file
            expected_type: Expected file type ('csv', 'json', 'data', etc.)
            **kwargs: Type-specific validation options
                - min_rows, max_rows, expected_rows: For CSV
                - required_columns: For CSV

        Returns (is_valid, error_message, metadata).
        """
        if not os.path.exists(file_path):
            return False, f"File not found: {file_path}", {}

        # Determine type from extension if not specified
        if expected_type is None:
            ext = os.path.splitext(file_path)[1].lower()
            expected_type = {
                '.csv': 'csv',
                '.json': 'json',
                '.txt': 'text',
                '.xml': 'xml',
            }.get(ext, 'data')

        # Type-specific validation
        if expected_type == 'csv':
            return cls.validate_csv_file(
                file_path,
                min_rows=kwargs.get('min_rows'),
                max_rows=kwargs.get('max_rows'),
                expected_rows=kwargs.get('expected_rows'),
                required_columns=kwargs.get('required_columns'),
            )
        elif expected_type == 'json':
            return cls.validate_json_file(file_path)
        else:
            # Generic validation - just check for HTML/error content
            try:
                with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read(5000)

                is_html, html_patterns = cls.is_html_content(content, expected_type=expected_type)
                if is_html:
                    return False, f"File contains HTML content when {expected_type} expected", {"html_patterns": html_patterns}

                is_error, error_patterns = cls.is_error_content(content)
                if is_error:
                    return False, f"File contains error page content", {"error_patterns": error_patterns}

                return True, None, {"size": os.path.getsize(file_path)}

            except Exception as e:
                return False, f"Error reading file: {e}", {}


@dataclass
class ToolResult:
    """Result from tool execution."""
    success: bool
    output: Any
    error: str = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TodoItem:
    """Enhanced todo item with dependency support and data flow."""
    id: str
    content: str
    status: str  # pending, in_progress, completed, blocked, failed
    task_type: str = "general"  # research, code, validate, review, general
    depends_on: List[str] = field(default_factory=list)  # List of task IDs
    result: Optional[Any] = None  # Stores output when completed
    result_key: Optional[str] = None  # Key for passing to dependent tasks
    priority: str = "medium"  # high, medium, low
    can_parallel: bool = True  # Hint for orchestrator
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: Optional[str] = None
    error: Optional[str] = None
    # NEW: Data flow fields
    produces: Optional[str] = None  # Artifact this task produces: "file:<path>" or "data" or "metrics"
    target: Optional[Dict[str, Any]] = None  # Success criteria: {"metric": "name", "operator": ">=", "value": X}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "status": self.status,
            "task_type": self.task_type,
            "depends_on": self.depends_on,
            "result": self.result,
            "result_key": self.result_key,
            "priority": self.priority,
            "can_parallel": self.can_parallel,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "produces": self.produces,
            "target": self.target,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TodoItem":
        return cls(
            id=data.get("id", str(uuid.uuid4())[:8]),
            content=data.get("content", ""),
            status=data.get("status", "pending"),
            task_type=data.get("task_type", data.get("type", "general")),
            depends_on=data.get("depends_on", []),
            result=data.get("result"),
            result_key=data.get("result_key"),
            priority=data.get("priority", "medium"),
            can_parallel=data.get("can_parallel", True),
            created_at=data.get("created_at", datetime.now().isoformat()),
            completed_at=data.get("completed_at"),
            error=data.get("error"),
            produces=data.get("produces"),
            target=data.get("target"),
        )


class TodoGraph:
    """
    Dependency graph for todos.

    Provides:
    - Dependency resolution
    - Topological ordering
    - Ready task identification
    - Result propagation
    """

    def __init__(self):
        self._items: Dict[str, TodoItem] = {}
        self._results: Dict[str, Any] = {}  # result_key -> result mapping

    def add(self, item: TodoItem) -> None:
        """Add a todo item to the graph."""
        self._items[item.id] = item
        if item.result is not None and item.result_key:
            self._results[item.result_key] = item.result

    def get(self, id: str) -> Optional[TodoItem]:
        """Get a todo by ID."""
        return self._items.get(id)

    def update(self, id: str, **kwargs) -> Optional[TodoItem]:
        """Update a todo item."""
        item = self._items.get(id)
        if item:
            for key, value in kwargs.items():
                if hasattr(item, key):
                    setattr(item, key, value)
            # Update results cache if completed with result
            if item.status == "completed" and item.result is not None and item.result_key:
                self._results[item.result_key] = item.result
        return item

    def remove(self, id: str) -> bool:
        """Remove a todo from the graph."""
        if id in self._items:
            del self._items[id]
            return True
        return False

    def get_all(self) -> List[TodoItem]:
        """Get all todos."""
        return list(self._items.values())

    def get_result(self, key: str) -> Optional[Any]:
        """Get a result by key."""
        return self._results.get(key)

    def get_results_for_task(self, task_id: str) -> Dict[str, Any]:
        """Get all results from dependencies of a task."""
        item = self._items.get(task_id)
        if not item:
            return {}

        results = {}
        for dep_id in item.depends_on:
            dep_item = self._items.get(dep_id)
            if dep_item and dep_item.result is not None:
                key = dep_item.result_key or dep_id
                results[key] = dep_item.result

        return results

    def are_dependencies_met(self, task_id: str) -> bool:
        """Check if all dependencies of a task are completed."""
        item = self._items.get(task_id)
        if not item:
            return False

        for dep_id in item.depends_on:
            dep_item = self._items.get(dep_id)
            if not dep_item or dep_item.status != "completed":
                return False

        return True

    def get_ready_tasks(self) -> List[TodoItem]:
        """Get all tasks that are ready to execute (dependencies met, status pending)."""
        ready = []
        for item in self._items.values():
            if item.status == "pending" and self.are_dependencies_met(item.id):
                ready.append(item)
        return ready

    def get_parallel_batch(self) -> List[TodoItem]:
        """Get a batch of tasks that can be executed in parallel."""
        ready = self.get_ready_tasks()
        # Filter to only those marked as parallelizable
        return [t for t in ready if t.can_parallel]

    def get_blocked_tasks(self) -> List[TodoItem]:
        """Get tasks that are blocked by incomplete dependencies."""
        blocked = []
        for item in self._items.values():
            if item.status == "pending" and not self.are_dependencies_met(item.id):
                blocked.append(item)
        return blocked

    def get_execution_order(self) -> List[List[TodoItem]]:
        """
        Get tasks in execution order (topological sort with parallel batching).

        Returns list of batches, where each batch can be executed in parallel.
        """
        # Build adjacency for topological sort
        in_degree: Dict[str, int] = {id: 0 for id in self._items}

        for item in self._items.values():
            for dep_id in item.depends_on:
                if dep_id in self._items:
                    in_degree[item.id] += 1

        # Process in batches
        batches = []
        remaining = set(self._items.keys())

        while remaining:
            # Find all nodes with no remaining dependencies
            batch_ids = [
                id for id in remaining
                if in_degree[id] == 0
            ]

            if not batch_ids:
                # Circular dependency detected
                break

            batch = [self._items[id] for id in batch_ids]
            batches.append(batch)

            # Remove batch from graph
            for id in batch_ids:
                remaining.remove(id)
                # Decrease in-degree of dependents
                for item in self._items.values():
                    if id in item.depends_on:
                        in_degree[item.id] -= 1

        return batches

    def detect_cycles(self) -> List[List[str]]:
        """Detect circular dependencies. Returns list of cycles found."""
        cycles = []
        visited = set()
        rec_stack = set()

        def dfs(node_id: str, path: List[str]) -> None:
            visited.add(node_id)
            rec_stack.add(node_id)
            path.append(node_id)

            item = self._items.get(node_id)
            if item:
                for dep_id in item.depends_on:
                    if dep_id not in visited:
                        dfs(dep_id, path.copy())
                    elif dep_id in rec_stack:
                        # Found cycle
                        cycle_start = path.index(dep_id)
                        cycles.append(path[cycle_start:] + [dep_id])

            rec_stack.remove(node_id)

        for node_id in self._items:
            if node_id not in visited:
                dfs(node_id, [])

        return cycles


class TodoTool:
    """Task list management with dependency tracking."""

    name = "todo"
    description = """Manage task lists with dependency tracking and result passing.

FEATURES:
- Task IDs for referencing between tasks
- Dependency tracking (depends_on field)
- Result storage and passing between tasks
- Task types: research, code, validate, review, general
- Parallel execution hints

SCHEMA:
{
    "id": "task_1",              # Auto-generated if not provided
    "content": "Task description",
    "status": "pending",         # pending, in_progress, completed, blocked, failed
    "task_type": "research",     # research, code, validate, review, general
    "depends_on": ["task_0"],    # IDs of tasks this depends on
    "result": null,              # Output stored when completed
    "result_key": "api_research", # Key for passing result to dependents
    "priority": "high",          # high, medium, low
    "can_parallel": true         # Whether this can run in parallel with siblings
}

COMMANDS:
- Pass todos array to update the full list
- Use depends_on to create task chains
- Set result_key to name outputs for dependent tasks
- Query ready_tasks to see what can execute next"""

    # ANSI color codes for terminal output
    class Colors:
        RESET = "\033[0m"
        BOLD = "\033[1m"
        DIM = "\033[2m"
        # Foreground colors
        GREEN = "\033[32m"
        YELLOW = "\033[33m"
        BLUE = "\033[34m"
        MAGENTA = "\033[35m"
        CYAN = "\033[36m"
        WHITE = "\033[37m"
        RED = "\033[31m"
        # Bright variants
        BRIGHT_GREEN = "\033[92m"
        BRIGHT_YELLOW = "\033[93m"
        BRIGHT_CYAN = "\033[96m"
        BRIGHT_WHITE = "\033[97m"

    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Unique task identifier (auto-generated if not provided)"
                        },
                        "content": {
                            "type": "string",
                            "description": "Task description"
                        },
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed", "blocked", "failed"],
                            "description": "Task status"
                        },
                        "task_type": {
                            "type": "string",
                            "enum": ["research", "code", "validate", "review", "general"],
                            "description": "Type of task",
                            "default": "general"
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "IDs of tasks this depends on"
                        },
                        "result": {
                            "description": "Task output/result (set when completed)"
                        },
                        "result_key": {
                            "type": "string",
                            "description": "Key name for passing result to dependent tasks"
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                            "description": "Task priority",
                            "default": "medium"
                        },
                        "can_parallel": {
                            "type": "boolean",
                            "description": "Whether task can run in parallel with others",
                            "default": True
                        },
                        "produces": {
                            "type": "string",
                            "description": "Artifact this task produces. Format: 'file:<path>' for files, 'data' for structured data, 'metrics' for numeric results"
                        },
                        "target": {
                            "type": "object",
                            "description": "Success criteria. Example: {\"metric\": \"phase_coverage\", \"operator\": \">=\", \"value\": 6.0}",
                            "properties": {
                                "metric": {"type": "string", "description": "Name of the metric to check"},
                                "operator": {"type": "string", "enum": [">=", "<=", ">", "<", "==", "!="]},
                                "value": {"type": "number", "description": "Target value"}
                            }
                        }
                    },
                    "required": ["content", "status"]
                },
                "description": "List of todo items with dependencies"
            },
            "query": {
                "type": "string",
                "enum": ["ready_tasks", "blocked_tasks", "execution_order", "results"],
                "description": "Query the task graph for execution info"
            }
        }
    }

    # Status symbols
    STATUS_SYMBOL = {
        "pending": "☐",
        "in_progress": "◐",
        "completed": "☑",
        "blocked": "⊘",
        "failed": "✗"
    }

    PRIORITY_SYMBOL = {
        "high": "🔴",
        "medium": "🟡",
        "low": "🟢"
    }

    TYPE_SYMBOL = {
        "research": "🔍",
        "code": "💻",
        "validate": "✓",
        "review": "👁",
        "general": "📋"
    }

    def __init__(self):
        self.graph = TodoGraph()

    def execute(self, todos: List[Dict[str, Any]] = None, query: str = None) -> ToolResult:
        """Update todo list or query the task graph."""
        try:
            # Handle queries
            if query:
                return self._handle_query(query)

            # Handle todo updates
            if todos is None:
                return ToolResult(
                    success=True,
                    output=self._format_graph(),
                    metadata={"todos": [t.to_dict() for t in self.graph.get_all()]}
                )

            # Clear and rebuild graph
            self.graph = TodoGraph()

            for i, todo_dict in enumerate(todos):
                # Auto-generate ID if not provided
                if "id" not in todo_dict or not todo_dict["id"]:
                    todo_dict["id"] = f"task_{i}"

                item = TodoItem.from_dict(todo_dict)
                self.graph.add(item)

            # Check for cycles
            cycles = self.graph.detect_cycles()
            if cycles:
                cycle_str = " -> ".join(cycles[0])
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Circular dependency detected: {cycle_str}"
                )

            # Format output
            output = self._format_graph()

            return ToolResult(
                success=True,
                output=output,
                metadata={
                    "todos": [t.to_dict() for t in self.graph.get_all()],
                    "ready_tasks": [t.id for t in self.graph.get_ready_tasks()],
                    "blocked_tasks": [t.id for t in self.graph.get_blocked_tasks()],
                }
            )

        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def _handle_query(self, query: str) -> ToolResult:
        """Handle graph queries."""
        if query == "ready_tasks":
            ready = self.graph.get_ready_tasks()
            lines = ["## Ready Tasks (can execute now)", ""]
            for t in ready:
                lines.append(f"- [{t.id}] {t.content}")
                deps_results = self.graph.get_results_for_task(t.id)
                if deps_results:
                    lines.append(f"  Available inputs: {list(deps_results.keys())}")
            return ToolResult(
                success=True,
                output="\n".join(lines) if ready else "No tasks ready (check dependencies)",
                metadata={"ready_tasks": [t.to_dict() for t in ready]}
            )

        elif query == "blocked_tasks":
            blocked = self.graph.get_blocked_tasks()
            lines = ["## Blocked Tasks (waiting on dependencies)", ""]
            for t in blocked:
                pending_deps = [
                    dep_id for dep_id in t.depends_on
                    if self.graph.get(dep_id) and self.graph.get(dep_id).status != "completed"
                ]
                lines.append(f"- [{t.id}] {t.content}")
                lines.append(f"  Waiting on: {pending_deps}")
            return ToolResult(
                success=True,
                output="\n".join(lines) if blocked else "No blocked tasks",
                metadata={"blocked_tasks": [t.to_dict() for t in blocked]}
            )

        elif query == "execution_order":
            batches = self.graph.get_execution_order()
            lines = ["## Execution Order (parallel batches)", ""]
            for i, batch in enumerate(batches):
                parallel_hint = " (parallel)" if len(batch) > 1 else ""
                lines.append(f"**Batch {i + 1}**{parallel_hint}:")
                for t in batch:
                    status = self.STATUS_SYMBOL.get(t.status, "?")
                    lines.append(f"  {status} [{t.id}] {t.content}")
                lines.append("")
            return ToolResult(
                success=True,
                output="\n".join(lines),
                metadata={"batches": [[t.to_dict() for t in batch] for batch in batches]}
            )

        elif query == "results":
            lines = ["## Task Results", ""]
            for t in self.graph.get_all():
                if t.result is not None:
                    key = t.result_key or t.id
                    result_preview = str(t.result)[:100]
                    if len(str(t.result)) > 100:
                        result_preview += "..."
                    lines.append(f"**{key}** (from {t.id}):")
                    lines.append(f"  {result_preview}")
                    lines.append("")
            return ToolResult(
                success=True,
                output="\n".join(lines) if len(lines) > 2 else "No results yet",
                metadata={"results": self.graph._results}
            )

        return ToolResult(success=False, output=None, error=f"Unknown query: {query}")

    def _format_graph(self) -> str:
        """Format the todo graph for display with ANSI colors."""
        C = self.Colors
        todos = self.graph.get_all()

        if not todos:
            return f"{C.DIM}No tasks in list.{C.RESET}"

        # Count statuses
        counts = {"pending": 0, "in_progress": 0, "completed": 0, "blocked": 0, "failed": 0}

        # Status colors
        STATUS_COLOR = {
            "pending": C.WHITE,
            "in_progress": C.BRIGHT_CYAN,
            "completed": C.BRIGHT_GREEN,
            "blocked": C.YELLOW,
            "failed": C.RED,
        }

        # Build output with dependency info
        lines = [f"{C.BOLD}{C.CYAN}━━━ Task List ━━━{C.RESET}", ""]

        # Get execution order for grouping
        batches = self.graph.get_execution_order()

        for batch_num, batch in enumerate(batches):
            if len(batches) > 1:
                parallel_note = f" {C.DIM}(parallel){C.RESET}" if len(batch) > 1 else ""
                lines.append(f"{C.BOLD}{C.BLUE}▸ Phase {batch_num + 1}{C.RESET}{parallel_note}")

            for todo in batch:
                status = todo.status
                priority = todo.priority
                task_type = todo.task_type

                status_sym = self.STATUS_SYMBOL.get(status, "?")
                priority_sym = self.PRIORITY_SYMBOL.get(priority, "")
                type_sym = self.TYPE_SYMBOL.get(task_type, "")
                color = STATUS_COLOR.get(status, C.WHITE)

                # Main line with color
                task_id_display = f"{C.DIM}[{todo.id}]{C.RESET}"
                content_display = f"{color}{todo.content}{C.RESET}"
                line = f"  {color}{status_sym}{C.RESET} {task_id_display} {type_sym} {content_display}"
                lines.append(line)

                # Dependencies (dimmed)
                if todo.depends_on:
                    dep_status = []
                    for dep_id in todo.depends_on:
                        dep = self.graph.get(dep_id)
                        if dep:
                            dep_sym = self.STATUS_SYMBOL.get(dep.status, "?")
                            dep_color = STATUS_COLOR.get(dep.status, C.WHITE)
                            dep_status.append(f"{dep_color}{dep_sym}{C.RESET}{C.DIM}{dep_id}{C.RESET}")
                        else:
                            dep_status.append(f"?{dep_id}")
                    lines.append(f"     {C.DIM}↳ depends on: {', '.join(dep_status)}{C.RESET}")

                # Result preview (dimmed green for success)
                if todo.result is not None:
                    result_preview = str(todo.result)[:50]
                    if len(str(todo.result)) > 50:
                        result_preview += "..."
                    key_info = f" (key: {todo.result_key})" if todo.result_key else ""
                    lines.append(f"     {C.DIM}{C.GREEN}↳ result{key_info}: {result_preview}{C.RESET}")

                if status in counts:
                    counts[status] += 1

            lines.append("")

        # Summary bar
        total = len(todos)
        ready = len(self.graph.get_ready_tasks())
        blocked = len(self.graph.get_blocked_tasks())

        lines.append(f"{C.DIM}{'─' * 50}{C.RESET}")

        # Colored progress summary
        completed_str = f"{C.BRIGHT_GREEN}{counts['completed']}/{total} done{C.RESET}"
        active_str = f"{C.BRIGHT_CYAN}{counts['in_progress']} active{C.RESET}"
        ready_str = f"{C.WHITE}{ready} ready{C.RESET}"
        blocked_str = f"{C.YELLOW}{blocked} blocked{C.RESET}" if blocked > 0 else f"{C.DIM}0 blocked{C.RESET}"

        lines.append(f"{C.BOLD}Progress:{C.RESET} {completed_str} │ {active_str} │ {ready_str} │ {blocked_str}")

        # Next actions hint
        if ready > 0:
            ready_tasks = self.graph.get_ready_tasks()
            ready_ids = [t.id for t in ready_tasks[:3]]
            lines.append(f"{C.BRIGHT_WHITE}▶ Next:{C.RESET} {C.CYAN}{', '.join(ready_ids)}{C.RESET}")

        return "\n".join(lines)

    def to_schema(self) -> Dict:
        """Convert to OpenAI-style tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }

    # Public API for orchestrator integration
    def get_graph(self) -> TodoGraph:
        """Get the underlying graph for orchestrator use."""
        return self.graph

    def set_task_result(self, task_id: str, result: Any, error: str = None) -> Tuple[bool, Optional[str]]:
        """
        Set a task's result with validation.

        Validates:
        1. If task has 'produces' field with file path, verifies file exists
        2. If task has 'target' field, verifies result meets criteria

        Returns (success, error_message) tuple.
        """
        item = self.graph.get(task_id)
        if not item:
            return False, f"Task {task_id} not found"

        if error:
            item.status = "failed"
            item.error = error
            return True, None

        # Validate artifact if 'produces' is specified
        validation_error = self._validate_artifact(item, result)
        if validation_error:
            item.status = "failed"
            item.error = validation_error
            return False, validation_error

        # Validate target if specified
        target_error = self._validate_target(item, result)
        if target_error:
            item.status = "failed"
            item.error = target_error
            return False, target_error

        # All validations passed
        item.status = "completed"
        item.result = result
        item.completed_at = datetime.now().isoformat()
        if item.result_key:
            self.graph._results[item.result_key] = result

        return True, None

    def _validate_artifact(self, item: TodoItem, result: Any) -> Optional[str]:
        """
        Validate that the declared artifact exists AND contains valid content.

        This provides external validation that cannot be fabricated:
        1. File existence check
        2. Content validation (not HTML/error page)
        3. Structure validation for known types (CSV, JSON)
        4. Row count validation if specified
        5. Execution validation (command must run and succeed)
        6. Metric validation (check specific values in output files)

        The 'produces' field supports extended format:
        - "file:<path>" - basic file existence
        - "file:<path>:csv" - CSV with content validation
        - "file:<path>:csv:100" - CSV with exactly 100 data rows expected
        - "file:<path>:csv:100+" - CSV with at least 100 rows
        - "file:<path>:json" - JSON with validation
        - "exec:<command>" - command must run and exit with code 0
        - "metric:<file>:<field>:<op><value>" - check field in JSON/CSV file
          Examples: "metric:results.json:accuracy:>=0.95"
                    "metric:results.json:error:<0.01"
                    "metric:output.csv:row_count:>=100"
        """
        if not item.produces:
            return None

        produces = item.produces

        # Handle execution validation: "exec:<command>"
        if produces.startswith("exec:"):
            command = produces[5:]  # Remove "exec:" prefix
            return self._validate_exec(command)

        # Handle metric validation: "metric:<file>:<field>:<op><value>"
        if produces.startswith("metric:"):
            return self._validate_metric(produces)

        # Handle file artifacts: "file:<path>" or "file:<path>:<type>" or "file:<path>:<type>:<rows>"
        if produces.startswith("file:"):
            parts = produces.split(":", maxsplit=3)
            file_path = parts[1] if len(parts) > 1 else ""
            expected_type = parts[2] if len(parts) > 2 else None
            row_spec = parts[3] if len(parts) > 3 else None

            # Basic existence check
            if not os.path.exists(file_path):
                return f"Artifact not found: {file_path}. Task declared produces='{produces}' but file does not exist."

            # Content validation
            validation_kwargs = {}

            # Parse row specification
            if row_spec:
                if row_spec.endswith('+'):
                    validation_kwargs['min_rows'] = int(row_spec[:-1])
                elif row_spec.endswith('-'):
                    validation_kwargs['max_rows'] = int(row_spec[:-1])
                else:
                    validation_kwargs['expected_rows'] = int(row_spec)

            # Validate content
            is_valid, error_msg, metadata = ContentValidator.validate_file_content(
                file_path,
                expected_type=expected_type,
                **validation_kwargs
            )

            if not is_valid:
                return f"Artifact validation failed for {file_path}: {error_msg}"

            return None

        # Handle data/metrics - just check result is not None
        if produces in ("data", "metrics"):
            if result is None:
                return f"Task declared produces='{produces}' but result is None"
            return None

        # Unknown produces type - treat as file path and validate content
        if not os.path.exists(produces):
            return f"Artifact not found: {produces}"

        # Validate content of the file
        is_valid, error_msg, _ = ContentValidator.validate_file_content(produces)
        if not is_valid:
            return f"Artifact validation failed for {produces}: {error_msg}"

        return None

    def _validate_exec(self, command: str) -> Optional[str]:
        """
        Validate by running a command and checking exit code.

        This provides hard verification that cannot be fabricated:
        - Command is actually executed
        - Exit code must be 0 for success
        - Output is logged for audit trail

        Args:
            command: Shell command to run (e.g., "pytest tests/")

        Returns:
            Error message if validation fails, None if successful.
        """
        import subprocess

        try:
            print(f"    🔍 Validating: {command}")

            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
            )

            if result.returncode != 0:
                # Include first few lines of output for debugging
                stderr_preview = result.stderr[:500] if result.stderr else ""
                stdout_preview = result.stdout[:500] if result.stdout else ""
                output_preview = stderr_preview or stdout_preview

                return (
                    f"Execution validation failed: '{command}' exited with code {result.returncode}. "
                    f"Output: {output_preview[:200]}"
                )

            print(f"    ✓ Validation passed: {command}")
            return None

        except subprocess.TimeoutExpired:
            return f"Execution validation failed: '{command}' timed out after 300s"
        except Exception as e:
            return f"Execution validation failed: '{command}' error: {str(e)}"

    def _validate_metric(self, produces: str) -> Optional[str]:
        """
        Validate a specific metric value in an output file.

        Format: "metric:<file>:<field>:<op><value>"
        Examples:
            - "metric:results.json:accuracy:>=0.95"
            - "metric:results.json:error:<0.01"
            - "metric:results.json:converged:==true"
            - "metric:output.csv:row_count:>=100"

        Supported operators: >=, <=, >, <, ==, !=

        Args:
            produces: Full produces specification

        Returns:
            Error message if validation fails, None if successful.
        """
        import json

        # Parse the specification
        parts = produces.split(":", maxsplit=3)
        if len(parts) < 4:
            return f"Invalid metric specification: '{produces}'. Expected 'metric:<file>:<field>:<op><value>'"

        _, file_path, field, check = parts

        # Parse operator and value from check (e.g., ">=0.95" -> (">=", 0.95))
        op_match = re.match(r'^(>=|<=|>|<|==|!=)(.+)$', check)
        if not op_match:
            return f"Invalid metric check: '{check}'. Expected operator (>=, <=, >, <, ==, !=) followed by value"

        operator, expected_str = op_match.groups()

        # Convert expected value to appropriate type
        expected_str_lower = expected_str.lower()
        if expected_str_lower == 'true':
            expected = True
        elif expected_str_lower == 'false':
            expected = False
        elif expected_str_lower in ('none', 'null'):
            expected = None
        else:
            try:
                expected = float(expected_str)
            except ValueError:
                expected = expected_str  # Keep as string

        # Check file exists
        if not os.path.exists(file_path):
            return f"Metric validation failed: file not found: {file_path}"

        # Extract actual value from file
        actual = None
        try:
            ext = os.path.splitext(file_path)[1].lower()

            if ext == '.json':
                with open(file_path, 'r') as f:
                    data = json.load(f)

                # Support nested fields with dot notation
                actual = self._extract_json_value(data, field)

            elif ext == '.csv':
                # Special handling for CSV
                if field == 'row_count':
                    with open(file_path, 'r') as f:
                        reader = csv.reader(f)
                        rows = list(reader)
                        actual = len(rows) - 1  # Subtract header
                else:
                    return f"Metric validation: CSV field '{field}' not supported. Use 'row_count' or use JSON."

            else:
                return f"Metric validation: unsupported file type '{ext}'. Use .json or .csv"

        except Exception as e:
            return f"Metric validation failed: error reading {file_path}: {str(e)}"

        if actual is None:
            return f"Metric validation failed: field '{field}' not found in {file_path}"

        # Compare values
        comparison_result = self._compare_values(actual, operator, expected)

        if not comparison_result:
            return (
                f"Metric validation failed: {field}={actual} does not satisfy {operator}{expected}. "
                f"File: {file_path}"
            )

        print(f"    ✓ Metric validated: {field}={actual} {operator} {expected}")
        return None

    def _extract_json_value(self, data: Any, field: str) -> Any:
        """Extract a value from nested JSON using dot notation."""
        parts = field.split('.')
        current = data

        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list) and part.isdigit():
                idx = int(part)
                current = current[idx] if idx < len(current) else None
            else:
                return None

            if current is None:
                return None

        return current

    def _compare_values(self, actual: Any, operator: str, expected: Any) -> bool:
        """Compare actual value against expected using operator."""
        try:
            # Convert to same type for comparison
            if isinstance(expected, bool):
                actual = bool(actual)
            elif isinstance(expected, float) and not isinstance(actual, bool):
                actual = float(actual)

            ops = {
                '>=': lambda a, b: a >= b,
                '<=': lambda a, b: a <= b,
                '>': lambda a, b: a > b,
                '<': lambda a, b: a < b,
                '==': lambda a, b: a == b,
                '!=': lambda a, b: a != b,
            }

            return ops[operator](actual, expected)

        except (ValueError, TypeError):
            return False

    def _validate_target(self, item: TodoItem, result: Any) -> Optional[str]:
        """Validate that the result meets the target criteria."""
        if not item.target:
            return None

        target = item.target
        metric_name = target.get("metric")
        operator = target.get("operator", ">=")
        target_value = target.get("value")

        if metric_name is None or target_value is None:
            return None  # Incomplete target spec, skip validation

        # Extract metric value from result
        actual_value = None
        if isinstance(result, dict):
            actual_value = result.get(metric_name)
        elif isinstance(result, (int, float)):
            actual_value = result

        if actual_value is None:
            return f"Target metric '{metric_name}' not found in result"

        # Compare based on operator
        ops = {
            ">=": lambda a, b: a >= b,
            "<=": lambda a, b: a <= b,
            ">": lambda a, b: a > b,
            "<": lambda a, b: a < b,
            "==": lambda a, b: a == b,
            "!=": lambda a, b: a != b,
        }

        compare = ops.get(operator)
        if not compare:
            return f"Unknown operator: {operator}"

        if not compare(actual_value, target_value):
            return f"Target not met: {metric_name}={actual_value} (required {operator} {target_value})"

        return None

    def mark_in_progress(self, task_id: str) -> bool:
        """Mark a task as in progress."""
        item = self.graph.get(task_id)
        if item:
            item.status = "in_progress"
            return True
        return False


def get_tool() -> TodoTool:
    """Factory function for tool discovery."""
    return TodoTool()
