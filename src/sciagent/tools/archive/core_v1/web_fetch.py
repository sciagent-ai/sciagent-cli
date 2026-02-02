"""
Web content fetch tool.

Fetches raw text from a URL and returns a preview along with
metadata such as content length and HTTP status code. This tool
does not perform any analysis itself but accepts a prompt
parameter to indicate what the caller intends to analyse. The
agent may use this information when formulating subsequent
requests.
"""

from __future__ import annotations

from typing import Dict, Any, Optional
import requests

from sciagent.base_tool import BaseTool


class WebFetchTool(BaseTool):
    """Fetch and analyze web content for documentation and examples."""

    name = "web_fetch"
    description = "Fetch and analyze web content for documentation and examples"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "prompt": {"type": "string", "description": "What to analyze in the content"},
        },
        "required": ["url", "prompt"],
    }

    def run(self, tool_input: Dict[str, Any], agent: Optional[Any] = None) -> Dict[str, Any]:
        try:
            url = tool_input.get("url", "")
            prompt = tool_input.get("prompt", "")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            }
            response = requests.get(url, timeout=15, headers=headers)
            response.raise_for_status()
            content = response.text[:8000]
            output = (
                f"📄 Fetched from {url} ({len(content)} chars)\n\n"
                f"Content preview:\n{content[:1000]}"
                + ("..." if len(content) > 1000 else "")
                + f"\n\nAnalysis prompt: {prompt}"
            )
            return {
                "success": True,
                "output": output,
                "url": url,
                "content_length": len(content),
                "status_code": response.status_code,
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to fetch {tool_input.get('url', '')}: {str(e)}"}


def get_tool() -> BaseTool:
    return WebFetchTool()