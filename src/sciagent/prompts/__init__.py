"""
Prompts - Modular system prompt components

This package provides composable prompt sections that can be
loaded and combined dynamically.
"""

from .loader import load_prompt, build_system_prompt

__all__ = ["load_prompt", "build_system_prompt"]
