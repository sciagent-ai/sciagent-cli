"""
Git operations tool with intelligent workflows.

This tool provides advanced git functionality including smart commit message 
generation, branch management, conflict resolution guidance, and integration
with common development workflows.
"""

from __future__ import annotations

import os
import subprocess
import datetime
import re
from typing import Dict, Any, Optional, List, Tuple
from pathlib import Path

from sciagent.base_tool import BaseTool


class GitOperationsTool(BaseTool):
    """Execute git commands with intelligent workflows and automation."""

    name = "git_operations"
    description = "Execute git commands with smart commit workflows, branch management, and conflict resolution"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": [
                    "status", "add", "commit", "push", "pull", "diff", "log", 
                    "branch", "checkout", "merge", "rebase", "reset", "stash",
                    "smart_commit", "auto_commit_and_push", "create_branch", 
                    "conflict_resolution", "commit_message_gen"
                ],
                "description": "The git command or workflow to execute"
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Files to add/commit (for add, commit operations)"
            },
            "message": {"type": "string", "description": "Commit message (for commit operations)"},
            "branch": {"type": "string", "description": "Branch name (for branch operations)"},
            "remote": {"type": "string", "description": "Remote name", "default": "origin"},
            "auto_message": {"type": "boolean", "description": "Auto-generate commit message", "default": False},
            "force": {"type": "boolean", "description": "Force the operation", "default": False},
            "interactive": {"type": "boolean", "description": "Interactive mode for complex operations", "default": True},
            "additional_args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Additional git arguments"
            }
        },
        "required": ["command"]
    }

    def _run_git_command(self, args: List[str], cwd: Optional[str] = None) -> Tuple[bool, str, str]:
        """Run a git command and return success, stdout, stderr."""
        try:
            result = subprocess.run(
                ["git"] + args,
                capture_output=True,
                text=True,
                cwd=cwd or os.getcwd(),
                timeout=60
            )
            return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
        except subprocess.TimeoutExpired:
            return False, "", "Git command timed out"
        except Exception as e:
            return False, "", str(e)

    def _is_git_repo(self, path: Optional[str] = None) -> bool:
        """Check if current directory is a git repository."""
        success, _, _ = self._run_git_command(["rev-parse", "--git-dir"], path)
        return success

    def _get_git_status(self) -> Dict[str, Any]:
        """Get detailed git status information."""
        success, output, error = self._run_git_command(["status", "--porcelain=v1"])
        if not success:
            return {"error": error}
        
        status_info = {
            "modified": [],
            "added": [],
            "deleted": [],
            "untracked": [],
            "renamed": [],
            "copied": []
        }
        
        for line in output.split('\n'):
            if not line:
                continue
            
            status_code = line[:2]
            file_path = line[3:]
            
            if status_code == "??":
                status_info["untracked"].append(file_path)
            elif status_code[0] == "M" or status_code[1] == "M":
                status_info["modified"].append(file_path)
            elif status_code[0] == "A" or status_code[1] == "A":
                status_info["added"].append(file_path)
            elif status_code[0] == "D" or status_code[1] == "D":
                status_info["deleted"].append(file_path)
            elif status_code[0] == "R":
                status_info["renamed"].append(file_path)
            elif status_code[0] == "C":
                status_info["copied"].append(file_path)
        
        return status_info

    def _generate_commit_message(self, files: Optional[List[str]] = None) -> str:
        """Generate an intelligent commit message based on changes."""
        # Get diff information
        diff_cmd = ["diff", "--cached"] if files else ["diff", "HEAD~1"]
        success, diff_output, _ = self._run_git_command(diff_cmd)
        
        if not success or not diff_output:
            return "Update files"
        
        # Analyze changes
        additions = len(re.findall(r'^\+', diff_output, re.MULTILINE))
        deletions = len(re.findall(r'^\-', diff_output, re.MULTILINE))
        
        # Get file types
        success, files_output, _ = self._run_git_command(["diff", "--cached", "--name-only"])
        changed_files = files_output.split('\n') if files_output else []
        
        # Categorize changes
        file_types = set()
        for file in changed_files:
            if file:
                ext = Path(file).suffix.lower()
                if ext in ['.py', '.js', '.ts', '.java', '.cpp', '.c', '.go', '.rs']:
                    file_types.add('code')
                elif ext in ['.md', '.txt', '.rst']:
                    file_types.add('docs')
                elif ext in ['.json', '.yaml', '.yml', '.toml', '.ini']:
                    file_types.add('config')
                elif ext in ['.html', '.css', '.scss']:
                    file_types.add('frontend')
                elif ext in ['.sql']:
                    file_types.add('database')
                elif ext in ['.py', '.ipynb']:
                    file_types.add('notebook')
        
        # Generate message based on patterns
        if len(changed_files) == 1:
            action = "Add" if additions > deletions * 2 else "Update" if additions > deletions else "Fix"
            file_name = Path(changed_files[0]).name
            return f"{action} {file_name}"
        
        elif 'code' in file_types:
            if additions > deletions * 3:
                return f"Add new functionality ({len(changed_files)} files)"
            elif deletions > additions * 2:
                return f"Remove deprecated code ({len(changed_files)} files)"
            else:
                return f"Update implementation ({len(changed_files)} files)"
        
        elif 'docs' in file_types:
            return f"Update documentation ({len(changed_files)} files)"
        
        elif 'config' in file_types:
            return f"Update configuration ({len(changed_files)} files)"
        
        else:
            return f"Update {len(changed_files)} files"

    def _get_branch_info(self) -> Dict[str, Any]:
        """Get current branch information."""
        # Current branch
        success, current_branch, _ = self._run_git_command(["branch", "--show-current"])
        if not success:
            return {"error": "Unable to get branch info"}
        
        # Remote tracking
        success, remote_branch, _ = self._run_git_command([
            "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"
        ])
        
        # Ahead/behind status
        if success and remote_branch:
            success_ahead, ahead, _ = self._run_git_command([
                "rev-list", "--count", f"{remote_branch}..HEAD"
            ])
            success_behind, behind, _ = self._run_git_command([
                "rev-list", "--count", f"HEAD..{remote_branch}"
            ])
            
            return {
                "current": current_branch,
                "remote": remote_branch,
                "ahead": int(ahead) if success_ahead and ahead.isdigit() else 0,
                "behind": int(behind) if success_behind and behind.isdigit() else 0
            }
        
        return {"current": current_branch, "remote": None}

    def _smart_commit_workflow(self, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a smart commit workflow with validation and message generation."""
        files = tool_input.get("files", [])
        message = tool_input.get("message", "")
        auto_message = tool_input.get("auto_message", False)
        
        # Get current status
        status = self._get_git_status()
        if "error" in status:
            return {"success": False, "error": f"Git status failed: {status['error']}"}
        
        # If no files specified, use modified files
        if not files:
            files = status.get("modified", []) + status.get("untracked", [])
        
        if not files:
            return {"success": False, "error": "No files to commit"}
        
        results = []
        
        # Add files
        for file in files:
            success, output, error = self._run_git_command(["add", file])
            if not success:
                return {"success": False, "error": f"Failed to add {file}: {error}"}
            results.append(f"Added: {file}")
        
        # Generate message if needed
        if auto_message or not message:
            message = self._generate_commit_message(files)
            results.append(f"Generated commit message: {message}")
        
        # Add SciAgent attribution
        full_message = f"{message}\n\n🤖 Generated with [SciAgent]\n\nCo-Authored-By: SciAgent <noreply@sciagent.com>"
        
        # Commit
        success, output, error = self._run_git_command(["commit", "-m", full_message])
        if not success:
            return {"success": False, "error": f"Commit failed: {error}"}
        
        results.append(f"Committed: {message}")
        
        return {
            "success": True,
            "output": "\n".join(results),
            "commit_message": message,
            "files_committed": files,
            "commit_hash": output.split()[1] if output else "unknown"
        }

    def _auto_commit_and_push(self, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        """Auto-commit all changes and push to remote."""
        # First do smart commit
        commit_result = self._smart_commit_workflow(tool_input)
        if not commit_result["success"]:
            return commit_result
        
        results = [commit_result["output"]]
        
        # Get branch info for push
        branch_info = self._get_branch_info()
        current_branch = branch_info.get("current", "main")
        remote = tool_input.get("remote", "origin")
        
        # Push to remote
        success, output, error = self._run_git_command(["push", remote, current_branch])
        if not success:
            return {
                "success": False,
                "error": f"Push failed: {error}",
                "commit_successful": True,
                "commit_hash": commit_result.get("commit_hash")
            }
        
        results.append(f"Pushed to {remote}/{current_branch}")
        
        return {
            "success": True,
            "output": "\n".join(results),
            "commit_hash": commit_result.get("commit_hash"),
            "pushed_to": f"{remote}/{current_branch}"
        }

    def _create_branch(self, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        """Create and optionally checkout a new branch."""
        branch = tool_input.get("branch")
        if not branch:
            return {"success": False, "error": "Branch name required"}
        
        # Check if branch already exists
        success, existing_branches, _ = self._run_git_command(["branch", "--list", branch])
        if branch in existing_branches:
            return {"success": False, "error": f"Branch '{branch}' already exists"}
        
        # Create branch
        success, output, error = self._run_git_command(["checkout", "-b", branch])
        if not success:
            return {"success": False, "error": f"Failed to create branch: {error}"}
        
        return {
            "success": True,
            "output": f"Created and checked out new branch: {branch}",
            "branch": branch
        }

    def _conflict_resolution(self, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        """Provide guidance for resolving merge conflicts."""
        # Get conflicted files
        success, status_output, _ = self._run_git_command(["status", "--porcelain"])
        if not success:
            return {"success": False, "error": "Unable to get git status"}
        
        conflicted_files = []
        for line in status_output.split('\n'):
            if line.startswith('UU ') or line.startswith('AA ') or line.startswith('DD '):
                conflicted_files.append(line[3:])
        
        if not conflicted_files:
            return {"success": True, "output": "No merge conflicts detected"}
        
        guidance = []
        guidance.append(f"Found {len(conflicted_files)} conflicted files:")
        
        for file in conflicted_files:
            guidance.append(f"\n📁 {file}:")
            guidance.append("   1. Open the file in your editor")
            guidance.append("   2. Look for conflict markers: <<<<<<<, =======, >>>>>>>")
            guidance.append("   3. Choose which changes to keep")
            guidance.append("   4. Remove conflict markers")
            guidance.append(f"   5. Run: git add {file}")
        
        guidance.append("\nAfter resolving all conflicts:")
        guidance.append("   6. Run: git commit (to complete the merge)")
        
        return {
            "success": True,
            "output": "\n".join(guidance),
            "conflicted_files": conflicted_files,
            "resolution_steps": [
                "Edit conflicted files",
                "Remove conflict markers", 
                "Add resolved files",
                "Commit the merge"
            ]
        }

    def run(self, tool_input: Dict[str, Any], agent: Optional[Any] = None) -> Dict[str, Any]:
        command = tool_input.get("command")
        
        # Check if we're in a git repository (except for init)
        if command != "init" and not self._is_git_repo():
            return {"success": False, "error": "Not a git repository. Run 'git init' first."}
        
        # Handle special workflow commands
        if command == "smart_commit":
            return self._smart_commit_workflow(tool_input)
        elif command == "auto_commit_and_push":
            return self._auto_commit_and_push(tool_input)
        elif command == "create_branch":
            return self._create_branch(tool_input)
        elif command == "conflict_resolution":
            return self._conflict_resolution(tool_input)
        elif command == "commit_message_gen":
            message = self._generate_commit_message(tool_input.get("files"))
            return {"success": True, "output": message, "generated_message": message}
        
        # Handle standard git commands
        try:
            git_args = [command]
            
            # Add command-specific arguments
            if command in ["add", "commit"] and "files" in tool_input:
                git_args.extend(tool_input["files"])
            
            if command == "commit" and "message" in tool_input:
                git_args.extend(["-m", tool_input["message"]])
            
            if command in ["checkout", "branch"] and "branch" in tool_input:
                git_args.append(tool_input["branch"])
            
            if command == "push" and "remote" in tool_input:
                git_args.append(tool_input["remote"])
                if "branch" in tool_input:
                    git_args.append(tool_input["branch"])
            
            # Add additional arguments
            if "additional_args" in tool_input:
                git_args.extend(tool_input["additional_args"])
            
            # Execute command
            success, output, error = self._run_git_command(git_args)
            
            if success:
                # Track git operations in agent state
                if agent is not None and command in ["add", "commit", "push"]:
                    try:
                        agent.state.last_successful_operation = f"git {command}: {' '.join(git_args[1:])}"
                    except Exception:
                        pass
                
                result = {"success": True, "output": output or f"Git {command} completed successfully"}
                
                # Add extra info for status command
                if command == "status":
                    status_info = self._get_git_status()
                    branch_info = self._get_branch_info()
                    result.update({"status_details": status_info, "branch_info": branch_info})
                
                return result
            else:
                return {"success": False, "error": error or f"Git {command} failed"}
        
        except Exception as e:
            return {"success": False, "error": f"Git operation failed: {str(e)}"}


def get_tool() -> BaseTool:
    """Return an instance of GitOperationsTool."""
    return GitOperationsTool()