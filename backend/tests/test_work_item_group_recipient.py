# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""A decision work item addressed to groups (#1748, slice 2b): parsing, opening, refusal, inbox reach."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.models.decision_hooks import work_item_opening
from app.services.decision_inbox import _reaches
from app.services.work_item import _not_waiting_message
from app.services.work_item_recipient import RecipientError, group_refs, recipient_line

G1 = "11111111-1111-4111-8111-111111111111"
G2 = "22222222-2222-4222-8222-222222222222"


def test_group_refs_reads_one_or_several_groups_once_each():
    assert group_refs(f"group:{G1}") == [G1]
    assert group_refs(f"group:{G1}, group:{G2},group:{G1}") == [G1, G2]


def test_group_refs_leaves_a_person_or_nothing_to_the_person_rules():
    assert group_refs(None) is None
    assert group_refs("") is None
    assert group_refs("someone@example.com") is None
    assert group_refs(str(uuid.uuid4())) is None


@pytest.mark.parametrize("bad", [f"group:{G1},someone@example.com", "group:not-a-uuid"])
def test_group_refs_refuses_a_mixed_or_malformed_list(bad):
    with pytest.raises(RecipientError):
        group_refs(bad)


def _item(**kw):
    base = dict(
        ask={"question": "Accept the risk?"},
        blocked_reason=None,
        title="t",
        blocked_on_refs=None,
        payload={},
        waits_for_user_id=None,
        waits_for_group_ids=None,
        user_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_the_decision_of_a_group_item_names_every_group():
    kind, ask, recipient, _deadline = work_item_opening(_item(waits_for_group_ids=[G1, G2]))
    assert kind == "work_item"
    assert recipient == f"group:{G1}"
    assert ask["recipient_groups"] == [G1, G2]


def test_a_person_item_keeps_its_person():
    person = uuid.uuid4()
    _kind, ask, recipient, _deadline = work_item_opening(_item(waits_for_user_id=person))
    assert recipient == f"user:{person}"
    assert "recipient_groups" not in ask


def test_a_second_answer_is_refused_with_who_decided_first():
    item = SimpleNamespace(payload={"answer": {"answered_by": "Ada", "answered_at": "2026-11-24T12:00:00+00:00"}})
    assert _not_waiting_message(item) == "already decided by Ada at 2026-11-24T12:00:00+00:00"
    assert _not_waiting_message(SimpleNamespace(payload={})) == "this work item is not waiting for a person's answer"


def test_the_inbox_reaches_a_member_of_any_listed_group():
    row = SimpleNamespace(recipient=f"group:{G1}", ask={"recipient_groups": [G1, G2]})
    assert _reaches(row, [f"group:{G2}"], [G2]) is True
    assert _reaches(row, ["user:x"], []) is False


@pytest.mark.asyncio
async def test_the_tool_line_says_the_groups_decide():
    line = await recipient_line({"needs_human": True, "waits_for_group_ids": [G1]})
    assert line is not None and "first answer decides" in line
