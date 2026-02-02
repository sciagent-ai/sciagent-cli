"""
Reflection tool for post-iteration self-evaluation and learning.

This tool allows agents to analyze what worked, what failed, and plan
improvements for scientific workflows. Critical for complex multi-step
research tasks where learning from mistakes is essential.
"""

from __future__ import annotations

import json
import datetime
from pathlib import Path
from typing import Dict, Any, List
from uuid import uuid4

from sciagent.base_tool import BaseTool


class ReflectTool(BaseTool):
    """Analyze task progress and learn from successes and failures."""

    name = "reflect"
    description = "Analyze what worked, what failed, and plan improvements for better task execution"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "current_task": {
                "type": "string",
                "description": "Brief description of the current task or goal"
            },
            "what_worked": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of approaches, methods, or strategies that were successful",
                "default": []
            },
            "what_failed": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of approaches, methods, or errors that didn't work",
                "default": []
            },
            "obstacles_encountered": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific challenges, errors, or blockers faced",
                "default": []
            },
            "insights_gained": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key learnings or insights discovered during the task",
                "default": []
            },
            "next_steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Planned improvements or alternative approaches to try",
                "default": []
            },
            "confidence_before": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Confidence level before starting (0.0-1.0)",
                "default": 0.5
            },
            "confidence_after": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Confidence level after reflection (0.0-1.0)",
                "default": 0.5
            },
            "domain": {
                "type": "string",
                "enum": ["materials", "systems", "simulation", "experiment", "coding", "analysis", "general"],
                "description": "Domain or area this reflection applies to",
                "default": "general"
            }
        },
        "required": ["current_task"]
    }

    def run(self, tool_input: Dict[str, Any], agent: Any = None) -> Dict[str, Any]:
        try:
            current_task = tool_input["current_task"]
            what_worked = tool_input.get("what_worked", [])
            what_failed = tool_input.get("what_failed", [])
            obstacles = tool_input.get("obstacles_encountered", [])
            insights = tool_input.get("insights_gained", [])
            next_steps = tool_input.get("next_steps", [])
            confidence_before = tool_input.get("confidence_before", 0.5)
            confidence_after = tool_input.get("confidence_after", 0.5)
            domain = tool_input.get("domain", "general")
            
            # Create reflection directory
            reflection_dir = Path(".sciagent_workspace/reflections")
            reflection_dir.mkdir(parents=True, exist_ok=True)
            
            # Create reflection entry
            reflection_id = str(uuid4())
            reflection_entry = {
                "id": reflection_id,
                "timestamp": datetime.datetime.now().isoformat(),
                "task": current_task,
                "domain": domain,
                "analysis": {
                    "what_worked": what_worked,
                    "what_failed": what_failed,
                    "obstacles_encountered": obstacles,
                    "insights_gained": insights,
                    "next_steps": next_steps
                },
                "confidence": {
                    "before": confidence_before,
                    "after": confidence_after,
                    "change": confidence_after - confidence_before
                },
                "metadata": {
                    "agent_iteration": getattr(agent, 'iteration_count', 0) if agent else 0,
                    "total_tools_used": len(getattr(agent, 'state', {}).get('last_tool_executions', [])) if agent else 0
                }
            }
            
            # Save to timestamped file
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            reflection_file = reflection_dir / f"reflection_{timestamp}_{reflection_id[:8]}.json"
            with open(reflection_file, 'w') as f:
                json.dump(reflection_entry, f, indent=2)
            
            # Append to master reflection log
            reflection_log = reflection_dir / "reflection_log.jsonl"
            with open(reflection_log, 'a') as f:
                json.dump(reflection_entry, f)
                f.write('\n')
            
            # Generate actionable recommendations
            recommendations = self._generate_recommendations(reflection_entry)
            
            # Auto-save key insights to memory if save_memory tool is available
            memory_saved = []
            if insights:
                try:
                    # Try to access memory tools through agent
                    from .save_memory import SaveMemoryTool
                    save_tool = SaveMemoryTool()
                    
                    for i, insight in enumerate(insights):
                        memory_key = f"reflection_insight_{domain}_{reflection_id[:8]}_{i}"
                        memory_result = save_tool.run({
                            "key": memory_key,
                            "content": insight,
                            "tags": ["reflection", "insight", domain, "learning"],
                            "memory_type": "insight",
                            "confidence": confidence_after
                        })
                        if memory_result.get("success"):
                            memory_saved.append(memory_key)
                except Exception as e:
                    pass  # Memory saving is optional
            
            # Format output
            confidence_change = confidence_after - confidence_before
            confidence_emoji = "📈" if confidence_change > 0 else "📉" if confidence_change < 0 else "➡️"
            
            output_lines = [
                f"🤔 **Reflection on Task: {current_task}**",
                f"🎯 **Domain:** {domain}",
                f"{confidence_emoji} **Confidence:** {confidence_before:.1%} → {confidence_after:.1%} ({confidence_change:+.1%})",
                ""
            ]
            
            if what_worked:
                output_lines.append("✅ **What Worked:**")
                output_lines.extend([f"   • {item}" for item in what_worked])
                output_lines.append("")
            
            if what_failed:
                output_lines.append("❌ **What Failed:**")
                output_lines.extend([f"   • {item}" for item in what_failed])
                output_lines.append("")
            
            if obstacles:
                output_lines.append("🚧 **Obstacles Encountered:**")
                output_lines.extend([f"   • {item}" for item in obstacles])
                output_lines.append("")
            
            if insights:
                output_lines.append("💡 **Insights Gained:**")
                output_lines.extend([f"   • {item}" for item in insights])
                output_lines.append("")
            
            if next_steps:
                output_lines.append("➡️ **Next Steps:**")
                output_lines.extend([f"   • {item}" for item in next_steps])
                output_lines.append("")
            
            if recommendations:
                output_lines.append("🎯 **AI Recommendations:**")
                output_lines.extend([f"   • {rec}" for rec in recommendations])
                output_lines.append("")
            
            if memory_saved:
                output_lines.append(f"💾 **Insights saved to memory:** {len(memory_saved)} items")
            
            return {
                "success": True,
                "output": "\n".join(output_lines),
                "reflection_id": reflection_id,
                "confidence_change": confidence_change,
                "recommendations": recommendations,
                "insights_saved_to_memory": len(memory_saved),
                "file_path": str(reflection_file)
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to complete reflection: {str(e)}"
            }
    
    def _generate_recommendations(self, reflection_entry: Dict[str, Any]) -> List[str]:
        """Generate actionable recommendations based on reflection data."""
        recommendations = []
        analysis = reflection_entry["analysis"]
        confidence = reflection_entry["confidence"]
        domain = reflection_entry["domain"]
        
        # Confidence-based recommendations
        if confidence["change"] < -0.1:
            recommendations.append("Consider breaking down the task into smaller, more manageable steps")
        elif confidence["change"] > 0.2:
            recommendations.append("Document successful approach for future similar tasks")
        
        # Failure pattern analysis
        failed_items = analysis["what_failed"]
        if len(failed_items) > 2:
            recommendations.append("High failure rate suggests need for different approach or more preparation")
        
        # Domain-specific recommendations
        if domain == "simulation":
            if any("convergence" in failure.lower() for failure in failed_items):
                recommendations.append("Review mesh quality and boundary conditions for convergence issues")
            if any("parameter" in failure.lower() for failure in failed_items):
                recommendations.append("Consider parameter sensitivity analysis before full simulation")
        
        elif domain == "materials":
            if any("property" in failure.lower() for failure in failed_items):
                recommendations.append("Validate material property databases and measurement conditions")
        
        elif domain == "experiment":
            if any("reproduc" in failure.lower() for failure in failed_items):
                recommendations.append("Document experimental conditions in more detail for reproducibility")
        
        # Learning pattern recommendations
        insights = analysis["insights_gained"]
        if len(insights) > 3:
            recommendations.append("High insight generation - consider sharing learnings with team")
        elif len(insights) == 0 and len(analysis["obstacles_encountered"]) > 0:
            recommendations.append("Reflect more deeply on obstacles to extract actionable insights")
        
        return recommendations[:5]  # Limit to 5 recommendations


def get_tool() -> BaseTool:
    return ReflectTool()