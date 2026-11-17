"""The General Assistant forwards its stored provider reference.

The GA's own chat model gained a provider-config reference, and it is only worth
having if ``run_general_chat`` hands it to the router: a model id can be
advertised by several configs, so passing the model alone lets resolution pick
the newest one instead of the config the admin chose.

These drive ``run_general_chat`` as far as LLM resolution and stop there — the
turn itself is not the subject. The reuse-branch test is the subtle one: the
fast path that skips resolution keys on the GA's overrides being absent, and a
reference is now one of them.
"""

import uuid
from types import SimpleNamespace

import pytest


class _StopError(Exception):
    """Raised from the patched router once the call has been recorded."""


def _drive(monkeypatch, ga_row, *, prebuilt=None):
    """Run ``run_general_chat`` up to LLM resolution; return the router kwargs."""
    from app.services import general_chat

    seen: dict = {}

    async def _no_quota(*_args, **_kwargs):
        return None

    async def _get_llm(_db, _user_id, **kwargs):
        seen.update(kwargs)
        raise _StopError

    class _Db:
        async def execute(self, _statement):
            # Both the GA config and the user row are read this way; the user
            # row only needs to be absent for resolution to be reached.
            return SimpleNamespace(scalar_one_or_none=lambda: _Db._next.pop(0))

        _next: list = []

    db = _Db()
    _Db._next = [ga_row, None]

    monkeypatch.setattr("app.services.agent_chat.enforce_agentic_quota", _no_quota)
    monkeypatch.setattr("app.llm.router.get_llm_for_user", _get_llm)

    async def _platform_wait_policy(_db):
        from app.tools.wait_policy import WaitPolicy

        return WaitPolicy()

    monkeypatch.setattr("app.services.wait_settings.load_platform_wait_policy", _platform_wait_policy)

    async def _run():
        await general_chat.run_general_chat(
            db,
            str(uuid.uuid4()),
            [],
            "hello",
            prebuilt_llm=prebuilt,
        )

    return seen, _run


def _ga(**overrides):
    row = SimpleNamespace(
        llm_provider=None,
        llm_provider_id=None,
        llm_model=None,
        linked_agent_id=None,
        system_prompt="p",
        knowledge_base_ids=[],
        mcp_server_ids=[],
        custom_tool_ids=[],
        builtin_tool_ids=[],
        integration_bindings={},
        handover_agent_ids=[],
        handover_types={},
        available_channel_types=["web"],
        max_iterations=None,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


@pytest.mark.asyncio
async def test_ga_chat_forwards_its_stored_reference(monkeypatch):
    reference = uuid.uuid4()
    seen, run = _drive(
        monkeypatch,
        _ga(
            llm_provider="litellm",
            llm_provider_id=reference,
            llm_model="claude-sonnet-4-6",
        ),
    )
    with pytest.raises(_StopError):
        await run()
    assert seen["provider_id_override"] == reference
    assert seen["model_override"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_ga_chat_sends_no_reference_when_it_has_none(monkeypatch):
    """FR-006: a GA row saved before this feature resolves exactly as before."""
    seen, run = _drive(
        monkeypatch, _ga(llm_provider="anthropic", llm_model="claude-sonnet-4-6")
    )
    with pytest.raises(_StopError):
        await run()
    assert seen["provider_id_override"] is None
    assert seen["provider_override"] == "anthropic"


@pytest.mark.asyncio
async def test_reference_only_ga_still_resolves_rather_than_reusing(monkeypatch):
    """The LLM-reuse fast path skips resolution when the GA pins no provider and
    no model. A stored reference is a third override — without it in the
    condition, a GA configured with only a reference would silently keep the
    plain user LLM and the reference would never be honoured."""
    reference = uuid.uuid4()
    prebuilt = (SimpleNamespace(provider_name="reused"), "reused-model")
    seen, run = _drive(
        monkeypatch, _ga(llm_provider_id=reference), prebuilt=prebuilt
    )
    with pytest.raises(_StopError):  # resolution reached, not short-circuited
        await run()
    # ...but the reference itself is not forwarded — see the test below.
    assert seen["provider_id_override"] is None


@pytest.mark.asyncio
async def test_reference_without_its_model_is_not_forwarded(monkeypatch):
    """A reference speaks only for the model it was saved with. The endpoint
    writes ``llm_model`` and ``llm_provider_id`` independently, so a PUT clearing
    the model can leave the reference behind; forwarding it would pin whatever
    model resolution picks next — an agent default, or the user's own preference
    — to the GA config's key and billing."""
    seen, run = _drive(
        monkeypatch, _ga(llm_provider_id=uuid.uuid4(), llm_model=None)
    )
    with pytest.raises(_StopError):
        await run()
    assert seen["provider_id_override"] is None
    assert seen["model_override"] is None


@pytest.mark.asyncio
async def test_reference_with_its_model_is_forwarded(monkeypatch):
    """The gate above must not disarm the feature: with the model it was saved
    with, the reference is what pins the call to the admin's chosen config."""
    reference = uuid.uuid4()
    seen, run = _drive(
        monkeypatch,
        _ga(llm_provider_id=reference, llm_model="claude-sonnet-4-6"),
    )
    with pytest.raises(_StopError):
        await run()
    assert seen["provider_id_override"] == reference
    assert seen["model_override"] == "claude-sonnet-4-6"
