"""
Memory persistence tool for scientific workflows.

This tool allows agents to save insights, experimental results, 
and findings to persistent storage for later recall across sessions.
"""

from __future__ import annotations

import json
import datetime
import os
from pathlib import Path
from typing import Dict, Any, List
from uuid import uuid4

from sciagent.base_tool import BaseTool


class SaveMemoryTool(BaseTool):
    """Save insights and findings to persistent memory."""

    name = "save_memory"
    description = "Save important insights, results, or findings to persistent memory for future recall"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "key": {
                "type": "string", 
                "description": "Unique identifier for this memory (e.g., 'polymer_degradation_150C')"
            },
            "content": {
                "type": "string",
                "description": "The insight, result, or finding to remember"
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tags for categorization (e.g., ['materials', 'thermal', 'failure'])",
                "default": []
            },
            "memory_type": {
                "type": "string",
                "enum": ["insight", "result", "parameter", "failure", "method", "reference"],
                "description": "Type of memory being saved",
                "default": "insight"
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Confidence level in this information (0.0-1.0)",
                "default": 0.8
            }
        },
        "required": ["key", "content"]
    }

    def run(self, tool_input: Dict[str, Any], agent: Any = None) -> Dict[str, Any]:
        try:
            key = tool_input["key"]
            content = tool_input["content"]
            tags = tool_input.get("tags", [])
            memory_type = tool_input.get("memory_type", "insight")
            confidence = tool_input.get("confidence", 0.8)
            
            # Create memory directory if it doesn't exist
            memory_dir = Path(".sciagent_workspace/memory")
            memory_dir.mkdir(parents=True, exist_ok=True)
            
            # Create memory entry
            memory_entry = {
                "id": str(uuid4()),
                "key": key,
                "content": content,
                "tags": tags,
                "memory_type": memory_type,
                "confidence": confidence,
                "created_at": datetime.datetime.now().isoformat(),
                "updated_at": datetime.datetime.now().isoformat(),
                "access_count": 0
            }
            
            # Save to individual file for easy access
            memory_file = memory_dir / f"{key.replace('/', '_').replace(' ', '_')}.json"
            with open(memory_file, 'w') as f:
                json.dump(memory_entry, f, indent=2)
            
            # Also append to master memory log
            memory_log = memory_dir / "memory_log.jsonl"
            with open(memory_log, 'a') as f:
                json.dump(memory_entry, f)
                f.write('\n')
            
            return {
                "success": True,
                "output": f"💾 Saved memory: '{key}' with tags {tags}",
                "memory_id": memory_entry["id"],
                "file_path": str(memory_file),
                "tags_saved": len(tags)
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to save memory: {str(e)}"
            }


def get_tool() -> BaseTool:
    return SaveMemoryTool()