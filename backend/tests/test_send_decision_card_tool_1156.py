# SPDX-License-Identifier: Apache-2.0

"""#1156: the ``send_decision_card`` tool refuses a bad call before any read or send."""

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.services.builtin_tool_catalog import build_builtin_tool_catalog
from app.services.decision_graph_card_tool import TOOL_NAME, build_send_decision_card

ITEM = "0f1d2c3b-4a59-4687-9a1b-2c3d4e5f6a7b"


def _tool(
    user_id: str | None = "bbbbbbbb-1111-4222-8333-444444444444", item: dict[str, Any] | None = None
) -> tuple[Callable[..., Awaitable[str]], list[str]]:
    reads: list[str] = []

    async def read(work_item_id: str) -> dict[str, Any] | None:
        reads.append(work_item_id)
        return item

    return build_send_decision_card("aaaaaaaa-1111-4222-8333-444444444444", user_id, read), reads


@pytest.mark.parametrize(
    "target",
    [
        {},
        {"chat_id": "19:a@thread.v2", "team_id": "t", "channel_id": "c"},
        {"team_id": "t"},
        {"channel_id": "c"},
    ],
)
async def test_one_target_is_required(target: dict[str, str]) -> None:
    send, reads = _tool()
    answer = await send(ITEM, "Office", **target)
    assert answer.startswith("Error: name one target") and reads == []


async def test_integration_and_acting_user_are_required() -> None:
    send, reads = _tool()
    assert (await send(ITEM, " ", chat_id="48:notes")).startswith("Error: integration")
    send, reads = _tool(user_id=None)
    assert (await send(ITEM, "Office", chat_id="48:notes")).startswith("Error: this run acts for nobody")
    assert reads == []


async def test_an_unreadable_item_sends_nothing() -> None:
    send, reads = _tool(item=None)
    answer = await send(ITEM, "Office", chat_id="48:notes")
    assert reads == [ITEM] and "not found or not accessible" in answer


def test_the_tool_is_in_the_coordination_group() -> None:
    names = [d["name"] for d in build_builtin_tool_catalog(include_disabled=True)["coordination"]]
    assert TOOL_NAME in names
