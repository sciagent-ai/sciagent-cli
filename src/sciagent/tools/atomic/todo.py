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

import os
from typing import Dict, Any, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime
import uuid


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
        """Validate that the declared artifact exists."""
        if not item.produces:
            return None

        produces = item.produces

        # Handle file artifacts: "file:<path>"
        if produces.startswith("file:"):
            file_path = produces[5:]  # Remove "file:" prefix
            if not os.path.exists(file_path):
                return f"Artifact not found: {file_path}. Task declared produces='{produces}' but file does not exist."
            return None

        # Handle data/metrics - just check result is not None
        if produces in ("data", "metrics"):
            if result is None:
                return f"Task declared produces='{produces}' but result is None"
            return None

        # Unknown produces type - treat as file path
        if not os.path.exists(produces):
            return f"Artifact not found: {produces}"

        return None

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
