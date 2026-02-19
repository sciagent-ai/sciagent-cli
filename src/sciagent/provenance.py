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


@dataclass
class CrossReferenceResult:
    """Result of cross-reference verification between claims and evidence."""
    matches: List[Dict[str, Any]] = field(default_factory=list)
    mismatches: List[Dict[str, Any]] = field(default_factory=list)
    missing_evidence: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def all_verified(self) -> bool:
        """Returns True if all claims have matching evidence with no mismatches."""
        return len(self.mismatches) == 0 and len(self.missing_evidence) == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "matches": self.matches,
            "mismatches": self.mismatches,
            "missing_evidence": self.missing_evidence,
            "all_verified": self.all_verified,
        }

    def summary(self) -> str:
        """Generate human-readable summary."""
        lines = [
            f"Cross-Reference Verification:",
            f"  Matches: {len(self.matches)}",
            f"  Mismatches: {len(self.mismatches)}",
            f"  Missing Evidence: {len(self.missing_evidence)}",
            f"  Status: {'VERIFIED' if self.all_verified else 'ISSUES FOUND'}",
        ]
        return "\n".join(lines)


class CrossReferenceVerifier:
    """
    Cross-references task claims against multiple evidence sources.

    This provides independent verification by comparing:
    - Task claims vs fetch_log entries
    - Task claims vs exec_log entries
    - Task claims vs actual file contents

    Example:
        Task claims: "Downloaded 100 rows from NOAA"
        Cross-check:
        - fetch_log: URL contains "noaa"? ✓
        - fetch_log: status=200? ✓
        - file row_count >= 100? ✗ (only 50)
        Result: MISMATCH

    The verifier cannot be fooled by model claims because it only
    trusts external evidence (logs, files, execution records).
    """

    def __init__(self, log_dir: str = None):
        """Initialize cross-reference verifier."""
        self.fetch_logger = get_fetch_logger(log_dir)
        self.exec_logger = get_exec_logger(log_dir)
        self.validator = ContentValidator

    def verify_task_claims(
        self,
        claims: Dict[str, Any],
        working_dir: str = "."
    ) -> CrossReferenceResult:
        """
        Verify a set of claims against available evidence.

        Args:
            claims: Dictionary with claim details:
                - url: Claimed URL that was fetched
                - domain: Expected domain in URL (e.g., "noaa", "ncbi")
                - file_path: Path to claimed output file
                - row_count: Claimed number of rows downloaded
                - command: Claimed command that was executed
                - success: Whether command claimed to succeed
                - content_type: Expected content type (csv, json, etc.)
            working_dir: Base directory for relative file paths

        Returns:
            CrossReferenceResult with matches, mismatches, and missing evidence
        """
        result = CrossReferenceResult()

        # Extract claims
        claimed_url = claims.get("url")
        claimed_domain = claims.get("domain")
        claimed_file = claims.get("file_path")
        claimed_rows = claims.get("row_count")
        claimed_command = claims.get("command")
        claimed_success = claims.get("success", True)
        expected_type = claims.get("content_type")

        # 1. Verify URL fetch if claimed
        if claimed_url:
            self._verify_fetch_claim(
                result, claimed_url, claimed_domain, claimed_success
            )

        # 2. Verify file content if claimed
        if claimed_file:
            # Make path absolute
            file_path = claimed_file
            if not os.path.isabs(file_path):
                file_path = os.path.join(working_dir, file_path)

            self._verify_file_claim(
                result, file_path, expected_type, claimed_rows
            )

        # 3. Verify command execution if claimed
        if claimed_command:
            self._verify_exec_claim(
                result, claimed_command, claimed_success
            )

        # 4. Cross-reference URL domain with file source
        if claimed_url and claimed_domain and claimed_file:
            self._cross_reference_url_file(
                result, claimed_url, claimed_domain, claimed_file, working_dir
            )

        return result

    def _verify_fetch_claim(
        self,
        result: CrossReferenceResult,
        url: str,
        domain: str = None,
        expected_success: bool = True
    ):
        """Verify URL fetch claim against fetch log."""
        fetch_entry = self.fetch_logger.find_fetch_for_url(url)

        if fetch_entry is None:
            result.missing_evidence.append({
                "claim": f"Fetched URL: {url}",
                "evidence_type": "fetch_log",
                "issue": "No fetch record found in log",
            })
            return

        # Check success
        actual_success = fetch_entry.get("success", False)
        status_code = fetch_entry.get("status_code", 0)

        if expected_success and not actual_success:
            result.mismatches.append({
                "claim": f"Successfully fetched {url}",
                "evidence": f"Fetch failed with status {status_code}",
                "evidence_type": "fetch_log",
                "details": fetch_entry.get("error"),
            })
            return

        if expected_success and status_code >= 400:
            result.mismatches.append({
                "claim": f"Successfully fetched {url}",
                "evidence": f"HTTP error {status_code}",
                "evidence_type": "fetch_log",
            })
            return

        # Check domain if specified
        if domain and domain.lower() not in url.lower():
            result.mismatches.append({
                "claim": f"URL from domain '{domain}'",
                "evidence": f"URL does not contain '{domain}': {url}",
                "evidence_type": "url_analysis",
            })
            return

        # Check for error page content
        if fetch_entry.get("is_error_page", False):
            result.mismatches.append({
                "claim": f"Fetched valid data from {url}",
                "evidence": f"Content appears to be an error page",
                "evidence_type": "fetch_log",
                "details": fetch_entry.get("error_indicators"),
            })
            return

        # Success
        result.matches.append({
            "claim": f"Fetched {url}",
            "evidence": f"Fetch log confirms status={status_code}, success={actual_success}",
            "evidence_type": "fetch_log",
        })

    def _verify_file_claim(
        self,
        result: CrossReferenceResult,
        file_path: str,
        expected_type: str = None,
        claimed_rows: int = None
    ):
        """Verify file content claim against actual file."""
        if not os.path.exists(file_path):
            result.missing_evidence.append({
                "claim": f"Created file: {file_path}",
                "evidence_type": "file_system",
                "issue": "File does not exist",
            })
            return

        # Get file size
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            result.mismatches.append({
                "claim": f"Created file with data: {file_path}",
                "evidence": "File is empty (0 bytes)",
                "evidence_type": "file_system",
            })
            return

        # Validate content
        is_valid, error_msg, metadata = self.validator.validate_file_content(
            file_path, expected_type=expected_type
        )

        if not is_valid:
            result.mismatches.append({
                "claim": f"Created valid {expected_type or 'data'} file: {file_path}",
                "evidence": f"Content validation failed: {error_msg}",
                "evidence_type": "file_content",
                "details": metadata,
            })
            return

        # Check row count if claimed
        actual_rows = metadata.get("row_count")
        if claimed_rows is not None and actual_rows is not None:
            if actual_rows < claimed_rows:
                result.mismatches.append({
                    "claim": f"Downloaded {claimed_rows} rows",
                    "evidence": f"File contains only {actual_rows} rows",
                    "evidence_type": "file_content",
                    "details": {"claimed": claimed_rows, "actual": actual_rows},
                })
                return

        # Success
        result.matches.append({
            "claim": f"Created valid file: {file_path}",
            "evidence": f"File exists with {file_size} bytes, content validated",
            "evidence_type": "file_content",
            "details": metadata,
        })

    def _verify_exec_claim(
        self,
        result: CrossReferenceResult,
        command: str,
        expected_success: bool = True
    ):
        """Verify command execution claim against exec log."""
        executions = self.exec_logger.find_execution(command)

        if not executions:
            result.missing_evidence.append({
                "claim": f"Executed command: {command}",
                "evidence_type": "exec_log",
                "issue": "No execution record found in log",
            })
            return

        # Check most recent execution
        latest = executions[-1]
        actual_success = latest.get("success", False)
        exit_code = latest.get("exit_code")

        if expected_success and not actual_success:
            result.mismatches.append({
                "claim": f"Command succeeded: {command}",
                "evidence": f"Command failed with exit code {exit_code}",
                "evidence_type": "exec_log",
                "details": latest.get("error_indicators"),
            })
            return

        if latest.get("timeout", False):
            result.mismatches.append({
                "claim": f"Command completed: {command}",
                "evidence": "Command timed out",
                "evidence_type": "exec_log",
            })
            return

        # Success
        result.matches.append({
            "claim": f"Executed: {command}",
            "evidence": f"Exec log confirms exit_code={exit_code}, success={actual_success}",
            "evidence_type": "exec_log",
        })

    def _cross_reference_url_file(
        self,
        result: CrossReferenceResult,
        url: str,
        domain: str,
        file_path: str,
        working_dir: str
    ):
        """Cross-reference URL source with file content."""
        # Make path absolute
        if not os.path.isabs(file_path):
            file_path = os.path.join(working_dir, file_path)

        # Get fetch entry
        fetch_entry = self.fetch_logger.find_fetch_for_url(url)
        if not fetch_entry:
            return  # Already flagged as missing

        # Compare content lengths if available
        fetch_length = fetch_entry.get("content_length", 0)

        if os.path.exists(file_path):
            file_size = os.path.getsize(file_path)

            # Check for significant size mismatch
            if fetch_length > 0 and file_size > 0:
                ratio = file_size / fetch_length
                if ratio < 0.1:  # File is much smaller than fetched content
                    result.mismatches.append({
                        "claim": f"Saved fetched data from {domain} to {file_path}",
                        "evidence": f"File ({file_size}b) much smaller than fetch ({fetch_length}b)",
                        "evidence_type": "cross_reference",
                        "details": {"ratio": ratio, "file_size": file_size, "fetch_length": fetch_length},
                    })
                elif ratio > 10:  # File is much larger than fetched content
                    result.mismatches.append({
                        "claim": f"Saved fetched data from {domain} to {file_path}",
                        "evidence": f"File ({file_size}b) much larger than fetch ({fetch_length}b) - may include extra data",
                        "evidence_type": "cross_reference",
                        "details": {"ratio": ratio, "file_size": file_size, "fetch_length": fetch_length},
                    })
                else:
                    result.matches.append({
                        "claim": f"Data from {domain} saved to {file_path}",
                        "evidence": f"File size ({file_size}b) consistent with fetch ({fetch_length}b)",
                        "evidence_type": "cross_reference",
                    })

    def verify_batch(
        self,
        tasks: List[Dict[str, Any]],
        working_dir: str = "."
    ) -> Dict[str, CrossReferenceResult]:
        """
        Verify a batch of tasks.

        Args:
            tasks: List of task dictionaries containing claims
            working_dir: Base directory for relative file paths

        Returns:
            Dict mapping task_id -> CrossReferenceResult
        """
        results = {}

        for task in tasks:
            task_id = task.get("id", "unknown")

            # Build claims from task
            claims = {}

            # Extract URL from result
            task_result = task.get("result")
            if isinstance(task_result, dict):
                claims["url"] = task_result.get("url") or task_result.get("source_url")
                claims["row_count"] = task_result.get("row_count") or task_result.get("rows")

            # Extract file from produces
            produces = task.get("produces", "")
            if produces.startswith("file:"):
                parts = produces.split(":", maxsplit=3)
                claims["file_path"] = parts[1] if len(parts) > 1 else None
                claims["content_type"] = parts[2] if len(parts) > 2 else None

                # Extract row count from produces spec
                if len(parts) > 3:
                    row_spec = parts[3]
                    try:
                        if row_spec.endswith('+'):
                            claims["row_count"] = int(row_spec[:-1])
                        else:
                            claims["row_count"] = int(row_spec)
                    except ValueError:
                        pass

            # Extract domain hint from task content
            content = task.get("content", "").lower()
            for domain in ["noaa", "ncbi", "nasa", "usgs", "esa", "github"]:
                if domain in content:
                    claims["domain"] = domain
                    break

            # Skip tasks with no verifiable claims
            if not any(claims.values()):
                continue

            results[task_id] = self.verify_task_claims(claims, working_dir)

        return results


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


def cross_reference_claims(
    claims: Dict[str, Any],
    working_dir: str = "."
) -> CrossReferenceResult:
    """
    Convenience function for cross-reference verification.

    Example:
        result = cross_reference_claims({
            "url": "https://noaa.gov/data.csv",
            "domain": "noaa",
            "file_path": "output/noaa_data.csv",
            "row_count": 100,
            "content_type": "csv",
        })
        if not result.all_verified:
            print(f"Mismatches: {result.mismatches}")
            print(f"Missing: {result.missing_evidence}")
    """
    verifier = CrossReferenceVerifier()
    return verifier.verify_task_claims(claims, working_dir)
