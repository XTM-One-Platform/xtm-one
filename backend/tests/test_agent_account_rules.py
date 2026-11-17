"""The agent-account rule engine: every decision is a function of its inputs."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.services.agent_accounts.events import AccountEvent
from app.services.agent_accounts.providers import get_provider
from app.services.agent_accounts.rules import (
    AccountSettings,
    EventState,
    RuleError,
    apply_ai_label,
    evaluate,
    follows_channel_threads,
    hears_channel_in_full,
    label_text,
    listening_plan,
    normalize_active_hours,
    normalize_rule,
    normalize_rules,
    plan_hears,
    preset,
    rule_group_names,
    within_active_hours,
)

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)  # a Tuesday


def _event(**overrides) -> AccountEvent:
    values = {
        "provider": "test",
        "conversation_type": "direct",
        "conversation_id": "chat-1",
        "thread_id": "chat-1",
        "message_id": "m1",
        "sender_id": "alice-id",
        "sender_name": "Alice",
        "sender_email": "alice@example.com",
        "text": "Can you send me the report?",
        "created_at": NOW,
    }
    values.update(overrides)
    return AccountEvent(**values)


def _settings(mode: str = "on_behalf", **overrides) -> AccountSettings:
    base = preset(mode)
    values = {
        "mode": mode,
        "response_mode": base["response_mode"],
        "ai_label_enabled": base["ai_label_enabled"],
        "grace_period_seconds": base["grace_period_seconds"],
        "owner_takeover_minutes": base["owner_takeover_minutes"],
        "max_replies_per_hour": base["max_replies_per_hour"],
        "rules": base["rules"],
    }
    values.update(overrides)
    return AccountSettings(**values)


def _state(**overrides) -> EventState:
    return EventState(now=NOW, **overrides)


# -- presets ---------------------------------------------------------------


def test_a_delegate_answers_a_direct_message_for_approval_with_a_label_and_a_grace_period():
    decision = evaluate(_settings(), _event(), _state())
    assert decision.respond and decision.reason == "respond"
    assert decision.must_reply and decision.addressed
    assert decision.response_mode == "approval"
    assert decision.ai_label is True
    assert decision.grace_period_seconds == 120


def test_a_delegate_leaves_a_group_message_that_does_not_mention_it():
    decision = evaluate(_settings(), _event(conversation_type="group"), _state())
    assert not decision.respond and decision.reason == "not_addressed"


def test_a_delegate_answers_a_mention_in_a_channel():
    decision = evaluate(_settings(), _event(conversation_type="channel", mentions_account=True), _state())
    assert decision.respond and decision.must_reply


def test_an_ai_user_answers_at_once_without_a_label():
    decision = evaluate(_settings("ai_user"), _event(), _state())
    assert decision.respond and decision.response_mode == "auto"
    assert decision.ai_label is False and decision.grace_period_seconds == 0


# -- full monitoring: the AI decides ---------------------------------------


def test_a_fully_monitored_channel_hands_every_message_to_the_agent_to_decide():
    rules = normalize_rules(
        [{"id": "watch", "conversation_types": ["channel"], "conversations": [{"id": "ch-1"}],
          "trigger": "any", "reply": "decide"}]
    )
    settings = _settings("ai_user", rules=rules)
    decision = evaluate(settings, _event(conversation_type="channel", conversation_id="ch-1"), _state())
    assert decision.respond and not decision.must_reply and not decision.addressed
    other = evaluate(settings, _event(conversation_type="channel", conversation_id="ch-2"), _state())
    assert not other.respond and other.reason == "no_matching_rule"


# -- scope: conversations and people ---------------------------------------


def test_a_rule_focused_on_people_answers_them_and_lets_the_next_rule_decide_for_others():
    rules = normalize_rules(
        [
            {"id": "boss", "senders": [{"id": "boss-id"}], "trigger": "any", "response_mode": "auto",
             "instructions": "Be formal."},
            {"id": "rest", "conversation_types": ["direct"], "trigger": "any", "response_mode": "approval"},
        ]
    )
    settings = _settings(rules=rules, instructions="Sign as Sam.")
    boss = evaluate(settings, _event(sender_id="boss-id"), _state())
    assert boss.rule_id == "boss" and boss.response_mode == "auto"
    assert boss.instructions == "Sign as Sam.\n\nBe formal."
    alice = evaluate(settings, _event(), _state())
    assert alice.rule_id == "rest" and alice.response_mode == "approval"


def test_a_sender_matches_by_email_whatever_its_case():
    rules = normalize_rules([{"senders": [{"id": "x", "email": "Alice@Example.com"}], "trigger": "any"}])
    assert evaluate(_settings(rules=rules), _event(sender_id="other"), _state()).respond


def test_a_sender_matches_by_any_address_the_provider_knows_them_by():
    # A directory can hold a mail address and a sign-in name that differ.
    rules = normalize_rules([{"senders": [{"id": "x", "email": "alice.smith@corp.example"}], "trigger": "any"}])
    event = _event(sender_id="other", sender_emails=("alice@example.com", "Alice.Smith@corp.example"))
    assert evaluate(_settings(rules=rules), event, _state()).respond
    excluded = normalize_rules([{"exclude_senders": [{"id": "x", "email": "alice.smith@corp.example"}], "trigger": "any"}])
    assert evaluate(_settings(rules=excluded), event, _state()).reason == "no_matching_rule"


def test_a_conversation_no_rule_reaches_is_said_so_whoever_wrote_in_it():
    rules = normalize_rules(
        [{"conversation_types": ["channel"], "conversations": [{"id": "ch-1", "team_id": "t-1"}], "trigger": "any"}]
    )
    settings = _settings(rules=rules)
    elsewhere = _event(conversation_type="channel", conversation_id="ch-2", from_application=True)
    assert evaluate(settings, elsewhere, _state()).reason == "no_matching_rule"
    assert evaluate(settings, replace(elsewhere, from_application=False), _state()).reason == "no_matching_rule"
    covered = replace(elsewhere, conversation_id="ch-1")
    assert evaluate(settings, covered, _state()).reason == "application_author"
    assert evaluate(settings, covered, _state(agent_post=True)).reason == "application_author"
    # A disabled rule reaches nothing.
    off = _settings(rules=[{**rules[0], "enabled": False}])
    assert evaluate(off, covered, _state()).reason == "no_matching_rule"


def test_automated_mail_is_heard_only_by_a_rule_that_opts_in_and_names_its_senders():
    invitation = _event(sender_id="", sender_email="events@club.example", from_application=True)
    plain = normalize_rules([{"senders": [{"id": "@club.example"}], "trigger": "any"}])
    assert evaluate(_settings(rules=plain), invitation, _state()).reason == "application_author"
    opted = normalize_rules(
        [{"id": "club", "senders": [{"id": "@club.example"}], "trigger": "any", "include_automated": True}]
    )
    assert opted[0]["include_automated"] is True and plain[0]["include_automated"] is False
    decision = evaluate(_settings(rules=opted), invitation, _state())
    assert decision.respond and decision.rule_id == "club"
    # Another sender's newsletter is still left.
    other = replace(invitation, sender_email="news@shop.example")
    assert evaluate(_settings(rules=opted), other, _state()).reason == "application_author"
    # A rule naming nobody never hears applications, opted in or not.
    everyone = normalize_rules([{"trigger": "any", "include_automated": True}])
    assert evaluate(_settings(rules=everyone), invitation, _state()).reason == "application_author"


def test_an_automated_message_skips_the_rules_that_did_not_opt_in():
    rules = normalize_rules(
        [
            {"id": "people", "conversation_types": ["direct"], "trigger": "any"},
            {"id": "club", "senders": [{"id": "@club.example"}], "trigger": "any", "include_automated": True,
             "response_mode": "shadow"},
        ]
    )
    invitation = _event(sender_id="", sender_email="events@club.example", from_application=True)
    assert evaluate(_settings(rules=rules), invitation, _state()).rule_id == "club"
    person = replace(invitation, from_application=False)
    assert evaluate(_settings(rules=rules), person, _state()).rule_id == "people"


def test_the_decision_carries_who_the_sender_was_found_to_be_only_when_set():
    decision = evaluate(_settings(), _event(), _state())
    assert "sender" not in decision.as_dict()
    explained = replace(decision, sender={"known": False, "emails": ["alice@example.com"], "groups": []})
    assert explained.as_dict()["sender"] == {"known": False, "emails": ["alice@example.com"], "groups": []}


def test_the_groups_the_rules_name_keep_the_name_the_owner_saw():
    rules = normalize_rules(
        [
            {"id": "a", "groups": [{"id": "G-1", "name": "Executive Management"}], "trigger": "any"},
            {"id": "b", "groups": [{"id": "g-1", "name": "Renamed"}, {"id": "g-2"}], "trigger": "any"},
            {"id": "c", "enabled": False, "groups": [{"id": "g-3", "name": "Off"}], "trigger": "any"},
        ]
    )
    assert rule_group_names(_settings(rules=rules)) == {"g-1": "Executive Management", "g-2": "g-2"}


def test_an_excluded_sender_is_never_answered_by_that_rule():
    rules = normalize_rules([{"exclude_senders": [{"id": "alice-id"}], "trigger": "any"}])
    decision = evaluate(_settings(rules=rules), _event(), _state())
    assert not decision.respond and decision.reason == "no_matching_rule"


def test_the_first_matching_rule_wins_and_an_ignore_rule_silences():
    rules = normalize_rules(
        [
            {"id": "mute", "action": "ignore", "conversations": [{"id": "chat-1"}], "trigger": "any"},
            {"id": "all", "trigger": "any"},
        ]
    )
    decision = evaluate(_settings(rules=rules), _event(), _state())
    assert not decision.respond and decision.reason == "ignored_by_rule" and decision.rule_id == "mute"


def test_a_disabled_rule_is_skipped():
    rules = normalize_rules([{"id": "off", "enabled": False, "trigger": "any"}])
    assert evaluate(_settings(rules=rules), _event(), _state()).reason == "no_matching_rule"


# -- threads, takeover, rate ------------------------------------------------


def test_a_follow_up_in_a_thread_it_answered_is_the_agents_call():
    event = _event(conversation_type="channel")
    decision = evaluate(_settings(), event, _state(thread_active=True))
    assert decision.respond and decision.thread_continuation and not decision.must_reply


def test_a_rule_that_does_not_follow_threads_ignores_the_follow_up():
    rules = normalize_rules([{"conversation_types": ["channel"], "continue_threads": False}])
    decision = evaluate(_settings(rules=rules), _event(conversation_type="channel"), _state(thread_active=True))
    assert not decision.respond and decision.reason == "not_addressed"


def test_the_owner_writing_makes_the_agent_back_off():
    decision = evaluate(_settings(), _event(), _state(owner_active=True))
    assert not decision.respond and decision.reason == "owner_takeover"
    no_takeover = evaluate(_settings(owner_takeover_minutes=0), _event(), _state(owner_active=True))
    assert no_takeover.respond


def test_the_hourly_limit_stops_a_conversation_running_away():
    decision = evaluate(_settings(max_replies_per_hour=3), _event(), _state(replies_last_hour=3))
    assert not decision.respond and decision.reason == "rate_limited"
    assert evaluate(_settings(max_replies_per_hour=0), _event(), _state(replies_last_hour=99)).respond


def test_the_account_wide_hourly_limit_counts_every_conversation_together():
    """A mailbox starts a conversation with each new email: the per-conversation limit never trips."""
    fresh_conversation = _state(replies_last_hour=0, turns_last_hour=60)
    decision = evaluate(_settings(max_turns_per_hour=60), _event(), fresh_conversation)
    assert not decision.respond and decision.reason == "account_rate_limited"
    assert decision.rule_id == "direct"
    assert evaluate(_settings(max_turns_per_hour=60), _event(), _state(turns_last_hour=59)).respond
    assert evaluate(_settings(max_turns_per_hour=0), _event(), _state(turns_last_hour=999)).respond


@pytest.mark.parametrize(("mode", "kind", "turns"), [
    ("on_behalf", "chat", 0), ("ai_user", "chat", 0), ("on_behalf", "mailbox", 60), ("ai_user", "mailbox", 60),
])
def test_a_mailbox_starts_with_an_account_wide_turn_limit_and_every_account_with_a_recipient_limit(mode, kind, turns):
    base = preset(mode, kind)
    assert base["max_turns_per_hour"] == turns
    assert base["max_reply_recipients"] == 10


@pytest.mark.parametrize(
    ("event_fields", "settings_fields", "reason"),
    [
        ({"from_account": True}, {}, "own_message"),
        ({"from_application": True}, {}, "application_author"),
        ({"text": "   "}, {}, "empty_message"),
        ({}, {"enabled": False}, "account_disabled"),
        ({}, {"has_agent": False}, "no_agent"),
    ],
)
def test_what_is_never_answered(event_fields, settings_fields, reason):
    decision = evaluate(_settings(**settings_fields), _event(**event_fields), _state())
    assert not decision.respond and decision.reason == reason


def test_rule_overrides_replace_the_account_defaults():
    rules = normalize_rules([{"trigger": "any", "ai_label": False, "grace_period_seconds": 5, "response_mode": "shadow"}])
    decision = evaluate(_settings(rules=rules), _event(), _state())
    assert (decision.ai_label, decision.grace_period_seconds, decision.response_mode) == (False, 5, "shadow")


# -- active hours -------------------------------------------------------------


def test_active_hours_inside_and_outside_a_window():
    schedule = normalize_active_hours(
        {"timezone": "Europe/Paris", "windows": [{"days": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"}]}
    )
    assert within_active_hours(schedule, NOW)  # 14:00 in Paris on a Tuesday
    assert not within_active_hours(schedule, NOW.replace(hour=20))
    outside = {**schedule, "mode": "outside"}
    assert within_active_hours(outside, NOW.replace(hour=20))
    settings = _settings(active_hours=schedule)
    assert evaluate(settings, _event(), EventState(now=NOW.replace(hour=20))).reason == "outside_active_hours"


def test_a_window_past_midnight_belongs_to_the_day_it_starts():
    schedule = normalize_active_hours({"windows": [{"days": [2], "start": "22:00", "end": "02:00"}]})
    assert within_active_hours(schedule, NOW.replace(hour=23))
    assert within_active_hours(schedule, NOW.replace(day=7, hour=1))  # Wednesday 01:00
    assert not within_active_hours(schedule, NOW.replace(day=8, hour=1))  # Thursday 01:00


@pytest.mark.parametrize(
    "schedule",
    [
        {"timezone": "Mars/Olympus", "windows": [{"days": [1], "start": "09:00", "end": "10:00"}]},
        {"windows": []},
        {"windows": [{"days": [8], "start": "09:00", "end": "10:00"}]},
        {"windows": [{"days": [1], "start": "9h", "end": "10:00"}]},
        {"windows": [{"days": [1], "start": "09:00", "end": "09:00"}]},
    ],
)
def test_a_schedule_the_engine_cannot_apply_is_refused(schedule):
    with pytest.raises(RuleError):
        normalize_active_hours(schedule)


# -- normalization -----------------------------------------------------------------


def test_a_rule_is_stored_complete():
    rule = normalize_rule({"conversation_types": ["channel", "direct"]})
    assert rule["conversation_types"] == ["direct", "channel"]
    assert rule["trigger"] == "mention" and rule["reply"] == "always" and rule["action"] == "respond"
    assert rule["continue_threads"] is True and rule["id"]


@pytest.mark.parametrize(
    "raw",
    [
        "not a rule",
        {"trigger": "sometimes"},
        {"conversation_types": ["email"]},
        {"senders": [{"name": "no id"}]},
        {"grace_period_seconds": 99999},
        {"ai_label": "yes"},
    ],
)
def test_a_rule_the_engine_cannot_apply_is_refused(raw):
    with pytest.raises(RuleError):
        normalize_rule(raw)


def test_rules_are_bounded_and_their_ids_unique():
    with pytest.raises(RuleError):
        normalize_rules([{"id": "a"}, {"id": "a"}])
    with pytest.raises(RuleError):
        normalize_rules([{} for _ in range(51)])


def test_targets_are_deduplicated_by_id():
    rule = normalize_rule({"senders": ["a", {"id": "A"}, {"id": "b", "name": "Bob"}]})
    assert [s["id"] for s in rule["senders"]] == ["a", "b"]


# -- listening plan ----------------------------------------------------------------


def test_the_listening_plan_follows_the_answering_rules():
    plan = listening_plan(_settings())
    assert plan.chats and plan.all_channels and not plan.channels
    # The default rules wait for a mention in channels: their other messages never reach the account,
    # the threads the account answered in are followed by reading them.
    assert plan.all_channels_mentions_only is True
    assert not hears_channel_in_full(plan, "any-channel")
    assert follows_channel_threads(_settings(), "any-channel")


def test_a_named_channel_waiting_for_mentions_listens_to_mentions_only_threads_followed_or_not():
    for continue_threads in (True, False):
        rules = normalize_rules(
            [
                {"conversation_types": ["channel"], "conversations": [{"id": "ch-1", "team_id": "t-1", "name": "Ops > A"}],
                 "continue_threads": continue_threads},
                {"action": "ignore", "conversation_types": ["channel"], "conversations": [{"id": "ch-2"}]},
            ]
        )
        settings = _settings(rules=rules)
        plan = listening_plan(settings)
        assert not plan.chats and not plan.all_channels
        assert [(c.id, c.team_id, c.mentions_only, c.name) for c in plan.channels] == [("ch-1", "t-1", True, "Ops > A")]
        assert follows_channel_threads(settings, "ch-1") is continue_threads
        # An ignore rule takes no follow-up, and a channel no rule names takes none either.
        assert not follows_channel_threads(settings, "ch-2")


def test_a_rule_answering_every_message_hears_its_channels_in_full():
    named = normalize_rules(
        [
            {"conversation_types": ["channel"], "conversations": [{"id": "ch-1", "team_id": "t-1"}], "trigger": "any"},
            {"conversation_types": ["channel"], "conversations": [{"id": "ch-1"}, {"id": "ch-2", "team_id": "t-1"}]},
        ]
    )
    plan = listening_plan(_settings(rules=named))
    assert hears_channel_in_full(plan, "ch-1") and not hears_channel_in_full(plan, "ch-2")
    everywhere = listening_plan(_settings(rules=normalize_rules([{"conversation_types": ["channel"], "trigger": "any"}])))
    assert everywhere.all_channels and hears_channel_in_full(everywhere, "ch-9")
    assert not hears_channel_in_full(listening_plan(_settings(rules=normalize_rules([]))), "ch-9")


def test_a_message_the_plan_does_not_listen_to_shows_the_listening_is_behind_the_rules():
    rules = normalize_rules(
        [{"conversation_types": ["channel"], "conversations": [{"id": "ch-1", "team_id": "t-1"}], "trigger": "any"}]
    )
    plan = listening_plan(_settings(rules=rules))
    assert plan_hears(plan, _event(conversation_type="channel", conversation_id="ch-1"))
    assert not plan_hears(plan, _event(conversation_type="channel", conversation_id="ch-2"))
    assert not plan_hears(plan, _event(conversation_type="direct"))
    everything = listening_plan(_settings())
    assert plan_hears(everything, _event(conversation_type="channel", conversation_id="ch-2"))
    assert plan_hears(everything, _event(conversation_type="group"))


def test_a_paused_account_listens_to_nothing():
    assert listening_plan(_settings(enabled=False)).empty
    assert listening_plan(replace(_settings(), has_agent=False)).empty


# -- mailboxes ----------------------------------------------------------


@pytest.mark.parametrize("mode", ["on_behalf", "ai_user"])
def test_a_mailbox_answers_emails_to_it_alone_or_in_to_and_the_agent_decides(mode):
    base = preset(mode, "mailbox")
    settings = _settings(mode, rules=base["rules"], grace_period_seconds=0, owner_takeover_minutes=0)
    alone = evaluate(settings, _event(conversation_type="direct"), _state())
    in_to = evaluate(settings, _event(conversation_type="group", mentions_account=True), _state())
    cc_only = evaluate(settings, _event(conversation_type="group"), _state())
    assert alone.respond and in_to.respond and not alone.must_reply and not in_to.must_reply
    assert cc_only.reason == "not_addressed"
    # Mail is heard through the inbox alone: no rule reaches a channel.
    plan = listening_plan(settings)
    assert plan.chats and not plan.channels and not plan.all_channels
    assert (base["response_mode"], base["ai_label_enabled"]) == (
        ("approval", True) if mode == "on_behalf" else ("auto", False)
    )


def test_an_unknown_kind_of_platform_is_refused():
    with pytest.raises(RuleError):
        preset("on_behalf", "fax")


# -- provider capabilities -------------------------------------------------

MAILBOX = get_provider("microsoft_outlook").capabilities
CHAT = get_provider("microsoft_teams").capabilities


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"conversation_types": ["channel"]}, "Rule 1: this platform has no channel conversations."),
        ({"conversation_types": ["direct", "channel"]}, "Rule 1: this platform has no channel conversations."),
        (
            {"conversation_types": ["direct"], "conversations": [{"id": "thread-1"}]},
            "Rule 1: on this platform a rule cannot name specific conversations.",
        ),
    ],
    ids=["channel-only", "channel-among-others", "named-conversation"],
)
def test_a_rule_the_provider_cannot_serve_is_refused(raw, message):
    with pytest.raises(RuleError) as caught:
        normalize_rules([raw], capabilities=MAILBOX)
    assert str(caught.value) == message


def test_a_named_conversation_of_a_type_the_provider_lacks_is_refused():
    no_channels = replace(CHAT, channel=False)
    with pytest.raises(RuleError) as caught:
        normalize_rules([{"conversations": [{"id": "c1", "type": "channel"}]}], capabilities=no_channels)
    assert str(caught.value) == "Rule 1: this platform has no channel conversations."


def test_a_rule_naming_no_type_covers_the_provider_types_alone():
    (mailbox_rule,) = normalize_rules([{"conversation_types": []}], capabilities=MAILBOX)
    (chat_rule,) = normalize_rules([{"conversation_types": []}], capabilities=CHAT)
    assert mailbox_rule["conversation_types"] == ["direct", "group"]
    assert chat_rule["conversation_types"] == ["direct", "group", "channel"]


def test_a_rule_the_provider_serves_is_stored_as_without_capabilities():
    raw = [{"id": "vip", "conversation_types": ["direct", "group"], "senders": ["boss@example.com"]}]
    assert normalize_rules(raw, capabilities=MAILBOX) == normalize_rules(raw)
    named = [{"id": "c", "conversation_types": ["channel"], "conversations": [{"id": "c1", "type": "channel"}]}]
    assert normalize_rules(named, capabilities=CHAT) == normalize_rules(named)


@pytest.mark.parametrize("mode", ["on_behalf", "ai_user"])
def test_the_mailbox_presets_pass_the_mailbox_check(mode):
    rules = preset(mode, "mailbox")["rules"]
    assert normalize_rules(rules, capabilities=MAILBOX) == rules


def test_a_sender_rule_can_name_a_whole_domain():
    rules = normalize_rules(
        [
            {"name": "Not the vendor", "action": "ignore", "trigger": "any", "senders": [{"id": "@vendor.example"}]},
            {"name": "Internal", "trigger": "any", "senders": [{"id": "@example.com"}],
             "exclude_senders": [{"id": "@noreply.example.com"}]},
        ]
    )
    settings = _settings(rules=rules, owner_takeover_minutes=0)
    internal = evaluate(settings, _event(sender_email="Bob@Example.com"), _state())
    assert internal.respond and internal.rule_name == "Internal"
    vendor = evaluate(settings, _event(sender_email="sales@vendor.example"), _state())
    assert vendor.reason == "ignored_by_rule"
    # A domain names that domain, never a subdomain of it, nor a part of an address.
    assert not evaluate(settings, _event(sender_email="bob@sub.example.com"), _state()).respond
    assert not evaluate(settings, _event(sender_email="a@noreply.example.com"), _state()).respond
    assert not evaluate(settings, _event(sender_email="bob@example.com.attacker.example"), _state()).respond


def test_an_email_another_agent_account_sent_is_left_alone_whatever_its_id():
    decision = evaluate(_settings(), _event(from_agent_account=True), _state(agent_post=True))
    assert decision.reason == "agent_author"


# -- label ---------------------------------------------------------------------------


def test_the_label_names_the_account_and_sits_where_asked():
    label = label_text("on_behalf", None, "Sam Example")
    assert label == "Sent by an AI agent on behalf of Sam Example"
    assert apply_ai_label("Hello", label, "suffix") == f"Hello\n\n_{label}_"
    assert apply_ai_label("Hello", "AI", "prefix") == "_AI_\n\nHello"
    assert label_text("ai_user", "Your AI colleague {name}", "Ada") == "Your AI colleague Ada"
    assert apply_ai_label("Hello", "  ", "suffix") == "Hello"
