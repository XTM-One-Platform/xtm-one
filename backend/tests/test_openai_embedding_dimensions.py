"""Tests for OpenAI embedding-dimension handling.

Regression coverage for the "permanently stale KB" bug: ``OpenAIProvider.embed``
must forward the platform-configured ``embedding_dimensions`` to the OpenAI API
for the ``text-embedding-3`` family (Matryoshka shortening). Previously it never
did, so an admin who configured a non-native dimension got native-sized vectors
stored — leaving the knowledge base flagged permanently "stale" and making
"Re-embed all" loop forever without converging.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from app.llm.openai_provider import OpenAIProvider


class _FakeEmbeddings:
    def __init__(self, *, reject_dimensions: bool = False):
        self.calls: list[dict] = []
        self._reject_dimensions = reject_dimensions

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._reject_dimensions and "dimensions" in kwargs:
            raise Exception("This model does not support the 'dimensions' parameter")
        return SimpleNamespace(data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3])])


def _provider_with_fake(reject_dimensions: bool = False) -> tuple[OpenAIProvider, _FakeEmbeddings]:
    provider = OpenAIProvider(api_key="sk-dummy")
    fake = _FakeEmbeddings(reject_dimensions=reject_dimensions)
    provider.client = cast(Any, SimpleNamespace(embeddings=fake))
    return provider, fake


def test_openai_embed_passes_dimensions_for_text_embedding_3():
    provider, fake = _provider_with_fake()
    provider.embedding_dimensions = 1536

    asyncio.run(provider.embed("hello", model="text-embedding-3-large"))

    assert len(fake.calls) == 1
    assert fake.calls[0].get("dimensions") == 1536, (
        "embed() must forward the configured dimension for text-embedding-3-*"
    )


def test_openai_embed_omits_dimensions_when_not_configured():
    provider, fake = _provider_with_fake()
    provider.embedding_dimensions = None  # default — no explicit config

    asyncio.run(provider.embed("hello", model="text-embedding-3-large"))

    assert len(fake.calls) == 1
    assert "dimensions" not in fake.calls[0], (
        "without an explicit config, embed() must use the model's native size"
    )


def test_openai_embed_omits_dimensions_for_unsupported_model():
    provider, fake = _provider_with_fake()
    provider.embedding_dimensions = 768

    # ada-002 does not support the dimensions parameter; it must not be sent.
    asyncio.run(provider.embed("hello", model="text-embedding-ada-002"))

    assert len(fake.calls) == 1
    assert "dimensions" not in fake.calls[0]


def test_openai_embed_falls_back_when_dimensions_rejected():
    provider, fake = _provider_with_fake(reject_dimensions=True)
    provider.embedding_dimensions = 256

    vector = asyncio.run(provider.embed("hello", model="text-embedding-3-small"))

    assert vector == [0.1, 0.2, 0.3]
    assert len(fake.calls) == 2, "expected a dimensions call then a fallback retry"
    assert fake.calls[0].get("dimensions") == 256
    assert "dimensions" not in fake.calls[1]
