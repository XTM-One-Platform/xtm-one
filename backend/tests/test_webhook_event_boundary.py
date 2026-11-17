"""An integration-event assignment fires on the event it selected, not on a
sibling whose name merely starts with it.

GitHub event keys are ``{X-GitHub-Event}.{action}``. The old matchers were a
bare ``str.startswith``, so an assignment on ``pull_request`` also ran on every
``pull_request_review_comment.created``, and ``repository`` on
``repository_advisory.published``. The match now has to cross the ``.``.
"""

from __future__ import annotations

import logging
import uuid
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import webhooks
from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.api.v1.webhooks import (
    _AssignmentMatcher,
    _event_key_matches,
    _match_provider_assignments,
    _ProviderRouting,
)
from app.models.assignment import (
    Assignment,
    _load_trigger_config,
    uses_exact_event_matching,
)

# This file pins the exact rules a newly created or saved trigger uses;
# the legacy prefix match of older triggers is pinned, path by path, in
# test_webhook_event_matching_modes.py.
_event_key_allowed = partial(webhooks._event_key_allowed, exact=True)
_event_selected = partial(webhooks._event_selected, exact=True)
EXACT = {"event_matching": "exact"}


def _routing(*matchers: _AssignmentMatcher) -> _ProviderRouting:
    return _ProviderRouting(
        oauth_default_exists=False,
        extra_config_enc=None,
        client_secret_enc=None,
        has_agents=True,
        matchers=tuple(matchers),
    )


def _matcher(**overrides) -> _AssignmentMatcher:
    defaults = dict(
        agent_id=str(uuid.uuid4()),
        assignment_id=str(uuid.uuid4()),
        trigger_type="integration_event",
        provider="github",
        event="",
        path="",
        events_filter=(),
        exact=True,
    )
    defaults.update(overrides)
    return _AssignmentMatcher(**defaults)


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event_key,selected,expected",
    [
        ("issues", "issues", True),
        ("issues.opened", "issues", True),
        ("pull_request.opened", "pull_request", True),
        # The reported false positives.
        ("pull_request_review_comment.created", "pull_request", False),
        ("pull_request_review.submitted", "pull_request", False),
        ("pull_request_review_thread.resolved", "pull_request_review", False),
        ("repository_advisory.published", "repository", False),
        ("projects_v2_item.edited", "projects_v2", False),
        # An action-qualified selection matches its own action, never a
        # sibling action whose name starts with it.
        ("issues.opened", "issues.opened", True),
        ("issues.closed", "issues.opened", False),
        ("issues.opened_by_bot", "issues.opened", False),
        # Below the dot the key keeps nesting: a repository_dispatch action is
        # the caller's own event_type and may carry dots, and a generic
        # sender's type is often hierarchical.
        ("repository_dispatch.deploy.staging", "repository_dispatch.deploy", True),
        ("repository_dispatch.deployment", "repository_dispatch.deploy", False),
        ("customer.subscription.created", "customer.subscription", True),
        # A trailing dot or surrounding blanks typed into a config still mean
        # the event.
        ("issues.opened", "issues.", True),
        ("issues.opened", " issues ", True),
        ("", "issues", False),
        ("issues", "", False),
        ("issues", " . ", False),
    ],
)
def test_event_key_matches(event_key, selected, expected):
    assert _event_key_matches(event_key, selected) is expected


@pytest.mark.parametrize(
    "event_key,selected",
    [
        (None, "issues"),
        (3, "issues"),
        ({"type": "issues"}, "issues"),
        ("issues.opened", None),
        ("issues.opened", ["issues"]),
        ("issues.opened", 3),
    ],
)
def test_event_key_matches_refuses_a_non_string(event_key, selected):
    """Both sides are free JSON (the stored trigger_config, the request body):
    a non-string matches nothing instead of raising."""
    assert _event_key_matches(event_key, selected) is False


def test_events_allow_list_requires_the_boundary():
    assert _event_key_allowed("push", ["push"])
    assert _event_key_allowed("push.branch", ["push"])
    assert not _event_key_allowed("pull_request_review.submitted", ["pull_request"])
    assert _event_key_allowed("pull_request_review.submitted", ["push", "pull_request_review"])


def test_events_allow_list_reads_a_string_as_a_comma_separated_list():
    """A config holding a string instead of a list is not walked letter by
    letter: once the boundary is required, single letters match nothing."""
    assert _event_key_allowed("pull_request.opened", "pull_request")
    assert _event_key_allowed("issues.opened", "push, issues")
    assert not _event_key_allowed("pull_request_review.submitted", "pull_request")


def test_events_allow_list_skips_a_malformed_entry():
    assert _event_key_allowed("push.branch", [None, 3, "", "push"])
    assert not _event_key_allowed("pull_request.opened", [None, 3, "", "push"])


@pytest.mark.parametrize("events_filter", [[""], ["", ""], ",", [], None])
def test_events_allow_list_of_empty_entries_filters_nothing(events_filter):
    """The old bare prefix let every delivery through ``[""]`` (every key
    starts with ""). A list of nothing but empty entries keeps filtering
    nothing, like an empty one, instead of silently blocking every delivery."""
    assert _event_key_allowed("push", events_filter)
    assert _event_key_allowed("pull_request.opened", events_filter)


@pytest.mark.parametrize(
    "events_filter",
    [[None, 3], [None, ""], [["push"]], 5, True, False, 0, {}, {"push": True}],
)
def test_events_allow_list_holding_no_event_name_admits_nothing(events_filter):
    """A value that is not a string names no event. Skipping it must not turn
    the allow-list into no filter at all: on ``main`` these raised, so the
    assignment never ran, and it still does not run on everything."""
    assert not _event_key_allowed("push", events_filter)
    assert not _event_key_allowed("pull_request.opened", events_filter)


def test_events_allow_list_of_blanks_keeps_its_old_meaning():
    """A blank that is not empty never matched a key and still does not."""
    assert not _event_key_allowed("push", [" "])
    assert not _event_key_allowed("push", ".")


@pytest.mark.parametrize("selected", ["", None])
def test_no_event_selection_admits_every_event(selected):
    assert _event_selected("pull_request_review.submitted", selected)


@pytest.mark.parametrize(
    "selected",
    [3, True, ["pull_request"], {"event": "pull_request"}, "  ", ".", False, 0, {}],
)
def test_an_event_selection_naming_no_event_admits_none(selected):
    """The live receivers never stringify a selection: ``3`` does not match a
    key ``3``, and a list does not match its own items. A falsy malformed
    value (``False``, ``0``, ``{}``) is a selection too, not an absent one."""
    for event_key in ("pull_request.opened", "3", "3.opened", "push"):
        assert not _event_selected(event_key, selected)


def test_a_selection_naming_no_event_is_reported_once_per_assignment(caplog):
    assignment_id = uuid.uuid4()
    with caplog.at_level(logging.WARNING, logger=webhooks.logger.name):
        for _ in range(3):
            assert not _event_selected("push", ["push"], assignment_id=assignment_id)
            assert not _event_key_allowed("push", 5, assignment_id=assignment_id)
    reports = [r.getMessage() for r in caplog.records if str(assignment_id) in r.getMessage()]
    assert len(reports) == 2
    assert any("trigger_config.event names no event (['push'])" in m for m in reports)
    assert any("trigger_config.events names no event (5)" in m for m in reports)


def test_no_catalog_event_matches_a_sibling_event():
    """Walk every provider's declared events: none may select another."""
    checked = 0
    for provider, entry in PROVIDER_CATALOG.items():
        events = [e["event"] for e in entry.get("available_events") or []]
        for selected in events:
            assert _event_key_matches(f"{selected}.opened", selected), (provider, selected)
            for other in events:
                if other == selected:
                    continue
                checked += 1
                assert not _event_key_matches(other, selected), (provider, selected, other)
                assert not _event_key_matches(f"{other}.created", selected), (
                    provider,
                    selected,
                    other,
                )
    assert checked > 0


# ---------------------------------------------------------------------------
# The cached provider routing (/api/hooks/providers/{provider})
# ---------------------------------------------------------------------------


def test_provider_routing_skips_a_sibling_event():
    pr = _matcher(event="pull_request")
    review = _matcher(event="pull_request_review")
    routing = _routing(pr, review)

    assert _match_provider_assignments(routing, "github", "pull_request.opened") == [pr]
    assert _match_provider_assignments(
        routing, "github", "pull_request_review_comment.created"
    ) == []
    assert _match_provider_assignments(
        routing, "github", "pull_request_review.submitted"
    ) == [review]


def test_provider_routing_events_filter_requires_the_boundary():
    legacy = _matcher(
        trigger_type="webhook", provider=None, path="github", events_filter=("pull_request",)
    )
    routing = _routing(legacy)
    assert _match_provider_assignments(routing, "github", "pull_request.closed") == [legacy]
    assert _match_provider_assignments(
        routing, "github", "pull_request_review.submitted"
    ) == []


class _LoaderResult:
    def __init__(self, *, scalar_one=None, rows=None):
        self._scalar_one = scalar_one
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar_one

    def all(self):
        return self._rows


class _LoaderSession:
    """Scripted session for ``_load_provider_routing``: the OAuthDefault, the
    bound agents (id, owner, bindings, unreadable), then the assignment column tuples."""

    def __init__(self, agent_ids, assignment_rows):
        self._results = [
            _LoaderResult(scalar_one=None),
            _LoaderResult(rows=[(a, None, None, False) for a in agent_ids]),
            _LoaderResult(rows=assignment_rows),
        ]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _stmt):
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_routing_loader_reads_the_events_allow_list_like_the_live_paths(monkeypatch):
    """A string ``events`` is a comma list here too, and a value that is not a
    list must not make the loader raise (it backs every delivery of the
    provider) nor widen the assignment to every event."""
    agent_id = uuid.uuid4()
    as_string = uuid.uuid4()
    not_a_list = uuid.uuid4()
    unfiltered = uuid.uuid4()
    rows = [
        (as_string, agent_id, "webhook", {"path": "github", "events": "pull_request", **EXACT}),
        (not_a_list, agent_id, "webhook", {"path": "github", "events": 5, **EXACT}),
        (unfiltered, agent_id, "webhook", {"path": "github", "events": [""], **EXACT}),
    ]
    session = _LoaderSession([agent_id], rows)
    monkeypatch.setattr(webhooks, "async_session", lambda: session)

    routing = await webhooks._load_provider_routing("github")

    by_id = {m.assignment_id: m for m in routing.matchers}
    assert by_id[str(as_string)].events_filter == ("pull_request",)
    assert by_id[str(unfiltered)].events_filter == ()
    assert str(not_a_list) not in by_id

    def matched(event_key: str) -> set[str]:
        return {
            m.assignment_id
            for m in _match_provider_assignments(routing, "github", event_key)
        }

    assert matched("pull_request.opened") == {str(as_string), str(unfiltered)}
    assert matched("pull_request_review.submitted") == {str(unfiltered)}


@pytest.mark.asyncio
async def test_routing_loader_leaves_out_a_selection_naming_no_event(monkeypatch):
    """The snapshot read an ``event`` that is not a string as no selection,
    so the assignment ran on every delivery of the provider while the live
    receivers ran it on none. A config that is not
    an object must not make the loader raise either."""
    agent_id = uuid.uuid4()
    selected = uuid.uuid4()
    rows = [
        (selected, agent_id, "integration_event", {"provider": "github", "event": "pull_request", **EXACT}),
        *[
            (uuid.uuid4(), agent_id, "integration_event", {"provider": "github", "event": value, **EXACT})
            for value in (3, True, ["pull_request"], {"x": 1}, "  ", [], 0)
        ],
        (uuid.uuid4(), agent_id, "integration_event", ["not", "an", "object"]),
    ]
    session = _LoaderSession([agent_id], rows)
    monkeypatch.setattr(webhooks, "async_session", lambda: session)

    routing = await webhooks._load_provider_routing("github")

    assert [m.assignment_id for m in routing.matchers] == [str(selected)]
    for event_key in ("push", "3", "3.opened", "pull_request_review.submitted"):
        assert _match_provider_assignments(routing, "github", event_key) == []


_PARITY_KEYS = (
    "pull_request.opened",
    "pull_request_review.submitted",
    "issues.opened",
    "issues.opened_by_bot",
    "3",
    "push",
    None,
)


@pytest.mark.parametrize(
    "config",
    [
        {"provider": "github"},
        {"provider": "github", "event": ""},
        {"provider": "github", "event": None},
        {"provider": "github", "event": "pull_request"},
        {"provider": "github", "event": "pull_request."},
        {"provider": "github", "event": "issues.opened"},
        {"provider": "github", "event": 3},
        {"provider": "github", "event": ["pull_request"]},
        {"provider": "github", "event": "  "},
        {"provider": "github", "event_type": "pull_request"},
        {"provider": "github", "event_type": ["pull_request"]},
        {"provider": "github", "event": 3, "event_type": "issues"},
        {"provider": "slack", "event": "pull_request"},
    ],
)
@pytest.mark.parametrize("exact", [True, False])
def test_the_routing_snapshot_takes_the_deliveries_the_live_receivers_take(config, exact):
    """The integration URL filters the ORM row, load listener applied, with
    ``_event_selected``: the snapshot built from the same stored config must
    take exactly the same deliveries."""
    config = {**config, **EXACT} if exact else config
    row = Assignment(trigger_type="integration_event", trigger_config=dict(config))
    _load_trigger_config(row, None)
    live_config = row.trigger_config
    matcher = webhooks._assignment_matcher(
        "github", uuid.uuid4(), uuid.uuid4(), "integration_event", config
    )

    for event_key in _PARITY_KEYS:
        live = live_config.get("provider") == "github" and webhooks._event_selected(
            event_key, live_config.get("event"), exact=uses_exact_event_matching(live_config)
        )
        cached = matcher is not None and webhooks._matcher_admits(matcher, "github", event_key)
        assert cached is live, (config, event_key)


@pytest.mark.parametrize(
    "events",
    [
        None,
        [],
        [""],
        "pull_request",
        "push, issues",
        ["pull_request", None],
        [None, 3],
        5,
        [" "],
        False,
        0,
        {},
        {"pull_request": True},
    ],
)
@pytest.mark.parametrize("exact", [True, False])
def test_the_routing_snapshot_filters_an_allow_list_like_the_live_receivers(events, exact):
    config = {"path": "github", "events": events, **(EXACT if exact else {})}
    matcher = webhooks._assignment_matcher("github", uuid.uuid4(), uuid.uuid4(), "webhook", config)

    for event_key in _PARITY_KEYS:
        live = webhooks._event_key_allowed(event_key, config["events"], exact=exact)
        cached = matcher is not None and webhooks._matcher_admits(matcher, "github", event_key)
        assert cached is live, (events, event_key)


# ---------------------------------------------------------------------------
# The per-assignment URL (/api/hooks/integrations/{id}/assignments/{id})
# ---------------------------------------------------------------------------


class _Request(SimpleNamespace):
    async def body(self):
        return self._body


def _install_per_assignment_fakes(monkeypatch, *, provider: str, trigger_config: dict):
    integration = SimpleNamespace(
        id=uuid.uuid4(), provider=provider, extra_config_enc=None
    )
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=None,
        is_company_managed=False,
        name="Review PRs",
        trigger_type="integration_event",
        trigger_config=trigger_config,
        ignore_self_triggered_events=False,
    )
    agent = SimpleNamespace(id=assignment.agent_id, name="Reviewer", enabled=True)

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _DB:
        async def execute(self, stmt, *args, **kwargs):
            text = str(stmt)
            if "FROM agents" in text:
                return _Result(agent)
            if "FROM assignments" in text:
                return _Result(assignment)
            return _Result(integration)

        async def commit(self):
            return None

    class _SessionCtx:
        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(webhooks, "async_session", lambda: _SessionCtx())
    monkeypatch.setattr(webhooks, "enforce_rate_limit", AsyncMock(return_value=None))
    monkeypatch.setattr(
        webhooks, "_claim_webhook_dedup_slot", AsyncMock(return_value=(False, None, None))
    )
    monkeypatch.setattr(webhooks, "_try_verify_webhook_signature", lambda *a, **kw: None)
    trigger = AsyncMock(return_value="run-1")
    monkeypatch.setattr(webhooks, "trigger_assignment", trigger)
    return integration, assignment, trigger


def _github_request(event: str | None, action: str) -> _Request:
    headers = {"Content-Type": "application/json"}
    if event is not None:
        headers["X-GitHub-Event"] = event
    return _Request(
        headers=headers,
        _body=b'{"action": "%s"}' % action.encode(),
        method="POST",
        url="http://t/api/hooks/integrations/x/assignments/y",
        client=SimpleNamespace(host="203.0.113.7"),
        query_params={},
    )


@pytest.mark.asyncio
async def test_per_assignment_github_webhook_ignores_an_unselected_event(monkeypatch):
    integration, assignment, trigger = _install_per_assignment_fakes(
        monkeypatch,
        provider="github",
        trigger_config={"provider": "github", "event": "pull_request", **EXACT},
    )

    response = await webhooks.receive_assignment_webhook(
        str(integration.id),
        str(assignment.id),
        _github_request("pull_request_review_comment", "created"),
    )

    assert response["count"] == 0
    assert response["reason"] == "event not selected"
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_per_assignment_github_webhook_runs_the_selected_event(monkeypatch):
    integration, assignment, trigger = _install_per_assignment_fakes(
        monkeypatch,
        provider="github",
        trigger_config={"provider": "github", "event": "pull_request"},
    )

    response = await webhooks.receive_assignment_webhook(
        str(integration.id),
        str(assignment.id),
        _github_request("pull_request", "opened"),
    )

    assert response["count"] == 1
    trigger.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(("events", "runs"), [(False, False), (0, False), ({}, False), ([], True)])
async def test_per_assignment_webhook_reads_a_falsy_events_value_like_every_path(
    monkeypatch, events, runs
):
    """The per-assignment receiver asks the shared reader whether ``events``
    filters: on an exact trigger a falsy malformed value admits nothing here
    too, while the empty list (the handlers' own default) still filters
    nothing. A legacy trigger keeps main's reading (a falsy value filtered
    nothing), pinned in test_webhook_event_matching_modes.py."""
    integration, assignment, trigger = _install_per_assignment_fakes(
        monkeypatch,
        provider="github",
        trigger_config={"provider": "github", "events": events, **EXACT},
    )

    response = await webhooks.receive_assignment_webhook(
        str(integration.id),
        str(assignment.id),
        _github_request("pull_request", "opened"),
    )

    assert response["count"] == (1 if runs else 0)
    assert trigger.await_count == (1 if runs else 0)


@pytest.mark.asyncio
async def test_per_assignment_github_url_holds_only_a_github_delivery(monkeypatch):
    """The selection is enforced on what ``X-GitHub-Event`` identifies. A POST
    without it (a manual test, a relay that drops headers) is keyed on the
    generic ``type`` / ``action`` fallback, which never names a GitHub event,
    and this URL ran it before the check existed."""
    integration, assignment, trigger = _install_per_assignment_fakes(
        monkeypatch,
        provider="github",
        trigger_config={"provider": "github", "event": "pull_request"},
    )

    response = await webhooks.receive_assignment_webhook(
        str(integration.id),
        str(assignment.id),
        _github_request(None, "opened"),
    )

    assert response["count"] == 1
    trigger.assert_awaited_once()


# ---------------------------------------------------------------------------
# The integration-level URL (/api/hooks/integrations/{id})
# ---------------------------------------------------------------------------


class _FanOutResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._value))

    def all(self):
        # The agent query's ``(agent, unreadable)`` rows.
        return [(item, False) for item in self._value]


@pytest.mark.asyncio
async def test_integration_webhook_skips_a_malformed_selection_and_runs_the_rest(
    monkeypatch,
):
    """``trigger_config`` is free JSON: one assignment whose ``event`` is not a
    string must not fail the delivery for every other assignment bound to
    the integration."""
    integration = SimpleNamespace(
        id=uuid.uuid4(), provider="github", extra_config_enc=None
    )
    agent = SimpleNamespace(id=uuid.uuid4(), name="Reviewer", enabled=True)

    def _assignment(name: str, event: object) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid.uuid4(),
            agent_id=agent.id,
            name=name,
            trigger_type="integration_event",
            trigger_config={"provider": "github", "event": event},
            ignore_self_triggered_events=False,
        )

    malformed = _assignment("Malformed", ["pull_request"])
    selected = _assignment("Review PRs", "pull_request")

    class _DB:
        async def execute(self, stmt, *args, **kwargs):
            text = str(stmt)
            if "FROM agents" in text:
                return _FanOutResult([agent])
            if "FROM assignments" in text:
                return _FanOutResult([malformed, selected])
            return _FanOutResult(integration)

        async def commit(self):
            return None

    class _SessionCtx:
        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(webhooks, "async_session", lambda: _SessionCtx())
    monkeypatch.setattr(
        webhooks, "_claim_webhook_dedup_slot", AsyncMock(return_value=(False, None, None))
    )
    monkeypatch.setattr(webhooks, "_try_verify_webhook_signature", lambda *a, **kw: None)
    trigger = AsyncMock(return_value="run-1")
    monkeypatch.setattr(webhooks, "trigger_assignment", trigger)

    response = await webhooks.receive_integration_webhook(
        str(integration.id), _github_request("pull_request", "opened")
    )

    assert response["count"] == 1
    trigger.assert_awaited_once()
    assert trigger.await_args.kwargs["assignment"] is selected
