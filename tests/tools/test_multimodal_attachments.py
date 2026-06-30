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
import types
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
    capability table; nothing else on the client matters for this test.

    Provider is patched on the INSTANCE via ``types.MethodType``, not on
    the class. A previous version of this helper assigned to
    ``LLMClient._provider`` directly — that leaks across tests, and any
    later test that constructs a real ``LLMClient`` sees the patched
    provider regardless of its model id. Also seeds the ephemeral
    multimodal consumed-set so the dispatcher's first wire pass has a
    clean state per client.
    """
    c = LLMClient.__new__(LLMClient)
    c._consumed_artifact_ids = set()
    c._provider = types.MethodType(lambda self, _p=provider: _p, c)
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


def _multi_page_pdf(n_pages: int) -> bytes:
    """Build a real n-page PDF via pypdf so the page-count probe and the
    slicer have something authentic to chew on. Using ``_MINI_PDF`` n times
    isn't enough — pypdf needs a valid xref table per page."""
    import io as _io
    import pypdf

    writer = pypdf.PdfWriter()
    for _ in range(n_pages):
        writer.add_blank_page(width=612, height=792)
    buf = _io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_pdf_over_soft_cap_requires_pages_range(tmp_path: Path) -> None:
    """A 12-page PDF without ``pages`` is refused with an actionable error.
    This is the regression guard against the failure mode where a 12-page
    paper streamed to the model unsliced caused the next LLM turn to max
    out the output cap before emitting any tool call."""
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(_multi_page_pdf(12))

    result = FileOpsTool().execute(command="read", path=str(pdf))
    assert not result.success
    assert "12 pages" in result.error
    assert "pages" in result.error


def test_pdf_under_soft_cap_no_pages_required(tmp_path: Path) -> None:
    """A 5-page PDF (≤ ``MAX_PDF_PAGES_NO_RANGE``) still flows through
    without a `pages` argument — the guard only kicks in above the soft
    cap so short PDFs stay zero-friction."""
    pdf = tmp_path / "short.pdf"
    pdf.write_bytes(_multi_page_pdf(5))

    result = FileOpsTool().execute(command="read", path=str(pdf))
    assert result.success, result.error
    assert result.output["pages"] == 5
    assert result.output["page_range"] is None


def test_pdf_pages_range_slices_to_requested_pages(tmp_path: Path) -> None:
    """With ``pages="3-7"`` the artifact carries only those 5 pages — the
    re-encoded PDF round-trips through pypdf with the right page count and
    the artifact metadata records the slice for the provenance log."""
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(_multi_page_pdf(12))

    result = FileOpsTool().execute(command="read", path=str(pdf), pages="3-7")
    assert result.success, result.error
    assert result.output["pages"] == 5
    assert result.output["page_range"] == [3, 7]
    assert result.output["total_pages"] == 12
    assert "pages 3-7 of 12" in result.output["display_text"]

    import io as _io
    import pypdf
    reader = pypdf.PdfReader(_io.BytesIO(base64.b64decode(result.output["data"])))
    assert len(reader.pages) == 5


def test_pdf_pages_range_over_hard_cap_rejected(tmp_path: Path) -> None:
    """The per-read hard cap (``MAX_PDF_PAGES_PER_READ``) is enforced even
    when an explicit range is given — keeps a runaway ``pages="1-200"`` from
    sidestepping the guard."""
    pdf = tmp_path / "long.pdf"
    pdf.write_bytes(_multi_page_pdf(30))

    result = FileOpsTool().execute(command="read", path=str(pdf), pages="1-25")
    assert not result.success
    assert "cap is 20" in result.error


def test_pdf_pages_range_bad_value_rejected(tmp_path: Path) -> None:
    """Non-numeric / malformed ``pages`` strings come back with a clear
    error rather than a stack trace."""
    pdf = tmp_path / "short.pdf"
    pdf.write_bytes(_multi_page_pdf(5))

    result = FileOpsTool().execute(command="read", path=str(pdf), pages="abc")
    assert not result.success
    assert "Invalid `pages`" in result.error


# ---------------------------------------------------------------------------
# Provider dispatch — PDF
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider",
    ["gemini", "vertex_ai", "bedrock"],
)
def test_pdf_routes_to_image_url_data_uri(provider: str) -> None:
    """Gemini / Vertex / Bedrock accept a PDF via an ``image_url`` block
    whose URL is a ``data:application/pdf;base64,...`` URI; litellm
    rewrites that into the native ``inline_data`` / ``document`` block per
    provider.

    Anthropic is intentionally NOT in this list — litellm's image_url →
    Anthropic translation copies media_type as-is, producing an ``image``
    block with media_type=application/pdf which Anthropic rejects (it
    requires image/{png,jpeg,gif,webp}). PDFs going to Anthropic must use
    the native ``document`` block instead (see
    test_pdf_routes_to_anthropic_native_document)."""
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


def test_pdf_routes_to_anthropic_native_document() -> None:
    """Anthropic accepts PDFs only via its native ``document`` block:
    ``{"type": "document", "source": {"type": "base64",
    "media_type": "application/pdf", "data": ...}}``. Routing via the
    OpenAI image_url shape causes litellm to emit an Anthropic ``image``
    block with media_type=application/pdf, which the Anthropic API
    rejects (image blocks accept only image/{png,jpeg,gif,webp})."""
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

    client = _fake_client("anthropic")
    out = client._format_attachments_for_provider([msg.to_dict()])
    blocks = out[0]["content"]

    doc_block = next(b for b in blocks if b["type"] == "document")
    assert doc_block["source"]["type"] == "base64"
    assert doc_block["source"]["media_type"] == "application/pdf"
    assert doc_block["source"]["data"] == b64
    # And we must NOT also emit an image_url for the same attachment.
    assert all(b["type"] != "image_url" for b in blocks)


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
    ["openai", "gemini", "vertex_ai", "bedrock", "xai", "groq"],
)
def test_image_routes_to_image_url_everywhere(provider: str) -> None:
    """Images take ``image_url`` data URIs on every provider whose wire
    format isn't Anthropic-native. The capability table defaults unknown
    providers to that shape for images specifically (other kinds default
    to drop).

    Anthropic uses its native ``image`` block instead (see
    test_image_routes_to_anthropic_native)."""
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


def test_image_routes_to_anthropic_native() -> None:
    """Anthropic accepts images via the native ``image`` block:
    ``{"type": "image", "source": {"type": "base64",
    "media_type": "image/png", "data": ...}}``. We use the same
    shape that file_ops emits internally (sciagent's canonical
    format), so this is effectively a passthrough."""
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
    client = _fake_client("anthropic")
    out = client._format_attachments_for_provider([msg.to_dict()])
    blocks = out[0]["content"]
    img_block = next(b for b in blocks if b["type"] == "image")
    assert img_block["source"]["type"] == "base64"
    assert img_block["source"]["media_type"] == "image/png"
    assert img_block["source"]["data"] == b64
    # And no image_url for the same attachment.
    assert all(b["type"] != "image_url" for b in blocks)


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


# ---------------------------------------------------------------------------
# Ephemeral multimodal replay (one wire-send per artifact_id)
# ---------------------------------------------------------------------------


def _pdf_attachment(artifact_id: str, *, text_fallback: str = "TEXT") -> dict:
    """Canonical multimodal attachment dict matching what the agent loop
    builds in ``_execute_tool_calls`` after a file_ops PDF read."""
    return {
        "type": "document",
        "media_type": "application/pdf",
        "data": base64.b64encode(_MINI_PDF).decode(),
        "filename": "paper.pdf",
        "artifact_id": artifact_id,
        "text_fallback": text_fallback,
    }


def test_artifact_sent_as_multimodal_on_first_wire_format() -> None:
    """First time an artifact_id appears in a wire-format pass, the b64
    rides the multimodal block (existing behavior). The artifact_id is
    recorded on the client as consumed."""
    client = _fake_client("anthropic")

    msg = Message.create_multimodal(
        role="user",
        text="analyze",
        attachments=[_pdf_attachment("aid-first-send")],
    )

    out = client._format_attachments_for_provider([msg.to_dict()])
    blocks = out[0]["content"]
    doc_blocks = [b for b in blocks if b["type"] == "document"]
    assert len(doc_blocks) == 1, "first send must carry the b64 multimodal block"
    assert doc_blocks[0]["source"]["data"] == base64.b64encode(_MINI_PDF).decode()
    assert "aid-first-send" in client._consumed_artifact_ids


def test_artifact_swapped_for_text_fallback_on_second_wire_format() -> None:
    """Second pass: the same in-memory message goes through the dispatcher
    again (as it would on the next turn). Because the artifact_id is now
    consumed, the dispatcher drops the b64 and emits a text block carrying
    the carried text_fallback instead. This is the replay-bloat fix."""
    client = _fake_client("anthropic")

    msg = Message.create_multimodal(
        role="user",
        text="analyze",
        attachments=[_pdf_attachment("aid-replay", text_fallback="page 1: hello")],
    )

    # Turn 1 — first send. b64 goes out, artifact is consumed.
    client._format_attachments_for_provider([msg.to_dict()])
    # Turn 2 — same message list re-sent. b64 must NOT appear.
    out = client._format_attachments_for_provider([msg.to_dict()])
    blocks = out[0]["content"]

    assert all(b["type"] != "document" for b in blocks), (
        "second send must not re-ship the multimodal block"
    )
    text_blocks = [b for b in blocks if b["type"] == "text"]
    fallback_block = next(
        b for b in text_blocks if "page 1: hello" in b["text"]
    )
    # Marker tells the model the artifact was already shown.
    assert "previously shown above" in fallback_block["text"]
    assert "paper.pdf" in fallback_block["text"]


def test_artifact_ids_tracked_per_artifact_independently() -> None:
    """Two artifacts on two different turns: the first artifact's id is
    already consumed when the second turn arrives, but the second's id is
    fresh and must still ship its b64."""
    client = _fake_client("anthropic")

    msg_turn1 = Message.create_multimodal(
        role="user",
        text="first",
        attachments=[_pdf_attachment("aid-1", text_fallback="FIRST")],
    )
    client._format_attachments_for_provider([msg_turn1.to_dict()])

    # Turn 2 — the loop re-sends turn-1's message AND adds a second one.
    msg_turn2 = Message.create_multimodal(
        role="user",
        text="second",
        attachments=[_pdf_attachment("aid-2", text_fallback="SECOND")],
    )
    out = client._format_attachments_for_provider(
        [msg_turn1.to_dict(), msg_turn2.to_dict()]
    )

    # Turn-1 message: only text (the fallback).
    t1_blocks = out[0]["content"]
    assert all(b["type"] != "document" for b in t1_blocks)
    assert any("FIRST" in b.get("text", "") for b in t1_blocks)

    # Turn-2 message: still has the document block.
    t2_blocks = out[1]["content"]
    assert any(b["type"] == "document" for b in t2_blocks)
    assert "aid-2" in client._consumed_artifact_ids


def test_artifact_without_id_keeps_legacy_unbounded_replay() -> None:
    """Backward compat: a block without an ``_artifact_id`` (e.g. a direct
    Message.create_multimodal call from a legacy caller) bypasses the
    ephemeral machinery entirely and rides as a normal multimodal block on
    every send. The opt-in is the artifact_id."""
    client = _fake_client("anthropic")

    msg = Message.create_multimodal(
        role="user",
        text="legacy",
        attachments=[{
            "type": "document",
            "media_type": "application/pdf",
            "data": base64.b64encode(_MINI_PDF).decode(),
            "filename": "legacy.pdf",
        }],
    )
    out1 = client._format_attachments_for_provider([msg.to_dict()])
    out2 = client._format_attachments_for_provider([msg.to_dict()])

    for out in (out1, out2):
        types = [b["type"] for b in out[0]["content"]]
        assert "document" in types
    assert client._consumed_artifact_ids == set()


def test_consumed_set_is_per_client_instance() -> None:
    """The consumed set is bound to the LLMClient instance — two sessions
    (two LLMClient instances) do not share. Verifies no cross-session
    leakage when a subagent constructs its own client."""
    client_a = _fake_client("anthropic")
    client_b = _fake_client("anthropic")

    msg = Message.create_multimodal(
        role="user",
        text="shared",
        attachments=[_pdf_attachment("aid-shared")],
    )
    client_a._format_attachments_for_provider([msg.to_dict()])

    # client_b has never seen this artifact — must ship the b64.
    out_b = client_b._format_attachments_for_provider([msg.to_dict()])
    assert any(b["type"] == "document" for b in out_b[0]["content"])
    assert "aid-shared" not in client_b._consumed_artifact_ids or (
        client_b._consumed_artifact_ids == {"aid-shared"}
    )
    # After the b call, client_b records it for ITS session.
    assert "aid-shared" in client_b._consumed_artifact_ids
