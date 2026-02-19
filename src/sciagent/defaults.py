"""
Default configuration values for the agent framework.

Single source of truth for all default settings.

Model Selection Strategy:
- SCIENTIFIC_MODEL: Best quality for scientific code (physics, simulations, numerical methods)
- VISION_MODEL: Image and multimodal analysis (plots, microscopy, diagrams)
- CODING_MODEL: Good for general coding, debugging, research tasks
- FAST_MODEL: Quick/cheap for file exploration, extraction, simple queries

Cost hierarchy: SCIENTIFIC > VISION > CODING > FAST (roughly 10x between each tier)

=============================================================================
ALTERNATIVE MODELS BY PROVIDER
=============================================================================

IMPORTANT: Only Anthropic models are tested with SciAgent. Alternatives listed
below are based on comparable capabilities but have NOT been validated. Your
mileage may vary. Report issues at github.com/sciagent/sciagent-cli.

LiteLLM format: provider/model-name (e.g., openai/gpt-4.1, gemini/gemini-3-pro-preview)

-----------------------------------------------------------------------------
SCIENTIFIC TIER (Best quality, complex reasoning)
-----------------------------------------------------------------------------
Anthropic:    anthropic/claude-opus-4-5-20251101      [DEFAULT - TESTED]
OpenAI:       openai/gpt-4.1                          [untested]
OpenAI:       openai/o3                               [untested - reasoning model]
OpenAI:       openai/o3-pro                           [untested - max reasoning]
Google:       gemini/gemini-3-pro-preview             [untested]
Google:       gemini/gemini-2.5-pro                   [untested - thinking model]
xAI:          xai/grok-4-1-fast-reasoning             [untested]
xAI:          xai/grok-4-0709                         [untested]
DeepSeek:     deepseek/deepseek-reasoner              [untested - V3.2 thinking]

-----------------------------------------------------------------------------
VISION TIER (Multimodal/image analysis)
-----------------------------------------------------------------------------
Anthropic:    anthropic/claude-opus-4-5-20251101      [DEFAULT - TESTED]
OpenAI:       openai/gpt-4.1                          [untested - supports vision]
OpenAI:       openai/o3                               [untested - visual reasoning]
Google:       gemini/gemini-3-pro-preview             [untested - native multimodal]
Google:       gemini/gemini-3-pro-image-preview       [untested - image specialist]
xAI:          xai/grok-4-1-fast-reasoning             [untested - text/image input]
xAI:          xai/grok-2-vision-1212                  [untested - vision model]

Open-Source Vision Models (via Together AI / self-hosted):
  together_ai/Qwen/Qwen2.5-VL-72B-Instruct            [untested - Apache 2.0]
  together_ai/meta-llama/Llama-3.2-90B-Vision-Instruct [untested]
  Gemma 3 (4B/12B/27B)                                [untested - via Ollama/vLLM]
  DeepSeek-VL (1.3B/4.5B)                             [untested - MoE, efficient]
  InternVL3                                           [untested - industrial/3D]

-----------------------------------------------------------------------------
CODING TIER (Implementation, debugging, research)
-----------------------------------------------------------------------------
Anthropic:    anthropic/claude-sonnet-4-20250514      [DEFAULT - TESTED]
OpenAI:       openai/gpt-4.1-mini                     [untested]
OpenAI:       openai/o4-mini                          [untested - fast reasoning]
Google:       gemini/gemini-3-flash-preview           [untested]
Google:       gemini/gemini-2.5-flash                 [untested]
xAI:          xai/grok-4-1-fast-non-reasoning         [untested]
xAI:          xai/grok-code-fast-1                    [untested - code specialist]
DeepSeek:     deepseek/deepseek-chat                  [untested - V3.2 non-thinking]

Open-Source Coding Models (via Together AI / self-hosted):
  together_ai/Qwen/Qwen3-235B-A22B-Instruct           [untested - Apache 2.0]
  together_ai/deepseek-ai/DeepSeek-V3                 [untested - 671B MoE]
  together_ai/meta-llama/Llama-3.3-70B-Instruct       [untested]

-----------------------------------------------------------------------------
FAST TIER (Speed/cost optimized)
-----------------------------------------------------------------------------
Anthropic:    anthropic/claude-3-haiku-20240307       [DEFAULT - TESTED]
OpenAI:       openai/gpt-4.1-nano                     [untested]
OpenAI:       openai/o4-mini                          [untested]
Google:       gemini/gemini-2.5-flash-lite            [untested]
xAI:          xai/grok-3-mini                         [untested]

Open-Source Fast Models (via Together AI / Groq / self-hosted):
  groq/llama-3.3-70b-versatile                        [untested - Groq is fast]
  together_ai/meta-llama/Llama-3.2-3B-Instruct        [untested - very small]
  together_ai/Qwen/Qwen2.5-7B-Instruct                [untested]

=============================================================================
"""

# =============================================================================
# ACTIVE MODEL CONFIGURATION
# =============================================================================

# Scientific computing model - Opus proven better for domain-specific code
# Use for: simulation code, numerical methods, scientific APIs (S4, GROMACS, etc.)
#SCIENTIFIC_MODEL = "anthropic/claude-opus-4-5-20251101"
SCIENTIFIC_MODEL = "anthropic/claude-sonnet-4-20250514"
#SCIENTIFIC_MODEL = "xai/grok-4-1-fast-reasoning"

# General coding model - Sonnet for implementation, debugging, research
# Use for: sub-agents, general coding tasks, web research
CODING_MODEL = "anthropic/claude-sonnet-4-20250514"
#CODING_MODEL = "xai/grok-code-fast-1"

# Fast model for simple tasks - Haiku for speed and cost
# Use for: file exploration, extraction, summarization, simple queries
FAST_MODEL = "anthropic/claude-3-haiku-20240307"
#FAST_MODEL = "xai/grok-3-mini"

# Vision/Multimodal model for image analysis
# Use for: scientific plots, microscopy, diagrams, data visualization analysis
VISION_MODEL = "anthropic/claude-opus-4-5-20251101"

# Verification model - Used by the independent verifier subagent
# This model has FRESH CONTEXT (no conversation history) and acts as a skeptical auditor.
# User can change this to a different model for provider/bias and cost/quality tradeoffs.
VERIFICATION_MODEL = "anthropic/claude-sonnet-4-20250514"

# Default for main agent - Opus for scientific work quality
DEFAULT_MODEL = SCIENTIFIC_MODEL

# =============================================================================
# CONTENT LIMITS
# =============================================================================

# Content limits for web fetching
WEB_FETCH_MAX_CONTENT = 100000      # Max chars to fetch (before LLM processing)
WEB_FETCH_DISPLAY_LIMIT = 16000     # Max chars to display (when no LLM processing)
