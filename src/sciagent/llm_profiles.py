"""LLM profile abstraction — provider-agnostic budget + caching config.

Built on litellm's community-maintained model registry (`litellm.model_cost`,
queried via `litellm.get_model_info`). litellm covers hundreds of models with
context windows, caching support, and costs — community-updated within days
of provider launches. Hand-rolling our own ``PROFILES`` dict would go stale;
querying litellm keeps us current as Opus 4.x, GPT-5.x, Gemini 3.x, Grok
4.x lineups churn.

We layer a *thin* sciagent overlay only for fields litellm doesn't carry:

  - **Cache write threshold (chars)** — Anthropic gates cache_control on a
    minimum of 1024–4096 tokens depending on the model; emitting
    cache_control on a too-short message wastes a breakpoint. Approximated
    in chars (~4 chars/token) so the check is cheap.
  - **Cache TTL preference** (`5m` | `1h`) — Anthropic-specific.
  - **Beta headers** — Anthropic gates compaction, extended output, etc.
    behind beta headers that litellm doesn't surface.
  - **sciagent soft budgets** — compaction trigger as a fraction of
    context window; optional cumulative-session soft cap (env-configurable;
    None disables).

The empirical "Anthropic ~4M cumulative disconnect" baked into the previous
``agent.py`` is folklore — not in any official Anthropic doc. We do NOT
encode it as a default. Users who want a wrap-up gate can set
``SCIAGENT_SESSION_SOFT_BUDGET``.

User-facing copy that consumes a profile must speak in *fractions of context
window* or *absolute tokens*, never name a provider. Rate-limit handling and
provider-specific exceptions are the LLM client's concern, not the profile.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


# Approximate chars/token used when gating cache_control by char count. Most
# tokenizers land between 3.5 and 4.5 chars/token for English; 4.0 is the
# usual rule-of-thumb. We err slightly loose so messages near the threshold
# tend to get cached rather than missed.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class LLMProfile:
    """Resolved per-model profile combining litellm registry + sciagent overlay.

    Values flow from three sources:
      1. ``litellm.get_model_info(model)`` — context, output max, costs,
         caching support flag.
      2. ``_OVERLAY[provider]`` — sciagent additions (TTL, min-cache, beta).
      3. Env vars — runtime user overrides for the cross-provider knobs.
    """

    model: str
    provider: str

    # --- From litellm registry ------------------------------------------
    context_window: int                     # max_input_tokens
    output_max: int                         # max_output_tokens
    supports_caching: bool                  # supports_prompt_caching

    input_cost_per_token: Optional[float]
    output_cost_per_token: Optional[float]
    cache_read_cost_per_token: Optional[float]
    cache_write_cost_per_token: Optional[float]

    # --- sciagent overlay -----------------------------------------------
    cache_min_input_chars: int              # below this, skip cache_control
    cache_ttl: str                          # "5m" | "1h" (Anthropic-only)
    beta_headers: Tuple[str, ...]           # extra request headers if needed

    compact_at_fraction: float              # compact when context > this * window
    session_soft_budget: Optional[int]      # cumulative tokens; None = no soft cap

    @property
    def compact_threshold_tokens(self) -> int:
        """Token count above which the agent should run compaction."""
        return int(self.context_window * self.compact_at_fraction)

    def cache_control_eligible(self, content_chars: int) -> bool:
        """True if a content block of this length should get cache_control."""
        return self.supports_caching and content_chars >= self.cache_min_input_chars


# Per-provider overlay. Only fields litellm's registry doesn't carry.
# Keep this small and conservative — defaults that work for any model in the
# provider's lineup, not per-model tuning. Per-model tuning belongs in env
# vars or a future sciagent.toml, not in code.
_OVERLAY: Dict[str, Dict[str, Any]] = {
    "anthropic": {
        # Anthropic minimum tokens to cache: 1024 (older), 2048 (Sonnet 4.6),
        # 4096 (Opus 4.x, Haiku 4.5). Pick the most permissive; the API will
        # silently no-op if the actual limit is higher for the chosen model.
        # In chars: 1024 * 4 = 4096.
        "cache_min_input_chars": 4096,
        "cache_ttl": "5m",
    },
    "openai": {
        # Caching is automatic and free; no cache_control to emit. Setting
        # cache_min_input_chars high effectively disables our (unused) gate.
        "cache_min_input_chars": 1_000_000,
        "cache_ttl": "5m",
    },
    "gemini": {
        # Implicit caching auto-applies on 2.5+. Explicit caching needs
        # ≥32k tokens — large; we don't emit it client-side here.
        "cache_min_input_chars": 1_000_000,
        "cache_ttl": "1h",
    },
    "vertex_ai": {
        "cache_min_input_chars": 1_000_000,
        "cache_ttl": "1h",
    },
    "xai": {
        "cache_min_input_chars": 1_000_000,
        "cache_ttl": "5m",
    },
    "ollama": {
        # No remote caching. Don't emit cache_control.
        "cache_min_input_chars": 1_000_000,
        "cache_ttl": "5m",
    },
}


def _provider_from_id(model: str) -> str:
    """Best-effort provider extraction from a litellm-style model id.

    Accepts both ``provider/model`` and bare ``model``. Bare model ids are
    routed to litellm at call time anyway; we only need a string for the
    overlay lookup.
    """
    if not isinstance(model, str) or not model:
        return "unknown"
    if "/" in model:
        return model.split("/", 1)[0]
    # Bare model ids — litellm has its own routing rules. Best-effort heuristics.
    lower = model.lower()
    if lower.startswith("claude"):
        return "anthropic"
    if lower.startswith("gpt") or lower.startswith("o1") or lower.startswith("o3"):
        return "openai"
    if lower.startswith("gemini"):
        return "gemini"
    if lower.startswith("grok"):
        return "xai"
    return "unknown"


def _env_float(key: str, default: float) -> float:
    val = os.environ.get(key)
    if val is None:
        return default
    try:
        f = float(val)
    except ValueError:
        return default
    if not 0.0 < f <= 1.0:
        return default
    return f


def _env_int(key: str) -> Optional[int]:
    val = os.environ.get(key)
    if val is None:
        return None
    try:
        return int(val)
    except ValueError:
        return None


def _registry_lookup(model: str) -> Dict[str, Any]:
    """Query litellm's model registry, tolerantly. Returns {} on failure.

    Tries the id verbatim first, then a small set of mechanical variants:
      - bare name (strip provider prefix)
      - ``<id>-preview`` (covers models still in preview at the registry)
      - ``<id>-latest`` (covers point-in-time aliases)

    No hand-rolled alias map. If the user's id resolves under any of those
    forms, we get the real numbers; otherwise the caller falls back to
    safe defaults. Keeping the variants mechanical (not provider-specific)
    means we follow litellm's registry as it updates — when Google moves
    Gemini 3 from preview to GA and litellm drops ``-preview`` from the
    canonical id, the verbatim lookup starts succeeding without a code
    change here.
    """
    candidates = [model]
    if "/" in model:
        bare = model.split("/", 1)[1]
        if bare not in candidates:
            candidates.append(bare)
    # Suffix variants — only consulted if the verbatim/bare form misses.
    for suffix in ("-preview", "-latest"):
        v = f"{model}{suffix}"
        if v not in candidates:
            candidates.append(v)

    try:
        import litellm
    except Exception:
        return {}

    for candidate in candidates:
        try:
            info = litellm.get_model_info(candidate)
        except Exception:
            continue
        if isinstance(info, dict) and (
            info.get("max_input_tokens") or info.get("max_tokens")
        ):
            return info
    return {}


# Sane defaults when litellm doesn't know the model (custom endpoint, local
# server with a non-standard id, future model not yet in the registry).
# Numbers are conservative — most models in 2026 fit in these or larger.
_DEFAULT_CONTEXT_WINDOW = 32_768
_DEFAULT_OUTPUT_MAX = 4_096


def profile_for(model: str) -> LLMProfile:
    """Resolve a profile for ``model``. Stable across calls; no caching needed
    (litellm's lookup is already a dict access)."""
    info = _registry_lookup(model)

    provider = (
        info.get("litellm_provider")
        or _provider_from_id(model)
    )
    overlay = _OVERLAY.get(provider, {})

    context_window = int(
        info.get("max_input_tokens")
        or info.get("max_tokens")
        or _DEFAULT_CONTEXT_WINDOW
    )
    output_max = int(info.get("max_output_tokens") or _DEFAULT_OUTPUT_MAX)
    supports_caching = bool(info.get("supports_prompt_caching"))

    return LLMProfile(
        model=model,
        provider=provider,
        context_window=context_window,
        output_max=output_max,
        supports_caching=supports_caching,
        input_cost_per_token=info.get("input_cost_per_token"),
        output_cost_per_token=info.get("output_cost_per_token"),
        cache_read_cost_per_token=info.get("cache_read_input_token_cost"),
        cache_write_cost_per_token=info.get("cache_creation_input_token_cost"),
        cache_min_input_chars=int(overlay.get("cache_min_input_chars", 2048)),
        cache_ttl=str(overlay.get("cache_ttl", "5m")),
        beta_headers=tuple(overlay.get("beta_headers", ())),
        compact_at_fraction=_env_float("SCIAGENT_COMPACT_AT_PCT", 0.6),
        session_soft_budget=_env_int("SCIAGENT_SESSION_SOFT_BUDGET"),
    )


__all__ = ["LLMProfile", "profile_for", "CHARS_PER_TOKEN"]
