"""Pin the cache_control marker count under Anthropic's 4-block hard cap.

The earlier implementation marked every user message > 2000 chars with
cache_control. In a long compute-subagent run with multiple big tool
results that produced 5+ markers, exceeding Anthropic's 4-block cap.
The API rejected the request mid-run with:

    A maximum of 4 blocks with cache_control may be provided. Found 5.

Strategy enforced here: max 2 markers (1 system + 1 latest qualifying
user message). Anthropic caches prefix-style, so a single user marker
at the latest position covers the whole prefix and gives the best hit
rate for the next turn.
"""

from __future__ import annotations

from sciagent.llm import LLMClient


def _count_cache_markers(formatted):
    """Count cache_control markers across all message content blocks."""
    n = 0
    for msg in formatted:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    n += 1
    return n


def _make_anthropic_client() -> LLMClient:
    """LLMClient targeting an Anthropic model so the cache path activates.
    No API call is made; we only exercise _format_messages_with_prompt_caching."""
    return LLMClient(model="claude-opus-4-7", api_key="test-noop")


def test_no_marker_on_non_anthropic_models():
    """Cache markers are Anthropic-specific. Other providers see messages
    untouched."""
    client = LLMClient(model="gpt-4o", api_key="test-noop")
    long_user = "x" * 5000
    msgs = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": long_user},
    ]
    out = client._format_messages_with_prompt_caching(msgs)
    assert _count_cache_markers(out) == 0


def test_system_message_always_marked():
    client = _make_anthropic_client()
    msgs = [{"role": "system", "content": "system prompt"}]
    out = client._format_messages_with_prompt_caching(msgs)
    assert _count_cache_markers(out) == 1
    # System content is now structured.
    assert isinstance(out[0]["content"], list)
    assert out[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_short_user_message_not_marked():
    client = _make_anthropic_client()
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},  # under 2000 chars
    ]
    out = client._format_messages_with_prompt_caching(msgs)
    # Only the system marker; short user untouched.
    assert _count_cache_markers(out) == 1
    assert out[1]["content"] == "hi"  # left as-is


def test_single_long_user_marked():
    client = _make_anthropic_client()
    long = "x" * 5000
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": long},
    ]
    out = client._format_messages_with_prompt_caching(msgs)
    assert _count_cache_markers(out) == 2  # system + the one long user


def test_many_long_user_messages_capped_at_two_markers_total():
    """The bug: previously this produced 5 markers (1 system + 4 user).
    Anthropic capped requests at 4, so this fired a 400 mid-run.
    Now it must produce exactly 2 markers — system + the LATEST user
    message — so we sit safely under the 4-block cap regardless of
    how many large tool results pile up in the conversation.
    """
    client = _make_anthropic_client()
    long = "x" * 5000
    msgs = [{"role": "system", "content": "sys"}]
    # Eight long user turns — what a bug-fixing compute subagent looks like.
    for i in range(8):
        msgs.append({"role": "user", "content": long + f"\nturn {i}"})
    out = client._format_messages_with_prompt_caching(msgs)
    assert _count_cache_markers(out) == 2


def test_marked_user_is_the_latest_long_one():
    """The marker should be on the LATEST qualifying user message — that
    placement caches the entire prefix and gives the best hit rate when
    the next turn arrives (it appends after the marker)."""
    client = _make_anthropic_client()
    long = "x" * 5000
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": long + "\nfirst"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": long + "\nsecond"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": long + "\nthird"},
    ]
    out = client._format_messages_with_prompt_caching(msgs)
    # The first and second long user messages stay as plain strings.
    assert out[1]["content"] == long + "\nfirst"
    assert out[3]["content"] == long + "\nsecond"
    # Only the third (latest) is restructured + marked.
    assert isinstance(out[5]["content"], list)
    assert out[5]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert _count_cache_markers(out) == 2


def test_under_anthropic_four_block_cap_always():
    """Property test: regardless of how big the conversation grows, the
    total cache_control marker count must stay ≤ 4 (Anthropic's hard cap)."""
    client = _make_anthropic_client()
    long = "x" * 5000
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(50):
        msgs.append({"role": "user", "content": long + f"\nturn {i}"})
        msgs.append({"role": "assistant", "content": f"ok {i}"})
    out = client._format_messages_with_prompt_caching(msgs)
    assert _count_cache_markers(out) <= 4


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
