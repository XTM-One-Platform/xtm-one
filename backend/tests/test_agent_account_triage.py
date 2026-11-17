"""The triage of an agent account's mailbox: the structure a turn answers in, and what it files."""

from __future__ import annotations

import json

import pytest

from app.services.agent_accounts import triage
from app.services.agent_accounts.providers import MessageMark
from app.services.agent_accounts.rules import RuleError


def test_a_rule_that_always_replies_leaves_out_no_reply_but_never_handing_over():
    free = triage.output_schema(must_reply=False)
    bound = triage.output_schema(must_reply=True)
    assert free["properties"]["decision"]["enum"] == ["reply", "needs_owner", "no_reply"]
    assert bound["properties"]["decision"]["enum"] == ["reply", "needs_owner"]
    assert "no_reply" not in bound["properties"]["decision"]["description"]
    assert free["required"] == ["decision", "reply", "note"] and free["additionalProperties"] is False


def _answer(**fields) -> str:
    return json.dumps(fields)


def test_each_decision_is_read_back_with_its_note():
    assert triage.read(_answer(decision="reply", reply=" Sure. ", note="Easy."), must_reply=False) == triage.Triage(
        "reply", "Sure.", "Easy."
    )
    assert triage.read(_answer(decision="needs_owner", reply="ignored", note="A price."), must_reply=False) == (
        triage.Triage("needs_owner", "", "A price.")
    )
    assert triage.read(_answer(decision="no_reply", reply="", note="FYI."), must_reply=False) == triage.Triage(
        "no_reply", "", "FYI."
    )


def test_no_reply_where_a_reply_was_required_goes_to_the_owner():
    read = triage.read(_answer(decision="no_reply", reply="", note="Nothing to say."), must_reply=True)
    assert read == triage.Triage("needs_owner", "", "Nothing to say.")
    assert triage.unanswered(must_reply=False) == triage.Triage("no_reply")


@pytest.mark.parametrize(
    "text",
    ["Sure, I will send it.", "[]", "", _answer(decision="ignore", reply="", note=""), _answer(decision="reply", reply="  ", note="")],
    ids=["prose", "list", "empty", "unknown", "empty-reply"],
)
def test_anything_else_is_no_decision(text):
    assert triage.read(text, must_reply=False) is None


def test_a_fenced_answer_and_a_long_note_are_read():
    fenced = "```json\n" + _answer(decision="no_reply", reply="", note="  one\n line " + "x" * 600) + "\n```"
    read = triage.read(fenced, must_reply=False)
    assert read.decision == "no_reply" and read.note.startswith("one line ") and len(read.note) == triage.NOTE_LIMIT


def test_the_mailbox_actions_are_stored_whole_and_checked():
    assert triage.normalize_mailbox_actions(None) is None
    stored = triage.normalize_mailbox_actions({"mark_read_no_reply": True, "category_replied": "  ", "category_no_reply": None})
    assert stored == {**triage.DEFAULT_MAILBOX_ACTIONS, "mark_read_no_reply": True, "category_replied": "", "category_no_reply": ""}
    for bad in ({"flag": True}, {"flag_needs_owner": "yes"}, {"category_replied": 3}, {"category_replied": "x" * 256}, []):
        with pytest.raises(RuleError):
            triage.normalize_mailbox_actions(bad)


def test_the_actions_in_effect_ignore_what_no_longer_reads():
    assert triage.effective_mailbox_actions(None) == triage.DEFAULT_MAILBOX_ACTIONS
    effective = triage.effective_mailbox_actions({"flag_needs_owner": "no", "category_needs_owner": " Mine ", "old": 1})
    assert effective["flag_needs_owner"] is True and effective["category_needs_owner"] == "Mine"


def test_each_outcome_files_what_the_owner_chose():
    actions = triage.DEFAULT_MAILBOX_ACTIONS
    assert triage.mark_for("reply", actions) == MessageMark(read=True, category="Answered by AI")
    assert triage.mark_for("needs_owner", actions) == MessageMark(flag=True, category="Needs your attention")
    assert triage.mark_for("no_reply", actions) == MessageMark(read=False, category="No reply needed")
    quiet = {**actions, "flag_needs_owner": False, "category_needs_owner": ""}
    assert triage.mark_for("needs_owner", quiet).empty


def test_shadow_mode_files_only_the_flag_of_a_handed_over_message():
    actions = triage.DEFAULT_MAILBOX_ACTIONS
    assert triage.mark_for("needs_owner", actions, shadow=True) == MessageMark(flag=True)
    assert triage.mark_for("reply", actions, shadow=True).empty
    assert triage.mark_for("no_reply", actions, shadow=True).empty
    assert triage.mark_for("needs_owner", {**actions, "flag_needs_owner": False}, shadow=True).empty
