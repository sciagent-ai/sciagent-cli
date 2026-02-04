"""
Default configuration values for the agent framework.

Single source of truth for all default settings.
"""

# Default LLM model - change this to update the default across the entire codebase
DEFAULT_MODEL = "anthropic/claude-sonnet-4-20250514"

# Fast model for content processing (web fetch extraction, summarization)
# This model is used for processing large documents before returning to main agent
# Should be cheap and fast - Haiku is ideal
FAST_MODEL = "anthropic/claude-3-haiku-20240307"

# Content limits for web fetching
WEB_FETCH_MAX_CONTENT = 50000      # Max chars to fetch (before LLM processing)
WEB_FETCH_DISPLAY_LIMIT = 8000     # Max chars to display (when no LLM processing)
