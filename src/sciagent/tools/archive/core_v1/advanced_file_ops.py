"""
Advanced file operations tool.

This tool provides enhanced file operations with context awareness, 
backup management, encoding handling, and intelligent file analysis.
Designed to complement the basic str_replace_editor with advanced features.
"""

from __future__ import annotations

import os
import datetime
import tempfile
import shutil
from typing import Dict, Any, Optional, List, Tuple, Union
from pathlib import Path
import mimetypes
import chardet
import hashlib

from sciagent.base_tool import BaseTool


class AdvancedFileOperationsTool(BaseTool):
    """Advanced file operations with context awareness and safety features."""

    name = "advanced_file_ops"
    description = "Enhanced file operations with backup, encoding detection, and intelligent analysis"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": [
                    "read_with_context", "write_with_backup", "move", "copy", 
                    "analyze", "compare", "find_duplicates", "search_content",
                    "batch_rename", "archive", "restore_backup", "get_info",
                    "validate_encoding", "fix_encoding", "create_tree"
                ],
                "description": "The file operation to perform"
            },
            "path": {"type": "string", "description": "Primary file or directory path"},
            "target_path": {"type": "string", "description": "Target path for move/copy operations"},
            "content": {"type": "string", "description": "Content to write"},
            "encoding": {"type": "string", "description": "Text encoding (auto-detect if not specified)"},
            "create_backup": {"type": "boolean", "description": "Create backup before operations", "default": True},
            "line_numbers": {"type": "boolean", "description": "Include line numbers in output", "default": False},
            "start_line": {"type": "integer", "description": "Start line for range operations"},
            "end_line": {"type": "integer", "description": "End line for range operations"},
            "pattern": {"type": "string", "description": "Search pattern or regex"},
            "replacement": {"type": "string", "description": "Replacement text for search operations"},
            "recursive": {"type": "boolean", "description": "Recursive operation for directories", "default": False},
            "include_hidden": {"type": "boolean", "description": "Include hidden files", "default": False},
            "max_size": {"type": "integer", "description": "Maximum file size to process (bytes)", "default": 10485760}
        },
        "required": ["command", "path"]
    }

    def _detect_encoding(self, file_path: str) -> Tuple[str, float]:
        """Detect file encoding with confidence score."""
        try:
            with open(file_path, 'rb') as f:
                raw_data = f.read(min(32768, os.path.getsize(file_path)))  # Sample first 32KB
            
            result = chardet.detect(raw_data)
            encoding = result.get('encoding', 'utf-8')
            confidence = result.get('confidence', 0.0)
            
            # Fallback to utf-8 for low confidence
            if confidence < 0.7 and encoding.lower() not in ['utf-8', 'ascii']:
                encoding = 'utf-8'
                confidence = 0.5
            
            return encoding, confidence
        except Exception:
            return 'utf-8', 0.5

    def _create_backup(self, file_path: str, backup_dir: Optional[str] = None) -> str:
        """Create a timestamped backup of the file."""
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        file_hash = hashlib.md5(file_path.encode()).hexdigest()[:8]
        
        if backup_dir:
            os.makedirs(backup_dir, exist_ok=True)
            backup_path = os.path.join(backup_dir, f"{Path(file_path).name}.backup_{timestamp}_{file_hash}")
        else:
            backup_path = f"{file_path}.backup_{timestamp}_{file_hash}"
        
        shutil.copy2(file_path, backup_path)
        return backup_path

    def _get_file_info(self, file_path: str) -> Dict[str, Any]:
        """Get comprehensive file information."""
        path = Path(file_path)
        
        if not path.exists():
            return {"error": f"File not found: {file_path}"}
        
        stat = path.stat()
        
        info = {
            "path": str(path.absolute()),
            "name": path.name,
            "size": stat.st_size,
            "modified": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "created": datetime.datetime.fromtimestamp(stat.st_ctime).isoformat(),
            "is_file": path.is_file(),
            "is_dir": path.is_dir(),
            "is_symlink": path.is_symlink(),
            "permissions": oct(stat.st_mode)[-3:],
            "owner_readable": os.access(file_path, os.R_OK),
            "owner_writable": os.access(file_path, os.W_OK),
            "owner_executable": os.access(file_path, os.X_OK),
        }
        
        if path.is_file():
            # MIME type detection
            mime_type, _ = mimetypes.guess_type(file_path)
            info["mime_type"] = mime_type
            
            # Text file analysis
            if mime_type and mime_type.startswith('text/') or path.suffix.lower() in ['.txt', '.py', '.js', '.html', '.css', '.md']:
                encoding, confidence = self._detect_encoding(file_path)
                info["encoding"] = encoding
                info["encoding_confidence"] = confidence
                
                try:
                    with open(file_path, 'r', encoding=encoding) as f:
                        content = f.read()
                        info["line_count"] = content.count('\n') + 1
                        info["character_count"] = len(content)
                        info["word_count"] = len(content.split())
                except Exception as e:
                    info["read_error"] = str(e)
        
        return info

    def _read_with_context(self, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        """Read file with enhanced context and range support."""
        file_path = tool_input["path"]
        start_line = tool_input.get("start_line")
        end_line = tool_input.get("end_line")
        line_numbers = tool_input.get("line_numbers", False)
        encoding = tool_input.get("encoding")
        max_size = tool_input.get("max_size", 10485760)  # 10MB default
        
        if not os.path.exists(file_path):
            return {"success": False, "error": f"File not found: {file_path}"}
        
        file_size = os.path.getsize(file_path)
        if file_size > max_size:
            return {
                "success": False, 
                "error": f"File too large ({file_size} bytes > {max_size} bytes limit)"
            }
        
        # Auto-detect encoding if not specified
        if not encoding:
            encoding, confidence = self._detect_encoding(file_path)
        else:
            confidence = 1.0
        
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                if start_line or end_line:
                    lines = f.readlines()
                    start_idx = (start_line - 1) if start_line else 0
                    end_idx = end_line if end_line else len(lines)
                    selected_lines = lines[start_idx:end_idx]
                    
                    if line_numbers:
                        content_lines = []
                        for i, line in enumerate(selected_lines, start=start_idx + 1):
                            content_lines.append(f"{i:4d}: {line.rstrip()}")
                        content = '\n'.join(content_lines)
                    else:
                        content = ''.join(selected_lines)
                else:
                    content = f.read()
                    if line_numbers:
                        lines = content.split('\n')
                        content_lines = [f"{i+1:4d}: {line}" for i, line in enumerate(lines)]
                        content = '\n'.join(content_lines)
            
            file_info = self._get_file_info(file_path)
            
            return {
                "success": True,
                "output": content,
                "file_info": file_info,
                "encoding": encoding,
                "encoding_confidence": confidence,
                "lines_read": content.count('\n') + 1 if content else 0,
                "characters_read": len(content)
            }
        
        except UnicodeDecodeError as e:
            return {
                "success": False,
                "error": f"Encoding error with {encoding}: {str(e)}. Try specifying a different encoding."
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _write_with_backup(self, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        """Write file with automatic backup creation."""
        file_path = tool_input["path"]
        content = tool_input["content"]
        encoding = tool_input.get("encoding", "utf-8")
        create_backup = tool_input.get("create_backup", True)
        
        backup_path = None
        
        try:
            # Create backup if file exists
            if os.path.exists(file_path) and create_backup:
                backup_path = self._create_backup(file_path)
            
            # Ensure directory exists
            os.makedirs(os.path.dirname(file_path) or ".", exist_ok=True)
            
            # Write content to temporary file first
            temp_path = f"{file_path}.tmp_{os.getpid()}"
            
            try:
                with open(temp_path, 'w', encoding=encoding) as f:
                    f.write(content)
                
                # Atomic move
                os.rename(temp_path, file_path)
                
                file_info = self._get_file_info(file_path)
                
                return {
                    "success": True,
                    "output": f"File written successfully: {file_path}",
                    "file_info": file_info,
                    "backup_created": backup_path,
                    "encoding": encoding,
                    "bytes_written": len(content.encode(encoding))
                }
            
            except Exception as e:
                # Clean up temp file
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise e
        
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _analyze_file(self, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        """Perform comprehensive file analysis."""
        file_path = tool_input["path"]
        
        if not os.path.exists(file_path):
            return {"success": False, "error": f"File not found: {file_path}"}
        
        analysis = {}
        
        # Basic file info
        analysis.update(self._get_file_info(file_path))
        
        # Content analysis for text files
        if Path(file_path).is_file():
            try:
                encoding, confidence = self._detect_encoding(file_path)
                
                if confidence > 0.8:  # High confidence text file
                    with open(file_path, 'r', encoding=encoding) as f:
                        content = f.read()
                    
                    analysis["content_analysis"] = {
                        "lines": content.count('\n') + 1,
                        "characters": len(content),
                        "words": len(content.split()),
                        "blank_lines": content.count('\n\n'),
                        "max_line_length": max(len(line) for line in content.split('\n')) if content else 0,
                    }
                    
                    # Language-specific analysis
                    ext = Path(file_path).suffix.lower()
                    if ext == '.py':
                        analysis["python_analysis"] = self._analyze_python_file(content)
                    elif ext in ['.js', '.ts']:
                        analysis["javascript_analysis"] = self._analyze_javascript_file(content)
                    elif ext in ['.json']:
                        analysis["json_analysis"] = self._analyze_json_file(content)
            
            except Exception as e:
                analysis["content_error"] = str(e)
        
        return {"success": True, "output": "File analysis completed", "analysis": analysis}

    def _analyze_python_file(self, content: str) -> Dict[str, Any]:
        """Analyze Python file structure."""
        import ast
        
        try:
            tree = ast.parse(content)
            
            classes = []
            functions = []
            imports = []
            
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    classes.append({
                        "name": node.name,
                        "line": node.lineno,
                        "methods": [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
                    })
                elif isinstance(node, ast.FunctionDef) and not any(node.lineno >= cls["line"] for cls in classes):
                    functions.append({"name": node.name, "line": node.lineno})
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    if isinstance(node, ast.Import):
                        imports.extend([alias.name for alias in node.names])
                    else:
                        imports.append(node.module or "")
            
            return {
                "classes": classes,
                "functions": functions,
                "imports": list(set(imports)),
                "is_valid_syntax": True
            }
        
        except SyntaxError as e:
            return {"syntax_error": str(e), "is_valid_syntax": False}

    def _analyze_javascript_file(self, content: str) -> Dict[str, Any]:
        """Basic JavaScript file analysis."""
        import re
        
        # Simple regex-based analysis (could be enhanced with proper parser)
        functions = re.findall(r'function\s+(\w+)\s*\(', content)
        arrow_functions = re.findall(r'(\w+)\s*=\s*\([^)]*\)\s*=>', content)
        classes = re.findall(r'class\s+(\w+)', content)
        imports = re.findall(r'import\s+.*?\s+from\s+[\'"]([^\'"]+)[\'"]', content)
        
        return {
            "functions": functions,
            "arrow_functions": arrow_functions,
            "classes": classes,
            "imports": imports,
            "lines_of_code": len([line for line in content.split('\n') if line.strip() and not line.strip().startswith('//')])
        }

    def _analyze_json_file(self, content: str) -> Dict[str, Any]:
        """Analyze JSON file structure."""
        import json
        
        try:
            data = json.loads(content)
            
            def analyze_structure(obj, depth=0):
                if isinstance(obj, dict):
                    return {
                        "type": "object",
                        "keys": list(obj.keys()),
                        "key_count": len(obj),
                        "max_depth": max([analyze_structure(v, depth + 1).get("max_depth", depth) for v in obj.values()], default=depth)
                    }
                elif isinstance(obj, list):
                    return {
                        "type": "array", 
                        "length": len(obj),
                        "item_types": list(set(type(item).__name__ for item in obj)),
                        "max_depth": max([analyze_structure(item, depth + 1).get("max_depth", depth) for item in obj], default=depth)
                    }
                else:
                    return {"type": type(obj).__name__, "max_depth": depth}
            
            structure = analyze_structure(data)
            
            return {
                "is_valid_json": True,
                "structure": structure,
                "size_bytes": len(content.encode('utf-8'))
            }
        
        except json.JSONDecodeError as e:
            return {"json_error": str(e), "is_valid_json": False}

    def _find_duplicates(self, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        """Find duplicate files by content hash."""
        directory = tool_input["path"]
        recursive = tool_input.get("recursive", True)
        
        if not os.path.isdir(directory):
            return {"success": False, "error": f"Not a directory: {directory}"}
        
        hash_map: Dict[str, List[str]] = {}
        
        def scan_directory(path: str):
            for item in os.listdir(path):
                item_path = os.path.join(path, item)
                
                if os.path.isdir(item_path) and recursive:
                    scan_directory(item_path)
                elif os.path.isfile(item_path):
                    try:
                        with open(item_path, 'rb') as f:
                            file_hash = hashlib.md5(f.read()).hexdigest()
                        
                        if file_hash not in hash_map:
                            hash_map[file_hash] = []
                        hash_map[file_hash].append(item_path)
                    
                    except Exception:
                        continue  # Skip files that can't be read
        
        scan_directory(directory)
        
        duplicates = {hash_val: paths for hash_val, paths in hash_map.items() if len(paths) > 1}
        
        return {
            "success": True,
            "output": f"Found {len(duplicates)} sets of duplicate files",
            "duplicates": duplicates,
            "total_files_scanned": sum(len(paths) for paths in hash_map.values()),
            "duplicate_count": sum(len(paths) for paths in duplicates.values())
        }

    def run(self, tool_input: Dict[str, Any], agent: Optional[Any] = None) -> Dict[str, Any]:
        command = tool_input.get("command")
        
        try:
            if command == "read_with_context":
                return self._read_with_context(tool_input)
            elif command == "write_with_backup":
                return self._write_with_backup(tool_input)
            elif command == "analyze":
                return self._analyze_file(tool_input)
            elif command == "get_info":
                file_info = self._get_file_info(tool_input["path"])
                if "error" in file_info:
                    return {"success": False, "error": file_info["error"]}
                return {"success": True, "output": "File info retrieved", "file_info": file_info}
            elif command == "find_duplicates":
                return self._find_duplicates(tool_input)
            elif command == "validate_encoding":
                encoding, confidence = self._detect_encoding(tool_input["path"])
                return {
                    "success": True,
                    "output": f"Detected encoding: {encoding} (confidence: {confidence:.2f})",
                    "encoding": encoding,
                    "confidence": confidence
                }
            else:
                return {"success": False, "error": f"Unknown command: {command}"}
        
        except Exception as e:
            return {"success": False, "error": str(e)}


def get_tool() -> BaseTool:
    """Return an instance of AdvancedFileOperationsTool."""
    return AdvancedFileOperationsTool()