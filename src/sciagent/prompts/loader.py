"""
Prompt Loader - Compose system prompts from modular sections

This module loads prompt sections from .md files and composes
them into a complete system prompt with dynamic content injection.
"""

from pathlib import Path
from typing import List, Optional

PROMPTS_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    """
    Load a prompt section by name.

    Args:
        name: Section name (without .md extension)

    Returns:
        Content of the prompt file, or empty string if not found
    """
    path = PROMPTS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text()
    return ""


def build_system_prompt(
    working_dir: str,
    sections: Optional[List[str]] = None,
    skill_descriptions: str = "",
    registry_path: str = "",
) -> str:
    """
    Build complete system prompt from sections.

    Args:
        working_dir: Absolute path to the working directory
        sections: List of section names to include (default: all standard sections)
        skill_descriptions: Formatted string of available skills
        registry_path: Path to services registry (for Docker services)

    Returns:
        Complete system prompt with placeholders replaced
    """
    default_sections = [
        "core",
        "delegation",
        "planning",
        "exploration",
        "verification",
        "errors",
    ]

    sections = sections or default_sections

    parts = []
    for section in sections:
        content = load_prompt(section)
        if content:
            parts.append(content)

    prompt = "\n\n".join(parts)

    # Inject dynamic content
    prompt = prompt.replace("{working_dir}", working_dir)
    prompt = prompt.replace("{skill_descriptions}", skill_descriptions)
    prompt = prompt.replace("{registry_path}", registry_path)

    return prompt
