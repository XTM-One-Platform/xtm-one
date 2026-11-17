"""The activity page's groups of an agent account's outcomes - no database."""

from __future__ import annotations

from app.services import inbound_claims
from app.services.agent_accounts import activity, pipeline


def test_a_mail_another_pipeline_took_is_its_own_group_not_left_alone():
    """An email an assignment run (or another account) claimed first is "Handled elsewhere"."""
    assert pipeline.ALREADY_HANDLED == "already_handled"
    assert activity.group_of(pipeline.ALREADY_HANDLED) == "already_handled"
    assert "already_handled" in activity.GROUPS
    # The reasons of a decision stay "left alone".
    assert activity.group_of("not_mentioned") == "left"
    assert activity.group_of("owner_replied") == "left"


def test_every_mapped_outcome_lands_in_a_known_group_and_groups_are_unique():
    assert len(set(activity.GROUPS)) == len(activity.GROUPS)
    assert set(activity.OUTCOME_GROUPS.values()) <= set(activity.GROUPS)
    assert "left" not in activity.OUTCOME_GROUPS.values()


def test_the_holder_reads_in_words_for_a_log_line():
    assert inbound_claims.describe_holder(inbound_claims.ASSIGNMENT_HOLDER) == "an assignment run of this agent"
    assert inbound_claims.describe_holder(inbound_claims.account_holder("f28538b0")) == "the agent account f28538b0"


def test_the_holder_travels_as_data_for_the_page_to_word():
    """The page words the holder in the viewer's language, never a backend sentence."""
    assert inbound_claims.holder_parts(inbound_claims.ASSIGNMENT_HOLDER) == ("assignment", None)
    assert inbound_claims.holder_parts(inbound_claims.account_holder("f28538b0")) == ("agent_account", "f28538b0")
    assert inbound_claims.holder_parts(None) == (None, None)
    assert inbound_claims.holder_parts("something-else") == (None, None)
