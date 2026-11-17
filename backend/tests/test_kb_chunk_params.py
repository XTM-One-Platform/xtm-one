"""Knowledge base chunking parameters are bounded, and chunking terminates.

`chunk_text` split an oversized section by advancing `chunk_size - chunk_overlap`
tokens, so an overlap reaching the size never advanced - and nothing bounded
the two values on create or import. One such collection plus one long document
froze a worker's event loop. These tests pin both halves: every write path
refuses out-of-range values with a 422 naming the range, and `chunk_text`
itself terminates whatever a legacy row holds.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import textwrap
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.v1 import knowledge_bases as kb_mod
from app.api.v1.schemas.knowledge_base import CreateKnowledgeBaseRequest
from app.services.chunking import (
    CHUNK_SIZE_MAX,
    CHUNK_SIZE_MIN,
    chunk_params_problem,
    chunk_text,
    normalize_chunk_params,
)

VALID = [
    (512, 128),
    (2048, 256),
    (384, 96),
    (512, 0),
    (64, 32),
    (500, 100),
    (5333, 2666),  # the largest size + overlap within CHUNK_TOKENS_MAX
    (CHUNK_SIZE_MAX, 0),
]
INVALID = [
    (512, 512),  # overlap == size: the split never advanced
    (256, 300),  # overlap > size: the split went backwards
    (512, 257),  # more than half
    (6000, 2001),  # size + overlap over CHUNK_TOKENS_MAX
    (8192, 4096),  # one merged chunk would reach ~12k tokens
    (8192, 0),  # over text-embedding-3's 8,191-token input
    (512, -1),
    (0, 0),
    (CHUNK_SIZE_MIN - 1, 0),
    (CHUNK_SIZE_MAX + 1, 0),
]


@pytest.mark.parametrize(("size", "overlap"), VALID)
def test_valid_parameters_are_accepted_and_left_unchanged(size, overlap):
    assert chunk_params_problem(size, overlap) is None
    assert normalize_chunk_params(size, overlap) == (size, overlap)


@pytest.mark.parametrize(("size", "overlap"), INVALID)
def test_out_of_range_parameters_are_refused(size, overlap):
    problem = chunk_params_problem(size, overlap)
    assert problem
    assert "chunk_" in problem


def test_non_integers_are_refused():
    assert chunk_params_problem(True, 0)
    assert chunk_params_problem(512, "128")


@pytest.mark.parametrize(("size", "overlap"), INVALID)
def test_normalized_parameters_always_advance(size, overlap):
    norm_size, norm_overlap = normalize_chunk_params(size, overlap)
    assert chunk_params_problem(norm_size, norm_overlap) is None
    assert norm_size - norm_overlap >= norm_size // 2


def test_every_built_in_knowledge_base_is_within_the_bounds():
    """The seed sync writes these onto installed collections on every boot."""
    from app.agents.builtin_agents import BUILTIN_KNOWLEDGE_BASES

    for kb in BUILTIN_KNOWLEDGE_BASES:
        size = kb.get("chunk_size", 512)
        overlap = kb.get("chunk_overlap", 128)
        assert chunk_params_problem(size, overlap) is None, kb.get("slug")


# -- chunk_text terminates -------------------------------------------------------

_BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("word_fallback", [False, True], ids=["tiktoken", "words"])
@pytest.mark.parametrize(("size", "overlap"), [(512, 512), (256, 300), (0, 0), (-5, 3)])
def test_chunk_text_terminates_on_out_of_range_parameters(word_fallback, size, overlap):
    """Run in a child process with a timeout: a regression must fail this test,
    not hang the suite."""
    script = textwrap.dedent(
        f"""
        import app.services.chunking as chunking
        if {word_fallback!r}:
            def _boom():
                raise RuntimeError("encoder unavailable")
            chunking._get_encoder = _boom
        text = " ".join(f"word{{i}}" for i in range(3000))
        chunks = chunking.chunk_text(text, chunk_size={size}, chunk_overlap={overlap})
        assert chunks and all(chunk for chunk in chunks)
        print(len(chunks))
        """
    )
    result = subprocess.run(  # noqa: S603 - this interpreter, a fixed script
        [sys.executable, "-c", script],
        cwd=_BACKEND,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) > 0


# text-embedding-3-small / -large refuse an input longer than this.
_EMBEDDING_INPUT_TOKENS_MAX = 8191


@pytest.mark.parametrize("size", [64, 512, 2048, 5333, 6000, CHUNK_SIZE_MAX])
def test_accepted_parameters_never_emit_a_chunk_the_embedder_refuses(size):
    """The merge keeps up to `chunk_overlap` tokens, then appends a section of
    up to `chunk_size`: an overlap-sized paragraph followed by a size-sized one
    is the largest chunk any accepted pair can produce."""
    from app.services.chunking import _get_encoder, chunk_overlap_max

    overlap = chunk_overlap_max(size)
    assert chunk_params_problem(size, overlap) is None
    enc = _get_encoder()

    def paragraph(tokens: int) -> str:
        text = "start" + " word" * (tokens - 1)
        assert len(enc.encode(text)) == tokens
        return text

    text = "\n\n".join([paragraph(overlap or 1), paragraph(size), paragraph(size)])
    chunks = chunk_text(text, chunk_size=size, chunk_overlap=overlap)
    longest = max(len(enc.encode(chunk)) for chunk in chunks)
    assert longest <= _EMBEDDING_INPUT_TOKENS_MAX


def test_many_short_sections_never_exceed_the_embedding_input():
    """Sections are counted without the separators joining them: thousands of
    one-token paragraphs used to merge into a ~16k-token chunk at 8000/0."""
    from app.services.chunking import _get_encoder

    enc = _get_encoder()
    text = "\n\n".join(["alpha"] * 12000)
    chunks = chunk_text(text, chunk_size=CHUNK_SIZE_MAX, chunk_overlap=0)
    assert max(len(enc.encode(chunk)) for chunk in chunks) <= _EMBEDDING_INPUT_TOKENS_MAX
    assert "".join(chunks).count("alpha") >= 12000


@pytest.mark.parametrize(
    ("text", "size", "overlap"),
    [
        ("\n\n".join(["alpha"] * 3000), 512, 128),  # chunks well past 512 tokens
        ("\n\n".join(f"Para {i}. " + ("word " * 200) for i in range(40)), 2048, 256),
    ],
    ids=["short-sections", "paragraphs"],
)
def test_chunks_within_the_embedding_input_keep_their_boundaries(
    monkeypatch, text, size, overlap
):
    """Only a chunk past the embedding limit is re-split: every other chunk is
    exactly the plain join it always was, so collections keep their chunks."""
    import app.services.chunking as chunking

    guarded = chunking.chunk_text(text, chunk_size=size, chunk_overlap=overlap)
    monkeypatch.setattr(
        chunking, "_merged_chunks", lambda parts, *_args: ["\n\n".join(parts)]
    )
    assert guarded == chunking.chunk_text(text, chunk_size=size, chunk_overlap=overlap)


def test_chunk_text_is_unchanged_for_valid_parameters():
    text = "\n\n".join(f"Para {i}. " + ("word " * 200) for i in range(12))
    chunks = chunk_text(text, chunk_size=512, chunk_overlap=128)
    assert len(chunks) > 3
    assert chunk_text(text, chunk_size=512, chunk_overlap=128) == chunks


# -- Write paths -----------------------------------------------------------------


def test_create_request_refuses_an_overlap_reaching_the_size():
    with pytest.raises(ValidationError) as exc:
        CreateKnowledgeBaseRequest(name="Wiki", chunk_size=512, chunk_overlap=512)
    assert "chunk_overlap must be between 0 and half of chunk_size" in str(exc.value)
    # The defaults stay valid.
    assert CreateKnowledgeBaseRequest(name="Wiki").chunk_overlap == 128


@pytest.mark.parametrize(
    ("field", "raw"), [("chunk_overlap", True), ("chunk_size", "512"), ("chunk_size", 512.0)]
)
def test_create_request_refuses_non_integers_before_coercion(field, raw):
    """Lax `int` would store `true` as 1 and "512" as 512: checked on the raw value."""
    with pytest.raises(ValidationError) as exc:
        CreateKnowledgeBaseRequest(name="Wiki", **{field: raw})
    assert f"{field} must be an integer" in str(exc.value)


def test_create_endpoint_answers_422_with_the_range():
    app = FastAPI()

    @app.post("/kb")
    async def _create(body: CreateKnowledgeBaseRequest):
        return {"ok": True}

    client = TestClient(app)
    refused = client.post(
        "/kb", json={"name": "Wiki", "chunk_size": 512, "chunk_overlap": 600}
    )
    assert refused.status_code == 422
    assert "chunk_overlap must be between 0 and half" in refused.text
    not_an_int = client.post("/kb", json={"name": "Wiki", "chunk_overlap": True})
    assert not_an_int.status_code == 422
    assert "chunk_overlap must be an integer" in not_an_int.text
    assert client.post("/kb", json={"name": "Wiki"}).status_code == 200


def _export_zip(*, chunk_size: object, chunk_overlap: object) -> bytes:
    manifest = {
        "format": kb_mod._KB_EXPORT_FORMAT,
        "version": kb_mod._KB_EXPORT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "export_id": str(uuid.uuid4()),
        "part": 1,
        "total_parts": 1,
        "knowledge_base": {
            "name": "Imported KB",
            "description": None,
            "tags": [],
            "embedding_model": "text-embedding-3-large",
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
        },
        "documents": [],
        "sync_sources": [],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_import_refuses_out_of_range_chunk_parameters():
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    package = UploadFile(
        file=io.BytesIO(_export_zip(chunk_size=512, chunk_overlap=512)),
        filename="kb.zip",
    )
    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await kb_mod.import_knowledge_base(
            files=[package], user=user, db=db, _agentic_gate=None
        )
    assert exc.value.status_code == 422
    assert "chunk_overlap must be between 0 and half" in exc.value.detail
    db.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("size", "overlap"), [(512, True), ("512", 128)])
async def test_import_refuses_non_integer_chunk_parameters(size, overlap):
    """The manifest is coerced on parse, so the raw JSON type is checked there."""
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    package = UploadFile(
        file=io.BytesIO(_export_zip(chunk_size=size, chunk_overlap=overlap)),
        filename="kb.zip",
    )
    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await kb_mod.import_knowledge_base(
            files=[package], user=user, db=db, _agentic_gate=None
        )
    assert exc.value.status_code == 400
    assert "must be an integer" in exc.value.detail
    assert exc.value.detail.startswith("Invalid manifest.json:")
    db.add.assert_not_called()


def test_exported_manifest_parses_back():
    """Strict chunk fields still accept every manifest an export writes."""
    from app.api.v1.schemas.knowledge_base import KBExportManifest

    manifest = KBExportManifest.model_validate_json(
        _export_zip_manifest_json(chunk_size=512, chunk_overlap=128)
    )
    assert (manifest.knowledge_base.chunk_size, manifest.knowledge_base.chunk_overlap) == (
        512,
        128,
    )
    assert KBExportManifest.model_validate_json(manifest.model_dump_json()) == manifest


def _export_zip_manifest_json(*, chunk_size: int, chunk_overlap: int) -> bytes:
    with zipfile.ZipFile(
        io.BytesIO(_export_zip(chunk_size=chunk_size, chunk_overlap=chunk_overlap))
    ) as zf:
        return zf.read("manifest.json")


def test_export_writes_the_values_a_legacy_row_is_chunked_with():
    """A row written before the bounds exports as importable values instead of
    a package the import would refuse."""
    kb = SimpleNamespace(
        id=uuid.uuid4(),
        name="Legacy",
        description=None,
        tags=[],
        embedding_model="text-embedding-3-large",
        chunk_size=512,
        chunk_overlap=512,
    )
    manifest = kb_mod._build_kb_export_manifest(
        kb=kb,
        export_id=uuid.uuid4(),
        part_number=1,
        total_parts=1,
        documents=[],
        sync_sources=[],
    )
    exported = manifest.knowledge_base
    assert (exported.chunk_size, exported.chunk_overlap) == normalize_chunk_params(512, 512)
    assert chunk_params_problem(exported.chunk_size, exported.chunk_overlap) is None
