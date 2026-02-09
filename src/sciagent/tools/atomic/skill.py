"""
Skill Tool - Load and invoke specialized workflow skills.

Skills are defined in SKILL.md files and provide step-by-step
instructions for complex, domain-specific workflows.

Use this tool when:
- The task matches an available skill (sci-compute, build-service, etc.)
- You need structured guidance for a complex workflow
- The task involves specialized domain knowledge

The skill returns detailed instructions that you should follow step by step.
"""

from __future__ import annotations

from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ToolResult:
    """Result from tool execution."""
    success: bool
    output: Any
    error: str = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class SkillTool:
    """Tool for invoking specialized skill workflows."""

    name = "skill"
    description = """Load and follow a specialized workflow skill.

Use this when the task matches an available skill.
The skill provides step-by-step instructions for complex workflows.

{skill_list}

Example:
  skill(skill_name="sci-compute")
  -> Returns detailed workflow instructions to follow
"""

    parameters = {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Name of the skill to invoke"
            }
        },
        "required": ["skill_name"]
    }

    def __init__(self, loader):
        """
        Initialize the skill tool.

        Args:
            loader: SkillLoader instance with loaded skills
        """
        self.loader = loader

        # Update description with available skills
        skill_list = self.loader.get_descriptions()
        self.description = self.description.replace("{skill_list}", skill_list)

        # Update enum in parameters with available skill names
        skill_names = [s.name for s in self.loader.skills.values()]
        if skill_names:
            self.parameters = {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Name of the skill to invoke",
                        "enum": skill_names
                    }
                },
                "required": ["skill_name"]
            }

    def execute(self, skill_name: str) -> ToolResult:
        """
        Load and return skill workflow instructions.

        Args:
            skill_name: Name of the skill to load

        Returns:
            ToolResult with the skill's workflow instructions
        """
        skill = self.loader.get(skill_name)

        if not skill:
            available = [s.name for s in self.loader.skills.values()]
            return ToolResult(
                success=False,
                output=None,
                error=f"Skill '{skill_name}' not found. Available skills: {available}"
            )

        # Format the output with skill metadata and workflow
        output = f"""## Skill Loaded: {skill.name}

{skill.description}

---

**Follow this workflow:**

{skill.workflow}

---

Now execute this workflow step by step. Use the todo tool to track your progress through each phase.
"""

        return ToolResult(
            success=True,
            output=output,
            metadata={
                "skill_name": skill.name,
                "skill_path": str(skill.path)
            }
        )

    def to_schema(self) -> Dict[str, Any]:
        """Return tool schema for LLM."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


def get_tool(skills_dir: Optional[Path] = None) -> Optional[SkillTool]:
    """
    Factory function for tool discovery.

    Args:
        skills_dir: Optional path to skills directory

    Returns:
        SkillTool instance if skills exist, None otherwise
    """
    from ...skills import SkillLoader

    loader = SkillLoader(skills_dir)
    if loader.skills:
        return SkillTool(loader)
    return None
