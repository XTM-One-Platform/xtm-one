"""The refusal shape when AI is disabled (FR-009/FR-010).

Both gates must emit the *same* payload. They are attached at ~83 route call
sites between them, so a divergence here is a divergence across the whole
write and execution surface.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import deps
from app.services.ai_state import AI_DISABLED_CODE


class _Session:
    async def execute(self, _query):  # pragma: no cover - never reached
        raise AssertionError("the AI gate must short-circuit before this")


async def _never(_db):  # pragma: no cover - asserts ordering
    raise AssertionError("the licence check must not run when AI is disabled")


@pytest.mark.parametrize("gate", ["require_agentic_ai", "require_chat_enabled"])
@pytest.mark.asyncio
async def test_both_gates_refuse_with_the_same_payload(monkeypatch, gate):
    monkeypatch.setattr("app.services.ai_state.is_ai_enabled", _false)
    with pytest.raises(HTTPException) as caught:
        await getattr(deps, gate)(db=_Session())

    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == AI_DISABLED_CODE
    assert caught.value.detail["message"], "a client that only displays it needs text"


@pytest.mark.parametrize(
    ("gate", "licence_check"),
    [
        ("require_agentic_ai", "app.api.v1.platform.is_agentic_ai_enabled"),
        ("require_chat_enabled", "app.api.v1.platform.is_chat_enabled"),
    ],
)
@pytest.mark.asyncio
async def test_ai_disabled_is_checked_before_the_licence(
    monkeypatch, gate, licence_check
):
    """Ordering is the substance: "an admin switched this off" is the more
    actionable answer than "buy a licence", and it is deterministic."""
    monkeypatch.setattr("app.services.ai_state.is_ai_enabled", _false)
    monkeypatch.setattr(licence_check, _never)

    with pytest.raises(HTTPException) as caught:
        await getattr(deps, gate)(db=_Session())

    assert caught.value.detail["code"] == AI_DISABLED_CODE


@pytest.mark.parametrize("gate", ["require_agentic_ai", "require_chat_enabled"])
@pytest.mark.asyncio
async def test_the_licence_gate_still_applies_when_ai_is_enabled(monkeypatch, gate):
    """This feature must not accidentally un-gate the licence."""
    monkeypatch.setattr("app.services.ai_state.is_ai_enabled", _true)
    check = (
        "app.api.v1.platform.is_agentic_ai_enabled"
        if gate == "require_agentic_ai"
        else "app.api.v1.platform.is_chat_enabled"
    )
    monkeypatch.setattr(check, _false)
    monkeypatch.setattr("app.api.v1.platform.get_deployment_edition", _edition)

    with pytest.raises(HTTPException) as caught:
        await getattr(deps, gate)(db=_Session())

    assert caught.value.status_code == 403
    detail = caught.value.detail
    code = detail.get("code") if isinstance(detail, dict) else None
    assert code != AI_DISABLED_CODE, "a licence refusal must not claim AI is off"


async def _false(_db):
    return False


async def _true(_db):
    return True


async def _edition(_db):
    return "community"
