"""Reactions as feedback: the emoji, the setting, the rule that turns reactions into ratings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.services import reaction_feedback as rf
from app.services.reaction_feedback import PlatformReaction, ReactionFeedbackConfig

UP = "\U0001f44d"
DOWN = "\U0001f44e"
HEART = "❤"
TONE = "\U0001f3fd"  # medium skin tone
VS16 = "️"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(UP, UP, id="plain"),
        pytest.param(UP + TONE, UP, id="skin-tone"),
        pytest.param(f"  {UP}  ", UP, id="whitespace"),
        pytest.param(HEART + VS16, HEART, id="presentation-selector"),
        pytest.param("\U0001f468‍\U0001f4bb", "\U0001f468‍\U0001f4bb", id="zwj-sequence"),
        pytest.param("\U0001f1eb\U0001f1f7", "\U0001f1eb\U0001f1f7", id="flag"),
        pytest.param(
            "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f",
            "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f",
            id="subdivision-flag",
        ),
        pytest.param("\U0001f468\U0001f3fd‍\U0001f4bb", "\U0001f468‍\U0001f4bb", id="zwj-sequence-skin-tone"),
        pytest.param(UP + DOWN, None, id="two-emojis"),
        pytest.param(UP + TONE + DOWN, None, id="two-emojis-skin-tone"),
        pytest.param(UP * 2, None, id="same-emoji-twice"),
        pytest.param("\U0001f501" * 3, None, id="same-emoji-three-times"),
        pytest.param("\U0001f1eb\U0001f1f7\U0001f1e9\U0001f1ea", None, id="two-flags"),
        pytest.param("\U0001f1eb\U0001f1f7\U0001f1e9", None, id="flag-and-lone-indicator"),
        pytest.param("\U0001f3f4" * 2, None, id="two-black-flags"),
        pytest.param("like", None, id="word"),
        pytest.param("1", None, id="digit"),
        pytest.param("", None, id="empty"),
        pytest.param("   ", None, id="blank"),
        pytest.param(None, None, id="null"),
        pytest.param(42, None, id="not-a-string"),
        pytest.param(UP + "a", None, id="emoji-and-text"),
        pytest.param(TONE, None, id="skin-tone-alone"),
        pytest.param(UP * 17, None, id="too-long"),
        pytest.param("<b>" + UP, None, id="markup"),
    ],
)
def test_an_emoji_has_one_spelling_and_text_is_not_one(raw, expected):
    assert rf.normalize_emoji(raw) == expected


def test_the_default_setting_is_on_with_thumbs():
    assert rf.DEFAULT_CONFIG == {"enabled": True, "positive": [UP], "negative": [DOWN]}
    assert rf.effective_config(None) == ReactionFeedbackConfig()


def test_a_setting_is_normalized_and_deduplicated():
    config = ReactionFeedbackConfig(positive=[UP, UP + TONE, HEART + VS16], negative=[DOWN])
    assert config.positive == [UP, HEART]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"positive": [UP], "negative": [UP + TONE]}, id="both-signs"),
        pytest.param({"positive": ["like"]}, id="not-an-emoji"),
        pytest.param({"positive": [UP + UP]}, id="several-emojis-in-one-entry"),
        pytest.param({"positive": UP}, id="not-a-list"),
        pytest.param({"positive": [chr(0x1F600 + i) for i in range(11)]}, id="too-many"),
        pytest.param({"enabled": "maybe"}, id="not-a-bool"),
    ],
)
def test_an_invalid_setting_is_refused(payload):
    with pytest.raises(ValidationError):
        ReactionFeedbackConfig.model_validate(payload)


def test_an_unreadable_stored_setting_reads_as_off():
    assert rf.effective_config({"positive": [UP], "negative": [UP]}).enabled is False
    assert rf.effective_config({"enabled": False}).enabled is False


@pytest.mark.parametrize(
    ("emojis", "expected"),
    [
        pytest.param({UP}, ("positive", UP), id="up"),
        pytest.param({DOWN}, ("negative", DOWN), id="down"),
        pytest.param({UP, HEART}, ("positive", UP), id="up-and-unmapped"),
        pytest.param({UP, DOWN}, None, id="both-signs"),
        pytest.param({HEART}, None, id="unmapped"),
        pytest.param(set(), None, id="none"),
    ],
)
def test_a_raters_emojis_give_one_rating_or_none(emojis, expected):
    assert rf.rating_of(ReactionFeedbackConfig(), emojis) == expected


def test_the_stored_emoji_is_the_settings_first_one_the_rater_used():
    config = ReactionFeedbackConfig(positive=[HEART, UP])
    assert rf.rating_of(config, {UP, HEART}) == ("positive", HEART)


def test_a_rater_is_a_keyed_digest_never_the_platform_id():
    ref = rf.rater_ref("microsoft_teams", "aad-user-1")
    assert len(ref) == 64 and "aad-user-1" not in ref
    assert ref == rf.rater_ref("microsoft_teams", "aad-user-1")
    assert ref != rf.rater_ref("slack", "aad-user-1")
    assert ref != rf.rater_ref("microsoft_teams", "aad-user-2")


def test_ratings_by_rater_reads_every_raters_whole_set():
    reactions = [
        PlatformReaction("alice", UP),
        PlatformReaction("bob", DOWN + TONE),
        PlatformReaction("carol", UP),
        PlatformReaction("carol", DOWN),
        PlatformReaction("dave", HEART),
        PlatformReaction("agent", UP),
        PlatformReaction("", UP),
        PlatformReaction("erin", "custom"),
    ]
    ratings = rf.ratings_by_rater(
        ReactionFeedbackConfig(), reactions, platform="microsoft_teams", exclude_user_ids=["agent"]
    )
    ref = lambda user: rf.rater_ref("microsoft_teams", user)  # noqa: E731
    assert ratings == {ref("alice"): ("positive", UP), ref("bob"): ("negative", DOWN)}


def test_the_raters_of_one_answer_are_capped_whatever_order_the_provider_lists_them_in(monkeypatch):
    monkeypatch.setattr(rf, "MAX_RATERS_PER_TARGET", 2)
    reactions = [PlatformReaction(f"user-{i}", UP) for i in range(5)] + [PlatformReaction("user-0", DOWN)]
    ratings = rf.ratings_by_rater(ReactionFeedbackConfig(), reactions, platform="p")
    # user-0 holds both signs: no rating, and no place taken under the cap.
    raters = sorted(rf.rater_ref("p", f"user-{i}") for i in range(1, 5))
    assert ratings == {raters[0]: ("positive", UP), raters[1]: ("positive", UP)}
    # The same reactions listed in another order keep the same raters.
    assert rf.ratings_by_rater(ReactionFeedbackConfig(), reactions[::-1], platform="p") == ratings


def test_a_reaction_rates_an_answer_for_fourteen_days():
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    assert rf.within_window(now - timedelta(days=14), now) is True
    assert rf.within_window(now - timedelta(days=14, seconds=1), now) is False
    assert rf.within_window(datetime(2026, 10, 19, 12, 0), now) is True  # naive reads as UTC
    assert rf.within_window(None, now) is False
