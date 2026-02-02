"""
Memory retrieval tool for scientific workflows.

This tool allows agents to search and retrieve previously saved
insights, experimental results, and findings from persistent storage.
"""

from __future__ import annotations

import json
import glob
import re
from pathlib import Path
from typing import Dict, Any, List
from datetime import datetime

from sciagent.base_tool import BaseTool


class RecallMemoryTool(BaseTool):
    """Search and retrieve insights from persistent memory."""

    name = "recall_memory"
    description = "Search and retrieve previously saved insights, results, or findings from persistent memory"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query - can be keywords, concepts, or specific terms"
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Filter by specific tags (e.g., ['materials', 'thermal'])",
                "default": []
            },
            "memory_type": {
                "type": "string",
                "enum": ["insight", "result", "parameter", "failure", "method", "reference", "any"],
                "description": "Filter by memory type",
                "default": "any"
            },
            "min_confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Minimum confidence level for results",
                "default": 0.0
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "Maximum number of results to return",
                "default": 5
            }
        },
        "required": ["query"]
    }

    def run(self, tool_input: Dict[str, Any], agent: Any = None) -> Dict[str, Any]:
        try:
            query = tool_input["query"].lower()
            filter_tags = tool_input.get("tags", [])
            memory_type_filter = tool_input.get("memory_type", "any")
            min_confidence = tool_input.get("min_confidence", 0.0)
            max_results = tool_input.get("max_results", 5)
            
            memory_dir = Path(".sciagent_workspace/memory")
            if not memory_dir.exists():
                return {
                    "success": True,
                    "output": "📭 No memories found - memory system is empty",
                    "results": [],
                    "total_found": 0
                }
            
            # Search through all memory files
            memory_files = glob.glob(str(memory_dir / "*.json"))
            matches = []
            
            for memory_file in memory_files:
                if memory_file.endswith("memory_log.jsonl"):
                    continue  # Skip the log file
                    
                try:
                    with open(memory_file, 'r') as f:
                        memory = json.load(f)
                    
                    # Apply filters
                    if memory_type_filter != "any" and memory.get("memory_type") != memory_type_filter:
                        continue
                        
                    if memory.get("confidence", 0) < min_confidence:
                        continue
                    
                    if filter_tags:
                        memory_tags = set(memory.get("tags", []))
                        if not any(tag in memory_tags for tag in filter_tags):
                            continue
                    
                    # Calculate relevance score
                    content_lower = memory.get("content", "").lower()
                    key_lower = memory.get("key", "").lower()
                    tags_lower = " ".join(memory.get("tags", [])).lower()
                    
                    # Search in content, key, and tags
                    query_words = query.split()
                    content_matches = sum(1 for word in query_words if word in content_lower)
                    key_matches = sum(2 for word in query_words if word in key_lower)  # Key matches weighted more
                    tag_matches = sum(1.5 for word in query_words if word in tags_lower)
                    
                    total_score = content_matches + key_matches + tag_matches
                    
                    if total_score > 0:
                        # Update access count
                        memory["access_count"] = memory.get("access_count", 0) + 1
                        memory["last_accessed"] = datetime.now().isoformat()
                        
                        # Save updated memory back
                        with open(memory_file, 'w') as f:
                            json.dump(memory, f, indent=2)
                        
                        matches.append({
                            "memory": memory,
                            "relevance_score": total_score
                        })
                        
                except (json.JSONDecodeError, IOError) as e:
                    continue  # Skip corrupted files
            
            # Sort by relevance score and limit results
            matches.sort(key=lambda x: x["relevance_score"], reverse=True)
            top_matches = matches[:max_results]
            
            if not top_matches:
                return {
                    "success": True,
                    "output": f"🔍 No memories found matching '{query}' with the given filters",
                    "results": [],
                    "total_found": 0
                }
            
            # Format results
            results = []
            output_lines = [f"🧠 Found {len(top_matches)} relevant memories for '{query}':"]
            
            for i, match in enumerate(top_matches, 1):
                memory = match["memory"]
                score = match["relevance_score"]
                
                result_entry = {
                    "key": memory["key"],
                    "content": memory["content"],
                    "tags": memory.get("tags", []),
                    "memory_type": memory.get("memory_type", "unknown"),
                    "confidence": memory.get("confidence", 0),
                    "created_at": memory.get("created_at"),
                    "access_count": memory.get("access_count", 0),
                    "relevance_score": score
                }
                results.append(result_entry)
                
                # Format for display
                tags_str = ", ".join(memory.get("tags", [])) if memory.get("tags") else "no tags"
                confidence_str = f"{memory.get('confidence', 0):.1%}" if memory.get("confidence") is not None else "unknown"
                
                output_lines.append(
                    f"\n{i}. **{memory['key']}** ({memory.get('memory_type', 'unknown')}, confidence: {confidence_str})"
                    f"\n   📝 {memory['content'][:200]}{'...' if len(memory['content']) > 200 else ''}"
                    f"\n   🏷️  Tags: {tags_str}"
                    f"\n   📊 Relevance: {score:.1f}, Accessed: {memory.get('access_count', 0)} times"
                )
            
            return {
                "success": True,
                "output": "\n".join(output_lines),
                "results": results,
                "total_found": len(matches),
                "displayed": len(top_matches)
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to recall memories: {str(e)}"
            }


def get_tool() -> BaseTool:
    return RecallMemoryTool()