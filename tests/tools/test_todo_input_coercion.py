"""todo() must tolerate the LLM JSON-stringifying its arg.

Symptom from real traces:
    todo(todos=[   {     "id": "analyze_manuscript",...)
    ✗ Error: todos[0] is a string, not a dict. ... Got: '['

What actually happened: the LLM emitted a JSON-encoded list as the
argument value, and the dispatcher delivered it as a plain ``str``.
Python's for-loop then iterates the string's characters, and the first
character is ``[`` — hence the misleading "todos[0] = '['" message.

Fix: normalize ``str → json.loads → list`` (and ``dict → [dict]``) before
iterating. These tests pin that normalization so the regression doesn't
return.
"""

from __future__ import annotations

import json

import pytest

from sciagent.tools.atomic.todo import TodoTool


def test_json_string_list_arg_is_parsed():
    """The exact regression: agent passes the whole list as a JSON string.
    Tool must parse it instead of iterating characters."""
    tool = TodoTool()
    payload = json.dumps([
        {"id": "a", "content": "first", "task_type": "general"},
        {"id": "b", "content": "second", "depends_on": ["a"]},
    ])
    out = tool.execute(todos=payload)
    assert out.success is True, out.error
    # Both items should be in the graph.
    metadata = getattr(out, "metadata", None) or {}
    todos = metadata.get("todos")
    if todos is not None:
        assert {t["id"] for t in todos} == {"a", "b"}


def test_single_dict_arg_is_wrapped_in_list():
    """A single todo dict (not wrapped in a list) is a natural shorthand.
    Wrap rather than reject."""
    tool = TodoTool()
    out = tool.execute(todos={"id": "x", "content": "do it"})
    assert out.success is True, out.error


def test_unparseable_string_returns_clear_error():
    """A string that's not valid JSON gets a useful error pointing at the
    list-of-dicts shape — NOT the misleading 'todos[0] is a string'."""
    tool = TodoTool()
    out = tool.execute(todos="not json at all")
    assert out.success is False
    assert "doesn't parse as JSON" in (out.error or "")
    # Confirm we're not seeing the legacy character-iteration message.
    assert "todos[0]" not in (out.error or "")


def test_non_list_non_string_non_dict_rejected():
    """A bare int / bool / None-but-iterable is genuinely wrong shape;
    error message must name the actual type instead of crashing."""
    tool = TodoTool()
    out = tool.execute(todos=42)  # type: ignore[arg-type]
    assert out.success is False
    assert "must be a list of dicts" in (out.error or "")
    assert "int" in (out.error or "")


def test_real_list_of_dicts_still_works():
    """The happy path must still be the happy path — coercion code can't
    break the case where the agent already passes a Python list."""
    tool = TodoTool()
    out = tool.execute(todos=[{"id": "x", "content": "ok"}])
    assert out.success is True, out.error
