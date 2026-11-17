"""Tests for the custom OpenAI-compatible provider."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from app.llm.custom_openai_provider import CustomOpenAIProvider


def test_custom_openai_keeps_base_url_without_v1_suffix():
    provider = CustomOpenAIProvider(api_key="dummy||http://infinity:7997")
    assert provider.base_url == "http://infinity:7997"


def test_custom_openai_preserves_explicit_v1_base_url():
    provider = CustomOpenAIProvider(api_key="dummy||http://localhost:8000/v1")
    assert provider.base_url == "http://localhost:8000/v1"


def test_custom_openai_embed_uses_requested_model():
    """embed() passes ``dimensions`` to activate Matryoshka on supported models."""
    from app.models.embedding import EMBEDDING_DIMENSIONS

    provider = CustomOpenAIProvider(api_key="dummy||http://localhost:1234")
    calls: list[dict] = []
    fake_vector = [0.1] * EMBEDDING_DIMENSIONS

    class FakeEmbeddings:
        async def create(self, *, input: str, model: str, dimensions: int | None = None):
            calls.append({"input": input, "model": model, "dimensions": dimensions})
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=fake_vector)]
            )

    provider.client = cast(
        Any,
        SimpleNamespace(embeddings=FakeEmbeddings(), api_key="dummy"),
    )

    vector = asyncio.run(
        provider.embed("hello from test", model="text-embedding-nomic-embed-text-v1.5")
    )

    assert vector == fake_vector
    assert len(vector) == EMBEDDING_DIMENSIONS
    assert len(calls) == 1
    assert calls[0]["input"] == "hello from test"
    assert calls[0]["model"] == "text-embedding-nomic-embed-text-v1.5"
    assert calls[0]["dimensions"] == EMBEDDING_DIMENSIONS, (
        "embed() must pass dimensions=EMBEDDING_DIMENSIONS to activate Matryoshka"
    )


def test_custom_openai_embed_falls_back_when_dimensions_rejected():
    """embed() retries without ``dimensions`` when the endpoint rejects it."""
    from app.models.embedding import EMBEDDING_DIMENSIONS

    provider = CustomOpenAIProvider(api_key="dummy||http://localhost:1234")
    fake_vector = [0.1] * EMBEDDING_DIMENSIONS
    call_count = 0

    class FakeEmbeddingsRejectsDimensions:
        async def create(self, *, input: str, model: str, dimensions: int | None = None):
            nonlocal call_count
            call_count += 1
            if dimensions is not None:
                raise Exception("unknown parameter: dimensions")
            return SimpleNamespace(data=[SimpleNamespace(embedding=fake_vector)])

    provider.client = cast(
        Any,
        SimpleNamespace(embeddings=FakeEmbeddingsRejectsDimensions(), api_key="dummy"),
    )

    vector = asyncio.run(
        provider.embed("hello fallback", model="old-embed-model")
    )

    assert vector == fake_vector
    assert call_count == 2, "Expected first call with dimensions, then retry without"


def test_custom_openai_embed_requires_explicit_model():
    provider = CustomOpenAIProvider(api_key="dummy||http://localhost:1234")

    try:
        asyncio.run(provider.embed("hello from test"))
    except ValueError as exc:
        assert "explicit embedding model" in str(exc)
    else:
        raise AssertionError("Expected embed() to require an explicit model")


def test_custom_openai_embed_rejects_wrong_dimensions():
    """embed() must raise ValueError when the returned vector has wrong dims."""
    from app.models.embedding import EMBEDDING_DIMENSIONS

    provider = CustomOpenAIProvider(api_key="dummy||http://localhost:1234")

    class FakeEmbeddingsWrongDim:
        async def create(self, *, input: str, model: str, dimensions: int | None = None):
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3])]  # only 3 dims
            )

    provider.client = cast(
        Any,
        SimpleNamespace(embeddings=FakeEmbeddingsWrongDim(), api_key="dummy"),
    )

    try:
        asyncio.run(
            provider.embed("hello", model="small-model")
        )
    except ValueError as exc:
        msg = str(exc)
        assert "3" in msg  # returned 3 dims
        assert str(EMBEDDING_DIMENSIONS) in msg
    else:
        raise AssertionError("Expected embed() to raise ValueError for wrong dimensions")


def test_custom_openai_list_embedding_models_filters_candidates(monkeypatch):
    provider = CustomOpenAIProvider(api_key="dummy||http://localhost:1234")

    async def fake_list_model_ids():
        return [
            "google/gemma-3-4b",
            "text-embedding-nomic-embed-text-v1.5",
            "bge-small-en-v1.5",
        ]

    monkeypatch.setattr(provider, "_list_model_ids", fake_list_model_ids)

    models = asyncio.run(provider.list_embedding_models())

    assert models == [
        "text-embedding-nomic-embed-text-v1.5",
        "bge-small-en-v1.5",
    ]


def test_custom_openai_probe_supported_dimensions_picks_first_success():
    from app.models.embedding import ALLOWED_EMBEDDING_DIMENSIONS

    provider = CustomOpenAIProvider(api_key="dummy||http://localhost:1234")
    calls: list[int | None] = []

    class FakeEmbeddingsProbe:
        async def create(self, *, input: str, model: str, dimensions: int | None = None):
            calls.append(dimensions)
            if dimensions in (768, 1024):
                raise Exception("unsupported dimensions")
            if dimensions is None:
                raise Exception("dimensions is required in this test path")
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.1] * dimensions)]
            )

    provider.client = cast(
        Any,
        SimpleNamespace(embeddings=FakeEmbeddingsProbe(), api_key="dummy"),
    )

    vector, requested = asyncio.run(
        provider.probe_supported_dimensions(
            "hello probe",
            model="text-embedding-nomic-embed-text-v1.5",
            candidates=ALLOWED_EMBEDDING_DIMENSIONS,
        )
    )

    expected = ALLOWED_EMBEDDING_DIMENSIONS[2]
    assert requested == expected
    assert len(vector) == expected
    assert calls == list(ALLOWED_EMBEDDING_DIMENSIONS[:3])


def test_custom_openai_probe_supported_dimensions_falls_back_without_dimensions():
    from app.models.embedding import ALLOWED_EMBEDDING_DIMENSIONS, EMBEDDING_DIMENSIONS

    provider = CustomOpenAIProvider(api_key="dummy||http://localhost:1234")
    calls: list[int | None] = []

    class FakeEmbeddingsProbeFallback:
        async def create(self, *, input: str, model: str, dimensions: int | None = None):
            calls.append(dimensions)
            if dimensions is not None:
                raise Exception("unknown parameter: dimensions")
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.1] * EMBEDDING_DIMENSIONS)]
            )

    provider.client = cast(
        Any,
        SimpleNamespace(embeddings=FakeEmbeddingsProbeFallback(), api_key="dummy"),
    )

    vector, requested = asyncio.run(
        provider.probe_supported_dimensions(
            "hello probe",
            model="legacy-embedding-model",
            candidates=ALLOWED_EMBEDDING_DIMENSIONS,
        )
    )

    assert requested is None
    assert len(vector) == EMBEDDING_DIMENSIONS
    assert calls == [*ALLOWED_EMBEDDING_DIMENSIONS, None]
