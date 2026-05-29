"""L1-L5 portability tests for sciagent's LLM layer.

Per DESIGN_LLM_PORTABILITY.md §0 testing principle: do not mock
``litellm.completion``. Drive every test through litellm itself so the
real request-shaping and response-parsing paths execute; the
``mock_response`` kwarg skips only the wire call.
"""

from __future__ import annotations

import os
from typing import Dict, Iterable, Optional

import litellm
import pytest

from sciagent.agent import AgentConfig
from sciagent.llm import (
    LLMClient,
    LLMResponse,
    _CACHE_CONTROL_PROVIDERS,
    _extract_cache_metrics,
    _resolve_provider,
)
from sciagent.llm_profiles import _OVERLAY, profile_for
from sciagent.orchestrator import (
    OrchestratorConfig,
    TaskOrchestrator,
)
from sciagent.startup import detect_provider_from_model
from sciagent.subagent import SubAgentOrchestrator
from sciagent.tools.atomic.todo import TodoTool
from sciagent.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# L1 — provider detection via litellm.get_llm_provider
# ---------------------------------------------------------------------------


def test_l1_resolve_anthropic_native():
    assert _resolve_provider("anthropic/claude-sonnet-4-6") == "anthropic"


def test_l1_resolve_bedrock_fronting_anthropic():
    """Bedrock-fronted Claude resolves to ``bedrock`` (not ``anthropic``)
    even though the model id contains 'anthropic' and 'claude'. The
    previous substring-match predicate misclassified this as native."""
    out = _resolve_provider("bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0")
    assert out == "bedrock"


def test_l1_resolve_vertex_ai_fronting_anthropic():
    assert _resolve_provider("vertex_ai/claude-3-5-sonnet@20241022") == "vertex_ai"


def test_l1_resolve_openai():
    assert _resolve_provider("openai/gpt-4.1") == "openai"


def test_l1_resolve_gemini():
    assert _resolve_provider("gemini/gemini-2.5-flash") == "gemini"


def test_l1_resolve_xai():
    assert _resolve_provider("xai/grok-4-1-fast-reasoning") == "xai"
    assert _resolve_provider("xai/grok-4-0709") == "xai"


def test_l1_resolve_vllm_does_not_crash():
    """A custom litellm provider id (vllm) must not raise. Whatever
    litellm returns is fine; sciagent treats it as the provider-neutral
    path downstream."""
    out = _resolve_provider("vllm/meta-llama-3.3-70b")
    assert isinstance(out, str) and out  # non-empty


def test_l1_resolve_unknown_returns_unknown():
    """A bare id litellm can't classify resolves to ``unknown`` instead
    of crashing, so callers default to the safe (no cache_control, no
    temp clamp) path."""
    assert _resolve_provider("grok-3-mini") == "unknown"


def test_l1_resolve_empty_string():
    assert _resolve_provider("") == "unknown"


def test_l1_startup_shim_uses_litellm():
    """startup.detect_provider_from_model used to be a parallel substring
    matcher; L1 collapsed it onto _resolve_provider so both call sites
    can't drift apart."""
    assert detect_provider_from_model("bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0") == "bedrock"
    assert detect_provider_from_model("openai/gpt-4.1") == "openai"


def test_l1_client_provider_method():
    """LLMClient._provider() routes through _resolve_provider — replaces
    the three _is_*_model substring predicates."""
    client = LLMClient(model="anthropic/claude-sonnet-4-6", api_key="test-noop")
    assert client._provider() == "anthropic"
    client2 = LLMClient(model="openai/gpt-4.1", api_key="test-noop")
    assert client2._provider() == "openai"


# ---------------------------------------------------------------------------
# L2 — normalized cache telemetry
# ---------------------------------------------------------------------------


def _llm_response(model: str) -> object:
    """Drive a real litellm.completion through mock_response so the
    response object is shaped by litellm's parsers (not by a hand-rolled
    fake that might lie about the schema). The usage object on mock
    responses lacks cache fields by design — that's exactly the no-cache
    code path _extract_cache_metrics must handle gracefully.
    """
    return litellm.completion(
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        mock_response="ok",
    )


@pytest.mark.parametrize(
    "model,provider",
    [
        ("anthropic/claude-sonnet-4-6", "anthropic"),
        ("openai/gpt-4.1", "openai"),
        ("gemini/gemini-2.5-flash", "gemini"),
        ("xai/grok-4-1-fast-reasoning", "xai"),
    ],
)
def test_l2_extract_cache_metrics_no_hit_path(model: str, provider: str):
    """Each provider's no-cache-hit response normalizes to three keys
    with zero counts. Routes through litellm's real response parsers."""
    response = _llm_response(model)
    metrics = _extract_cache_metrics(response, provider)
    assert set(metrics.keys()) == {
        "cache_read_tokens",
        "cache_write_tokens",
        "cache_hit_in_input",
    }
    assert metrics["cache_read_tokens"] == 0
    assert metrics["cache_write_tokens"] == 0
    assert metrics["cache_hit_in_input"] is False


def test_l2_extract_cache_metrics_anthropic_separate_from_input():
    """When Anthropic surfaces cache fields directly on usage, the
    normalized shape sets cache_hit_in_input=False (separate from
    prompt_tokens) — matches the cost-rollup contract in §4.2."""
    response = _llm_response("anthropic/claude-sonnet-4-6")
    response.usage.cache_read_input_tokens = 2048
    response.usage.cache_creation_input_tokens = 512
    metrics = _extract_cache_metrics(response, "anthropic")
    assert metrics == {
        "cache_read_tokens": 2048,
        "cache_write_tokens": 512,
        "cache_hit_in_input": False,
    }


def test_l2_extract_cache_metrics_openai_folded_into_input():
    """OpenAI returns cached_tokens nested under prompt_tokens_details
    and folds them into prompt_tokens. The normalized shape signals
    cache_hit_in_input=True so cost code doesn't double-count."""

    class _Details:
        cached_tokens = 1024

    response = _llm_response("openai/gpt-4.1")
    response.usage.prompt_tokens_details = _Details()
    metrics = _extract_cache_metrics(response, "openai")
    assert metrics == {
        "cache_read_tokens": 1024,
        "cache_write_tokens": 0,
        "cache_hit_in_input": True,
    }


def test_l2_extract_cache_metrics_gemini_direct_usage_metadata():
    """Gemini direct API surfaces cachedContentTokenCount on
    usageMetadata (camelCase off the wire). Normalized to the same
    cache_hit_in_input=True shape as OpenAI-compat."""

    class _Meta:
        cachedContentTokenCount = 4096

    response = _llm_response("gemini/gemini-2.5-flash")
    response.usageMetadata = _Meta()  # type: ignore[attr-defined]
    metrics = _extract_cache_metrics(response, "gemini")
    assert metrics["cache_read_tokens"] == 4096
    assert metrics["cache_hit_in_input"] is True


def test_l2_extract_cache_metrics_missing_usage_returns_zeros():
    class _Empty:
        usage = None

    metrics = _extract_cache_metrics(_Empty(), "anthropic")
    assert metrics == {
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_hit_in_input": False,
    }


def test_l2_llmresponse_exposes_normalized_fields():
    r = LLMResponse(
        content="hi",
        cache_read_tokens=2048,
        cache_write_tokens=128,
        cache_hit_in_input=False,
    )
    assert r.cache_hit is True
    assert r.tokens_cached == 2048
    assert r.tokens_written_to_cache == 128
    # Deprecated Anthropic-named aliases still resolve for one release.
    assert r.cache_read_input_tokens == 2048
    assert r.cache_creation_input_tokens == 128


def test_l2_cache_control_gate_anthropic_only():
    """cache_control:ephemeral emission is Anthropic-only.

    Gemini was tried (litellm issue #4284 supports the translation), but
    Google's cachedContents API rejects calls that ALSO carry
    system_instruction / tools / tool_config in the same request — exactly
    what sciagent sends. Implicit Gemini cache (auto on 2.5+) still yields
    the 90% discount on repeated prefixes, so we lose nothing material.
    See memory `feedback_gemini_explicit_cache_incompatible_with_tools.md`.
    """
    assert "anthropic" in _CACHE_CONTROL_PROVIDERS
    assert "gemini" not in _CACHE_CONTROL_PROVIDERS
    assert "openai" not in _CACHE_CONTROL_PROVIDERS
    assert "xai" not in _CACHE_CONTROL_PROVIDERS


def _count_cache_markers(formatted):
    n = 0
    for msg in formatted:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    n += 1
    return n


def test_l2_gemini_emits_no_cache_markers():
    """Gemini falls through ``_format_messages_with_prompt_caching``
    untouched; messages reach litellm without any cache_control fields."""
    client = LLMClient(model="gemini/gemini-2.5-flash", api_key="test-noop")
    msgs = [
        {"role": "system", "content": "sys " * 200},
        {"role": "user", "content": "x" * 5000},
    ]
    out = client._format_messages_with_prompt_caching(msgs)
    assert _count_cache_markers(out) == 0
    # Messages pass through with string content intact.
    assert out[0]["content"] == "sys " * 200
    assert out[1]["content"] == "x" * 5000


def test_l2_anthropic_keeps_two_marker_strategy():
    """The Anthropic-only second-marker pass on the latest long user
    message survives L2; the existing prompt-caching tests cover the
    four-block cap but this assertion pins the two-marker shape."""
    client = LLMClient(model="anthropic/claude-sonnet-4-6", api_key="test-noop")
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "x" * 5000},
    ]
    out = client._format_messages_with_prompt_caching(msgs)
    assert _count_cache_markers(out) == 2


def test_l2_openai_skips_cache_control():
    """OpenAI auto-caches without client markers — we must not emit
    cache_control for it (the API rejects the field)."""
    client = LLMClient(model="openai/gpt-4.1", api_key="test-noop")
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "x" * 5000},
    ]
    out = client._format_messages_with_prompt_caching(msgs)
    assert _count_cache_markers(out) == 0


def test_l2_gemini_call_with_tools_round_trips_through_litellm():
    """Regression check: a Gemini call carrying tools (sciagent's
    standard shape) must not raise when we go through litellm. Previously
    the cache_control emission caused a 400 from Google's cachedContents
    API ("CachedContent can not be used with GenerateContent request
    setting system_instruction, tools or tool_config")."""
    client = LLMClient(model="gemini/gemini-2.5-flash", api_key="test-noop")
    msgs = [
        {"role": "system", "content": "sys " * 200},
        {"role": "user", "content": "hi"},
    ]
    formatted = client._format_messages_with_prompt_caching(msgs)
    # Should not raise.
    litellm.completion(
        model="gemini/gemini-2.5-flash",
        messages=formatted,
        mock_response="ok",
        tools=[{
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo input",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                },
            },
        }],
    )


# ---------------------------------------------------------------------------
# L3 — role-model overrides on OrchestratorConfig
# ---------------------------------------------------------------------------


def test_l3_orchestratorconfig_role_models_default_none():
    cfg = OrchestratorConfig()
    assert cfg.scientific_model is None
    assert cfg.coding_model is None
    assert cfg.fast_model is None
    assert cfg.vision_model is None
    assert cfg.verifier_model is None


def test_l3_resolve_helpers_fall_back_to_defaults_constants():
    """None on the config field resolves to the matching
    defaults.*_MODEL constant — the H1 verifier_model pattern, mirrored
    for the other four roles."""
    from sciagent import defaults

    cfg = OrchestratorConfig()
    assert cfg.resolve_scientific_model() == defaults.SCIENTIFIC_MODEL
    assert cfg.resolve_coding_model() == defaults.CODING_MODEL
    assert cfg.resolve_fast_model() == defaults.FAST_MODEL
    assert cfg.resolve_vision_model() == defaults.VISION_MODEL
    assert cfg.resolve_verifier_model() == defaults.VERIFICATION_MODEL


def test_l3_resolve_helpers_honor_override():
    cfg = OrchestratorConfig(
        scientific_model="openai/gpt-4.1",
        coding_model="openai/gpt-4.1-mini",
        fast_model="openai/gpt-4.1-nano",
        vision_model="openai/gpt-4.1",
    )
    assert cfg.resolve_scientific_model() == "openai/gpt-4.1"
    assert cfg.resolve_coding_model() == "openai/gpt-4.1-mini"
    assert cfg.resolve_fast_model() == "openai/gpt-4.1-nano"
    assert cfg.resolve_vision_model() == "openai/gpt-4.1"


def test_l3_taskorchestrator_patches_registered_subagent_kinds(tmp_path):
    """Setting orchestrator.coding_model should flow through to the
    debug/research/general/compute/analyze subagent kinds; scientific
    (plan) and fast (explore) are unaffected when only coding_model
    is overridden. Mirrors how H1's verifier_model patches the
    verifier kind in-place."""
    sub = SubAgentOrchestrator(working_dir=str(tmp_path))
    todo = TodoTool()
    cfg = OrchestratorConfig(coding_model="openai/gpt-4.1-mini")
    TaskOrchestrator(
        todo_tool=todo,
        subagent_orchestrator=sub,
        config=cfg,
        working_dir=str(tmp_path),
    )
    assert sub.registry.get("debug").model == "openai/gpt-4.1-mini"
    assert sub.registry.get("research").model == "openai/gpt-4.1-mini"
    assert sub.registry.get("general").model == "openai/gpt-4.1-mini"
    # plan (scientific) and explore (fast) untouched.
    from sciagent.defaults import SCIENTIFIC_MODEL, FAST_MODEL

    assert sub.registry.get("plan").model == SCIENTIFIC_MODEL
    assert sub.registry.get("explore").model == FAST_MODEL


def test_l3_cross_family_verifier_leaves_other_roles_alone(tmp_path):
    """The bench's cross-family-verifier cell sets only verifier_model;
    every other role must keep the sciagent default. Mirrors
    DESIGN_BENCH.md §5.2."""
    sub = SubAgentOrchestrator(working_dir=str(tmp_path))
    todo = TodoTool()
    cfg = OrchestratorConfig(verifier_model="openai/gpt-5-mini")
    TaskOrchestrator(
        todo_tool=todo,
        subagent_orchestrator=sub,
        config=cfg,
        working_dir=str(tmp_path),
    )
    assert sub.registry.get("verifier").model == "openai/gpt-5-mini"
    from sciagent.defaults import (
        SCIENTIFIC_MODEL,
        CODING_MODEL,
        FAST_MODEL,
    )

    assert sub.registry.get("plan").model == SCIENTIFIC_MODEL
    assert sub.registry.get("general").model == CODING_MODEL
    assert sub.registry.get("explore").model == FAST_MODEL


def test_l3_fast_model_override_repaints_web_tool(tmp_path):
    """The web_fetch summarizer reads FAST_MODEL at construction time;
    L3 must reconstruct WebTool with the override so an --set
    orchestrator.fast_model=… cell actually changes summarization."""
    sub = SubAgentOrchestrator(working_dir=str(tmp_path))
    todo = TodoTool()
    cfg = OrchestratorConfig(fast_model="openai/gpt-4.1-nano")
    TaskOrchestrator(
        todo_tool=todo,
        subagent_orchestrator=sub,
        config=cfg,
        working_dir=str(tmp_path),
    )
    web_tool = sub.tools.get("web")
    assert web_tool is not None
    assert web_tool._fast_model == "openai/gpt-4.1-nano"


def test_l3_web_tool_falls_back_to_default_when_no_override(tmp_path):
    sub = SubAgentOrchestrator(working_dir=str(tmp_path))
    todo = TodoTool()
    TaskOrchestrator(
        todo_tool=todo,
        subagent_orchestrator=sub,
        config=OrchestratorConfig(),
        working_dir=str(tmp_path),
    )
    web_tool = sub.tools.get("web")
    # No override -> _fast_model stays None; the tool internally falls
    # back to defaults.FAST_MODEL when called.
    assert web_tool._fast_model is None


# ---------------------------------------------------------------------------
# L4 — _reasoning_call_kwargs per provider
# ---------------------------------------------------------------------------


def test_l4_no_reasoning_returns_empty():
    client = LLMClient(model="anthropic/claude-sonnet-4-6", api_key="test-noop")
    assert client._reasoning_call_kwargs() == {}


def test_l4_anthropic_clamps_temperature_to_one():
    """Anthropic's extended thinking requires temperature=1. The clamp
    is the only per-provider quirk we explicitly handle; everything
    else rides litellm's drop_params."""
    client = LLMClient(
        model="anthropic/claude-sonnet-4-6",
        api_key="test-noop",
        reasoning_effort="high",
    )
    out = client._reasoning_call_kwargs()
    assert out == {"reasoning_effort": "high", "temperature": 1}


@pytest.mark.parametrize(
    "model",
    [
        "openai/o4-mini",
        "gemini/gemini-2.5-flash",
        "xai/grok-4-1-fast-reasoning",
        "xai/grok-4-0709",
        "xai/grok-4-3",
    ],
)
def test_l4_non_anthropic_omits_temperature_clamp(model: str):
    """Only Anthropic gets the temperature=1 clamp. OpenAI o-series
    rejects temperature (litellm drops it); Gemini translates
    reasoning_effort to thinking_budget; xAI Grok 4 (non-4.3) doesn't
    accept reasoning_effort at all (litellm PR #16265 drops it
    per-model). We don't enumerate any of that here — drop_params owns
    it. We just verify we don't smuggle a temperature override in."""
    client = LLMClient(model=model, api_key="test-noop", reasoning_effort="high")
    out = client._reasoning_call_kwargs()
    assert "temperature" not in out
    assert out["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-sonnet-4-6",
        "openai/o4-mini",
        "gemini/gemini-2.5-flash",
        "xai/grok-4-0709",
        "xai/grok-4-1-fast-reasoning",
    ],
)
def test_l4_litellm_accepts_reasoning_kwargs(model: str):
    """litellm-acceptance net: shape the kwargs we'd send and route them
    through litellm.completion(mock_response=...). litellm runs its real
    request-validation + drop_params logic — if a future litellm bump
    regresses on any provider's drop_params handling, this fires before
    the bench cell would. No real API call (mock_response short-circuits
    the wire)."""
    client = LLMClient(model=model, api_key="test-noop", reasoning_effort="high")
    kwargs = client._reasoning_call_kwargs()
    # Should not raise.
    response = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        mock_response="ok",
        **kwargs,
    )
    assert response.choices[0].message.content == "ok"


# ---------------------------------------------------------------------------
# L5 — per-provider session_soft_budget
# ---------------------------------------------------------------------------


def _without_env(var: str):
    """Context-manager-ish helper: pop var, return restoration callable.

    We can't use ``monkeypatch.delenv`` here because the test is a plain
    function; the existing test suite imports modules at top level so
    profile_for sees os.environ at call time, not import time. Pop +
    restore covers the case where a developer happens to have the var
    set locally.
    """
    prior = os.environ.pop(var, None)

    def restore():
        if prior is not None:
            os.environ[var] = prior

    return restore


def test_l5_agentconfig_session_soft_budget_default_is_none():
    """L5 flipped the default from 4_000_000 to None so AgentLoop can
    resolve from the provider overlay instead of carrying the
    Anthropic-folklore constant for every provider."""
    assert AgentConfig().session_soft_budget is None


def test_l5_overlay_anthropic_carries_four_million():
    assert _OVERLAY["anthropic"]["session_soft_budget_tokens"] == 4_000_000


def test_l5_overlay_openai_carries_one_point_five_million():
    assert _OVERLAY["openai"]["session_soft_budget_tokens"] == 1_500_000


def test_l5_overlay_gemini_xai_two_million():
    assert _OVERLAY["gemini"]["session_soft_budget_tokens"] == 2_000_000
    assert _OVERLAY["xai"]["session_soft_budget_tokens"] == 2_000_000


def test_l5_profile_resolves_anthropic_budget_to_overlay():
    restore = _without_env("SCIAGENT_SESSION_SOFT_BUDGET")
    try:
        prof = profile_for("anthropic/claude-sonnet-4-6")
        assert prof.session_soft_budget == 4_000_000
    finally:
        restore()


def test_l5_profile_resolves_openai_budget_to_overlay():
    restore = _without_env("SCIAGENT_SESSION_SOFT_BUDGET")
    try:
        prof = profile_for("openai/gpt-4.1")
        assert prof.session_soft_budget == 1_500_000
    finally:
        restore()


def test_l5_env_var_overrides_overlay():
    """SCIAGENT_SESSION_SOFT_BUDGET still wins — existing operator
    escape hatch. L5 only added a per-provider default below the env
    layer."""
    prior = os.environ.get("SCIAGENT_SESSION_SOFT_BUDGET")
    os.environ["SCIAGENT_SESSION_SOFT_BUDGET"] = "999999"
    try:
        prof = profile_for("anthropic/claude-sonnet-4-6")
        assert prof.session_soft_budget == 999999
    finally:
        if prior is None:
            os.environ.pop("SCIAGENT_SESSION_SOFT_BUDGET", None)
        else:
            os.environ["SCIAGENT_SESSION_SOFT_BUDGET"] = prior


def test_l5_agentloop_resolves_from_profile_for_anthropic(tmp_path):
    restore = _without_env("SCIAGENT_SESSION_SOFT_BUDGET")
    try:
        from sciagent.agent import AgentConfig, AgentLoop

        cfg = AgentConfig(
            model="anthropic/claude-sonnet-4-6",
            working_dir=str(tmp_path),
            verbose=False,
            auto_save=False,
        )
        loop = AgentLoop(config=cfg)
        assert loop.config.session_soft_budget == 4_000_000
    finally:
        restore()


def test_l5_agentloop_resolves_from_profile_for_openai(tmp_path):
    restore = _without_env("SCIAGENT_SESSION_SOFT_BUDGET")
    try:
        from sciagent.agent import AgentConfig, AgentLoop

        cfg = AgentConfig(
            model="openai/gpt-4.1",
            working_dir=str(tmp_path),
            verbose=False,
            auto_save=False,
        )
        loop = AgentLoop(config=cfg)
        assert loop.config.session_soft_budget == 1_500_000
    finally:
        restore()


def test_l5_explicit_override_beats_profile(tmp_path):
    """An explicit int on AgentConfig (the --set agent.session_soft_budget=N
    path) wins over the overlay default — needed so bench cells can pin
    a tight per-cell budget without editing source."""
    restore = _without_env("SCIAGENT_SESSION_SOFT_BUDGET")
    try:
        from sciagent.agent import AgentConfig, AgentLoop

        cfg = AgentConfig(
            model="openai/gpt-4.1",
            session_soft_budget=250_000,
            working_dir=str(tmp_path),
            verbose=False,
            auto_save=False,
        )
        loop = AgentLoop(config=cfg)
        assert loop.config.session_soft_budget == 250_000
    finally:
        restore()


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
