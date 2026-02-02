"""
Task Orchestrator - Dependency-aware task execution with result passing.

Integrates:
- TodoGraph for dependency tracking
- SubAgentOrchestrator for parallel/sequential execution
- Result injection for dependent tasks

FEATURES:
1. Automatic dependency resolution
2. Parallel execution of independent tasks
3. Result passing between dependent tasks
4. Progress tracking and reporting
5. Error handling and task retry
"""

from __future__ import annotations

import time
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .tools.atomic.todo import TodoTool, TodoGraph, TodoItem
from .subagent import SubAgentOrchestrator, SubAgentResult, SubAgentConfig


@dataclass
class ExecutionResult:
    """Result of a task execution."""
    task_id: str
    success: bool
    output: Any
    error: Optional[str] = None
    duration_seconds: float = 0.0
    iterations: int = 0


@dataclass
class OrchestratorConfig:
    """Configuration for the orchestrator."""
    max_parallel_tasks: int = 4
    retry_failed_tasks: bool = False
    max_retries: int = 2
    timeout_per_task: float = 300.0  # 5 minutes
    verbose: bool = True


class TaskOrchestrator:
    """
    Dependency-aware task orchestrator.

    Reads tasks from TodoGraph and executes them in optimal order:
    - Tasks with no dependencies execute first
    - Independent tasks run in parallel
    - Dependent tasks wait for their inputs
    - Results are automatically passed to dependents
    """

    def __init__(
        self,
        todo_tool: TodoTool,
        subagent_orchestrator: Optional[SubAgentOrchestrator] = None,
        config: Optional[OrchestratorConfig] = None,
        task_executor: Optional[Callable[[TodoItem, Dict[str, Any]], ExecutionResult]] = None,
    ):
        self.todo = todo_tool
        self.subagent = subagent_orchestrator
        self.config = config or OrchestratorConfig()
        self._custom_executor = task_executor

        # Execution state
        self._execution_log: List[Dict[str, Any]] = []
        self._start_time: Optional[float] = None

    def execute_all(self) -> Dict[str, Any]:
        """
        Execute all tasks in the todo graph in dependency order.

        Returns summary of execution.
        """
        self._start_time = time.time()
        graph = self.todo.get_graph()

        if self.config.verbose:
            print("=" * 60)
            print("TASK ORCHESTRATOR - Starting Execution")
            print("=" * 60)

        # Get execution order (batches of parallelizable tasks)
        batches = graph.get_execution_order()

        if not batches:
            return {"success": True, "message": "No tasks to execute", "results": {}}

        total_tasks = sum(len(b) for b in batches)
        completed = 0
        failed = 0

        if self.config.verbose:
            print(f"Total tasks: {total_tasks} in {len(batches)} phases")
            print("-" * 60)

        # Execute batch by batch
        for batch_num, batch in enumerate(batches):
            if self.config.verbose:
                parallel_note = "(parallel)" if len(batch) > 1 else "(sequential)"
                print(f"\n### Phase {batch_num + 1}/{len(batches)} {parallel_note}")
                for t in batch:
                    print(f"  - [{t.id}] {t.content}")

            # Execute batch
            results = self._execute_batch(batch)

            for result in results:
                if result.success:
                    # Validate and set result - may fail if artifact/target validation fails
                    success, validation_error = self.todo.set_task_result(result.task_id, result.output)
                    if success:
                        completed += 1
                        if self.config.verbose:
                            print(f"  ✓ [{result.task_id}] completed in {result.duration_seconds:.1f}s")
                    else:
                        # Validation failed (artifact missing or target not met)
                        failed += 1
                        if self.config.verbose:
                            print(f"  ✗ [{result.task_id}] validation failed: {validation_error}")
                else:
                    failed += 1
                    self.todo.set_task_result(result.task_id, None, error=result.error)
                    if self.config.verbose:
                        print(f"  ✗ [{result.task_id}] failed: {result.error}")

                self._execution_log.append({
                    "task_id": result.task_id,
                    "success": result.success,
                    "duration": result.duration_seconds,
                    "error": result.error,
                    "timestamp": datetime.now().isoformat(),
                })

        # Summary
        total_duration = time.time() - self._start_time

        if self.config.verbose:
            print("\n" + "=" * 60)
            print("EXECUTION COMPLETE")
            print(f"  Completed: {completed}/{total_tasks}")
            print(f"  Failed: {failed}/{total_tasks}")
            print(f"  Duration: {total_duration:.1f}s")
            print("=" * 60)

        return {
            "success": failed == 0,
            "completed": completed,
            "failed": failed,
            "total": total_tasks,
            "duration_seconds": total_duration,
            "results": graph._results,
            "log": self._execution_log,
        }

    def execute_next(self) -> Optional[ExecutionResult]:
        """
        Execute the next ready task (single task, sequential mode).

        Returns result or None if no tasks ready.
        """
        graph = self.todo.get_graph()
        ready = graph.get_ready_tasks()

        if not ready:
            return None

        # Take highest priority ready task
        ready.sort(key=lambda t: {"high": 0, "medium": 1, "low": 2}.get(t.priority, 1))
        task = ready[0]

        return self._execute_task(task)

    def execute_ready_parallel(self) -> List[ExecutionResult]:
        """
        Execute all currently ready tasks in parallel.

        Returns list of results.
        """
        graph = self.todo.get_graph()
        ready = graph.get_parallel_batch()

        if not ready:
            return []

        return self._execute_batch(ready)

    def _execute_batch(self, tasks: List[TodoItem]) -> List[ExecutionResult]:
        """Execute a batch of tasks, potentially in parallel."""
        if len(tasks) == 1:
            # Single task, no parallelism needed
            return [self._execute_task(tasks[0])]

        # Filter parallelizable tasks
        parallel_tasks = [t for t in tasks if t.can_parallel]
        sequential_tasks = [t for t in tasks if not t.can_parallel]

        results = []

        # Execute parallel tasks
        if parallel_tasks:
            with ThreadPoolExecutor(max_workers=self.config.max_parallel_tasks) as executor:
                futures = {
                    executor.submit(self._execute_task, task): task
                    for task in parallel_tasks
                }

                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        result = future.result(timeout=self.config.timeout_per_task)
                        results.append(result)
                    except Exception as e:
                        results.append(ExecutionResult(
                            task_id=task.id,
                            success=False,
                            output=None,
                            error=f"Execution error: {str(e)}"
                        ))

        # Execute sequential tasks
        for task in sequential_tasks:
            results.append(self._execute_task(task))

        return results

    def _execute_task(self, task: TodoItem) -> ExecutionResult:
        """Execute a single task."""
        start_time = time.time()

        # Mark as in progress
        self.todo.mark_in_progress(task.id)

        # Get inputs from dependencies
        graph = self.todo.get_graph()
        inputs = graph.get_results_for_task(task.id)

        if self.config.verbose:
            if inputs:
                print(f"    Inputs for [{task.id}]: {list(inputs.keys())}")

        try:
            # Use custom executor if provided
            if self._custom_executor:
                result = self._custom_executor(task, inputs)
                return result

            # Use subagent executor if available
            if self.subagent:
                return self._execute_with_subagent(task, inputs)

            # Default: just mark complete with a placeholder
            return ExecutionResult(
                task_id=task.id,
                success=True,
                output=f"Task '{task.content}' completed (no executor configured)",
                duration_seconds=time.time() - start_time,
            )

        except Exception as e:
            return ExecutionResult(
                task_id=task.id,
                success=False,
                output=None,
                error=str(e),
                duration_seconds=time.time() - start_time,
            )

    def _execute_with_subagent(self, task: TodoItem, inputs: Dict[str, Any]) -> ExecutionResult:
        """Execute a task using the subagent system."""
        start_time = time.time()

        # Map task type to agent type
        agent_map = {
            "research": "researcher",
            "code": "general",
            "validate": "general",
            "review": "reviewer",
            "general": "general",
        }

        agent_name = agent_map.get(task.task_type, "general")

        # Build task prompt with inputs
        prompt = task.content
        if inputs:
            inputs_str = "\n".join([f"- {k}: {v}" for k, v in inputs.items()])
            prompt = f"{task.content}\n\n**Available inputs from previous tasks:**\n{inputs_str}"

        # Execute via subagent
        result = self.subagent.spawn(agent_name, prompt)

        return ExecutionResult(
            task_id=task.id,
            success=result.success,
            output=result.output,
            error=result.error,
            duration_seconds=time.time() - start_time,
            iterations=result.iterations,
        )

    def get_status(self) -> Dict[str, Any]:
        """Get current execution status."""
        graph = self.todo.get_graph()
        todos = graph.get_all()

        counts = {
            "pending": len([t for t in todos if t.status == "pending"]),
            "in_progress": len([t for t in todos if t.status == "in_progress"]),
            "completed": len([t for t in todos if t.status == "completed"]),
            "failed": len([t for t in todos if t.status == "failed"]),
            "blocked": len([t for t in todos if t.status == "blocked"]),
        }

        ready = graph.get_ready_tasks()
        blocked = graph.get_blocked_tasks()

        return {
            "counts": counts,
            "ready_tasks": [t.id for t in ready],
            "blocked_tasks": [t.id for t in blocked],
            "results": graph._results,
            "execution_log": self._execution_log,
        }

    def get_results(self) -> Dict[str, Any]:
        """Get all task results."""
        return self.todo.get_graph()._results


class WorkflowBuilder:
    """
    Helper for building task workflows declaratively.

    Example:
        workflow = WorkflowBuilder()
        workflow.add("research_api", "Research REST API patterns", task_type="research")
        workflow.add("research_auth", "Research auth methods", task_type="research")
        workflow.add("design", "Design the API", depends_on=["research_api", "research_auth"])
        workflow.add("implement", "Implement the API", depends_on=["design"], task_type="code")
        workflow.add("test", "Write tests", depends_on=["implement"], task_type="validate")

        todo_tool = workflow.build()
    """

    def __init__(self):
        self._tasks: List[Dict[str, Any]] = []

    def add(
        self,
        id: str,
        content: str,
        task_type: str = "general",
        depends_on: List[str] = None,
        result_key: str = None,
        priority: str = "medium",
        can_parallel: bool = True,
    ) -> "WorkflowBuilder":
        """Add a task to the workflow."""
        self._tasks.append({
            "id": id,
            "content": content,
            "status": "pending",
            "task_type": task_type,
            "depends_on": depends_on or [],
            "result_key": result_key or id,
            "priority": priority,
            "can_parallel": can_parallel,
        })
        return self

    def add_parallel(self, tasks: List[Dict[str, Any]]) -> "WorkflowBuilder":
        """Add multiple tasks that can run in parallel."""
        for task in tasks:
            task["can_parallel"] = True
            self._tasks.append(task)
        return self

    def add_sequence(self, tasks: List[Dict[str, Any]]) -> "WorkflowBuilder":
        """Add tasks that must run sequentially (each depends on previous)."""
        prev_id = None
        for task in tasks:
            if prev_id:
                task.setdefault("depends_on", []).append(prev_id)
            task["can_parallel"] = False
            self._tasks.append(task)
            prev_id = task["id"]
        return self

    def build(self) -> TodoTool:
        """Build and return a TodoTool with the workflow."""
        todo = TodoTool()
        todo.execute(todos=self._tasks)
        return todo

    def get_tasks(self) -> List[Dict[str, Any]]:
        """Get the task list for inspection."""
        return self._tasks.copy()


def create_orchestrator(
    working_dir: str = ".",
    max_parallel: int = 4,
    verbose: bool = True,
) -> tuple[TaskOrchestrator, TodoTool]:
    """
    Create an orchestrator with subagent support.

    Returns (orchestrator, todo_tool) tuple.
    """
    from tools import create_default_registry

    todo = TodoTool()
    tools = create_default_registry(working_dir)
    subagent = SubAgentOrchestrator(tools=tools, working_dir=working_dir, max_workers=max_parallel)

    config = OrchestratorConfig(
        max_parallel_tasks=max_parallel,
        verbose=verbose,
    )

    orchestrator = TaskOrchestrator(
        todo_tool=todo,
        subagent_orchestrator=subagent,
        config=config,
    )

    return orchestrator, todo


# Example usage and demonstration
if __name__ == "__main__":
    print("Task Orchestrator Demo")
    print("=" * 60)

    # Build a sample workflow
    workflow = WorkflowBuilder()

    # Phase 1: Research (parallel)
    workflow.add("research_api", "Research REST API best practices", task_type="research")
    workflow.add("research_auth", "Research authentication patterns", task_type="research")
    workflow.add("research_db", "Research database schema patterns", task_type="research")

    # Phase 2: Design (depends on all research)
    workflow.add(
        "design",
        "Design the system architecture",
        task_type="general",
        depends_on=["research_api", "research_auth", "research_db"],
    )

    # Phase 3: Implementation (parallel, depends on design)
    workflow.add("impl_api", "Implement API endpoints", task_type="code", depends_on=["design"])
    workflow.add("impl_auth", "Implement authentication", task_type="code", depends_on=["design"])
    workflow.add("impl_db", "Implement database layer", task_type="code", depends_on=["design"])

    # Phase 4: Testing (depends on all implementation)
    workflow.add(
        "test",
        "Write and run tests",
        task_type="validate",
        depends_on=["impl_api", "impl_auth", "impl_db"],
    )

    # Phase 5: Review
    workflow.add("review", "Code review", task_type="review", depends_on=["test"])

    # Build the todo tool
    todo = workflow.build()

    # Show the execution plan
    print("\nExecution Plan:")
    result = todo.execute(query="execution_order")
    print(result.output)

    # Show ready tasks
    print("\nReady to Execute:")
    result = todo.execute(query="ready_tasks")
    print(result.output)
