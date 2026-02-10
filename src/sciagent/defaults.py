"""
Default configuration values for the agent framework.

Single source of truth for all default settings.

Model Selection Strategy:
- SCIENTIFIC_MODEL: Best quality for scientific code (physics, simulations, numerical methods)
- CODING_MODEL: Good for general coding, debugging, research tasks
- FAST_MODEL: Quick/cheap for file exploration, extraction, simple queries

Cost hierarchy: SCIENTIFIC > CODING > FAST (roughly 10x between each tier)
"""

# Scientific computing model - Opus proven better for domain-specific code
# Use for: simulation code, numerical methods, scientific APIs (S4, GROMACS, etc.)
SCIENTIFIC_MODEL = "anthropic/claude-opus-4-5-20251101"

# General coding model - Sonnet for implementation, debugging, research
# Use for: sub-agents, general coding tasks, web research
CODING_MODEL = "anthropic/claude-sonnet-4-20250514"

# Fast model for simple tasks - Haiku for speed and cost
# Use for: file exploration, extraction, summarization, simple queries
FAST_MODEL = "anthropic/claude-3-haiku-20240307"

# Default for main agent - Opus for scientific work quality
DEFAULT_MODEL = SCIENTIFIC_MODEL

# Content limits for web fetching
WEB_FETCH_MAX_CONTENT = 100000      # Max chars to fetch (before LLM processing)
WEB_FETCH_DISPLAY_LIMIT = 16000     # Max chars to display (when no LLM processing)
