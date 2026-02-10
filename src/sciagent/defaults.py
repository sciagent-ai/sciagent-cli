"""
Default configuration values for the agent framework.

Single source of truth for all default settings.
"""

# Default LLM model - change this to update the default across the entire codebase
DEFAULT_MODEL = "anthropic/claude-sonnet-4-20250514"
#DEFAULT_MODEL = "openai/gpt-4o"

# Fast model for content processing (web fetch extraction, summarization)
# This model is used for processing large documents before returning to main agent
# Should be cheap and fast - Haiku is ideal
#FAST_MODEL = "anthropic/claude-sonnet-4-20250514"
FAST_MODEL = "anthropic/claude-3-haiku-20240307"
#FAST_MODEL = "openai/gpt-4o"

# Content limits for web fetching
WEB_FETCH_MAX_CONTENT = 100000      # Max chars to fetch (before LLM processing)
WEB_FETCH_DISPLAY_LIMIT = 16000     # Max chars to display (when no LLM processing)
