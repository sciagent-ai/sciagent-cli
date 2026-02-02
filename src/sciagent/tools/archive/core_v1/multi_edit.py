"""
Multi-edit tool for atomic file operations.

This tool enables batch editing of single or multiple files with atomic
transaction semantics. All edits are applied together or none at all,
ensuring consistency and providing rollback capabilities on failure.
"""

from __future__ import annotations

import os
import datetime
import tempfile
import shutil
from typing import Dict, Any, Optional, List, Tuple
from pathlib import Path
import hashlib

from sciagent.base_tool import BaseTool


class MultiEditTool(BaseTool):
    """Perform multiple atomic edits to single or multiple files."""

    name = "multi_edit"
    description = "Apply multiple edits to single or multiple files atomically with rollback on failure"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "Path to the file to edit"},
                        "edits": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "old_str": {"type": "string", "description": "String to replace"},
                                    "new_str": {"type": "string", "description": "Replacement string"},
                                    "occurrence": {"type": "integer", "description": "Which occurrence to replace (1-based, 0=all)", "default": 0}
                                },
                                "required": ["old_str", "new_str"]
                            }
                        }
                    },
                    "required": ["file_path", "edits"]
                },
                "description": "List of files and their edits to apply atomically"
            },
            "create_backup": {"type": "boolean", "description": "Create backup files before editing", "default": True},
            "validate_syntax": {"type": "boolean", "description": "Validate syntax after editing for known file types", "default": False}
        },
        "required": ["edits"]
    }

    def _create_backup(self, file_path: str) -> str:
        """Create a backup of the file and return backup path."""
        backup_path = f"{file_path}.backup_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(file_path, backup_path)
        return backup_path

    def _validate_syntax(self, file_path: str, content: str) -> Tuple[bool, str]:
        """Validate syntax for known file types."""
        ext = Path(file_path).suffix.lower()
        
        if ext == ".py":
            try:
                compile(content, file_path, 'exec')
                return True, ""
            except SyntaxError as e:
                return False, f"Python syntax error: {e}"
        elif ext == ".json":
            try:
                import json
                json.loads(content)
                return True, ""
            except json.JSONDecodeError as e:
                return False, f"JSON syntax error: {e}"
        elif ext in [".yaml", ".yml"]:
            try:
                import yaml
                yaml.safe_load(content)
                return True, ""
            except yaml.YAMLError as e:
                return False, f"YAML syntax error: {e}"
        
        # No validation for unknown file types
        return True, ""

    def _apply_file_edits(self, file_path: str, edits: List[Dict[str, Any]]) -> str:
        """Apply all edits to a single file and return new content."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Apply edits in reverse order by position to avoid offset issues
        for edit in edits:
            old_str = edit["old_str"]
            new_str = edit["new_str"]
            occurrence = edit.get("occurrence", 0)
            
            if old_str not in content:
                raise ValueError(f"String not found in {file_path}: {old_str[:50]}...")
            
            if occurrence == 0:  # Replace all occurrences
                content = content.replace(old_str, new_str)
            else:  # Replace specific occurrence
                parts = content.split(old_str)
                if occurrence > len(parts) - 1:
                    raise ValueError(f"Occurrence {occurrence} not found in {file_path}")
                
                # Rejoin with replacement at specific occurrence
                before = old_str.join(parts[:occurrence])
                after = old_str.join(parts[occurrence + 1:])
                content = before + new_str + (old_str + after if after else "")
        
        return content

    def _detect_language(self, file_path: str) -> str:
        """Infer the programming language from the file extension."""
        ext = Path(file_path).suffix.lower()
        language_map = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".java": "java",
            ".cpp": "cpp",
            ".c": "c",
            ".cs": "csharp",
            ".go": "go",
            ".rs": "rust",
            ".php": "php",
            ".rb": "ruby",
            ".swift": "swift",
            ".kt": "kotlin",
            ".scala": "scala",
            ".html": "html",
            ".css": "css",
            ".scss": "scss",
            ".md": "markdown",
            ".json": "json",
            ".yaml": "yaml",
            ".yml": "yaml",
            ".txt": "text",
        }
        return language_map.get(ext, "unknown")

    def run(self, tool_input: Dict[str, Any], agent: Optional[Any] = None) -> Dict[str, Any]:
        edits_list = tool_input.get("edits", [])
        create_backup = tool_input.get("create_backup", True)
        validate_syntax = tool_input.get("validate_syntax", False)
        
        if not edits_list:
            return {"success": False, "error": "No edits specified"}
        
        # Track changes for rollback
        original_contents: Dict[str, str] = {}
        backup_files: Dict[str, str] = {}
        modified_files: List[str] = []
        
        try:
            # Phase 1: Validate all files exist and prepare backups
            for file_edit in edits_list:
                file_path = file_edit["file_path"]
                
                if not os.path.exists(file_path):
                    return {"success": False, "error": f"File not found: {file_path}"}
                
                # Store original content
                with open(file_path, 'r', encoding='utf-8') as f:
                    original_contents[file_path] = f.read()
                
                # Create backup if requested
                if create_backup:
                    backup_files[file_path] = self._create_backup(file_path)
            
            # Phase 2: Apply all edits and validate
            new_contents: Dict[str, str] = {}
            
            for file_edit in edits_list:
                file_path = file_edit["file_path"]
                file_edits = file_edit["edits"]
                
                # Apply edits to this file
                new_content = self._apply_file_edits(file_path, file_edits)
                new_contents[file_path] = new_content
                
                # Validate syntax if requested
                if validate_syntax:
                    is_valid, error_msg = self._validate_syntax(file_path, new_content)
                    if not is_valid:
                        raise ValueError(f"Syntax validation failed for {file_path}: {error_msg}")
            
            # Phase 3: Write all files atomically
            for file_path, new_content in new_contents.items():
                # Write to temporary file first
                temp_file = f"{file_path}.tmp_{os.getpid()}"
                
                try:
                    with open(temp_file, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    
                    # Atomic rename
                    os.rename(temp_file, file_path)
                    modified_files.append(file_path)
                    
                    # Track file changes in agent state
                    if agent is not None:
                        try:
                            agent.state.files_tracking[file_path] = {
                                "modified": datetime.datetime.now().isoformat(),
                                "size": len(new_content),
                                "lines": new_content.count('\n') + 1,
                                "action": "multi_edited",
                                "language": self._detect_language(file_path),
                                "changes": {
                                    "old_size": len(original_contents[file_path]),
                                    "new_size": len(new_content),
                                    "diff": len(new_content) - len(original_contents[file_path]),
                                },
                                "backup_file": backup_files.get(file_path)
                            }
                        except Exception:
                            pass  # Don't fail the whole operation for tracking issues
                
                except Exception as e:
                    # Clean up temp file if it exists
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                    raise e
            
            # Success! Calculate summary
            total_size_change = sum(
                len(new_contents[fp]) - len(original_contents[fp]) 
                for fp in new_contents
            )
            
            files_summary = []
            for file_path in modified_files:
                old_size = len(original_contents[file_path])
                new_size = len(new_contents[file_path])
                files_summary.append(f"{file_path} ({new_size - old_size:+d} chars)")
            
            return {
                "success": True,
                "output": f"Successfully applied edits to {len(modified_files)} files:\n" + 
                         "\n".join(files_summary) + 
                         f"\nTotal size change: {total_size_change:+d} characters",
                "files_modified": modified_files,
                "backup_files": backup_files,
                "total_size_change": total_size_change,
                "syntax_validated": validate_syntax
            }
        
        except Exception as e:
            # Rollback: restore original files
            for file_path in modified_files:
                try:
                    if file_path in original_contents:
                        with open(file_path, 'w', encoding='utf-8') as f:
                            f.write(original_contents[file_path])
                except Exception as restore_error:
                    # Log but don't mask original error
                    pass
            
            # Clean up backup files on failure if they were created
            if create_backup:
                for backup_file in backup_files.values():
                    try:
                        if os.path.exists(backup_file):
                            os.remove(backup_file)
                    except Exception:
                        pass
            
            return {
                "success": False,
                "error": f"Multi-edit failed: {str(e)}. All changes have been rolled back.",
                "files_attempted": list(original_contents.keys()),
                "rollback_performed": True
            }


def get_tool() -> BaseTool:
    """Return an instance of MultiEditTool."""
    return MultiEditTool()