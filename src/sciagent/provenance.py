"""
Provenance Checker - External validation of data acquisition claims.

This module provides unbiased verification that cannot be fabricated by the model.
It cross-references:
1. Fetch logs (from web.py) - What actually happened during HTTP requests
2. File contents - What's actually in downloaded files
3. Task claims - What the model says it did

Key principle: Verification uses EXTERNAL EVIDENCE only, never model claims.

Usage:
    checker = ProvenanceChecker()
    result = checker.verify_data_acquisition(
        claimed_url="https://example.com/data.csv",
        local_file="output/data.csv",
        expected_type="csv",
        expected_rows=100
    )

    if not result.valid:
        print(f"Data fabrication detected: {result.issues}")
"""

from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from .tools.atomic.web import get_fetch_logger, FetchLogger
from .tools.atomic.shell import get_exec_logger, ExecLogger
from .tools.atomic.todo import ContentValidator


@dataclass
class ProvenanceIssue:
    """A single provenance issue detected."""
    severity: str  # "error", "warning", "info"
    category: str  # "fetch_failed", "content_mismatch", "fabrication", etc.
    message: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.category}: {self.message}"


@dataclass
class ProvenanceResult:
    """Result of provenance verification."""
    valid: bool
    issues: List[ProvenanceIssue] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def add_issue(self, severity: str, category: str, message: str, evidence: Dict = None):
        self.issues.append(ProvenanceIssue(
            severity=severity,
            category=category,
            message=message,
            evidence=evidence or {}
        ))
        if severity == "error":
            self.valid = False

    @property
    def errors(self) -> List[ProvenanceIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> List[ProvenanceIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "issues": [
                {
                    "severity": i.severity,
                    "category": i.category,
                    "message": i.message,
                    "evidence": i.evidence
                }
                for i in self.issues
            ],
            "metadata": self.metadata
        }


class ProvenanceChecker:
    """
    Verifies data provenance using external evidence only.

    This class provides hard validation that the model cannot bypass:
    1. Fetch log verification - Did the HTTP request actually succeed?
    2. Execution log verification - Did the command actually run?
    3. Content verification - Does the file contain valid data?
    4. Cross-reference - Do logs match claims?

    The key insight is that all evidence comes from external sources
    (file system, HTTP logs, execution logs) that the model cannot fabricate.
    """

    def __init__(self, log_dir: str = None):
        """
        Initialize provenance checker.

        Args:
            log_dir: Directory containing logs. Defaults to _logs/
        """
        self.fetch_logger = get_fetch_logger(log_dir)
        self.exec_logger = get_exec_logger(log_dir)
        self.validator = ContentValidator
        # Keep backwards compatibility
        self.logger = self.fetch_logger

    def verify_data_acquisition(
        self,
        claimed_url: str = None,
        local_file: str = None,
        expected_type: str = None,
        expected_rows: int = None,
        min_rows: int = None,
        required_columns: List[str] = None,
    ) -> ProvenanceResult:
        """
        Verify a data acquisition claim.

        This is the main entry point for validation. It checks:
        1. If a URL was claimed, verify the fetch log shows success
        2. If a file was claimed, verify it exists and contains valid data
        3. Cross-reference URL and file if both provided

        Args:
            claimed_url: URL that was supposedly fetched
            local_file: Local file that was supposedly created/downloaded
            expected_type: Expected file type ('csv', 'json', etc.)
            expected_rows: Expected number of data rows (for CSV)
            min_rows: Minimum number of rows (for CSV)
            required_columns: Required column names (for CSV)

        Returns:
            ProvenanceResult with validation outcome and any issues
        """
        result = ProvenanceResult(valid=True)
        result.metadata["timestamp"] = datetime.now().isoformat()
        result.metadata["claimed_url"] = claimed_url
        result.metadata["local_file"] = local_file

        # Step 1: Verify URL fetch if claimed
        if claimed_url:
            self._verify_fetch(claimed_url, result)

        # Step 2: Verify local file if claimed
        if local_file:
            self._verify_file(
                local_file, result,
                expected_type=expected_type,
                expected_rows=expected_rows,
                min_rows=min_rows,
                required_columns=required_columns,
            )

        # Step 3: Cross-reference URL and file
        if claimed_url and local_file and os.path.exists(local_file):
            self._cross_reference(claimed_url, local_file, result)

        return result

    def _verify_fetch(self, url: str, result: ProvenanceResult):
        """Verify that a URL fetch actually succeeded."""
        fetch_entry = self.logger.find_fetch_for_url(url)

        if fetch_entry is None:
            result.add_issue(
                severity="error",
                category="no_fetch_record",
                message=f"No fetch record found for URL: {url}. "
                        f"Claims to have downloaded data but no HTTP request was logged.",
                evidence={"url": url}
            )
            return

        # Check if fetch succeeded
        if not fetch_entry.get("success", False):
            result.add_issue(
                severity="error",
                category="fetch_failed",
                message=f"Fetch failed for URL: {url}. Error: {fetch_entry.get('error')}",
                evidence=fetch_entry
            )
            return

        # Check status code
        status_code = fetch_entry.get("status_code", 0)
        if status_code >= 400:
            result.add_issue(
                severity="error",
                category="http_error",
                message=f"HTTP error {status_code} for URL: {url}",
                evidence=fetch_entry
            )
            return

        # Check for error page content
        if fetch_entry.get("is_error_page", False):
            result.add_issue(
                severity="error",
                category="error_page",
                message=f"Fetched content appears to be an error page: {url}. "
                        f"Indicators: {fetch_entry.get('error_indicators', [])}",
                evidence=fetch_entry
            )
            return

        # Check for HTML when not expected
        if fetch_entry.get("is_html", False):
            content_type = fetch_entry.get("content_type", "")
            if "html" not in content_type.lower():
                result.add_issue(
                    severity="warning",
                    category="unexpected_html",
                    message=f"Content appears to be HTML but content-type is '{content_type}': {url}",
                    evidence=fetch_entry
                )

        # Success - record metadata
        result.metadata["fetch_verified"] = True
        result.metadata["fetch_entry"] = fetch_entry

    def _verify_file(
        self,
        file_path: str,
        result: ProvenanceResult,
        expected_type: str = None,
        expected_rows: int = None,
        min_rows: int = None,
        required_columns: List[str] = None,
    ):
        """Verify that a local file exists and contains valid data."""

        # Check file exists
        if not os.path.exists(file_path):
            result.add_issue(
                severity="error",
                category="file_not_found",
                message=f"Claimed output file does not exist: {file_path}",
                evidence={"file_path": file_path}
            )
            return

        # Check file size
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            result.add_issue(
                severity="error",
                category="empty_file",
                message=f"Output file is empty: {file_path}",
                evidence={"file_path": file_path, "size": file_size}
            )
            return

        # Content validation
        kwargs = {}
        if expected_rows is not None:
            kwargs['expected_rows'] = expected_rows
        if min_rows is not None:
            kwargs['min_rows'] = min_rows
        if required_columns is not None:
            kwargs['required_columns'] = required_columns

        is_valid, error_msg, metadata = self.validator.validate_file_content(
            file_path,
            expected_type=expected_type,
            **kwargs
        )

        if not is_valid:
            result.add_issue(
                severity="error",
                category="invalid_content",
                message=f"File content validation failed: {error_msg}",
                evidence={"file_path": file_path, "metadata": metadata}
            )
            return

        # Success - record metadata
        result.metadata["file_verified"] = True
        result.metadata["file_metadata"] = metadata

    def _cross_reference(self, url: str, file_path: str, result: ProvenanceResult):
        """Cross-reference fetch log with file content."""
        fetch_entry = self.logger.find_fetch_for_url(url)

        if not fetch_entry:
            return  # Already flagged in _verify_fetch

        # Compare content lengths if available
        fetch_length = fetch_entry.get("content_length", 0)
        file_size = os.path.getsize(file_path)

        # Allow for some variation due to encoding/processing
        if fetch_length > 0 and file_size > 0:
            ratio = file_size / fetch_length
            if ratio < 0.1 or ratio > 10:
                result.add_issue(
                    severity="warning",
                    category="size_mismatch",
                    message=f"File size ({file_size}) differs significantly from fetched content ({fetch_length})",
                    evidence={
                        "url": url,
                        "file_path": file_path,
                        "fetch_length": fetch_length,
                        "file_size": file_size,
                        "ratio": ratio
                    }
                )

    def verify_execution(
        self,
        claimed_command: str = None,
        must_have_run: bool = True,
        must_have_succeeded: bool = True,
    ) -> ProvenanceResult:
        """
        Verify that a command was actually executed.

        Args:
            claimed_command: Command pattern to search for (partial match)
            must_have_run: If True, fail if command not found in logs
            must_have_succeeded: If True, fail if command failed

        Returns:
            ProvenanceResult with verification outcome
        """
        result = ProvenanceResult(valid=True)
        result.metadata["timestamp"] = datetime.now().isoformat()
        result.metadata["claimed_command"] = claimed_command

        if not claimed_command:
            result.add_issue(
                severity="error",
                category="no_command",
                message="No command specified to verify",
            )
            return result

        # Find matching executions
        executions = self.exec_logger.find_execution(claimed_command)

        if not executions:
            if must_have_run:
                result.add_issue(
                    severity="error",
                    category="no_execution_record",
                    message=f"No execution record found for: {claimed_command}. "
                            f"Claims to have run command but no execution was logged.",
                    evidence={"claimed_command": claimed_command}
                )
            return result

        # Check most recent matching execution
        latest = executions[-1]
        result.metadata["execution_entry"] = latest

        if must_have_succeeded and not latest.get("success", False):
            exit_code = latest.get("exit_code", "unknown")
            error_indicators = latest.get("error_indicators", [])

            result.add_issue(
                severity="error",
                category="execution_failed",
                message=f"Command execution failed (exit code: {exit_code}). "
                        f"Errors: {error_indicators[:3]}",
                evidence=latest
            )
            return result

        if latest.get("timeout", False):
            result.add_issue(
                severity="error",
                category="execution_timeout",
                message=f"Command timed out: {claimed_command}",
                evidence=latest
            )
            return result

        # Success
        result.metadata["execution_verified"] = True
        return result

    def verify_tests_ran(self) -> ProvenanceResult:
        """
        Verify that tests were actually executed.

        Returns:
            ProvenanceResult with verification outcome
        """
        result = ProvenanceResult(valid=True)
        result.metadata["timestamp"] = datetime.now().isoformat()

        verification_runs = self.exec_logger.get_verification_runs()

        if not verification_runs:
            result.add_issue(
                severity="error",
                category="no_tests_run",
                message="No test/verification commands found in execution log. "
                        "Claims to have run tests but no test execution was logged.",
            )
            return result

        # Check if any tests passed
        passed = [r for r in verification_runs if r.get("success", False)]
        failed = [r for r in verification_runs if not r.get("success", True)]

        result.metadata["total_test_runs"] = len(verification_runs)
        result.metadata["passed"] = len(passed)
        result.metadata["failed"] = len(failed)

        if not passed and failed:
            result.add_issue(
                severity="error",
                category="all_tests_failed",
                message=f"All {len(failed)} test runs failed. "
                        f"Latest failure: {failed[-1].get('error_indicators', [])}",
                evidence={"failed_runs": failed[-3:]}  # Last 3 failures
            )
        elif failed:
            result.add_issue(
                severity="warning",
                category="some_tests_failed",
                message=f"{len(failed)} of {len(verification_runs)} test runs failed.",
                evidence={"failed_count": len(failed), "passed_count": len(passed)}
            )

        return result

    def get_execution_summary(self) -> Dict[str, Any]:
        """Get summary of all command executions."""
        executions = self.exec_logger.get_recent_executions(limit=0)

        if not executions:
            return {"total": 0, "succeeded": 0, "failed": 0, "timeouts": 0}

        succeeded = sum(1 for e in executions if e.get("success", False))
        failed = sum(1 for e in executions if not e.get("success", True) and not e.get("timeout", False))
        timeouts = sum(1 for e in executions if e.get("timeout", False))
        verification = sum(1 for e in executions if e.get("is_verification", False))

        return {
            "total": len(executions),
            "succeeded": succeeded,
            "failed": failed,
            "timeouts": timeouts,
            "verification_commands": verification,
        }

    def verify_all_tasks(
        self,
        tasks: List[Dict[str, Any]],
        working_dir: str = "."
    ) -> Dict[str, ProvenanceResult]:
        """
        Verify provenance for all tasks in a task list.

        Args:
            tasks: List of task dictionaries (from TodoTool)
            working_dir: Base directory for relative file paths

        Returns:
            Dict mapping task_id -> ProvenanceResult
        """
        results = {}

        for task in tasks:
            task_id = task.get("id", "unknown")

            # Skip non-data tasks
            task_type = task.get("task_type", "general")
            if task_type not in ("research", "general"):
                continue

            # Check if task produces a file
            produces = task.get("produces", "")
            if not produces:
                continue

            # Parse produces specification
            if produces.startswith("file:"):
                parts = produces.split(":", maxsplit=3)
                file_path = parts[1] if len(parts) > 1 else ""
                expected_type = parts[2] if len(parts) > 2 else None

                # Make path absolute
                if not os.path.isabs(file_path):
                    file_path = os.path.join(working_dir, file_path)

                # Check for URL in task result or content
                claimed_url = None
                result_data = task.get("result")
                if isinstance(result_data, dict):
                    claimed_url = result_data.get("url") or result_data.get("source_url")

                results[task_id] = self.verify_data_acquisition(
                    claimed_url=claimed_url,
                    local_file=file_path,
                    expected_type=expected_type,
                )

        return results

    def generate_report(self, results: Dict[str, ProvenanceResult]) -> str:
        """Generate a human-readable provenance report."""
        lines = [
            "=" * 60,
            "PROVENANCE VERIFICATION REPORT",
            f"Generated: {datetime.now().isoformat()}",
            "=" * 60,
            ""
        ]

        total_tasks = len(results)
        valid_tasks = sum(1 for r in results.values() if r.valid)
        invalid_tasks = total_tasks - valid_tasks

        lines.append(f"Summary: {valid_tasks}/{total_tasks} tasks verified")
        if invalid_tasks > 0:
            lines.append(f"⚠️  {invalid_tasks} task(s) have provenance issues")
        lines.append("")

        for task_id, result in results.items():
            status = "✓" if result.valid else "✗"
            lines.append(f"{status} Task: {task_id}")

            for issue in result.issues:
                icon = "❌" if issue.severity == "error" else "⚠️"
                lines.append(f"  {icon} {issue.category}: {issue.message}")

            if result.valid:
                if result.metadata.get("fetch_verified"):
                    lines.append("  ✓ Fetch verified")
                if result.metadata.get("file_verified"):
                    lines.append("  ✓ File content verified")

            lines.append("")

        lines.append("=" * 60)
        return "\n".join(lines)


def check_provenance(
    url: str = None,
    file_path: str = None,
    expected_type: str = None,
    expected_rows: int = None,
) -> ProvenanceResult:
    """
    Convenience function for quick provenance check.

    Example:
        result = check_provenance(
            url="https://example.com/data.csv",
            file_path="output/data.csv",
            expected_type="csv",
            expected_rows=100
        )
        if not result.valid:
            raise ValueError(f"Data provenance check failed: {result.issues}")
    """
    checker = ProvenanceChecker()
    return checker.verify_data_acquisition(
        claimed_url=url,
        local_file=file_path,
        expected_type=expected_type,
        expected_rows=expected_rows,
    )
