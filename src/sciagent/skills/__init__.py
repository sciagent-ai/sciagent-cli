"""
Skills - Loadable workflow definitions

Skills are specialized workflows defined in SKILL.md files.
Each skill has:
- YAML frontmatter with metadata (name, description, triggers)
- Markdown content with step-by-step instructions

The SkillLoader finds and parses these files, and the SkillTool
allows the agent to invoke them explicitly.
"""

from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Dict
import re

try:
    import yaml
except ImportError:
    yaml = None


@dataclass
class Skill:
    """A loadable skill definition."""
    name: str
    description: str
    triggers: List[str]  # Regex patterns for auto-matching
    workflow: str  # The actual instructions (markdown content)
    path: Path  # Source file path

    def matches(self, text: str) -> bool:
        """Check if text matches any trigger pattern."""
        for pattern in self.triggers:
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    return True
            except re.error:
                # Invalid regex, skip
                continue
        return False


class SkillLoader:
    """Loads skills from SKILL.md files."""

    def __init__(self, skills_dir: Optional[Path] = None):
        """
        Initialize the skill loader.

        Args:
            skills_dir: Directory containing skill subdirectories.
                       Each subdirectory should have a SKILL.md file.
                       Defaults to the package's skills directory.
        """
        self.skills_dir = skills_dir or self._default_skills_dir()
        self.skills: Dict[str, Skill] = {}
        self._load_all()

    def _default_skills_dir(self) -> Path:
        """Default to package's skills directory."""
        return Path(__file__).parent

    def _load_all(self):
        """Load all skills from directory."""
        if not self.skills_dir.exists():
            return

        # Look for SKILL.md in subdirectories
        for skill_dir in self.skills_dir.iterdir():
            if skill_dir.is_dir() and not skill_dir.name.startswith('.'):
                skill_file = skill_dir / "SKILL.md"
                if skill_file.exists():
                    skill = self._load_skill(skill_file)
                    if skill:
                        self.skills[skill.name] = skill

    def _load_skill(self, path: Path) -> Optional[Skill]:
        """Load a single skill from SKILL.md file."""
        content = path.read_text()

        # Parse YAML frontmatter
        if not content.startswith("---"):
            return None

        # Find end of frontmatter
        end = content.find("---", 3)
        if end == -1:
            return None

        frontmatter = content[3:end].strip()
        workflow = content[end + 3:].strip()

        # Parse YAML
        if yaml is None:
            # Fallback: simple parsing without yaml module
            meta = self._parse_simple_yaml(frontmatter)
        else:
            try:
                meta = yaml.safe_load(frontmatter)
            except yaml.YAMLError:
                return None

        if not meta:
            return None

        # Extract fields with defaults
        name = meta.get("name", path.parent.name)
        description = meta.get("description", "")
        triggers = meta.get("triggers", [])

        # Ensure triggers is a list
        if isinstance(triggers, str):
            triggers = [triggers]

        return Skill(
            name=name,
            description=description,
            triggers=triggers,
            workflow=workflow,
            path=path,
        )

    def _parse_simple_yaml(self, text: str) -> Dict:
        """Simple YAML-like parsing for name/description when yaml module unavailable."""
        result = {}
        for line in text.split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                result[key] = value
        return result

    def get(self, name: str) -> Optional[Skill]:
        """Get skill by name."""
        return self.skills.get(name)

    def list_skills(self) -> List[Dict[str, str]]:
        """List all available skills."""
        return [
            {"name": s.name, "description": s.description}
            for s in self.skills.values()
        ]

    def get_descriptions(self) -> str:
        """Get formatted skill descriptions for prompt."""
        if not self.skills:
            return "No skills available."

        lines = ["Available skills:"]
        for skill in self.skills.values():
            lines.append(f"- **{skill.name}**: {skill.description}")
        return "\n".join(lines)

    def match_skill(self, text: str) -> Optional[Skill]:
        """Find a skill that matches the given text."""
        for skill in self.skills.values():
            if skill.matches(text):
                return skill
        return None

    def reload(self):
        """Reload all skills from disk."""
        self.skills.clear()
        self._load_all()


# Convenience function
def load_skills(skills_dir: Optional[Path] = None) -> SkillLoader:
    """Load skills from the specified directory."""
    return SkillLoader(skills_dir)


__all__ = ["Skill", "SkillLoader", "load_skills"]
