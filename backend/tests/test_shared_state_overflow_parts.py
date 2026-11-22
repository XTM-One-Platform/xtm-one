"""An overflow key at the hard limit rolls the spill to its next part.

Gap shared-state-retention-cannot-roll-open-lists: a retention's
``overflow_key`` was one fixed key; once it reached the hard limit every
further spill was refused, so the source list could no longer shrink. The
spill now goes to ``BASE:part-2`` (then 3, ...) and the pointers the write
made name that part, so a read still resolves them.
"""

import uuid
from typing import Any

import pytest

import app.services.shared_state as shared_state_service
from app.core.config import settings
from app.services.shared_state import (
    OVERFLOW_POINTER,
    overflow_part_key,
    repoint_spilled,
    resolve_overflow_pointers,
)
from tests.test_shared_state_list_ops import COMPANY_AGENT, _Session, _state


def _sessions(monkeypatch: pytest.MonkeyPatch, *sessions: Any) -> None:
    queue = list(sessions)
    monkeypatch.setattr("app.core.database.async_session", lambda: queue.pop(0))


def test_the_first_part_is_the_base_key_then_numbered_parts() -> None:
    assert overflow_part_key("o", 1) == "o"
    assert overflow_part_key("o", 2) == "o:part-2"
    assert overflow_part_key("o", 3) == "o:part-3"


def test_repoint_changes_only_the_pointers_this_write_made() -> None:
    value: dict[str, Any] = {
        "gaps": [
            {"id": "a", "evidence": f"{OVERFLOW_POINTER}o/gaps/a#evidence", "note": f"{OVERFLOW_POINTER}o/gaps/a#note"},
            {"id": "b", "evidence": f"{OVERFLOW_POINTER}o/gaps/b#evidence"},
        ]
    }
    count = repoint_spilled(value, {"gaps": {"a": {"evidence": "x"}}}, "o", "o:part-2", "id")
    assert count == 1
    assert value["gaps"][0]["evidence"] == f"{OVERFLOW_POINTER}o:part-2/gaps/a#evidence"
    assert value["gaps"][0]["note"] == f"{OVERFLOW_POINTER}o/gaps/a#note"
    assert value["gaps"][1]["evidence"] == f"{OVERFLOW_POINTER}o/gaps/b#evidence"


def test_a_repointed_field_resolves_from_its_part() -> None:
    value = {"gaps": [{"id": "a", "evidence": f"{OVERFLOW_POINTER}o:part-2/gaps/a#evidence"}]}
    resolved, done, missing = resolve_overflow_pointers(value, {"o:part-2": {"gaps": {"a": {"evidence": "long"}}}})
    assert (done, missing) == (1, 0)
    assert isinstance(resolved, dict)
    assert resolved["gaps"][0]["evidence"] == "long"


@pytest.mark.asyncio
async def test_a_full_overflow_rolls_the_spill_to_its_next_part(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "shared_state_value_soft_limit_chars", 0)
    monkeypatch.setattr(settings, "shared_state_value_hard_limit_chars", 2000)
    monkeypatch.setattr(settings, "shared_state_list_max_items", 0)
    source = _state({"gaps": [{"id": "old", "state": "queued"}]})
    source.retention = {"max_item_chars": 300, "overflow_key": "gaps-overflow"}
    full = _state({"gaps": {"x": {"evidence": "f" * 1900}}})
    full.key = "gaps-overflow"
    part_two = _state({})
    part_two.key = "gaps-overflow:part-2"
    sessions = [_Session(source), _Session(full), _Session(part_two)]  # type: ignore[no-untyped-call]
    _sessions(monkeypatch, *sessions)
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="harness-gaps",
        append={"gaps": [{"id": "new", "state": "queued", "evidence": "e" * 500}]},
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert out["value"]["gaps"][1]["evidence"] == f"{OVERFLOW_POINTER}gaps-overflow:part-2/gaps/new#evidence"
    assert part_two.value == {"gaps": {"new": {"evidence": "e" * 500}}}
    assert full.value == {"gaps": {"x": {"evidence": "f" * 1900}}}
    assert out["overflow_rolled_to"] == "gaps-overflow:part-2"
    assert out["spilled_to"] == "gaps-overflow:part-2"
