# SPDX-License-Identifier: Apache-2.0

"""#2370: a decision filed on a CTEM case leads with "Open the case".

The platform derives the case from the gate key it defines (or
``payload.case_id``), puts "Open the case" first on every card surface, drops
an agent link to the same case, and exposes the case on the item read only
when the reader sees it. Pure, no database.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import settings
from app.models.decision_hooks import work_item_opening
from app.services import decision_answer_links as links
from app.services import work_item_ctem_case as ctem_case
from app.services.channel_notices import notice
from app.services.decision_ask import card_link_label, card_links

CASE = uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f")
ALICE = uuid.UUID("11111111-2222-4333-8444-555555555555")
PR = "https://github.com/OpenCTI-Platform/opencti/pull/20153"


@pytest.fixture(autouse=True)
def _urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "secret_key", "ctem-case-link-test-secret-0123456789abcdef")
    monkeypatch.setattr(settings, "decision_link_previous_secrets", "")
    monkeypatch.setattr(settings, "frontend_url", "https://one.example.test/")


# --- The key-to-case helper ---------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        f"ctem:review:{CASE}",
        f"ctem:validation:{CASE}",
        f"ctem:remediation:{CASE}",
        f"ctem:f1:launch:{CASE}",
        f"ctem:f1:case-mobilize:{CASE}",
        f"ctem:f1:retest:{CASE}",
        f"ctem:f1:case:{CASE}",
        f"ctem:f2:validate:{CASE}",
        f"ctem:f2:mobilize:{CASE}",
        f"ctem:f2:retest:{CASE}",
        f"ctem:f2:case:{CASE}",
    ],
)
def test_every_ctem_gate_key_names_its_case(key: str) -> None:
    assert ctem_case.case_id_of(key) == CASE


@pytest.mark.parametrize(
    "key",
    [
        None,
        "",
        "ctem:f1:state",
        "ctem:f4:state",
        f"dev:harness-gap-{CASE}",
        f"ctem:f3:case:{CASE}",
        "ctem:review:not-a-uuid",
    ],
)
def test_any_other_key_names_no_case(key: str | None) -> None:
    assert ctem_case.case_id_of(key) is None


def test_payload_case_id_wins_and_a_bad_one_falls_back_to_the_key() -> None:
    other = uuid.uuid4()
    assert ctem_case.case_id_of("anything", {"case_id": str(CASE)}) == CASE
    assert ctem_case.case_id_of(f"ctem:review:{other}", {"case_id": str(CASE)}) == CASE
    assert ctem_case.case_id_of(f"ctem:review:{CASE}", {"case_id": "nope"}) == CASE


# --- The ask every card surface reads -----------------------------------------


def _ask(**over: Any) -> dict[str, Any]:
    ask: dict[str, Any] = {
        "question": "Validate the fix on the case?",
        "options": [{"key": "A", "label": "Validate"}, {"key": "B", "label": "Wait"}],
        "recommendation": "A",
    }
    ask.update(over)
    return ask


def test_the_case_link_comes_first_and_an_agent_link_to_it_is_dropped() -> None:
    agent_case = {"label": "The case", "url": f"https://one.example.test/xtm-one/ctem/cases/{CASE}"}
    ask = ctem_case.with_case_link(
        _ask(links=[agent_case, {"label": "Open PR #20153", "url": PR}]),
        f"ctem:validation:{CASE}",
        {},
        "https://one.example.test/",
    )
    assert ask["links"] == [
        {"label": "Open the case", "url": f"https://one.example.test/ctem/cases/{CASE}", "kind": "ctem_case"},
        {"label": "Open PR #20153", "url": PR},
    ]


def test_a_non_ctem_item_keeps_its_ask_unchanged() -> None:
    ask = _ask(links=[{"label": "Open PR #20153", "url": PR}])
    assert ctem_case.with_case_link(ask, "dev:harness-gap-xtm-one-2370", {}, "https://one.example.test") is ask


@pytest.mark.parametrize("language", ["en", "es", "fr", "ja"])
def test_the_case_button_is_worded_in_the_reader_language(language: str) -> None:
    link = {"label": "Open the case", "url": f"https://x.test/ctem/cases/{CASE}", "kind": "ctem_case"}
    assert card_link_label(link, language) == notice("card_link_ctem_case", language)


def test_a_ctem_gate_decision_opens_with_the_case_link() -> None:
    item = SimpleNamespace(
        ask=_ask(),
        blocked_reason=None,
        title="t",
        waits_for_user_id=ALICE,
        user_id=ALICE,
        blocked_on_refs=[],
        payload={},
        correlation_key=f"ctem:remediation:{CASE}",
    )
    _, ask, _, _ = work_item_opening(item)
    assert card_links(ask, "en")[0] == {"label": "Open the case", "url": f"https://one.example.test/ctem/cases/{CASE}"}


def test_the_teams_link_card_carries_the_case_as_its_first_link_button() -> None:
    ask = ctem_case.with_case_link(
        _ask(links=[{"label": "Open PR #20153", "url": PR}]), f"ctem:review:{CASE}", {}, "https://one.example.test"
    )
    decision = SimpleNamespace(
        id=uuid.uuid4(),
        ask=ask,
        deadline=None,
        status="open",
        answer=None,
        answered_at=None,
        surface=None,
        source_type="work_item",
        source_id=uuid.uuid4(),
    )
    card = links.link_card(decision, ALICE, "teams_chat", "en")
    titles = [a["title"] for a in card["actions"]]
    assert titles == ["Validate", "Wait", "Answer with text", "Open the case", "Open PR #20153", "Open in XTM One"]
    assert card["actions"][3]["url"] == f"https://one.example.test/ctem/cases/{CASE}"


# --- The item read ------------------------------------------------------------


@pytest.fixture
def _xtm_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _self: False))


async def test_the_item_read_names_a_case_the_reader_sees(_xtm_one: None) -> None:
    async def read(case_id: uuid.UUID, _user: Any) -> dict[str, Any]:
        return {"id": str(case_id), "title": "Patch the VPN gateways", "state": "validating"}

    item = {"id": "w", "correlation_key": f"ctem:validation:{CASE}", "payload": {}}
    assert await ctem_case.readable_case(item, SimpleNamespace(id=ALICE), read) == {
        "id": str(CASE),
        "title": "Patch the VPN gateways",
        "state": "validating",
        "path": f"/ctem/cases/{CASE}",
    }


async def test_no_case_for_a_reader_who_cannot_see_it_nor_for_another_item(_xtm_one: None) -> None:
    async def hidden(_case_id: uuid.UUID, _user: Any) -> None:
        return None

    async def never(_case_id: uuid.UUID, _user: Any) -> dict[str, Any]:
        raise AssertionError("no case to read")

    user = SimpleNamespace(id=ALICE)
    assert await ctem_case.readable_case({"correlation_key": f"ctem:review:{CASE}"}, user, hidden) is None
    assert await ctem_case.readable_case({"correlation_key": "dev:x", "payload": {}}, user, never) is None


async def test_no_case_outside_xtm_one_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _self: True))

    async def never(_case_id: uuid.UUID, _user: Any) -> dict[str, Any]:
        raise AssertionError("no case read in copilot mode")

    assert (
        await ctem_case.readable_case({"correlation_key": f"ctem:review:{CASE}"}, SimpleNamespace(id=ALICE), never)
        is None
    )
