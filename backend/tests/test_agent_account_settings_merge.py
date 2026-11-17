"""#9898: an agent account write naming one setting never resets the others.

The reaction and mailbox settings are objects: a write that sets one of their
keys is merged over what the account holds, as an integration's extra_config is.
"""

from __future__ import annotations

import pytest

from app.services.agent_accounts import triage
from app.services.agent_accounts.management import merged_reaction_feedback
from app.services.agent_accounts.rules import RuleError
from app.services.reaction_feedback import ReactionFeedbackConfig

UP, DOWN, HEART = "\U0001f44d", "\U0001f44e", "\u2764"


def test_one_mailbox_action_keeps_the_others():
    stored = triage.normalize_mailbox_actions({"mark_read_replied": True, "category_replied": "Done"})
    merged = triage.merge_mailbox_actions(stored, {"mark_read_no_reply": True})
    assert merged["mark_read_replied"] is True
    assert merged["category_replied"] == "Done"
    assert merged["mark_read_no_reply"] is True


def test_a_mailbox_merge_on_defaults_and_its_checks():
    assert triage.merge_mailbox_actions(None, None) is None
    merged = triage.merge_mailbox_actions(None, {"category_no_reply": " Later "})
    assert merged == {**triage.DEFAULT_MAILBOX_ACTIONS, "category_no_reply": "Later"}
    with pytest.raises(RuleError):
        triage.merge_mailbox_actions({}, {"delete_it": True})
    with pytest.raises(RuleError):
        triage.merge_mailbox_actions({}, {"mark_read_replied": "yes"})


def test_one_reaction_field_keeps_the_others():
    stored = {"enabled": True, "positive": [HEART], "negative": [DOWN]}
    merged = merged_reaction_feedback(stored, ReactionFeedbackConfig.model_validate({"enabled": False}))
    assert merged == {"enabled": False, "positive": [HEART], "negative": [DOWN]}


def test_a_reaction_merge_that_overlaps_is_refused():
    stored = {"enabled": True, "positive": [HEART], "negative": [DOWN]}
    with pytest.raises(RuleError):
        merged_reaction_feedback(stored, ReactionFeedbackConfig.model_validate({"negative": [HEART]}))


def test_a_whole_reaction_setting_still_replaces():
    whole = ReactionFeedbackConfig.model_validate({"enabled": True, "positive": [UP], "negative": [DOWN]})
    assert merged_reaction_feedback({"enabled": False, "positive": [HEART], "negative": []}, whole) == {
        "enabled": True,
        "positive": [UP],
        "negative": [DOWN],
    }
