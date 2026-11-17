"""Regression tests for OpenAI image model remapping.

Covers:
- Deprecated model names (``dall-e-3``, ``dall-e-2``) are transparently
  remapped to ``gpt-image-1`` so existing platform configurations continue
  to work after OpenAI retired those model IDs.
- The remap is logged at WARNING level so ops can audit it in production.
- The ``response_format`` parameter (legal for DALL-E, rejected by GPT
  image models) is gated to the resolved model so a remap to
  ``gpt-image-1`` doesn't carry over a parameter the new endpoint
  rejects.
- ``list_image_models()`` filters deprecated IDs out of both the live API
  result and the offline fallback so the admin UI never offers them, and
  returns the default model when the live filter would otherwise yield
  an empty list (so settings validation doesn't reject a reachable
  OpenAI provider as image-unsupported).
"""

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.llm.openai_provider import OpenAIProvider


def _make_provider() -> OpenAIProvider:
    return OpenAIProvider(api_key="test-key")


def _fake_image_response() -> SimpleNamespace:
    """Build a minimal SDK-shaped response carrying an arbitrary byte payload.

    The 16 bytes here start with the PNG magic so anyone scanning the
    fixture sees the intent, but the payload is **not** a valid PNG
    image — it's just enough for ``base64.b64decode(...)`` in
    ``generate_image`` to return bytes without raising. Tests that
    actually need to decode the image must construct a real PNG.
    """
    raw_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    b64_payload = base64.b64encode(raw_bytes).decode()
    return SimpleNamespace(data=[SimpleNamespace(b64_json=b64_payload)])


@pytest.fixture
def image_provider_with_mock_generate():
    """Yield a provider plus the mock that intercepts ``images.generate``.

    Centralises the boilerplate every ``generate_image`` test needs (fake
    payload, ``patch.object`` on the SDK call) so adding new remap cases
    later (e.g. when ``gpt-image-1`` itself is retired) is a one-liner.

    Also resets the *process-wide* deprecation-WARNING dedup set
    (``OpenAIProvider._logged_deprecated_models``) so every test starts
    from a clean slate. Without this reset the first test that exercises
    a deprecated model would "consume" the WARNING and any later test
    asserting the WARNING was logged would see only DEBUG records.
    """
    OpenAIProvider._logged_deprecated_models.clear()
    provider = _make_provider()
    fake_response = _fake_image_response()
    with patch.object(
        provider.client.images, "generate", new_callable=AsyncMock
    ) as mock_generate:
        mock_generate.return_value = fake_response
        yield provider, mock_generate


def _called_kwargs(mock_generate: AsyncMock) -> dict:
    """Return the kwargs captured on the most recent ``images.generate`` call."""
    return dict(mock_generate.call_args.kwargs)


# ---------------------------------------------------------------------------
# Deprecated-model remapping in generate_image
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_image_remaps_dall_e_3_to_gpt_image_1(
    image_provider_with_mock_generate,
):
    """generate_image must silently substitute gpt-image-1 for dall-e-3,
    and the remapped call must NOT carry the legacy ``response_format``
    parameter (which the GPT image API rejects)."""
    provider, mock_generate = image_provider_with_mock_generate
    await provider.generate_image("a cat", model="dall-e-3")
    kwargs = _called_kwargs(mock_generate)
    assert kwargs["model"] == "gpt-image-1"
    # After remap, the legacy DALL-E parameter must be stripped.
    assert "response_format" not in kwargs


@pytest.mark.asyncio
async def test_generate_image_remaps_dall_e_2_to_gpt_image_1(
    image_provider_with_mock_generate,
):
    """generate_image must silently substitute gpt-image-1 for dall-e-2,
    and must NOT propagate the legacy ``response_format`` parameter."""
    provider, mock_generate = image_provider_with_mock_generate
    await provider.generate_image("a dog", model="dall-e-2")
    kwargs = _called_kwargs(mock_generate)
    assert kwargs["model"] == "gpt-image-1"
    assert "response_format" not in kwargs


@pytest.mark.asyncio
async def test_generate_image_passes_through_current_model_unchanged(
    image_provider_with_mock_generate,
):
    """generate_image must not remap gpt-image-1 (or any current model),
    and must NOT send ``response_format`` for the GPT image model."""
    provider, mock_generate = image_provider_with_mock_generate
    await provider.generate_image("a fish", model="gpt-image-1")
    kwargs = _called_kwargs(mock_generate)
    assert kwargs["model"] == "gpt-image-1"
    assert "response_format" not in kwargs


@pytest.mark.asyncio
async def test_generate_image_defaults_to_gpt_image_1_when_no_model_given(
    image_provider_with_mock_generate,
):
    """generate_image must default to gpt-image-1 when model is None,
    and must NOT include ``response_format`` for the GPT image model."""
    provider, mock_generate = image_provider_with_mock_generate
    await provider.generate_image("a bird", model=None)
    kwargs = _called_kwargs(mock_generate)
    assert kwargs["model"] == "gpt-image-1"
    assert "response_format" not in kwargs


# ---------------------------------------------------------------------------
# Size remapping after model remap (dall-e-3 → gpt-image-1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_size,expected_size",
    [
        ("1792x1024", "1536x1024"),  # landscape — closest gpt-image-1 equivalent
        ("1024x1792", "1024x1536"),  # portrait  — closest gpt-image-1 equivalent
    ],
)
async def test_generate_image_remaps_incompatible_dall_e_3_sizes(
    image_provider_with_mock_generate, legacy_size, expected_size
):
    """When dall-e-3 is remapped to gpt-image-1, sizes that are valid for
    dall-e-3 but not for gpt-image-1 must also be remapped to the closest
    supported equivalent; otherwise the OpenAI API returns 400."""
    provider, mock_generate = image_provider_with_mock_generate
    await provider.generate_image("a mountain", model="dall-e-3", size=legacy_size)
    kwargs = _called_kwargs(mock_generate)
    assert kwargs["model"] == "gpt-image-1"
    assert kwargs["size"] == expected_size


@pytest.mark.asyncio
async def test_generate_image_keeps_compatible_size_when_remapping_model(
    image_provider_with_mock_generate,
):
    """A size that is already valid for gpt-image-1 must be passed through
    unchanged even when the model itself is remapped from dall-e-3."""
    provider, mock_generate = image_provider_with_mock_generate
    await provider.generate_image("a lake", model="dall-e-3", size="1024x1024")
    kwargs = _called_kwargs(mock_generate)
    assert kwargs["model"] == "gpt-image-1"
    assert kwargs["size"] == "1024x1024"


@pytest.mark.asyncio
async def test_generate_image_falls_back_to_auto_for_unrecognised_size_after_remap(
    image_provider_with_mock_generate,
):
    """Any size that isn't in the explicit remap table and isn't valid for
    gpt-image-1 must fall back to ``'auto'`` rather than being forwarded
    (which would result in a 400 from the OpenAI API)."""
    provider, mock_generate = image_provider_with_mock_generate
    await provider.generate_image("a forest", model="dall-e-3", size="256x256")
    kwargs = _called_kwargs(mock_generate)
    assert kwargs["model"] == "gpt-image-1"
    assert kwargs["size"] == "auto"


@pytest.mark.asyncio
async def test_generate_image_logs_warning_when_remapping_incompatible_size(
    image_provider_with_mock_generate, caplog
):
    """A WARNING must be logged when the size is remapped so ops can audit it."""
    provider, _ = image_provider_with_mock_generate
    with caplog.at_level("WARNING", logger="app.llm.openai_provider"):
        await provider.generate_image("a river", model="dall-e-3", size="1792x1024")
    assert any(
        "1792x1024" in rec.getMessage() and "1536x1024" in rec.getMessage()
        for rec in caplog.records
    ), "Expected a WARNING line containing both the old and new size."


@pytest.mark.asyncio
async def test_generate_image_does_not_remap_size_for_non_deprecated_model(
    image_provider_with_mock_generate,
):
    """Size remapping must only trigger when the model itself was remapped.
    A direct call to gpt-image-1 with any size must pass it through as-is."""
    provider, mock_generate = image_provider_with_mock_generate
    await provider.generate_image("a desert", model="gpt-image-1", size="1792x1024")
    kwargs = _called_kwargs(mock_generate)
    assert kwargs["size"] == "1792x1024"  # passed through unchanged


# ---------------------------------------------------------------------------
# response_format gating — legal for DALL-E, rejected by GPT image models
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_image_keeps_response_format_for_legacy_dall_e_models(
    monkeypatch, image_provider_with_mock_generate
):
    """For any DALL-E model that is NOT in the deprecation list (i.e. a
    hypothetical future DALL-E variant), ``response_format`` should still
    be sent — the parameter is legal for the DALL-E API surface."""
    provider, mock_generate = image_provider_with_mock_generate
    # Pretend dall-e-future is a real, non-deprecated DALL-E model.
    monkeypatch.setattr(OpenAIProvider, "_DEPRECATED_IMAGE_MODELS", {})
    await provider.generate_image("a hippo", model="dall-e-future")
    kwargs = _called_kwargs(mock_generate)
    assert kwargs["model"] == "dall-e-future"
    assert kwargs["response_format"] == "b64_json"


@pytest.mark.asyncio
async def test_generate_image_passes_size_and_prompt_for_all_models(
    image_provider_with_mock_generate,
):
    """Shared kwargs (``prompt``, ``size``, ``n``) must always be sent
    regardless of the model branch."""
    provider, mock_generate = image_provider_with_mock_generate
    await provider.generate_image("a giraffe", model="gpt-image-1", size="512x512")
    kwargs = _called_kwargs(mock_generate)
    assert kwargs["prompt"] == "a giraffe"
    assert kwargs["size"] == "512x512"
    assert kwargs["n"] == 1


@pytest.mark.asyncio
async def test_generate_image_logs_warning_when_remapping_deprecated_model(
    image_provider_with_mock_generate, caplog
):
    """A WARNING log line must surface the remap so ops teams can audit it."""
    provider, _ = image_provider_with_mock_generate
    with caplog.at_level("WARNING", logger="app.llm.openai_provider"):
        await provider.generate_image("a turtle", model="dall-e-3")
    assert any(
        "dall-e-3" in rec.getMessage() and "gpt-image-1" in rec.getMessage()
        for rec in caplog.records
    ), "Expected a WARNING line containing both the old and new model IDs."


@pytest.mark.asyncio
async def test_generate_image_does_not_log_warning_for_current_model(
    image_provider_with_mock_generate, caplog
):
    """No remap log should fire when the configured model is already current."""
    provider, _ = image_provider_with_mock_generate
    with caplog.at_level("WARNING", logger="app.llm.openai_provider"):
        await provider.generate_image("a panda", model="gpt-image-1")
    assert not any(
        "deprecated" in rec.getMessage().lower() for rec in caplog.records
    ), "No deprecation warning expected for an active model."


# ---------------------------------------------------------------------------
# list_image_models — deprecated IDs filtered everywhere
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_image_models_fallback_excludes_dall_e_3():
    """When the OpenAI models API raises, the fallback must not include
    dall-e-3 since that model no longer exists on the OpenAI API."""
    provider = _make_provider()

    with patch.object(
        provider.client.models,
        "list",
        new_callable=AsyncMock,
        side_effect=Exception("network error"),
    ):
        models = await provider.list_image_models()

    assert "dall-e-3" not in models
    assert "gpt-image-1" in models


@pytest.mark.asyncio
async def test_list_image_models_filters_deprecated_from_live_response():
    """Deprecated IDs returned by the live OpenAI ``models`` endpoint must
    be stripped so the admin UI never offers a model that ``generate_image``
    would have to silently remap."""
    provider = _make_provider()

    fake_models = [
        SimpleNamespace(id="gpt-4o"),
        SimpleNamespace(id="gpt-image-1"),
        SimpleNamespace(id="dall-e-3"),
        SimpleNamespace(id="dall-e-2"),
        SimpleNamespace(id="whisper-1"),
    ]
    fake_list_response = SimpleNamespace(data=fake_models)

    with patch.object(
        provider.client.models,
        "list",
        new_callable=AsyncMock,
        return_value=fake_list_response,
    ):
        models = await provider.list_image_models()

    # Non-image models are filtered by prefix.
    assert "gpt-4o" not in models
    assert "whisper-1" not in models
    # Deprecated image models are filtered by the explicit deprecation list.
    assert "dall-e-3" not in models
    assert "dall-e-2" not in models
    # Current image model is kept.
    assert "gpt-image-1" in models
    # Sorted reverse for UI consistency.
    assert models == sorted(models, reverse=True)


@pytest.mark.asyncio
async def test_list_image_models_falls_back_when_live_response_yields_no_image_models():
    """If the live ``/models`` response contains only chat / embedding /
    audio models — or only deprecated image IDs — the filter would
    otherwise yield ``[]`` and settings validation would incorrectly
    reject a reachable OpenAI provider as image-unsupported. The fallback
    must return the default image model so ``generate_image`` can still
    serve requests via the remap path."""
    provider = _make_provider()

    fake_models = [
        SimpleNamespace(id="gpt-4o"),
        SimpleNamespace(id="dall-e-3"),  # filtered as deprecated
        SimpleNamespace(id="dall-e-2"),  # filtered as deprecated
        SimpleNamespace(id="whisper-1"),
    ]
    fake_list_response = SimpleNamespace(data=fake_models)

    with patch.object(
        provider.client.models,
        "list",
        new_callable=AsyncMock,
        return_value=fake_list_response,
    ):
        models = await provider.list_image_models()

    assert models == ["gpt-image-1"]


@pytest.mark.asyncio
async def test_list_image_models_falls_back_when_live_response_has_no_image_prefix_match():
    """Same fallback when the response contains zero image-prefix entries
    at all (not even deprecated ones)."""
    provider = _make_provider()

    fake_list_response = SimpleNamespace(
        data=[SimpleNamespace(id="gpt-4o"), SimpleNamespace(id="whisper-1")]
    )

    with patch.object(
        provider.client.models,
        "list",
        new_callable=AsyncMock,
        return_value=fake_list_response,
    ):
        models = await provider.list_image_models()

    assert models == ["gpt-image-1"]
