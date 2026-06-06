"""Multimodal attachment plumbing: read tool → canonical block → provider wire.

Covers the contract described in
``llm._format_attachments_for_provider`` and ``agent.MULTIMODAL_ARTIFACT_TYPES``:

  - The read tool emits an artifact descriptor (``type`` + ``media_type`` +
    ``data`` + ``text_fallback``) rather than pypdf-stripped text, so figures /
    tables / equations survive into the model context for providers that take
    the raw file natively.
  - The provider-dispatch boundary translates each canonical attachment block
    to the wire shape the active model accepts through litellm. PDF is the
    concrete kind shipped today; the same dispatch handles image / audio /
    video the moment a read tool emits them.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from sciagent.llm import LLMClient, Message, MULTIMODAL_BLOCK_TYPES
from sciagent.tools.atomic.file_ops import FileOpsTool


# Tiny but pypdf-parseable 1-page PDF. Inline rather than a fixture so the
# test stays hermetic — no network, no disk fixtures to keep in sync.
_MINI_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj "
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj "
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj "
    b"xref\n0 4\n0000000000 65535 f\n"
    b"0000000009 00000 n\n0000000056 00000 n\n0000000101 00000 n\n"
    b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n149\n%%EOF\n"
)


def _fake_client(provider: str) -> LLMClient:
    """Bypass __init__ (which would try to load credentials) and pin the
    provider so we can exercise ``_format_attachments_for_provider`` in
    isolation. The function only consults ``self._provider()`` + the
    capability table; nothing else on the client matters for this test."""
    c = LLMClient.__new__(LLMClient)
    LLMClient._provider = lambda self, _p=provider: _p
    return c


# ---------------------------------------------------------------------------
# Read tool emits the artifact descriptor
# ---------------------------------------------------------------------------


def test_file_ops_pdf_emits_document_artifact(tmp_path: Path) -> None:
    """``_read_pdf`` no longer strips a PDF to pypdf text. It hands the model
    the raw bytes as a ``document`` artifact, with a pypdf text_fallback
    alongside for providers / log consumers that need text."""
    pdf = tmp_path / "tiny.pdf"
    pdf.write_bytes(_MINI_PDF)

    result = FileOpsTool().execute(command="read", path=str(pdf))
    assert result.success, result.error

    out = result.output
    assert isinstance(out, dict)
    assert out["type"] == "document"
    assert out["media_type"] == "application/pdf"
    assert base64.b64decode(out["data"]).startswith(b"%PDF-")
    assert out["filename"] == "tiny.pdf"
    assert out["pages"] == 1
    # text_fallback exists even when extraction yields empty (blank page);
    # the key matters for downstream consumers, not the value.
    assert "text_fallback" in out
    assert "[PDF: tiny.pdf" in out["display_text"]


# ---------------------------------------------------------------------------
# Provider dispatch — PDF
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider",
    ["anthropic", "gemini", "vertex_ai", "bedrock"],
)
def test_pdf_routes_to_image_url_data_uri(provider: str) -> None:
    """Anthropic / Gemini / Vertex / Bedrock all accept a PDF via an
    ``image_url`` block whose URL is a ``data:application/pdf;base64,...``
    URI; litellm rewrites that into the native ``document`` / ``inline_data``
    block per provider. The dispatch boundary's job is just to emit that
    one shape — litellm owns the rest."""
    b64 = base64.b64encode(_MINI_PDF).decode()
    msg = Message.create_multimodal(
        role="user",
        text="summarize",
        attachments=[{
            "type": "document",
            "media_type": "application/pdf",
            "data": b64,
            "filename": "x.pdf",
        }],
    )

    client = _fake_client(provider)
    out = client._format_attachments_for_provider([msg.to_dict()])
    blocks = out[0]["content"]

    types = [b["type"] for b in blocks]
    assert "image_url" in types
    assert "text" in types

    image_url_block = next(b for b in blocks if b["type"] == "image_url")
    assert image_url_block["image_url"]["url"].startswith(
        "data:application/pdf;base64,"
    )


def test_pdf_routes_to_openai_file_block() -> None:
    """OpenAI chat completions accept PDFs via the native ``file`` block —
    ``{"type": "file", "file": {"file_data": "...", "filename": "..."}}``.
    The data URI inside ``file_data`` is what GPT-5 expects."""
    b64 = base64.b64encode(_MINI_PDF).decode()
    msg = Message.create_multimodal(
        role="user",
        text="summarize",
        attachments=[{
            "type": "document",
            "media_type": "application/pdf",
            "data": b64,
            "filename": "paper.pdf",
        }],
    )

    client = _fake_client("openai")
    out = client._format_attachments_for_provider([msg.to_dict()])
    blocks = out[0]["content"]

    file_block = next(b for b in blocks if b["type"] == "file")
    assert file_block["file"]["filename"] == "paper.pdf"
    assert file_block["file"]["file_data"].startswith(
        "data:application/pdf;base64,"
    )


def test_pdf_dropped_with_text_fallback_when_provider_unsupported() -> None:
    """If the active provider has no row in the capability table for the
    block kind, the block is dropped — the read tool's ``text_fallback``
    already rides the tool-result channel as the user-visible content, so
    we never silently send a base64 string the provider can't decode."""
    b64 = base64.b64encode(_MINI_PDF).decode()
    msg = Message.create_multimodal(
        role="user",
        text="here is the fallback text",
        attachments=[{
            "type": "document",
            "media_type": "application/pdf",
            "data": b64,
            "filename": "paper.pdf",
        }],
    )

    client = _fake_client("some_future_provider_with_no_pdf_support")
    out = client._format_attachments_for_provider([msg.to_dict()])
    blocks = out[0]["content"]
    types = [b["type"] for b in blocks]
    assert types == ["text"], (
        "Unsupported attachment kind must be dropped, not passed through raw — "
        "the text fallback the read tool emitted is what the model sees."
    )


# ---------------------------------------------------------------------------
# Provider dispatch — image (regression: existing path still works)
# ---------------------------------------------------------------------------


_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.mark.parametrize(
    "provider",
    ["anthropic", "openai", "gemini", "vertex_ai", "bedrock", "xai", "groq"],
)
def test_image_routes_to_image_url_everywhere(provider: str) -> None:
    """Images take ``image_url`` data URIs on every provider we've ever
    seen; the capability table defaults unknown providers to that shape
    for images specifically (other kinds default to drop)."""
    b64 = base64.b64encode(_TINY_PNG).decode()
    msg = Message.create_multimodal(
        role="user",
        text="describe",
        attachments=[{
            "type": "image",
            "media_type": "image/png",
            "data": b64,
        }],
    )
    client = _fake_client(provider)
    out = client._format_attachments_for_provider([msg.to_dict()])
    blocks = out[0]["content"]
    assert any(b["type"] == "image_url" for b in blocks)


# ---------------------------------------------------------------------------
# Canonical shape contract
# ---------------------------------------------------------------------------


def test_canonical_block_types_kept_in_sync() -> None:
    """``MULTIMODAL_BLOCK_TYPES`` (llm.py) and ``MULTIMODAL_ARTIFACT_TYPES``
    (agent.py) must agree — they're two views of the same set. Drift would
    mean a tool emits an artifact the agent collects but the LLM dispatch
    doesn't translate, or vice versa."""
    from sciagent.agent import MULTIMODAL_ARTIFACT_TYPES

    assert MULTIMODAL_BLOCK_TYPES == MULTIMODAL_ARTIFACT_TYPES
