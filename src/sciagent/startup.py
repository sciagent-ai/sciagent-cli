"""
Startup display and configuration status checking.

Provides helpful feedback to users about their configuration state
and tips for getting started.
"""
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .defaults import DEFAULT_MODEL


# API key environment variable names and their signup URLs
API_KEYS = {
    "anthropic": {
        "env": "ANTHROPIC_API_KEY",
        "url": "https://console.anthropic.com/settings/keys",
        "name": "Anthropic",
    },
    "openai": {
        "env": "OPENAI_API_KEY",
        "url": "https://platform.openai.com/api-keys",
        "name": "OpenAI",
    },
    "gemini": {
        "env": "GEMINI_API_KEY",
        "url": "https://aistudio.google.com/apikey",
        "name": "Google AI",
    },
    "brave": {
        "env": "BRAVE_SEARCH_API_KEY",
        "url": "https://brave.com/search/api/",
        "name": "Brave Search",
    },
}


def detect_provider_from_model(model: str) -> str:
    """Detect the provider from model name."""
    model_lower = model.lower()
    if "anthropic" in model_lower or "claude" in model_lower:
        return "anthropic"
    elif "gpt" in model_lower or "openai" in model_lower:
        return "openai"
    elif "gemini" in model_lower:
        return "gemini"
    return "unknown"


def check_api_key(env_var: str) -> bool:
    """Check if an API key is set."""
    value = os.getenv(env_var)
    return bool(value and value.strip())


def get_api_key_status() -> Dict[str, bool]:
    """Get the status of all known API keys."""
    return {name: check_api_key(info["env"]) for name, info in API_KEYS.items()}


def get_required_key_for_model(model: str) -> Tuple[str, Dict]:
    """Get the required API key info for a model."""
    provider = detect_provider_from_model(model)
    if provider in API_KEYS:
        return provider, API_KEYS[provider]
    return "unknown", {"env": "UNKNOWN_API_KEY", "url": "", "name": "Unknown"}


def show_startup_banner(
    model: str,
    project_dir: Path,
    interactive: bool = False,
    verbose: bool = True,
    tools_loaded: Optional[List[str]] = None,
    subagents: bool = False,
) -> None:
    """
    Show an informative startup banner with configuration status.
    """
    if not verbose:
        return

    print()
    print("╭" + "─" * 58 + "╮")
    print("│" + " SciAgent ".center(58) + "│")
    print("╰" + "─" * 58 + "╯")
    print()

    # Configuration status
    print("Configuration:")
    print(f"  • Model: {model}")
    print(f"  • Project: {project_dir}")
    if subagents:
        print(f"  • Subagents: enabled")
    print()

    # API Key status
    key_status = get_api_key_status()
    provider, required_info = get_required_key_for_model(model)
    required_is_set = key_status.get(provider, False)
    brave_is_set = key_status.get("brave", False)

    print("API Keys:")

    # Show required key first (for selected model)
    if required_is_set:
        print(f"  ✓ {required_info['env']}")
    else:
        print(f"  ✗ {required_info['env']} ← REQUIRED")
        print(f"      Get key: {required_info['url']}")

    # Show Brave Search key (important for web search quality)
    brave_info = API_KEYS["brave"]
    if brave_is_set:
        print(f"  ✓ {brave_info['env']}")
    else:
        print(f"  ○ {brave_info['env']} ← Recommended")
        print(f"      Without this, web search falls back to DuckDuckGo (less reliable)")
        print(f"      Get free key: {brave_info['url']}")

    print()

    # Show warnings if required key is missing
    if not required_is_set:
        print("─" * 60)
        print("⚠️  Required API key not set!")
        print()
        print("  Quick setup:")
        print(f"    export {required_info['env']}='your-api-key'")
        print()
        print(f"  Get your key at: {required_info['url']}")
        print()
        print("  Add to ~/.bashrc or ~/.zshrc for persistence:")
        print(f"    echo 'export {required_info['env']}=\"your-key\"' >> ~/.zshrc")
        print("─" * 60)
        print()

    # Tips section for interactive mode
    if interactive:
        print("Interactive Commands:")
        print("  • exit     - Quit the session")
        print("  • status   - Show session stats")
        print("  • clear    - Clear conversation context")
        print()

    # Tools info
    if tools_loaded:
        tool_count = len(tools_loaded)
        print(f"Tools: {tool_count} available")
        preview = tools_loaded[:6]
        extra = f" (+{tool_count - 6} more)" if tool_count > 6 else ""
        print(f"  {', '.join(preview)}{extra}")
        print()


def show_quick_help() -> None:
    """Show quick help for command line usage."""
    print("Quick Start:")
    print("  sciagent -p ~/my-project 'your task'    Run a task")
    print("  sciagent -p ~/my-project -i             Interactive mode")
    print("  sciagent --help                         All options")
    print()


def show_getting_started() -> None:
    """Show detailed getting started guide."""
    print()
    print("Getting Started")
    print("═" * 60)
    print()
    print("1. Set up an API key (choose your preferred provider):")
    print()
    print("   # Anthropic (Claude models)")
    print(f"   export ANTHROPIC_API_KEY='sk-ant-...'")
    print(f"     → {API_KEYS['anthropic']['url']}")
    print()
    print("   # OpenAI (GPT models)")
    print(f"   export OPENAI_API_KEY='sk-...'")
    print(f"     → {API_KEYS['openai']['url']}")
    print()
    print("   # Google AI (Gemini models)")
    print(f"   export GEMINI_API_KEY='...'")
    print(f"     → {API_KEYS['gemini']['url']}")
    print()
    print("   Tip: SciAgent uses LiteLLM - see https://docs.litellm.ai/docs/providers")
    print("        for 100+ supported providers and model formats.")
    print()
    print("2. (Recommended) Set up Brave Search for better web search:")
    print(f"   export BRAVE_SEARCH_API_KEY='BSA...'")
    print(f"     → {API_KEYS['brave']['url']} (free tier available)")
    print()
    print("3. Create a project directory:")
    print("   mkdir ~/my-project && cd ~/my-project")
    print()
    print("4. Run sciagent:")
    print("   sciagent 'Create a Python script that fetches weather data'")
    print()
    print("5. Or use interactive mode for multi-turn conversations:")
    print("   sciagent --interactive")
    print()
    print("═" * 60)
    print()


def check_configuration_ready(model: str) -> Tuple[bool, List[str]]:
    """
    Check if configuration is ready to run.

    Returns:
        Tuple of (is_ready, list of issues)
    """
    issues = []

    # Check required API key
    provider, info = get_required_key_for_model(model)
    if not check_api_key(info["env"]):
        issues.append(f"Missing {info['env']} - get key at {info['url']}")

    return len(issues) == 0, issues


def check_optional_keys() -> List[str]:
    """Return list of recommendations for missing optional keys."""
    recommendations = []

    if not check_api_key(API_KEYS["brave"]["env"]):
        recommendations.append(
            f"Set {API_KEYS['brave']['env']} for better web search "
            f"(currently using DuckDuckGo fallback)"
        )

    return recommendations
