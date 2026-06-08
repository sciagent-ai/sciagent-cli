"""
File operations tool - unified read/write/edit/list.

This tool IS the memory system. The filesystem persists data.
Supports text files, images (PNG, JPG, GIF, WebP) and PDFs.

PDFs are handed to the model as a native multimodal attachment (not
pre-extracted text), so figures, tables, and equations survive. A pypdf
text extraction is kept alongside as a fallback. Provider wire-format
dispatch lives in ``llm._format_attachments_for_provider``.
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
    description = (
        "File operations: read, write, edit, list. Read supports text files, "
        "PDFs, images (PNG, JPG, GIF, WebP), and common code formats. PDFs "
        "over 10 pages require an explicit `pages` range (e.g., \"1-10\"); "
        "the per-read cap is 20 pages."
    )

    # PDFs ride a native multimodal channel; an unbounded page count turns
    # into an unbounded token bill on the next LLM turn, which has bitten
    # us with max_tokens-cap truncations. Mirror Claude Code's Read tool:
    # require an explicit `pages` range past the soft cap, hard-cap per
    # read at the second number.
    MAX_PDF_PAGES_NO_RANGE = 10
    MAX_PDF_PAGES_PER_READ = 20

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
            "max_lines": {
                "type": "integer",
                "description": "Max lines to return (default 200). Use search tool for specific content in large files.",
                "default": 200
            },
            "pattern": {
                "type": "string",
                "description": "Search pattern - only return lines containing this (like grep)"
            },
            "tail": {
                "type": "integer",
                "description": "Read last N lines (like tail -n). Useful for logs/errors."
            },
            "pages": {
                "type": "string",
                "description": "PDF page range: \"5\" or \"1-10\"."
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
            return self._read(
                path,
                kwargs.get("start_line"),
                kwargs.get("end_line"),
                kwargs.get("max_lines", 200),
                kwargs.get("pattern"),
                kwargs.get("tail"),
                kwargs.get("pages"),
            )
        elif command == "write":
            return self._write(path, kwargs.get("content", ""))
        elif command == "edit":
            return self._edit(path, kwargs.get("old_str", ""), kwargs.get("new_str", ""))
        elif command == "list":
            return self._list(path, kwargs.get("recursive", False), kwargs.get("show_hidden", False))
        else:
            return ToolResult(success=False, output=None, error=f"Unknown command: {command}")

    def _read(
        self,
        path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        max_lines: int = 200,
        pattern: Optional[str] = None,
        tail: Optional[int] = None,
        pages: Optional[str] = None,
    ) -> ToolResult:
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
                return self._read_pdf(p, pages)

            content = p.read_text(encoding="utf-8")
            lines = content.splitlines()
            total_lines = len(lines)

            # Tail mode - read last N lines (like tail -n)
            if tail is not None:
                tail_start = max(0, total_lines - tail)
                lines = lines[tail_start:]
                # Format with line numbers
                numbered = []
                for i, line in enumerate(lines):
                    line_num = tail_start + i + 1
                    if len(line) > 500:
                        line = line[:500] + "..."
                    numbered.append(f"{line_num:4d} | {line}")
                output = "\n".join(numbered)
                if total_lines > tail:
                    output = f"[Showing last {tail} of {total_lines} lines]\n\n" + output
                return ToolResult(success=True, output=output, error=None)

            # Apply line range if specified
            if start_line is not None:
                start_idx = max(0, start_line - 1)
                end_idx = len(lines) if (end_line == -1 or end_line is None) else end_line
                lines = lines[start_idx:end_idx]
                line_offset = start_idx
            else:
                line_offset = 0

            # Filter by pattern if specified (like grep)
            if pattern:
                matching = []
                for i, line in enumerate(lines):
                    if pattern.lower() in line.lower():
                        line_num = i + line_offset + 1
                        matching.append(f"{line_num:4d} | {line}")
                if not matching:
                    return ToolResult(
                        success=True,
                        output=f"No lines matching '{pattern}' in {path} ({total_lines} total lines)",
                        error=None
                    )
                return ToolResult(
                    success=True,
                    output="\n".join(matching[:max_lines]),
                    error=None
                )

            # Truncate if too many lines
            truncated = False
            if len(lines) > max_lines:
                lines = lines[:max_lines]
                truncated = True

            # Add line numbers
            numbered = []
            for i, line in enumerate(lines):
                line_num = i + line_offset + 1
                # Truncate long lines
                if len(line) > 500:
                    line = line[:500] + "..."
                numbered.append(f"{line_num:4d} | {line}")

            output = "\n".join(numbered)
            if truncated:
                output += f"\n\n[Truncated: showing {max_lines}/{total_lines} lines. Use start_line/end_line or pattern to find specific content]"

            return ToolResult(
                success=True,
                output=output,
                error=None
            )

        except Exception as e:
            return ToolResult(success=False, output=None, error=str(e))

    def _read_pdf(self, path: Path, pages: Optional[str] = None) -> ToolResult:
        """Read PDF as a multimodal artifact: raw bytes + pypdf text fallback.

        Anthropic, Gemini, and OpenAI all accept PDFs natively through litellm —
        the model does its own layout-aware rendering server-side, which
        preserves figures/tables/equations that pypdf would strip. So we no
        longer pre-extract text and throw away the rest; we hand the model the
        whole PDF and keep a pypdf text fallback for log/debug and for any
        provider that can't take the raw artifact.

        Page-range guard: if the PDF is larger than ``MAX_PDF_PAGES_NO_RANGE``,
        ``pages`` must be supplied. The slice is capped at
        ``MAX_PDF_PAGES_PER_READ``. Without this guard a 12-page paper turns
        into ~30k input tokens and the next LLM turn frequently maxes out
        before emitting a tool call.

        Provider-format dispatch happens once in ``llm._format_attachments_for_provider``.
        """
        try:
            pdf_bytes = path.read_bytes()
        except Exception as e:
            return ToolResult(success=False, output=None, error=f"Failed to read PDF: {str(e)}")

        total_pages = self._pdf_page_count(pdf_bytes)

        page_range: Optional[tuple[int, int]] = None
        if pages is not None and str(pages).strip():
            try:
                page_range = self._parse_pdf_page_range(pages, total_pages)
            except ValueError as e:
                return ToolResult(success=False, output=None, error=str(e))
        elif total_pages > self.MAX_PDF_PAGES_NO_RANGE:
            return ToolResult(
                success=False,
                output=None,
                error=(
                    f"PDF has {total_pages} pages; specify a `pages` range "
                    f"(e.g., pages=\"1-{self.MAX_PDF_PAGES_NO_RANGE}\"). "
                    f"Max {self.MAX_PDF_PAGES_PER_READ} pages per read."
                ),
            )

        if page_range is not None:
            sliced = self._slice_pdf(pdf_bytes, page_range)
            if sliced is None:
                return ToolResult(
                    success=False,
                    output=None,
                    error="Failed to slice PDF to requested page range",
                )
            pdf_bytes = sliced

        text_fallback, emitted_pages = self._pdf_text_fallback(pdf_bytes)
        b64_data = base64.b64encode(pdf_bytes).decode("utf-8")
        size_kb = len(pdf_bytes) / 1024

        if page_range is not None:
            pages_str = f"pages {page_range[0] + 1}-{page_range[1]} of {total_pages}"
        elif total_pages:
            pages_str = f"{total_pages} pages"
        else:
            pages_str = "PDF"
        display_text = (
            f"[PDF: {path.name} ({pages_str}, {size_kb:.1f} KB) "
            "— handed to model as native attachment]"
        )

        # Internal canonical artifact shape — same ``type`` keys as Anthropic's
        # native multimodal blocks (image / document / audio / video). The agent
        # loop recognizes this set via ``MULTIMODAL_ARTIFACT_TYPES``; the LLM
        # provider-dispatch in ``llm._format_attachments_for_provider`` translates
        # to whichever wire format the active model accepts. To extend to
        # ``.docx`` / ``.xlsx`` / ``.wav`` / ``.mp4`` / etc., emit the same shape
        # with the matching ``type`` + ``media_type``; no other code needs changes.
        return ToolResult(
            success=True,
            output={
                "type": "document",
                "media_type": "application/pdf",
                "data": b64_data,
                "filename": path.name,
                "file_path": str(path),
                "pages": emitted_pages or (
                    page_range[1] - page_range[0] if page_range else total_pages
                ),
                "total_pages": total_pages,
                "page_range": [page_range[0] + 1, page_range[1]] if page_range else None,
                "size_kb": round(size_kb, 2),
                "text_fallback": text_fallback,
                "display_text": display_text,
            },
            error=None,
        )

    def _pdf_page_count(self, pdf_bytes: bytes) -> int:
        """Cheap page-count probe. Returns 0 if pypdf is unavailable or the
        PDF can't be opened — in that case the page-range guard is skipped
        and the existing ship-whole-PDF behavior is preserved."""
        if not PYPDF_AVAILABLE:
            return 0
        try:
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            return len(reader.pages)
        except Exception:
            return 0

    def _parse_pdf_page_range(self, pages: str, total: int) -> tuple[int, int]:
        """Parse ``"5"`` or ``"1-10"`` into a 0-indexed ``(start, end)`` tuple
        with ``end`` exclusive. Raises ``ValueError`` on bad input, out-of-bounds
        ranges, or spans over ``MAX_PDF_PAGES_PER_READ``."""
        s = str(pages).strip()
        try:
            if "-" in s:
                a, b = s.split("-", 1)
                start = int(a.strip())
                end = int(b.strip())
            else:
                start = int(s)
                end = start
        except ValueError:
            raise ValueError(
                f"Invalid `pages` value: {pages!r}. "
                "Expected \"5\" or \"1-10\"."
            )
        if start < 1 or end < start:
            raise ValueError(f"Invalid page range: {pages!r}")
        if total and end > total:
            raise ValueError(
                f"Page range {pages!r} exceeds total pages ({total})"
            )
        span = end - start + 1
        if span > self.MAX_PDF_PAGES_PER_READ:
            raise ValueError(
                f"Page range {pages!r} requests {span} pages; cap is "
                f"{self.MAX_PDF_PAGES_PER_READ} per read"
            )
        return (start - 1, end)

    def _slice_pdf(self, pdf_bytes: bytes, page_range: tuple[int, int]) -> Optional[bytes]:
        """Re-encode just the requested page range. Returns None if pypdf is
        unavailable or the slice fails."""
        if not PYPDF_AVAILABLE:
            return None
        try:
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            writer = pypdf.PdfWriter()
            for i in range(page_range[0], page_range[1]):
                writer.add_page(reader.pages[i])
            buf = io.BytesIO()
            writer.write(buf)
            return buf.getvalue()
        except Exception:
            return None

    def _pdf_text_fallback(self, pdf_bytes: bytes) -> tuple[str, int]:
        """Best-effort pypdf text extraction. Returns (text, page_count).

        Used for the provenance log, the tool-result display string, and as
        a fallback for providers that can't ingest the raw PDF.
        """
        if not PYPDF_AVAILABLE:
            return ("[pypdf not installed; raw PDF was still handed to the model]", 0)
        try:
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            text_parts = []
            for page_num, page in enumerate(reader.pages):
                try:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(f"--- Page {page_num + 1} ---\n{page_text}")
                except Exception as e:
                    text_parts.append(f"--- Page {page_num + 1} ---\n[Error extracting: {e}]")
            return ("\n\n".join(text_parts), len(reader.pages))
        except Exception as e:
            return (f"[pypdf text fallback failed: {e}]", 0)

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
