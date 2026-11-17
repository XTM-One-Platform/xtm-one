"""What an agent account's sync found, grouped so its owner can act."""

from __future__ import annotations

from datetime import UTC, datetime

from app.services.agent_accounts import problems
from app.services.agent_accounts.problems import Problem
from app.services.agent_accounts.providers import ERROR_KINDS, DesiredSubscription, ProviderError

CHANNEL = DesiredSubscription(
    "/teams/t/channels/a/messages", covers="channel", name="Ops > A", permission="ChannelMessage.Read.All"
)


def test_every_kind_a_provider_reports_has_a_place_in_the_report():
    assert set(ERROR_KINDS) <= set(problems.KINDS)
    assert ProviderError("x", kind="made-up").kind == "error"


def test_a_refused_subscription_names_its_conversation_and_the_permission_it_needs():
    problem = Problem.of(ProviderError("Graph said no", kind="refused"), CHANNEL)
    assert (problem.kind, problem.covers, problem.name, problem.permission) == (
        "refused", "channel", "Ops > A", "ChannelMessage.Read.All",
    )
    # Another cause is no permission's doing.
    taken = Problem.of(ProviderError("Graph said no", kind="refused"), CHANNEL, kind="taken_by_account", holder="EA")
    assert (taken.kind, taken.permission, taken.holder) == ("taken_by_account", None, "EA")
    throttled = Problem.of(ProviderError("slow down", kind="throttled"), CHANNEL)
    assert throttled.permission is None and throttled.name == "Ops > A"


def test_problems_are_grouped_by_cause_the_account_wide_ones_first():
    refused = [
        Problem.of(
            ProviderError(f"Graph answered 403 for {name}", kind="refused"),
            DesiredSubscription(f"/teams/t/channels/{name}/messages", name=name, permission="ChannelMessage.Read.All"),
        )
        for name in ("A", "B", "A")
    ]
    found = problems.grouped(
        [*refused, Problem("integration", "The integration is not connected: sign it in again.")]
    )
    assert [g["kind"] for g in found] == ["integration", "refused"]
    group = found[1]
    assert group["count"] == 3 and group["permission"] == "ChannelMessage.Read.All"
    assert group["targets"] == [{"covers": "channel", "name": "A"}, {"covers": "channel", "name": "B"}]
    assert group["details"] == ["Graph answered 403 for A", "Graph answered 403 for B"]
    assert found[0]["targets"] == []


def test_a_platform_authored_reason_travels_as_a_note_the_provider_s_words_as_details():
    # A reason the platform words itself is rendered in the viewer's language.
    found = problems.grouped(
        [
            Problem("not_subscribed", "past the cap", covers="channel", name="A", note="past_channel_cap",
                    note_values={"limit": 50}),
            Problem("not_subscribed", "past the cap", covers="channel", name="B", note="past_channel_cap",
                    note_values={"limit": 50}),
            Problem("not_subscribed", "unlisted", covers="team", name="Locked", note="team_channels_unlisted"),
            Problem("not_subscribed", "Graph answered 404", covers="channel", name="C"),
        ]
    )
    (group,) = found
    assert group["notes"] == [
        {"code": "past_channel_cap", "values": {"limit": 50}},
        {"code": "team_channels_unlisted", "values": {}},
    ]
    assert group["details"] == ["Graph answered 404"]


def test_a_group_is_bounded_whatever_the_number_of_conversations():
    many = [
        Problem("refused", f"{'x' * 2000} {i}", covers="channel", name=f"channel {i}") for i in range(200)
    ]
    (group,) = problems.grouped(many)
    assert group["count"] == 200
    assert len(group["targets"]) == problems.MAX_TARGETS
    assert len(group["details"]) <= problems.MAX_DETAILS
    assert all(len(detail) <= problems.DETAIL_CHARS for detail in group["details"])


def test_the_report_says_when_and_how_much_of_the_account_is_heard():
    checked = datetime(2026, 10, 10, 20, 0, tzinfo=UTC)
    report = problems.report([], wanted=14, heard=14, checked_at=checked)
    assert report == {"checked_at": checked.isoformat(), "wanted": 14, "heard": 14, "problems": []}
