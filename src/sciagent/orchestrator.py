"""
Task Orchestrator - Dependency-aware task execution with result passing.

Integrates:
- TodoGraph for dependency tracking
- SubAgentOrchestrator for parallel/sequential execution
- Result injection for dependent tasks
- ProvenanceChecker for data validation gates

FEATURES:
1. Automatic dependency resolution
2. Parallel execution of independent tasks
3. Result passing between dependent tasks
4. Progress tracking and reporting
5. Error handling and task retry
6. DATA GATE: Hard verification before analysis phase (prevents fabrication)
"""

from __future__ import annotations

import os
import time
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .tools.atomic.todo import TodoTool, TodoGraph, TodoItem
from .subagent import SubAgentOrchestrator, SubAgentResult, SubAgentConfig
from .provenance import ProvenanceChecker, ProvenanceResult


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

    # DATA GATE: Hard verification settings
    enable_data_gate: bool = True  # Verify data before analysis
    data_gate_strict: bool = True  # Fail on any provenance error
    data_acquisition_types: tuple = ("research", "data", "download", "fetch")  # Task types that acquire data
    analysis_types: tuple = ("analysis", "code", "compute", "validate")  # Task types that use data

    # EXEC GATE: Verify commands actually ran
    enable_exec_gate: bool = True  # Verify execution before final output
    exec_gate_strict: bool = True  # Fail on execution verification errors
    verification_types: tuple = ("validate", "test", "verify", "check")  # Task types that should run verification
    output_types: tuple = ("output", "report", "final", "deliver")  # Task types that produce final output

    # LLM VERIFICATION GATE: Independent verification using fresh-context subagent
    enable_verification: bool = True  # Enable LLM-based verification
    verification_strict: bool = True  # True = block on verification failure; False = warn only
    verification_threshold: float = 0.7  # Minimum confidence for "verified" verdict to pass

    # OUTCOME VERIFICATION: Pass original goal to verifier for scientific validation
    original_request: Optional[str] = None  # Original user request/goal for outcome verification


class TaskOrchestrator:
    """
    Dependency-aware task orchestrator.

    Reads tasks from TodoGraph and executes them in optimal order:
    - Tasks with no dependencies execute first
    - Independent tasks run in parallel
    - Dependent tasks wait for their inputs
    - Results are automatically passed to dependents

    DATA GATE FEATURE:
    When enable_data_gate=True, the orchestrator verifies data provenance
    before allowing analysis tasks to proceed. This prevents data fabrication
    by requiring external evidence (fetch logs, file validation) that the
    model cannot fabricate.

    Data acquisition tasks (research, download, fetch) produce files.
    Before analysis tasks can run, the orchestrator:
    1. Checks fetch logs - Did HTTP requests actually succeed?
    2. Validates file content - Is the data valid (not HTML/error pages)?
    3. Cross-references - Do fetch logs match file contents?

    If validation fails with data_gate_strict=True, execution stops.
    """

    def __init__(
        self,
        todo_tool: TodoTool,
        subagent_orchestrator: Optional[SubAgentOrchestrator] = None,
        config: Optional[OrchestratorConfig] = None,
        task_executor: Optional[Callable[[TodoItem, Dict[str, Any]], ExecutionResult]] = None,
        working_dir: str = ".",
    ):
        self.todo = todo_tool
        self.subagent = subagent_orchestrator
        self.config = config or OrchestratorConfig()
        self._custom_executor = task_executor
        self.working_dir = working_dir

        # Execution state
        self._execution_log: List[Dict[str, Any]] = []
        self._start_time: Optional[float] = None

        # Provenance checking (data gate)
        self._provenance_checker = ProvenanceChecker()
        self._data_gate_passed: bool = False
        self._provenance_results: Dict[str, ProvenanceResult] = {}

        # Execution verification (exec gate)
        self._exec_gate_passed: bool = False
        self._exec_verification_results: Dict[str, Any] = {}

        # LLM verification (verification gate)
        self._llm_verification_passed: bool = False
        self._llm_verification_results: Dict[str, Any] = {}

    def execute_all(self) -> Dict[str, Any]:
        """
        Execute all tasks in the todo graph in dependency order.

        DATA GATE: If enable_data_gate=True, verifies data provenance
        before allowing analysis tasks to proceed.

        Returns summary of execution.
        """
        self._start_time = time.time()
        graph = self.todo.get_graph()
        self._data_gate_passed = False
        self._exec_gate_passed = False

        if self.config.verbose:
            print("=" * 60)
            print("TASK ORCHESTRATOR - Starting Execution")
            if self.config.enable_data_gate:
                print("DATA GATE: Enabled (will verify data before analysis)")
            if self.config.enable_exec_gate:
                print("EXEC GATE: Enabled (will verify execution before output)")
            print("=" * 60)

        # Get execution order (batches of parallelizable tasks)
        batches = graph.get_execution_order()

        if not batches:
            return {"success": True, "message": "No tasks to execute", "results": {}}

        total_tasks = sum(len(b) for b in batches)
        completed = 0
        failed = 0
        data_gate_failed = False
        exec_gate_failed = False

        if self.config.verbose:
            print(f"Total tasks: {total_tasks} in {len(batches)} phases")
            print("-" * 60)

        # Execute batch by batch
        for batch_num, batch in enumerate(batches):
            # DATA GATE CHECK: Before analysis tasks, verify data provenance
            if self.config.enable_data_gate and not self._data_gate_passed:
                if self._batch_contains_analysis_tasks(batch):
                    print("\n" + "=" * 60)
                    print("🔒 DATA GATE: Verifying data provenance before analysis")
                    print("=" * 60)

                    gate_result = self._run_data_gate()

                    if not gate_result["passed"]:
                        if self.config.data_gate_strict:
                            print("❌ DATA GATE FAILED - Execution stopped")
                            print("Reason: Data provenance could not be verified")
                            for issue in gate_result.get("issues", []):
                                print(f"  - {issue}")
                            data_gate_failed = True
                            break
                        else:
                            print("⚠️ DATA GATE: Issues detected but continuing (strict=False)")
                    else:
                        print("✅ DATA GATE PASSED - Data provenance verified")
                        self._data_gate_passed = True

                    print("=" * 60 + "\n")

            # EXEC GATE CHECK: Before output tasks, verify execution actually happened
            if self.config.enable_exec_gate and not self._exec_gate_passed:
                if self._batch_contains_output_tasks(batch):
                    print("\n" + "=" * 60)
                    print("🔒 EXEC GATE: Verifying command execution before output")
                    print("=" * 60)

                    gate_result = self._run_exec_gate()

                    if not gate_result["passed"]:
                        if self.config.exec_gate_strict:
                            print("❌ EXEC GATE FAILED - Execution stopped")
                            print("Reason: Command execution could not be verified")
                            for issue in gate_result.get("issues", []):
                                print(f"  - {issue}")
                            exec_gate_failed = True
                            break
                        else:
                            print("⚠️ EXEC GATE: Issues detected but continuing (strict=False)")
                    else:
                        print("✅ EXEC GATE PASSED - Execution verified")
                        self._exec_gate_passed = True

                    print("=" * 60 + "\n")

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

        # LLM VERIFICATION GATE: Run after all tasks complete but before final summary
        llm_verification_failed = False
        if self.config.enable_verification and not data_gate_failed and not exec_gate_failed:
            verification_tasks = self._get_tasks_requiring_verification()
            if verification_tasks:
                print("\n" + "=" * 60)
                print("🔒 LLM VERIFICATION GATE: Independent verification of claims")
                print("=" * 60)

                self._llm_verification_results = self._run_llm_verification_gate(verification_tasks)

                if not self._llm_verification_results["passed"]:
                    if self.config.verification_strict:
                        print("❌ LLM VERIFICATION FAILED - Execution stopped")
                        print("Reason: Independent verifier found issues with claims")
                        llm_verification_failed = True
                    else:
                        print("⚠️ LLM VERIFICATION: Issues detected but continuing (strict=False)")
                        self._llm_verification_passed = False
                else:
                    print("✅ LLM VERIFICATION PASSED - Claims independently verified")
                    self._llm_verification_passed = True

                print("=" * 60 + "\n")

        # Summary
        total_duration = time.time() - self._start_time

        if self.config.verbose:
            print("\n" + "=" * 60)
            if data_gate_failed:
                print("EXECUTION STOPPED - DATA GATE FAILED")
            elif exec_gate_failed:
                print("EXECUTION STOPPED - EXEC GATE FAILED")
            elif llm_verification_failed:
                print("EXECUTION STOPPED - LLM VERIFICATION FAILED")
            else:
                print("EXECUTION COMPLETE")
            print(f"  Completed: {completed}/{total_tasks}")
            print(f"  Failed: {failed}/{total_tasks}")
            print(f"  Duration: {total_duration:.1f}s")
            if self.config.enable_data_gate:
                print(f"  Data Gate: {'PASSED' if self._data_gate_passed else 'FAILED' if data_gate_failed else 'NOT REACHED'}")
            if self.config.enable_exec_gate:
                print(f"  Exec Gate: {'PASSED' if self._exec_gate_passed else 'FAILED' if exec_gate_failed else 'NOT REACHED'}")
            if self.config.enable_verification:
                print(f"  LLM Verification: {'PASSED' if self._llm_verification_passed else 'FAILED' if llm_verification_failed else 'NOT REACHED'}")
            print("=" * 60)

        return {
            "success": failed == 0 and not data_gate_failed and not exec_gate_failed and not llm_verification_failed,
            "completed": completed,
            "failed": failed,
            "total": total_tasks,
            "duration_seconds": total_duration,
            "results": graph._results,
            "log": self._execution_log,
            "data_gate_passed": self._data_gate_passed,
            "data_gate_failed": data_gate_failed,
            "exec_gate_passed": self._exec_gate_passed,
            "exec_gate_failed": exec_gate_failed,
            "llm_verification_passed": self._llm_verification_passed,
            "llm_verification_failed": llm_verification_failed,
            "provenance_results": {
                task_id: result.to_dict()
                for task_id, result in self._provenance_results.items()
            },
            "exec_verification_results": self._exec_verification_results,
            "llm_verification_results": self._llm_verification_results,
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

    # =========================================================================
    # DATA GATE METHODS
    # =========================================================================

    def _batch_contains_analysis_tasks(self, batch: List[TodoItem]) -> bool:
        """Check if a batch contains analysis tasks that require data gate."""
        for task in batch:
            task_type = task.task_type.lower()
            # Check if it's an analysis task type
            if any(t in task_type for t in self.config.analysis_types):
                return True
            # Also check task content for analysis keywords
            content_lower = task.content.lower()
            analysis_keywords = ["analyze", "analysis", "compute", "calculate", "process", "run simulation"]
            if any(kw in content_lower for kw in analysis_keywords):
                return True
        return False

    def _get_data_acquisition_tasks(self) -> List[TodoItem]:
        """Get all completed data acquisition tasks."""
        graph = self.todo.get_graph()
        tasks = []

        for task in graph.get_all():
            if task.status != "completed":
                continue

            task_type = task.task_type.lower()

            # Check if it's a data acquisition task
            is_data_task = any(t in task_type for t in self.config.data_acquisition_types)

            # Also check if task produces a file
            has_file_output = task.produces and task.produces.startswith("file:")

            # Check content for data acquisition keywords
            content_lower = task.content.lower()
            data_keywords = ["download", "fetch", "retrieve", "get data", "acquire", "scrape"]
            has_data_keyword = any(kw in content_lower for kw in data_keywords)

            if is_data_task or has_file_output or has_data_keyword:
                tasks.append(task)

        return tasks

    def _run_data_gate(self) -> Dict[str, Any]:
        """
        Run the data gate verification.

        This uses EXTERNAL EVIDENCE ONLY:
        1. Fetch logs - What HTTP requests actually returned
        2. File content - What's actually in the files
        3. File existence - Whether claimed files exist

        The model cannot fabricate this evidence.

        Returns:
            {
                "passed": bool,
                "issues": List[str],
                "verified_tasks": int,
                "failed_tasks": int,
            }
        """
        data_tasks = self._get_data_acquisition_tasks()

        if not data_tasks:
            if self.config.verbose:
                print("  No data acquisition tasks to verify")
            return {"passed": True, "issues": [], "verified_tasks": 0, "failed_tasks": 0}

        if self.config.verbose:
            print(f"  Verifying {len(data_tasks)} data acquisition task(s)...")

        issues = []
        verified = 0
        failed = 0

        for task in data_tasks:
            if self.config.verbose:
                print(f"  Checking [{task.id}] {task.content[:50]}...")

            # Extract file path from produces field
            file_path = None
            expected_type = None
            expected_rows = None

            if task.produces:
                if task.produces.startswith("file:"):
                    parts = task.produces.split(":", maxsplit=3)
                    file_path = parts[1] if len(parts) > 1 else None
                    expected_type = parts[2] if len(parts) > 2 else None
                    row_spec = parts[3] if len(parts) > 3 else None

                    if row_spec:
                        try:
                            if row_spec.endswith('+'):
                                expected_rows = int(row_spec[:-1])  # Minimum
                            else:
                                expected_rows = int(row_spec)
                        except ValueError:
                            pass
                else:
                    file_path = task.produces

            # Make path absolute
            if file_path and not os.path.isabs(file_path):
                file_path = os.path.join(self.working_dir, file_path)

            # Extract claimed URL from result
            claimed_url = None
            if isinstance(task.result, dict):
                claimed_url = task.result.get("url") or task.result.get("source_url")

            # Run provenance check
            result = self._provenance_checker.verify_data_acquisition(
                claimed_url=claimed_url,
                local_file=file_path,
                expected_type=expected_type,
                expected_rows=expected_rows if expected_rows else None,
            )

            self._provenance_results[task.id] = result

            if result.valid:
                verified += 1
                if self.config.verbose:
                    print(f"    ✓ Verified")
            else:
                failed += 1
                for issue in result.errors:
                    issue_str = f"[{task.id}] {issue.category}: {issue.message}"
                    issues.append(issue_str)
                    if self.config.verbose:
                        print(f"    ✗ {issue.category}: {issue.message}")

        passed = failed == 0

        if self.config.verbose:
            print(f"\n  Results: {verified} verified, {failed} failed")

        return {
            "passed": passed,
            "issues": issues,
            "verified_tasks": verified,
            "failed_tasks": failed,
        }

    def get_provenance_report(self) -> str:
        """Generate a human-readable provenance report."""
        return self._provenance_checker.generate_report(self._provenance_results)

    # =========================================================================
    # EXEC GATE METHODS
    # =========================================================================

    def _batch_contains_output_tasks(self, batch: List[TodoItem]) -> bool:
        """Check if a batch contains output tasks that require exec gate."""
        for task in batch:
            task_type = task.task_type.lower()
            # Check if it's an output task type
            if any(t in task_type for t in self.config.output_types):
                return True
            # Also check task content for output keywords
            content_lower = task.content.lower()
            output_keywords = ["final", "output", "report", "deliver", "summary", "conclude", "complete"]
            if any(kw in content_lower for kw in output_keywords):
                return True
        return False

    def _get_verification_tasks(self) -> List[TodoItem]:
        """Get all completed verification/test tasks."""
        graph = self.todo.get_graph()
        tasks = []

        for task in graph.get_all():
            if task.status != "completed":
                continue

            task_type = task.task_type.lower()

            # Check if it's a verification task
            is_verify_task = any(t in task_type for t in self.config.verification_types)

            # Check content for verification keywords
            content_lower = task.content.lower()
            verify_keywords = ["test", "verify", "validate", "check", "assert", "run", "execute", "pytest", "unittest"]
            has_verify_keyword = any(kw in content_lower for kw in verify_keywords)

            if is_verify_task or has_verify_keyword:
                tasks.append(task)

        return tasks

    def _run_exec_gate(self) -> Dict[str, Any]:
        """
        Run the execution gate verification.

        This uses EXTERNAL EVIDENCE ONLY:
        1. Exec logs - What commands actually ran
        2. Exit codes - Did commands succeed
        3. Output analysis - Were there errors in output

        The model cannot fabricate this evidence.

        Returns:
            {
                "passed": bool,
                "issues": List[str],
                "verified_commands": int,
                "failed_commands": int,
            }
        """
        issues = []

        # Get execution summary from provenance checker
        exec_summary = self._provenance_checker.get_execution_summary()

        if self.config.verbose:
            print(f"  Execution summary: {exec_summary['total']} commands logged")
            print(f"    - Succeeded: {exec_summary['succeeded']}")
            print(f"    - Failed: {exec_summary['failed']}")
            print(f"    - Timeouts: {exec_summary['timeouts']}")
            print(f"    - Verification commands: {exec_summary['verification_commands']}")

        self._exec_verification_results["summary"] = exec_summary

        # Check 1: Were any commands executed at all?
        if exec_summary["total"] == 0:
            issues.append("No commands were executed. Claims may be fabricated.")

        # Check 2: Did verification commands run?
        verify_tasks = self._get_verification_tasks()
        if verify_tasks and exec_summary["verification_commands"] == 0:
            task_ids = [t.id for t in verify_tasks]
            issues.append(
                f"Tasks {task_ids} claim verification but no test commands found in exec log."
            )

        # Check 3: Verify test commands actually ran and passed
        if verify_tasks:
            if self.config.verbose:
                print(f"\n  Verifying {len(verify_tasks)} verification task(s)...")

            test_result = self._provenance_checker.verify_tests_ran()
            self._exec_verification_results["tests"] = test_result.to_dict()

            if not test_result.valid:
                for issue in test_result.errors:
                    issues.append(f"Test verification: {issue.message}")
                if self.config.verbose:
                    for issue in test_result.errors:
                        print(f"    ✗ {issue.category}: {issue.message}")
            else:
                if self.config.verbose:
                    metadata = test_result.metadata
                    print(f"    ✓ Tests verified: {metadata.get('passed', 0)} passed, {metadata.get('failed', 0)} failed")

        # Check 4: Were there critical failures?
        if exec_summary["failed"] > 0:
            fail_rate = exec_summary["failed"] / exec_summary["total"]
            if fail_rate > 0.5:  # More than 50% failed
                issues.append(
                    f"High failure rate: {exec_summary['failed']}/{exec_summary['total']} "
                    f"commands failed ({fail_rate:.0%})"
                )
            elif exec_summary["failed"] > 0:
                # Warning only for low failure rate
                if self.config.verbose:
                    print(f"  ⚠️ {exec_summary['failed']} command(s) failed (may be expected)")

        # Check 5: Timeouts indicate potential issues
        if exec_summary["timeouts"] > 0:
            issues.append(f"{exec_summary['timeouts']} command(s) timed out")

        passed = len(issues) == 0

        if self.config.verbose:
            if passed:
                print(f"\n  Results: Execution verified")
            else:
                print(f"\n  Results: {len(issues)} issue(s) found")

        return {
            "passed": passed,
            "issues": issues,
            "verified_commands": exec_summary["succeeded"],
            "failed_commands": exec_summary["failed"],
            "summary": exec_summary,
        }

    def get_exec_verification_report(self) -> str:
        """Generate a human-readable execution verification report."""
        lines = [
            "=" * 60,
            "EXECUTION VERIFICATION REPORT",
            f"Generated: {datetime.now().isoformat()}",
            "=" * 60,
            ""
        ]

        summary = self._exec_verification_results.get("summary", {})
        lines.append(f"Total commands executed: {summary.get('total', 0)}")
        lines.append(f"  Succeeded: {summary.get('succeeded', 0)}")
        lines.append(f"  Failed: {summary.get('failed', 0)}")
        lines.append(f"  Timeouts: {summary.get('timeouts', 0)}")
        lines.append(f"  Verification commands: {summary.get('verification_commands', 0)}")
        lines.append("")

        tests = self._exec_verification_results.get("tests", {})
        if tests:
            lines.append("Test Verification:")
            lines.append(f"  Result: {'PASSED' if tests.get('valid') else 'FAILED'}")
            for issue in tests.get("issues", []):
                lines.append(f"  - {issue.get('message', issue)}")
            lines.append("")

        lines.append("=" * 60)
        return "\n".join(lines)

    # =========================================================================
    # LLM VERIFICATION GATE METHODS
    # =========================================================================

    def _get_tasks_requiring_verification(self) -> List[TodoItem]:
        """
        Get tasks that require LLM verification.

        Tasks are selected for verification if:
        1. They have verify=True flag set
        2. They are final output tasks (task_type in output_types)
        3. They are the last task in the execution order
        """
        graph = self.todo.get_graph()
        tasks = []

        all_tasks = graph.get_all()
        execution_order = graph.get_execution_order()

        # Find final tasks (last batch)
        final_task_ids = set()
        if execution_order:
            final_batch = execution_order[-1]
            final_task_ids = {t.id for t in final_batch}

        for task in all_tasks:
            if task.status != "completed":
                continue

            # Check if task has verify flag
            if getattr(task, 'verify', False):
                tasks.append(task)
                continue

            # Check if task is output type
            task_type = task.task_type.lower()
            if any(t in task_type for t in self.config.output_types):
                tasks.append(task)
                continue

            # Check if task is in final batch
            if task.id in final_task_ids:
                tasks.append(task)
                continue

            # Check content for output keywords
            content_lower = task.content.lower()
            output_keywords = ["final", "output", "report", "deliver", "summary", "conclude"]
            if any(kw in content_lower for kw in output_keywords):
                tasks.append(task)

        return tasks

    def _build_verification_context(self, task: TodoItem) -> Dict[str, Any]:
        """
        Build the context for LLM verification of a task.

        Returns claim summary and evidence to pass to the verifier subagent.
        """
        import json

        # Build claim summary - start with original goal for outcome verification
        claim_parts = []

        # Add original request/goal if available (critical for outcome verification)
        if self.config.original_request:
            claim_parts.append(f"ORIGINAL USER GOAL: {self.config.original_request}\n")

        claim_parts.append(f"Task: {task.content}")

        if task.result:
            result_str = str(task.result)
            if len(result_str) > 1000:
                result_str = result_str[:1000] + "..."
            claim_parts.append(f"Claimed result: {result_str}")

        if task.produces:
            claim_parts.append(f"Claimed output: {task.produces}")

        claim = "\n".join(claim_parts)

        # Build evidence summary
        evidence_parts = []

        # Add fetch log evidence
        fetch_entries = self._provenance_checker.fetch_logger.get_recent_fetches(limit=10)
        if fetch_entries:
            evidence_parts.append("## Fetch Log (recent HTTP requests)")
            for entry in fetch_entries[-5:]:  # Last 5 entries
                url = entry.get("url", "unknown")
                status = entry.get("status_code", "?")
                success = entry.get("success", False)
                evidence_parts.append(f"- {url[:80]}: status={status}, success={success}")

        # Add exec log evidence
        exec_entries = self._provenance_checker.exec_logger.get_recent_executions(limit=10)
        if exec_entries:
            evidence_parts.append("\n## Exec Log (recent commands)")
            for entry in exec_entries[-5:]:
                cmd = entry.get("command", "unknown")[:60]
                exit_code = entry.get("exit_code", "?")
                success = entry.get("success", False)
                evidence_parts.append(f"- {cmd}: exit={exit_code}, success={success}")

        # Add file evidence if produces file
        if task.produces and task.produces.startswith("file:"):
            parts = task.produces.split(":", maxsplit=3)
            file_path = parts[1] if len(parts) > 1 else ""

            if not os.path.isabs(file_path):
                file_path = os.path.join(self.working_dir, file_path)

            evidence_parts.append(f"\n## File Evidence: {file_path}")

            if os.path.exists(file_path):
                file_size = os.path.getsize(file_path)
                evidence_parts.append(f"- File exists: Yes")
                evidence_parts.append(f"- File size: {file_size} bytes")

                # Read first 500 chars of file
                try:
                    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                        content_preview = f.read(500)
                    evidence_parts.append(f"- Content preview:\n```\n{content_preview}\n```")
                except Exception as e:
                    evidence_parts.append(f"- Content read error: {e}")
            else:
                evidence_parts.append(f"- File exists: No")

        # Add provenance results if available
        if task.id in self._provenance_results:
            prov_result = self._provenance_results[task.id]
            evidence_parts.append("\n## Provenance Check")
            evidence_parts.append(f"- Valid: {prov_result.valid}")
            if prov_result.issues:
                for issue in prov_result.issues[:3]:
                    evidence_parts.append(f"- Issue: {issue.message}")

        evidence = "\n".join(evidence_parts) if evidence_parts else "No evidence available"

        return {
            "claim": claim,
            "evidence": evidence,
            "task_id": task.id,
        }

    def _run_llm_verification_gate(self, tasks: List[TodoItem]) -> Dict[str, Any]:
        """
        Run LLM verification on selected tasks using the verifier subagent.

        The verifier subagent has:
        - FRESH CONTEXT (no conversation history)
        - Adversarial prompt focused on finding issues
        - Only sees claim + evidence, not reasoning

        Returns:
            {
                "passed": bool,
                "tasks_verified": int,
                "tasks_failed": int,
                "results": {task_id: verification_result}
            }
        """
        import json

        if not self.subagent:
            return {
                "passed": True,
                "tasks_verified": 0,
                "tasks_failed": 0,
                "results": {},
                "skipped": "No subagent orchestrator available"
            }

        results = {}
        verified_count = 0
        failed_count = 0

        for task in tasks:
            if self.config.verbose:
                print(f"  Verifying [{task.id}] {task.content[:50]}...")

            # Build context for verification
            context = self._build_verification_context(task)

            # Build the verification prompt with claim and evidence injected
            verification_prompt = f"""Please verify the following claim and evidence.

## CLAIM TO AUDIT
{context['claim']}

## EVIDENCE PROVIDED
{context['evidence']}

Respond with a JSON object containing your verdict, confidence, issues found, etc.
"""

            # Spawn verifier subagent with fresh context
            try:
                verifier_result = self.subagent.spawn("verifier", verification_prompt)

                if verifier_result.success:
                    # Parse the JSON response
                    try:
                        # Try to extract JSON from the output
                        output = verifier_result.output
                        json_match = output.find("{")
                        json_end = output.rfind("}") + 1

                        if json_match != -1 and json_end > json_match:
                            json_str = output[json_match:json_end]
                            verification = json.loads(json_str)
                        else:
                            verification = {
                                "verdict": "insufficient",
                                "confidence": 0.0,
                                "issues": ["Could not parse verifier response"],
                                "reasoning": output[:500]
                            }
                    except json.JSONDecodeError:
                        verification = {
                            "verdict": "insufficient",
                            "confidence": 0.0,
                            "issues": ["Invalid JSON response from verifier"],
                            "reasoning": verifier_result.output[:500]
                        }
                else:
                    verification = {
                        "verdict": "insufficient",
                        "confidence": 0.0,
                        "issues": [f"Verifier error: {verifier_result.error}"],
                        "reasoning": "Verification subagent failed to complete"
                    }

                results[task.id] = verification

                # Check if verification passed
                verdict = verification.get("verdict", "insufficient")
                confidence = verification.get("confidence", 0.0)

                if verdict == "verified" and confidence >= self.config.verification_threshold:
                    verified_count += 1
                    if self.config.verbose:
                        print(f"    ✓ Verified (confidence: {confidence:.2f})")
                elif verdict == "refuted":
                    failed_count += 1
                    if self.config.verbose:
                        print(f"    ✗ Refuted: {verification.get('reasoning', 'No reason given')[:100]}")
                        for issue in verification.get("issues", [])[:3]:
                            print(f"      - {issue}")
                else:
                    failed_count += 1
                    if self.config.verbose:
                        print(f"    ⚠ Insufficient evidence (confidence: {confidence:.2f})")
                        for issue in verification.get("missing_evidence", [])[:3]:
                            print(f"      - Missing: {issue}")

            except Exception as e:
                results[task.id] = {
                    "verdict": "insufficient",
                    "confidence": 0.0,
                    "issues": [f"Verification error: {str(e)}"],
                }
                failed_count += 1
                if self.config.verbose:
                    print(f"    ✗ Error: {str(e)}")

        passed = failed_count == 0

        return {
            "passed": passed,
            "tasks_verified": verified_count,
            "tasks_failed": failed_count,
            "results": results,
        }

    def get_llm_verification_report(self) -> str:
        """Generate a human-readable LLM verification report."""
        lines = [
            "=" * 60,
            "LLM VERIFICATION REPORT",
            f"Generated: {datetime.now().isoformat()}",
            "=" * 60,
            ""
        ]

        results = self._llm_verification_results.get("results", {})
        verified = self._llm_verification_results.get("tasks_verified", 0)
        failed = self._llm_verification_results.get("tasks_failed", 0)
        passed = self._llm_verification_results.get("passed", False)

        lines.append(f"Overall: {'PASSED' if passed else 'FAILED'}")
        lines.append(f"Tasks verified: {verified}")
        lines.append(f"Tasks failed: {failed}")
        lines.append("")

        for task_id, result in results.items():
            verdict = result.get("verdict", "unknown")
            confidence = result.get("confidence", 0.0)
            lines.append(f"Task: {task_id}")
            lines.append(f"  Verdict: {verdict} (confidence: {confidence:.2f})")

            issues = result.get("issues", [])
            if issues:
                lines.append("  Issues:")
                for issue in issues[:5]:
                    lines.append(f"    - {issue}")

            supporting = result.get("supporting_facts", [])
            if supporting:
                lines.append("  Supporting evidence:")
                for fact in supporting[:3]:
                    lines.append(f"    + {fact}")

            fabrication = result.get("fabrication_indicators", [])
            if fabrication:
                lines.append("  Fabrication indicators:")
                for indicator in fabrication[:3]:
                    lines.append(f"    ! {indicator}")

            lines.append("")

        lines.append("=" * 60)
        return "\n".join(lines)


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
    enable_data_gate: bool = True,
    data_gate_strict: bool = True,
    enable_exec_gate: bool = True,
    exec_gate_strict: bool = True,
    enable_verification: bool = True,
    verification_strict: bool = True,
    verification_threshold: float = 0.7,
) -> tuple[TaskOrchestrator, TodoTool]:
    """
    Create an orchestrator with subagent support and verification gates.

    Args:
        working_dir: Base directory for file operations
        max_parallel: Maximum parallel tasks
        verbose: Enable verbose output
        enable_data_gate: Enable data provenance verification before analysis
        data_gate_strict: Fail execution if data gate verification fails
        enable_exec_gate: Enable execution verification before output
        exec_gate_strict: Fail execution if exec gate verification fails
        enable_verification: Enable LLM-based independent verification
        verification_strict: Fail execution if LLM verification fails (default: True, blocks on failure)
        verification_threshold: Minimum confidence for "verified" verdict (default: 0.7)

    Returns (orchestrator, todo_tool) tuple.
    """
    from tools import create_default_registry

    todo = TodoTool()
    tools = create_default_registry(working_dir)
    subagent = SubAgentOrchestrator(tools=tools, working_dir=working_dir, max_workers=max_parallel)

    config = OrchestratorConfig(
        max_parallel_tasks=max_parallel,
        verbose=verbose,
        enable_data_gate=enable_data_gate,
        data_gate_strict=data_gate_strict,
        enable_exec_gate=enable_exec_gate,
        exec_gate_strict=exec_gate_strict,
        enable_verification=enable_verification,
        verification_strict=verification_strict,
        verification_threshold=verification_threshold,
    )

    orchestrator = TaskOrchestrator(
        todo_tool=todo,
        subagent_orchestrator=subagent,
        config=config,
        working_dir=working_dir,
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
