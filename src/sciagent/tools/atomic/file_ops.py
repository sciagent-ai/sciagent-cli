"""
File operations tool - unified read/write/edit/list.

This tool IS the memory system. The filesystem persists data.
Supports text files, PDFs (with pypdf), and images (PNG, JPG, GIF, WebP).
"""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path
from typing import Dict, Any, Optional, List, Union
from dataclasses import dataclass

# Optional: PDF extraction support
try:
    import pypdf
    PYPDF_AVAILABLE = True
except ImportError:
    try:
        import PyPDF2 as pypdf
        PYPDF_AVAILABLE = False  # Mark as available via fallback
        PYPDF_AVAILABLE = True
    except ImportError:
        PYPDF_AVAILABLE = False


@dataclass
class ToolResult:
    """Result from tool execution."""
    success: bool
    output: Any
    error: Optional[str] = None


class FileOpsTool:
    """Unified file operations - read, write, edit, list."""

    name = "file_ops"
    description = "File operations: read, write, edit, list. Read supports text files, PDFs, images (PNG, JPG, GIF, WebP), and common code formats."

    # Supported image extensions and their media types
    IMAGE_EXTENSIONS = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }

    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["read", "write", "edit", "list"],
                "description": "Operation to perform"
            },
            "path": {
                "type": "string",
                "description": "File or directory path"
            },
            "content": {
                "type": "string",
                "description": "Content for write command"
            },
            "old_str": {
                "type": "string",
                "description": "String to replace (for edit command)"
            },
            "new_str": {
                "type": "string",
                "description": "Replacement string (for edit command)"
            },
            "start_line": {
                "type": "integer",
                "description": "Start line for read (1-indexed)"
            },
            "end_line": {
                "type": "integer",
                "description": "End line for read (1-indexed, -1 for EOF)"
            },
            "recursive": {
                "type": "boolean",
                "description": "Recursive listing",
                "default": False
            },
            "show_hidden": {
                "type": "boolean",
                "description": "Show hidden files in list",
                "default": False
            }
        },
        "required": ["command", "path"]
    }

    # Detect language from extension
    LANGUAGE_MAP = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".java": "java", ".cpp": "cpp", ".c": "c", ".go": "go",
        ".rs": "rust", ".rb": "ruby", ".php": "php", ".swift": "swift",
        ".html": "html", ".css": "css", ".json": "json",
        ".yaml": "yaml", ".yml": "yaml", ".md": "markdown",
        ".txt": "text", ".sh": "bash", ".sql": "sql",
    }

    def __init__(self, working_dir: str = "."):
        self.working_dir = Path(working_dir).resolve()
        # Track files that have been read in this session (for read-before-write warnings)
        self._read_files: set = set()

    def _resolve_path(self, path: str) -> Path:
        """Resolve path relative to working directory."""
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.working_dir / p
        return p

    def _detect_language(self, path: str) -> str:
        """Detect language from file extension."""
        ext = Path(path).suffix.lower()
        return self.LANGUAGE_MAP.get(ext, "unknown")

    def execute(self, command: str, path: str, **kwargs) -> ToolResult:
        """Execute file operation."""
        if command == "read":
            return self._read(path, kwargs.get("start_line"), kwargs.get("end_line"))
        elif command == "write":
            return self._write(path, kwargs.get("content", ""))
        elif command == "edit":
            return self._edit(path, kwargs.get("old_str", ""), kwargs.get("new_str", ""))
        elif command == "list":
            return self._list(path, kwargs.get("recursive", False), kwargs.get("show_hidden", False))
        else:
            return ToolResult(success=False, output=None, error=f"Unknown command: {command}")

    def _read(self, path: str, start_line: Optional[int] = None, end_line: Optional[int] = None) -> ToolResult:
        """Read file contents. Supports text files, PDFs, and images."""
        try:
            p = self._resolve_path(path)

            if not p.exists():
                return ToolResult(success=False, output=None, error=f"File not found: {path}")

            # Track that this file has been read
            self._read_files.add(str(p))

            if p.is_dir():
                return self._list(path, recursive=False, show_hidden=False)

            # Handle image files - return base64 encoded data for multimodal LLM
            if p.suffix.lower() in self.IMAGE_EXTENSIONS:
                return self._read_image(p)

            # Handle PDF files specially
            if p.suffix.lower() == '.pdf':
                return self._read_pdf(p, start_line, end_line)

            content = p.read_text(encoding="utf-8")
            lines = content.splitlines()

            # Apply line range if specified
            if start_line is not None:
                start_idx = max(0, start_line - 1)
                end_idx = len(lines) if (end_line == -1 or end_line is None) else end_line
                lines = lines[start_idx:end_idx]
                line_offset = start_idx
            else:
                line_offset = 0

            # Add line numbers
            numbered = []
            for i, line in enumerate(lines):
                line_num = i + line_offset + 1
                numbered.append(f"{line_num:4d} | {line}")

            return ToolResult(
                success=True,
                output="\n".join(numbered),
                error=None
            )

        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def _read_pdf(self, path: Path, start_line: Optional[int] = None, end_line: Optional[int] = None) -> ToolResult:
        """Extract text from PDF file."""
        if not PYPDF_AVAILABLE:
            return ToolResult(
                success=False,
                output=None,
                error="PDF reading requires pypdf. Install with: pip install pypdf"
            )

        try:
            pdf_bytes = path.read_bytes()
            pdf_file = io.BytesIO(pdf_bytes)
            reader = pypdf.PdfReader(pdf_file)

            text_parts = []
            for page_num, page in enumerate(reader.pages):
                try:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(f"--- Page {page_num + 1} ---\n{page_text}")
                except Exception as e:
                    text_parts.append(f"--- Page {page_num + 1} ---\n[Error extracting: {e}]")

            content = "\n\n".join(text_parts)
            lines = content.splitlines()

            # Apply line range if specified
            if start_line is not None:
                start_idx = max(0, start_line - 1)
                end_idx = len(lines) if (end_line == -1 or end_line is None) else end_line
                lines = lines[start_idx:end_idx]
                line_offset = start_idx
            else:
                line_offset = 0

            # Add line numbers
            numbered = []
            for i, line in enumerate(lines):
                line_num = i + line_offset + 1
                numbered.append(f"{line_num:4d} | {line}")

            return ToolResult(
                success=True,
                output="\n".join(numbered),
                error=None
            )

        except Exception as e:
            return ToolResult(success=False, output=None, error=f"Failed to read PDF: {str(e)}")

    def _read_image(self, path: Path) -> ToolResult:
        """
        Read image file and return base64-encoded data for multimodal LLM.

        Returns a structured output that can be used to create multimodal messages.
        The output contains: type, media_type, data (base64), and file info.
        """
        try:
            # Get media type from extension
            media_type = self.IMAGE_EXTENSIONS.get(path.suffix.lower(), "image/png")

            # Read and encode image
            image_bytes = path.read_bytes()
            b64_data = base64.b64encode(image_bytes).decode("utf-8")

            # Get file size for display
            size_kb = len(image_bytes) / 1024

            return ToolResult(
                success=True,
                output={
                    "type": "image",
                    "media_type": media_type,
                    "data": b64_data,
                    "file_path": str(path),
                    "size_kb": round(size_kb, 2),
                    "display_text": f"[Image: {path.name} ({size_kb:.1f} KB, {media_type})]"
                },
                error=None
            )

        except Exception as e:
            return ToolResult(success=False, output=None, error=f"Failed to read image: {str(e)}")

    def _write(self, path: str, content: str) -> ToolResult:
        """Write content to file."""
        # Validate content is not empty
        if content is None or content == "":
            return ToolResult(
                success=False,
                output=None,
                error=f"Cannot write to {path}: content is empty. Provide non-empty content."
            )

        try:
            p = self._resolve_path(path)

            # Create parent directories
            p.parent.mkdir(parents=True, exist_ok=True)

            p.write_text(content, encoding="utf-8")

            return ToolResult(
                success=True,
                output=f"Wrote {len(content)} chars to {path}",
                error=None
            )

        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def _edit(self, path: str, old_str: str, new_str: str) -> ToolResult:
        """Replace string in file (must be unique)."""
        try:
            p = self._resolve_path(path)

            if not p.exists():
                return ToolResult(success=False, output=None, error=f"File not found: {path}")

            # Check if file was read first (warn but don't block)
            was_read = str(p) in self._read_files
            warning_prefix = ""
            if not was_read:
                warning_prefix = (
                    "[WARNING] Editing file without reading it first. "
                    "Consider reading files before editing to understand context.\n\n"
                )

            content = p.read_text(encoding="utf-8")
            count = content.count(old_str)

            if count == 0:
                return ToolResult(success=False, output=None, error=f"String not found in {path}")
            if count > 1:
                return ToolResult(success=False, output=None, error=f"String appears {count} times (must be unique)")

            new_content = content.replace(old_str, new_str)
            p.write_text(new_content, encoding="utf-8")

            return ToolResult(
                success=True,
                output=f"{warning_prefix}Edited {path} (changed {len(old_str)} -> {len(new_str)} chars)",
                error=None
            )

        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def _list(self, path: str, recursive: bool = False, show_hidden: bool = False) -> ToolResult:
        """List directory contents."""
        try:
            p = self._resolve_path(path)

            if not p.exists():
                return ToolResult(success=False, output=None, error=f"Path not found: {path}")

            if not p.is_dir():
                return ToolResult(success=False, output=None, error=f"Not a directory: {path}")

            items: List[Dict[str, Any]] = []

            if recursive:
                for root, dirs, files in os.walk(p):
                    if not show_hidden:
                        dirs[:] = [d for d in dirs if not d.startswith(".")]
                        files = [f for f in files if not f.startswith(".")]

                    for name in dirs + files:
                        full_path = os.path.join(root, name)
                        is_dir = os.path.isdir(full_path)
                        size = 0 if is_dir else os.path.getsize(full_path)
                        items.append({
                            "path": os.path.relpath(full_path, p),
                            "is_dir": is_dir,
                            "size": size
                        })
            else:
                for name in sorted(os.listdir(p)):
                    if not show_hidden and name.startswith("."):
                        continue
                    full_path = p / name
                    is_dir = full_path.is_dir()
                    size = 0 if is_dir else full_path.stat().st_size
                    items.append({
                        "path": name,
                        "is_dir": is_dir,
                        "size": size
                    })

            # Sort: directories first, then alphabetically
            items.sort(key=lambda x: (not x["is_dir"], x["path"]))

            # Format output
            lines = []
            for item in items:
                indicator = "/" if item["is_dir"] else ""
                size_str = "-" if item["is_dir"] else f"{item['size']}b"
                lines.append(f"{item['path']}{indicator}  {size_str}")

            return ToolResult(
                success=True,
                output="\n".join(lines) if lines else "(empty directory)",
                error=None
            )

        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def to_schema(self) -> Dict:
        """Convert to OpenAI-style tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


def get_tool(working_dir: str = ".") -> FileOpsTool:
    """Factory function for tool discovery."""
    return FileOpsTool(working_dir)
