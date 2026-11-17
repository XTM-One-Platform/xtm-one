"""Integration-event trigger filters on webhook-delivered events.

The provider, integration and per-assignment webhook receivers apply an
``integration_event`` trigger's ``filter`` to the delivered body before any run
or gate call (``webhooks._trigger_filter_reason``), the cached provider routing
included (``_AssignmentMatcher.payload_filter``). Events outside the mail,
Teams, DocuSign and calendar families use the generic payload predicate
(``integration_event_filters.payload_filter_reason``).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.v1.webhooks import (
    _assignment_matcher,
    _match_provider_assignments,
    _ProviderRouting,
    _trigger_filter_reason,
)
from app.services import graph_event_subscriptions as subs
from app.services.integration_event_filters import event_filter_reason, payload_filter_reason

_REPOS = ["XTM-One-Platform/xtm-one", "FiligranHQ/platform"]
_FILTER = {"action": "completed", "workflow_run.conclusion": "failure", "repository.full_name": _REPOS}


def _workflow_run(action: str = "completed", conclusion: str | None = "failure", repo: str = _REPOS[0]) -> dict:
    return {
        "action": action,
        "workflow_run": {"conclusion": conclusion, "head_branch": "fix/x"},
        "repository": {"full_name": repo},
    }


def _routing(*trigger_configs: dict) -> tuple[_ProviderRouting, list[str]]:
    ids = [str(uuid.uuid4()) for _ in trigger_configs]
    matchers = tuple(
        _assignment_matcher("github", wf_id, uuid.uuid4(), "integration_event", config)
        for wf_id, config in zip(ids, trigger_configs, strict=True)
    )
    assert all(m is not None for m in matchers)
    routing = _ProviderRouting(
        oauth_default_exists=False,
        extra_config_enc=None,
        client_secret_enc=None,
        has_agents=True,
        matchers=matchers,  # type: ignore[arg-type]
    )
    return routing, ids


# -- the generic payload predicate ------------------------------------------


def test_a_matching_workflow_run_passes():
    assert payload_filter_reason(_workflow_run(), _FILTER) is None
    assert event_filter_reason("workflow_run", _workflow_run(), _FILTER) is None


@pytest.mark.parametrize(
    ("payload", "key"),
    [
        (_workflow_run(action="in_progress", conclusion=None), "action"),
        (_workflow_run(conclusion="success"), "workflow_run.conclusion"),
        (_workflow_run(repo="someone/else"), "repository.full_name"),
    ],
)
def test_a_non_matching_workflow_run_is_dropped_naming_the_key(payload, key):
    reason = event_filter_reason("workflow_run", payload, _FILTER)
    assert reason is not None and key in reason


def test_a_missing_field_is_dropped():
    assert "workflow_run.conclusion" in (payload_filter_reason({"action": "completed"}, _FILTER) or "")


def test_a_literal_dotted_key_wins_over_the_path():
    payload = {"a.b": "literal", "a": {"b": "nested"}}
    assert payload_filter_reason(payload, {"a.b": "literal"}) is None
    assert payload_filter_reason(payload, {"a.b": "nested"}) is not None


def test_nested_objects_are_a_subset_and_lists_any_of():
    payload = {"workflow_run": {"conclusion": "failure", "event": "push"}}
    assert payload_filter_reason(payload, {"workflow_run": {"conclusion": ["failure", "timed_out"]}}) is None
    assert payload_filter_reason(payload, {"workflow_run": {"conclusion": "success"}}) is not None


def test_a_none_option_does_not_match_every_value():
    assert payload_filter_reason({"x": "value"}, {"x": [None]}) is not None


def test_no_filter_passes_everything():
    assert payload_filter_reason(_workflow_run(), {}) is None
    assert event_filter_reason("workflow_run", _workflow_run(), None) is None


def test_read_side_keys_are_not_compared_with_the_payload():
    assert payload_filter_reason({"a": 1}, {"scope": {"hubspot_object": ["deal"]}, "channels": ["t:c"]}) is None


# -- the mail, Teams and calendar filters are unchanged ----------------------


def test_mail_teams_and_calendar_filters_keep_their_keys():
    mail = {"from": "ceo@example.com", "subject": "Board"}
    assert event_filter_reason("new_email", mail, {"from": {"allow": ["example.com"]}}) is None
    assert event_filter_reason("new_email", mail, {"from": {"deny": ["example.com"]}}) == "sender is on the deny list"
    # A mail filter key the payload does not carry is not a generic payload key.
    assert event_filter_reason("new_email", mail, {"exclude_automated": True}) is None
    teams = {"team_id": "t", "channel_id": "c", "from_name": "Bot"}
    assert event_filter_reason("new_teams_message", teams, {"exclude_authors": ["Bot"]}) == "author is excluded"
    assert event_filter_reason("new_teams_message", teams, {"channels": ["t:c"]}) is None
    calendar = {"change_kind": "updated"}
    assert event_filter_reason("calendar_event_changed", calendar, {"change_kinds": ["created"]}) is not None
    assert event_filter_reason("calendar_event_changed", calendar, {"change_kinds": ["updated"]}) is None


# -- the webhook receivers ---------------------------------------------------


def test_the_receiver_helper_applies_the_trigger_filter():
    assert _trigger_filter_reason("workflow_run", _workflow_run(), _FILTER) is None
    assert _trigger_filter_reason("workflow_run", _workflow_run(conclusion="success"), _FILTER) is not None
    assert _trigger_filter_reason("workflow_run", _workflow_run(conclusion="success"), {}) is None
    assert _trigger_filter_reason("workflow_run", ["not", "a", "dict"], _FILTER) is None


def test_the_snapshot_matcher_carries_the_filter():
    config = {"provider": "github", "event": "workflow_run", "event_matching": "exact", "filter": _FILTER}
    matcher = _assignment_matcher("github", uuid.uuid4(), uuid.uuid4(), "integration_event", config)
    assert matcher is not None and matcher.payload_filter == _FILTER
    # Still hashable: the dict is left out of the hash.
    hash(matcher)
    webhook = _assignment_matcher("github", uuid.uuid4(), uuid.uuid4(), "webhook", {"path": "github", "filter": _FILTER})
    assert webhook is not None and webhook.payload_filter is None


@pytest.mark.parametrize(
    "payload",
    [
        _workflow_run(action="in_progress", conclusion=None),
        _workflow_run(conclusion="success"),
        _workflow_run(repo="someone/else"),
    ],
)
def test_the_provider_snapshot_drops_and_counts_a_filtered_delivery(payload):
    config = {"provider": "github", "event": "workflow_run", "event_matching": "exact", "filter": _FILTER}
    routing, ids = _routing(config)
    filtered: dict[str, int] = {}
    event_key = f"workflow_run.{payload['action']}"
    assert _match_provider_assignments(routing, "github", event_key, payload=payload, filtered=filtered) == []
    assert filtered == {ids[0]: 1}


def test_the_provider_snapshot_runs_a_matching_delivery_exactly_once():
    config = {"provider": "github", "event": "workflow_run", "event_matching": "exact", "filter": _FILTER}
    unfiltered = {"provider": "github", "event": "workflow_run", "event_matching": "exact"}
    routing, ids = _routing(config, unfiltered)
    filtered: dict[str, int] = {}
    matched = _match_provider_assignments(
        routing, "github", "workflow_run.completed", payload=_workflow_run(), filtered=filtered
    )
    assert sorted(m.assignment_id for m in matched) == sorted(ids)
    assert filtered == {}
    # Without the payload (the pre-claim scan), the filter is not applied.
    assert len(_match_provider_assignments(routing, "github", "workflow_run.in_progress")) == 2


# -- the delivery label ------------------------------------------------------


class _Redis:
    async def get(self, _key):
        return None


@pytest.mark.asyncio
async def test_a_webhook_delivered_event_is_not_labelled_a_poll():
    wf = SimpleNamespace(id=uuid.uuid4(), trigger_config={"provider": "github", "event": "workflow_run"})
    mode = await subs.delivery_mode(_Redis(), wf)
    assert mode["mode"] == "webhook" and "interval_seconds" not in mode


@pytest.mark.asyncio
async def test_a_polled_event_keeps_its_poll_interval():
    wf = SimpleNamespace(id=uuid.uuid4(), trigger_config={"provider": "slack", "event": "new_slack_message"})
    mode = await subs.delivery_mode(_Redis(), wf)
    assert mode["mode"] == "poll" and mode["interval_seconds"] == 300
