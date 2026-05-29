"""
LLM Interface - Model-agnostic LLM calls via litellm
"""
# Suppress pydantic serialization warnings BEFORE any imports
# These warnings occur when litellm's pydantic models serialize responses
# and fields don't match expected schema (e.g., thinking_blocks for Claude)
import warnings
import os
import time

# Method 1: Comprehensive warning filters
# Filter by message content (most reliable for runtime warnings)
warnings.filterwarnings("ignore", message=".*Pydantic serializer warnings.*")
warnings.filterwarnings("ignore", message=".*PydanticSerializationUnexpectedValue.*")
warnings.filterwarnings("ignore", message=".*Expected.*fields but got.*")
warnings.filterwarnings("ignore", message=".*serialized value may not be as expected.*")

# Filter by category and module
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic.main")
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic.*")

# Method 2: Monkey-patch pydantic's warning mechanism (backup)
# This runs after pydantic loads but before serialization
def _suppress_pydantic_serializer_warnings():
    """Suppress pydantic's internal serializer warnings at the source."""
    try:
        import pydantic
        if hasattr(pydantic, 'warnings'):
            pydantic.warnings.filterwarnings = lambda *args, **kwargs: None
    except Exception:
        pass

import json
from functools import lru_cache
from typing import List, Dict, Any, Optional, Generator, Union
from dataclasses import dataclass, field

from .defaults import DEFAULT_MODEL

try:
    import litellm
    from litellm import completion
    from litellm.caching import Cache
    LITELLM_AVAILABLE = True

    # Suppress LiteLLM's verbose INFO logging (e.g., "LiteLLM completion() model=...")
    import logging
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)

    # Apply pydantic warning suppression after litellm loads
    _suppress_pydantic_serializer_warnings()

    # LiteLLM's built-in response cache is intentionally DISABLED.
    #
    # Disabled 2026-05-29 after the DBAASP cross-provider smoke surfaced a
    # litellm internal bug on the OpenAI path:
    #     Cache._set_preset_cache_key_in_kwargs() got multiple values for
    #     keyword argument 'preset_cache_key'
    # The error spammed once per OpenAI call but was non-fatal — completions
    # still returned correctly, only the cache write failed. Sciagent gets
    # negligible benefit from response-level caching within a single session
    # (identical prompts don't typically repeat back-to-back), so disabling
    # is cleaner than working around the upstream bug.
    #
    # Prompt-level caching (Anthropic cache_control markers + OpenAI/Gemini
    # implicit cache) is separate and still active — see
    # _format_messages_with_prompt_caching and llm_profiles.py:_OVERLAY.
    litellm.cache = None
    litellm.enable_cache = False

except ImportError:
    LITELLM_AVAILABLE = False
    print("Warning: litellm not installed. Run: pip install litellm")


@lru_cache(maxsize=128)
def _resolve_provider(model: str) -> str:
    """Return the canonical litellm provider id for ``model``.

    Wraps ``litellm.get_llm_provider`` which already handles ``provider/model``
    parsing, router aliases, and api-base inference. Falls back to ``"unknown"``
    so callers can stay neutral (skip cache_control, skip temp clamp, etc.)
    when the id doesn't resolve — e.g. a bare model name or a custom router
    alias litellm hasn't been told about.
    """
    if not LITELLM_AVAILABLE or not model:
        return "unknown"
    try:
        _, provider, _, _ = litellm.get_llm_provider(model)
        return provider or "unknown"
    except Exception:
        return "unknown"


# Providers that accept Anthropic-shape ``cache_control: ephemeral`` markers
# on input messages. Anthropic only.
#
# Gemini was tried (litellm issue #4284 documents the translation), but
# Google's `cachedContents` API rejects calls that ALSO carry
# `system_instruction`, `tools`, or `tool_config` in the same request —
# they have to live INSIDE the cached content. sciagent's tool surface is
# dynamic per call, so any sciagent run hits the 400:
#     "CachedContent can not be used with GenerateContent request setting
#      system_instruction, tools or tool_config."
# Implicit Gemini cache (auto on 2.5+) still yields the 90% discount on
# repeated prefixes, so we lose nothing material by skipping markers.
# OpenAI auto-caches without client markers; xAI exposes no cache surface
# today — both fall through unchanged.
_CACHE_CONTROL_PROVIDERS = frozenset({"anthropic"})


# Providers that report cached input tokens *separately* from prompt_tokens.
# For these, total input = prompt_tokens + cache_read_tokens. Everywhere
# else the cache hit is already folded into prompt_tokens (OpenAI, Gemini,
# vLLM/OpenAI-compat). The bench's cost rollup uses ``cache_hit_in_input``
# to apply the right derivation per-row.
_CACHE_SEPARATE_FROM_INPUT_PROVIDERS = frozenset({"anthropic", "bedrock", "vertex_ai"})


def _extract_cache_metrics(response: Any, provider: str) -> Dict[str, Any]:
    """Normalize cache-hit reporting across providers.

    Returns a three-key dict — ``cache_read_tokens``, ``cache_write_tokens``,
    ``cache_hit_in_input`` — populated from whichever shape the active
    provider returns:

      - Anthropic (direct / Bedrock / Vertex AI fronting Claude):
        ``usage.cache_read_input_tokens`` and
        ``usage.cache_creation_input_tokens`` are reported *separately*
        from ``usage.prompt_tokens`` → ``cache_hit_in_input=False``.
      - OpenAI: ``usage.prompt_tokens_details.cached_tokens`` is folded
        into ``usage.prompt_tokens`` → ``cache_hit_in_input=True``.
      - Gemini via OpenAI-compat: same shape as OpenAI.
      - Gemini direct: ``usageMetadata.cachedContentTokenCount`` folded
        into the total input → ``cache_hit_in_input=True``.
      - xAI: no cache surface today; returns zeros.
      - Unknown providers / missing fields: zeros; never raises.

    Downstream consumers (provenance v2, cost rollup) branch on
    ``cache_hit_in_input``, not on provider id.
    """
    zero = {"cache_read_tokens": 0, "cache_write_tokens": 0, "cache_hit_in_input": False}
    usage = getattr(response, "usage", None)
    if usage is None:
        return zero

    # Anthropic-shape fields (also surfaced when Bedrock/Vertex AI fronts
    # an Anthropic model). Reported separately from prompt_tokens.
    if provider in _CACHE_SEPARATE_FROM_INPUT_PROVIDERS:
        read = getattr(usage, "cache_read_input_tokens", 0) or 0
        write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        if read or write:
            return {
                "cache_read_tokens": int(read),
                "cache_write_tokens": int(write),
                "cache_hit_in_input": False,
            }
        # Vertex-AI fronting Gemini will not surface those fields; fall through.

    # OpenAI / Gemini-OpenAI-compat / vLLM: cached_tokens under
    # prompt_tokens_details, already inside prompt_tokens.
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        read = getattr(details, "cached_tokens", 0) or 0
        if read:
            return {
                "cache_read_tokens": int(read),
                "cache_write_tokens": 0,
                "cache_hit_in_input": True,
            }

    # Gemini direct API: cachedContentTokenCount on usageMetadata (camelCase
    # from the wire; snake_case alias guarded for SDK variation).
    meta = (
        getattr(response, "usageMetadata", None)
        or getattr(response, "usage_metadata", None)
    )
    if meta is not None:
        read = (
            getattr(meta, "cachedContentTokenCount", 0)
            or getattr(meta, "cached_content_token_count", 0)
            or 0
        )
        if read:
            return {
                "cache_read_tokens": int(read),
                "cache_write_tokens": 0,
                "cache_hit_in_input": True,
            }

    return zero


@dataclass
class Message:
    """
    Represents a message in the conversation.

    Supports multimodal content - can be a string or a list of content blocks
    for images and text combined.

    Content block format for images:
    [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}},
        {"type": "text", "text": "Analyze this image..."}
    ]
    """
    role: str  # "system", "user", "assistant", "tool"
    content: Union[str, List[Dict[str, Any]]]  # Text or multimodal content blocks
    tool_calls: Optional[List[Dict]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None  # For tool messages

    def to_dict(self) -> Dict:
        msg = {"role": self.role, "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        if self.name:
            msg["name"] = self.name
        return msg

    @classmethod
    def from_dict(cls, d: Dict) -> "Message":
        return cls(
            role=d["role"],
            content=d.get("content", ""),
            tool_calls=d.get("tool_calls"),
            tool_call_id=d.get("tool_call_id"),
            name=d.get("name")
        )

    @property
    def has_images(self) -> bool:
        """Check if this message contains image content."""
        if isinstance(self.content, list):
            return any(
                block.get("type") == "image"
                for block in self.content
                if isinstance(block, dict)
            )
        return False

    @staticmethod
    def create_multimodal(role: str, text: str, images: List[Dict[str, Any]]) -> "Message":
        """
        Create a multimodal message with text and images.

        Args:
            role: Message role (user, assistant, etc.)
            text: Text content
            images: List of image dicts with keys: media_type, data (base64)

        Returns:
            Message with multimodal content blocks
        """
        content_blocks = []

        # Add images first
        for img in images:
            content_blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img["media_type"],
                    "data": img["data"]
                }
            })

        # Add text
        if text:
            content_blocks.append({
                "type": "text",
                "text": text
            })

        return Message(role=role, content=content_blocks)


@dataclass
class ToolCall:
    """Represents a tool call from the LLM"""
    id: str
    name: str
    arguments: Dict[str, Any]
    
    @classmethod
    def from_response(cls, tool_call: Dict) -> "ToolCall":
        """Parse tool call from LLM response"""
        args = tool_call.get("function", {}).get("arguments", "{}")
        if isinstance(args, str):
            args = json.loads(args)
        return cls(
            id=tool_call.get("id", ""),
            name=tool_call.get("function", {}).get("name", ""),
            arguments=args
        )


@dataclass
class LLMResponse:
    """Structured response from LLM.

    Cache metrics carry three normalized scalars on top of the legacy
    free-form ``cache_info`` dict; downstream code (provenance log, cost
    rollup, bench Pareto plot) reads the normalized fields and does not
    branch on provider. See L2 in DESIGN_LLM_PORTABILITY.md.
    """
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: Dict[str, int] = field(default_factory=dict)
    cache_info: Dict[str, Any] = field(default_factory=dict)  # raw per-provider snapshot
    reasoning_content: Optional[str] = None  # Extended thinking/reasoning from model

    # L2 normalized cache fields. Same shape regardless of provider:
    #   - cache_read_tokens:  cached input tokens served this turn.
    #   - cache_write_tokens: tokens added to cache this turn (Anthropic).
    #   - cache_hit_in_input: True iff cache_read_tokens is already counted
    #                         inside usage.prompt_tokens (OpenAI, Gemini);
    #                         False when reported separately (Anthropic).
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_hit_in_input: bool = False

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def has_reasoning(self) -> bool:
        """Check if response includes extended thinking/reasoning"""
        return self.reasoning_content is not None and len(self.reasoning_content) > 0

    @property
    def cache_hit(self) -> bool:
        """True iff any cached input tokens were served this turn."""
        return self.cache_read_tokens > 0

    @property
    def tokens_cached(self) -> int:
        """Number of tokens read from cache (alias for cache_read_tokens)."""
        return self.cache_read_tokens

    @property
    def tokens_written_to_cache(self) -> int:
        """Number of tokens written to cache (alias for cache_write_tokens)."""
        return self.cache_write_tokens

    # Deprecated Anthropic-named aliases. Kept for one release so bench
    # code from before L2 still parses provenance v2 logs. Remove after
    # bench v1.1 ships.
    @property
    def cache_read_input_tokens(self) -> int:
        return self.cache_read_tokens

    @property
    def cache_creation_input_tokens(self) -> int:
        return self.cache_write_tokens


class LLMClient:
    """
    Model-agnostic LLM client using litellm
    
    Supports: OpenAI, Anthropic, Google, Mistral, local models, etc.
    """
    
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        temperature: float = 0.0,
        max_tokens: int = 16384,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        reasoning_effort: Optional[str] = None,  # "low", "medium", "high" or None
        max_retries: int = 3,  # Max retries for rate limit errors
        retry_base_delay: float = 2.0,  # Base delay for exponential backoff (seconds)
    ):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort  # Extended thinking (Claude, Gemini, OpenAI o-series)
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        # Per-call usage snapshot from the most recent litellm.completion call.
        # H3 (schema v2): callers — typically AgentLoop emitting a tool_result
        # for a tool that wrapped this LLM call — read this dict to copy
        # cost_usd / tokens_in / tokens_out / model into the provenance log.
        # Values come straight from litellm's response (usage + _hidden_params);
        # provider-specific shaping is litellm's job, not ours.
        self._last_usage: Dict[str, Any] = {
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
            "model": None,
        }

        # Set API key if provided. Provider id comes from litellm's resolver
        # (L1) so router aliases and bedrock/vertex_ai-fronted models route
        # to the right env var instead of accidentally hitting the Anthropic
        # branch via substring match.
        if api_key:
            env_for_provider = {
                "anthropic": "ANTHROPIC_API_KEY",
                "openai": "OPENAI_API_KEY",
                "gemini": "GEMINI_API_KEY",
                "vertex_ai": "GEMINI_API_KEY",
                "xai": "XAI_API_KEY",
            }
            key_var = env_for_provider.get(_resolve_provider(model))
            if key_var:
                os.environ[key_var] = api_key

        if base_url:
            self.base_url = base_url
        else:
            self.base_url = None

        # Configure litellm
        if LITELLM_AVAILABLE:
            litellm.drop_params = True  # Ignore unsupported params (safe for reasoning_effort)
            
    def _provider(self) -> str:
        """Canonical litellm provider id for the active model.

        Delegates to the module-level ``_resolve_provider`` (litellm-backed,
        lru-cached) so a single source of truth handles ``provider/model``
        parsing, router aliases, and api-base inference. Returns ``"unknown"``
        when litellm can't classify the id — callers treat that as the
        provider-neutral path.
        """
        return _resolve_provider(self.model)

    def _reasoning_call_kwargs(self) -> Dict[str, Any]:
        """Return the kwargs delta to apply when reasoning is requested.

        Returns ``{}`` when reasoning is off. Otherwise sends
        ``reasoning_effort`` straight through and lets litellm sort out
        per-provider quirks:

          - **Anthropic** with extended thinking requires ``temperature=1``
            (the API rejects anything else); litellm doesn't rewrite the
            temperature for us, so we clamp here.
          - **OpenAI o-series** rejects ``temperature``/``top_p``; litellm's
            ``drop_params=True`` strips them at the wire.
          - **Gemini 2.5/3** translates ``reasoning_effort`` to
            ``thinking_budget`` natively.
          - **xAI Grok 4 (non-4.3)** does not accept ``reasoning_effort``;
            litellm PR #16265 (March 2026) drops it per-model.

        We do not enumerate Grok variants here on purpose — that would
        duplicate litellm's per-model knowledge and bit-rot the moment xAI
        ships a new variant. The litellm-acceptance unit tests catch drift
        if any provider regresses on its drop_params handling.
        """
        if not self.reasoning_effort:
            return {}
        kwargs: Dict[str, Any] = {"reasoning_effort": self.reasoning_effort}
        if self._provider() == "anthropic":
            kwargs["temperature"] = 1
        return kwargs

    def _call_with_retry(self, call_kwargs: Dict[str, Any], is_stream: bool = False):
        """
        Execute LLM completion with retry logic for transient errors.

        Retries (with exponential backoff: ``base_delay * 2 ** attempt``):
          - **Rate limits** — RateLimitError / 429 (provider-agnostic).
          - **Transient server / connection failures** — InternalServerError
            (5xx), APIConnectionError, ServiceUnavailableError, Timeout, and
            "server disconnected" / "connection reset" patterns. These are
            provider-side hiccups that frequently succeed on a single retry.

        Provider-specific branching is avoided: we identify retryable errors
        by litellm's exception class names (which are vendor-neutral) and
        common substrings, never by which provider was called.
        """
        import re

        # Independent budgets: rate-limit retries (configurable) +
        # transient-error retries (capped at 1 — a hard outage should fail
        # fast). Each error class only consumes its own budget.
        transient_max_retries = 1
        transient_attempts = 0
        rate_limit_attempts = 0

        last_error = None
        while True:
            try:
                return completion(**call_kwargs)
            except Exception as e:
                error_str = str(e)
                error_type = type(e).__name__
                error_lc = error_str.lower()

                is_rate_limit = (
                    "RateLimitError" in error_type
                    or "rate_limit" in error_lc
                    or "rate limit" in error_lc
                    or "429" in error_str
                )

                # Transient server/connection failures. litellm normalizes
                # these across providers via its exception hierarchy:
                #   InternalServerError, APIConnectionError, Timeout,
                #   ServiceUnavailableError. Falling back to substring
                #   matching covers wrappers/adapters that don't surface the
                #   class cleanly (e.g., "AnthropicException - Server
                #   disconnected without sending a response").
                is_transient = (
                    "InternalServerError" in error_type
                    or "APIConnectionError" in error_type
                    or "ServiceUnavailableError" in error_type
                    or "Timeout" in error_type
                    or "server disconnected" in error_lc
                    or "connection reset" in error_lc
                    or "connection aborted" in error_lc
                    or " 502" in error_str or " 503" in error_str
                    or " 504" in error_str or " 520" in error_str
                )

                if not is_rate_limit and not is_transient:
                    raise

                last_error = e

                if is_transient and not is_rate_limit:
                    if transient_attempts >= transient_max_retries:
                        raise
                    transient_attempts += 1
                    wait_time = self.retry_base_delay * (2 ** (transient_attempts - 1))
                    print(
                        f"⚠️  Transient LLM error ({error_type}). Retrying "
                        f"in {wait_time:.1f}s ({transient_attempts}/"
                        f"{transient_max_retries})..."
                    )
                    time.sleep(wait_time)
                    continue

                # Rate-limit branch.
                if rate_limit_attempts >= self.max_retries:
                    raise

                wait_time = self.retry_base_delay * (2 ** rate_limit_attempts)
                match = re.search(r"try again in ([\d.]+)s", error_str)
                if match:
                    suggested_wait = float(match.group(1))
                    wait_time = max(wait_time, suggested_wait + 0.5)

                rate_limit_attempts += 1
                print(
                    f"⏳ Rate limit hit. Waiting {wait_time:.1f}s before "
                    f"retry ({rate_limit_attempts}/{self.max_retries})..."
                )
                time.sleep(wait_time)

    def _capture_last_usage(self, response: Any, call_kwargs: Dict[str, Any]) -> None:
        """Stash per-call tokens / cost / model on ``self._last_usage``.

        litellm exposes prompt/completion tokens on ``response.usage`` and
        the computed dollar cost on ``response._hidden_params["response_cost"]``
        for providers it supports. Missing fields stay ``None`` — we do NOT
        recompute cost from static token × price tables here. Provider
        branching belongs in litellm, not in sciagent.
        """
        hidden_params = getattr(response, "_hidden_params", {}) or {}
        resp_usage = getattr(response, "usage", None)
        self._last_usage = {
            "tokens_in": getattr(resp_usage, "prompt_tokens", None) if resp_usage else None,
            "tokens_out": getattr(resp_usage, "completion_tokens", None) if resp_usage else None,
            "cost_usd": hidden_params.get("response_cost"),
            "model": call_kwargs.get("model"),
        }

    def _format_images_for_provider(self, messages: List[Dict]) -> List[Dict]:
        """
        Convert image content blocks to OpenAI format for litellm.

        IMPORTANT: litellm expects OpenAI-style multimodal format for ALL providers.
        litellm then handles conversion to provider-specific formats internally.

        Our internal format (Anthropic-style):
            {"type": "image", "source": {"type": "base64", "media_type": "...", "data": "..."}}

        litellm expected format (OpenAI-style):
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
        """
        formatted = []
        for msg in messages:
            msg_copy = msg.copy()
            content = msg_copy.get("content")

            if isinstance(content, list):
                new_content = []
                for block in content:
                    if not isinstance(block, dict):
                        continue

                    block_type = block.get("type")

                    if block_type == "image":
                        # Convert Anthropic-style image format to OpenAI format for litellm
                        source = block.get("source", {})
                        media_type = source.get("media_type", "image/png")
                        data = source.get("data", "")
                        if data:  # Only add if there's actual data
                            new_content.append({
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{media_type};base64,{data}"
                                }
                            })
                    elif block_type == "text":
                        # Keep text blocks, but strip any Anthropic-specific fields
                        new_content.append({
                            "type": "text",
                            "text": block.get("text", "")
                        })
                    elif block_type == "image_url":
                        # Already in OpenAI format, pass through
                        new_content.append(block)
                    else:
                        # Unknown block type, pass through
                        new_content.append(block)

                # Only update if we have content
                if new_content:
                    msg_copy["content"] = new_content

            formatted.append(msg_copy)

        return formatted

    def _format_messages_with_prompt_caching(self, messages: List[Dict]) -> List[Dict]:
        """
        Format messages to opt into Anthropic prompt caching.

        Caching reduces input cost ~90% on Anthropic when a stable prefix
        recurs across turns. We emit ``cache_control: {"type": "ephemeral"}``
        markers and litellm passes them straight through.

        Anthropic marker placement: up to 2 markers (system + LAST long user
        message). Anthropic caps cache_control markers at 4 per request;
        staying at 2 leaves headroom. Earlier "mark every long user message"
        produced 5+ markers in compute-subagent runs and the API rejected
        the request mid-run:
            "A maximum of 4 blocks with cache_control may be
             provided. Found 5."

        Other providers fall through unchanged:
          - **OpenAI** auto-caches without client markers (≥1024 tokens, in
            128-token increments).
          - **Gemini** was tried (litellm issue #4284 supports the
            translation), but Google's cachedContents API rejects calls that
            also carry tools / tool_config / system_instruction — sciagent's
            tool surface is dynamic, so the combination 400s. Gemini's
            implicit cache (auto on 2.5+) still yields the 90% discount on
            repeated prefixes.
          - **xAI** has no cache surface today.
          - **vLLM** varies.
        """
        provider = self._provider()
        if provider not in _CACHE_CONTROL_PROVIDERS:
            return messages

        formatted = [msg.copy() for msg in messages]

        # Pass 1: cache the system message (always; biggest static prefix).
        # Both Anthropic and Gemini benefit from this single marker.
        for msg_copy in formatted:
            if msg_copy["role"] != "system":
                continue
            content = msg_copy["content"]
            if isinstance(content, str):
                msg_copy["content"] = [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            elif isinstance(content, list):
                # Mark the last text block.
                for i, block in enumerate(content):
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "text"
                        and i == len(content) - 1
                    ):
                        block["cache_control"] = {"type": "ephemeral"}
                msg_copy["content"] = content

        # Pass 2 (Anthropic only): cache the LAST user message > 2000 chars.
        # Gemini caches one continuous block per request, so adding a second
        # marker doesn't help — keep Gemini at the single system marker.
        if provider == "anthropic":
            for msg_copy in reversed(formatted):
                if msg_copy["role"] != "user":
                    continue
                content = msg_copy["content"]
                if isinstance(content, str) and len(content) > 2000:
                    msg_copy["content"] = [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                    break  # Only mark the latest qualifying user message.

        return formatted

    def _format_tools(self, tools: List[Dict]) -> List[Dict]:
        """Format tools for the LLM API"""
        formatted = []
        for tool in tools:
            formatted.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}})
                }
            })
        return formatted
    
    def chat(
        self,
        messages: List[Message],
        tools: Optional[List[Dict]] = None,
        tool_choice: str = "auto",
        **kwargs
    ) -> LLMResponse:
        """
        Send messages to LLM and get response
        
        Args:
            messages: List of Message objects
            tools: List of tool definitions
            tool_choice: "auto", "none", or {"type": "function", "function": {"name": "..."}}
            
        Returns:
            LLMResponse with content and/or tool calls
        """
        if not LITELLM_AVAILABLE:
            raise RuntimeError("litellm not installed")

        # Convert messages to dicts
        msg_dicts = [m.to_dict() for m in messages]

        # Convert image blocks to provider-specific format
        msg_dicts = self._format_images_for_provider(msg_dicts)

        # Apply Anthropic prompt caching if applicable
        msg_dicts = self._format_messages_with_prompt_caching(msg_dicts)

        # Prepare kwargs
        call_kwargs = {
            "model": self.model,
            "messages": msg_dicts,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            **kwargs
        }

        if self.base_url:
            call_kwargs["base_url"] = self.base_url

        # Reasoning kwargs delta. _reasoning_call_kwargs centralizes the
        # per-provider quirks (Anthropic temp clamp); litellm's drop_params
        # handles the rest at the wire (OpenAI o-series temp drop, xAI Grok
        # variants that don't accept reasoning_effort, Gemini's
        # reasoning_effort -> thinking_budget translation).
        call_kwargs.update(self._reasoning_call_kwargs())

        # Add tools if provided
        if tools:
            call_kwargs["tools"] = self._format_tools(tools)
            call_kwargs["tool_choice"] = tool_choice

        # Make the call - wrap in warnings context to suppress pydantic serialization warnings
        # These warnings occur when litellm's response models have extra/missing fields
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning)
            response = self._call_with_retry(call_kwargs)

            # Parse response (also under warnings suppression as model_dump triggers them)
            choice = response.choices[0]
            message = choice.message

            # Extract tool calls if present
            tool_calls = []
            if hasattr(message, 'tool_calls') and message.tool_calls:
                for tc in message.tool_calls:
                    tool_calls.append(ToolCall.from_response(tc.model_dump()))

            # Extract usage info
            usage = {
                "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                "completion_tokens": response.usage.completion_tokens if response.usage else 0,
            }

            # L2: normalize cache-hit telemetry into provider-agnostic fields.
            # Raw per-provider snapshot stays in cache_info for debugging /
            # legacy readers; downstream code branches on the normalized
            # cache_hit_in_input flag.
            cache_metrics = _extract_cache_metrics(response, self._provider())
            cache_info = {}
            if response.usage:
                # Keep the Anthropic-named raw fields in cache_info for any
                # caller still inspecting them directly; remove after the
                # bench v1.1 migration window closes.
                if hasattr(response.usage, "cache_read_input_tokens"):
                    cache_info["cache_read_input_tokens"] = response.usage.cache_read_input_tokens or 0
                if hasattr(response.usage, "cache_creation_input_tokens"):
                    cache_info["cache_creation_input_tokens"] = response.usage.cache_creation_input_tokens or 0
                if hasattr(response, "_hidden_params"):
                    hidden = response._hidden_params or {}
                    if hidden.get("cache_hit"):
                        cache_info["litellm_cache_hit"] = True

            # H3: capture per-call usage + litellm-computed response cost so
            # the provenance log can record schema-v2 tool_result fields.
            self._capture_last_usage(response, call_kwargs)

            # Extract reasoning/thinking content (works for Claude, Gemini, OpenAI o-series)
            reasoning_content = None
            if hasattr(message, 'reasoning_content') and message.reasoning_content:
                reasoning_content = message.reasoning_content

        return LLMResponse(
            content=message.content or "",
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
            cache_info=cache_info,
            reasoning_content=reasoning_content,
            cache_read_tokens=cache_metrics["cache_read_tokens"],
            cache_write_tokens=cache_metrics["cache_write_tokens"],
            cache_hit_in_input=cache_metrics["cache_hit_in_input"],
        )
    
    def chat_stream(
        self,
        messages: List[Message],
        tools: Optional[List[Dict]] = None,
        **kwargs
    ) -> Generator[str, None, LLMResponse]:
        """
        Stream response from LLM

        Yields content chunks, returns final LLMResponse
        """
        if not LITELLM_AVAILABLE:
            raise RuntimeError("litellm not installed")

        msg_dicts = [m.to_dict() for m in messages]

        # Apply Anthropic prompt caching if applicable
        msg_dicts = self._format_messages_with_prompt_caching(msg_dicts)

        call_kwargs = {
            "model": self.model,
            "messages": msg_dicts,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
            **kwargs
        }

        if tools:
            call_kwargs["tools"] = self._format_tools(tools)

        # Suppress pydantic serialization warnings during streaming
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning)
            response = self._call_with_retry(call_kwargs, is_stream=True)

            full_content = ""
            tool_calls = []

            for chunk in response:
                if chunk.choices[0].delta.content:
                    content = chunk.choices[0].delta.content
                    full_content += content
                    yield content

        return LLMResponse(
            content=full_content,
            tool_calls=tool_calls,
            finish_reason="stop"
        )


# Convenience function for simple calls
def ask(
    prompt: str,
    model: str = DEFAULT_MODEL,
    system: Optional[str] = None,
    **kwargs
) -> str:
    """Simple one-shot LLM call"""
    client = LLMClient(model=model, **kwargs)
    messages = []
    if system:
        messages.append(Message(role="system", content=system))
    messages.append(Message(role="user", content=prompt))
    response = client.chat(messages)
    return response.content


def configure_cache(
    cache_type: str = "local",
    ttl: int = 3600,
    redis_host: Optional[str] = None,
    redis_port: int = 6379,
    disk_cache_dir: Optional[str] = None,
    enabled: bool = True
) -> None:
    """
    Configure LiteLLM's response caching at runtime.

    Args:
        cache_type: "local" (in-memory), "redis", or "disk"
        ttl: Cache time-to-live in seconds (default: 1 hour)
        redis_host: Redis server host (required if cache_type="redis")
        redis_port: Redis server port (default: 6379)
        disk_cache_dir: Directory for disk cache (required if cache_type="disk")
        enabled: Whether to enable caching (default: True)

    Examples:
        # Use in-memory cache (default)
        configure_cache(cache_type="local", ttl=3600)

        # Use Redis for persistent caching
        configure_cache(cache_type="redis", redis_host="localhost", ttl=7200)

        # Use disk cache
        configure_cache(cache_type="disk", disk_cache_dir="/tmp/llm_cache")

        # Disable caching
        configure_cache(enabled=False)
    """
    if not LITELLM_AVAILABLE:
        print("Warning: litellm not installed, caching not available")
        return

    litellm.enable_cache = enabled

    if not enabled:
        litellm.cache = None
        return

    cache_kwargs = {"type": cache_type, "ttl": ttl}

    if cache_type == "redis":
        if not redis_host:
            raise ValueError("redis_host required for Redis cache")
        cache_kwargs["host"] = redis_host
        cache_kwargs["port"] = redis_port

    elif cache_type == "disk":
        if not disk_cache_dir:
            raise ValueError("disk_cache_dir required for disk cache")
        cache_kwargs["disk_cache_dir"] = disk_cache_dir

    litellm.cache = Cache(**cache_kwargs)
