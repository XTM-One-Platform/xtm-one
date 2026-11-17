"""Legacy vs exact event matching on every webhook path.

A trigger saved before exact matching carries no ``event_matching`` marker and
keeps matching exactly as it did: a bare selection also runs on its sibling
events (``pull_request`` on ``pull_request_review``), a bare-prefix allow-list
entry keeps admitting what starts with it, and the per-assignment URL runs on
every GitHub delivery, ``ping`` included. It keeps that through every save that
leaves its events alone, and through a copy. A trigger created from scratch, or
whose events are chosen again, is written with ``event_matching: "exact"``, and
``WEBHOOK_EXACT_EVENT_MATCHING`` applies exact matching to every trigger. No
existing deployment has to change anything.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy.orm.attributes import set_committed_value

from app.api.v1 import security_audit, webhooks
from app.core import log_once
from app.core.config import settings
from app.models import assignment as assignment_model
from app.models.agent_copy import build_assignment_catalog_clone_kwargs, build_assignment_copy_kwargs
from app.models.assignment import (
    EVENT_MATCHING_EXACT,
    EVENT_MATCHING_KEY,
    EVENT_MATCHING_TRIGGER_TYPES,
    Assignment,
    _saved_trigger_keeps_its_matching,
    mark_exact_event_matching,
    new_trigger_config,
    settle_event_matching,
    uses_exact_event_matching,
)
from tests.notice_store import new_process, share_notices

MODES = ("legacy", "exact", "global")


@pytest.fixture(params=MODES)
def mode(request, monkeypatch):
    monkeypatch.setattr(settings, "webhook_exact_event_matching", request.param == "global")
    return request.param


def _config(mode: str, **config) -> dict:
    if mode == "exact":
        config[EVENT_MATCHING_KEY] = EVENT_MATCHING_EXACT
    return config


def _runs_on_sibling(mode: str) -> bool:
    return mode == "legacy"


# ---------------------------------------------------------------------------
# The mode and the shared rules
# ---------------------------------------------------------------------------


def test_the_mode_is_read_from_the_marker_then_the_global_setting(monkeypatch):
    monkeypatch.setattr(settings, "webhook_exact_event_matching", False)
    assert not uses_exact_event_matching({"provider": "github", "event": "issues"})
    assert uses_exact_event_matching({"event": "issues", "event_matching": "exact"})
    assert not uses_exact_event_matching(None)
    monkeypatch.setattr(settings, "webhook_exact_event_matching", True)
    assert uses_exact_event_matching({"provider": "github", "event": "issues"})


@pytest.mark.parametrize(
    "event_key,selected",
    [
        ("pull_request_review.submitted", "pull_request"),
        ("repository_advisory.published", "repository"),
        ("issues.opened_by_bot", "issues.opened"),
    ],
)
def test_legacy_selection_keeps_its_prefix_match(event_key, selected):
    assert webhooks._event_selected(event_key, selected, exact=False)
    assert not webhooks._event_selected(event_key, selected, exact=True)


@pytest.mark.parametrize("selected", [None, "", [], 0, False])
def test_legacy_empty_selection_admits_every_event_as_on_main(selected):
    assert webhooks._event_selected("push", selected, exact=False)


@pytest.mark.parametrize("selected", [[], {}, 0, False])
def test_exact_empty_value_that_is_not_none_or_blank_admits_nothing(selected):
    """Only ``null`` and ``""`` mean no selection on an
    exact trigger; ``[]``, ``{}``, ``0`` and ``False`` name no event."""
    assert not webhooks._event_selected("push", selected, exact=True)


@pytest.mark.parametrize(
    "events_filter,event_key",
    [
        (["pull_request"], "pull_request_review.submitted"),
        (["pull"], "pull_request.opened"),
        (["", "issues"], "push"),
    ],
)
def test_legacy_allow_list_keeps_mains_prefix_match(events_filter, event_key):
    assert webhooks._event_key_allowed(event_key, events_filter, exact=False)
    assert not webhooks._event_key_allowed(event_key, events_filter, exact=True)


@pytest.mark.parametrize("events_filter", [0, False, {}])
def test_falsy_allow_list_filters_nothing_on_legacy_and_everything_on_exact(events_filter):
    assert webhooks._event_key_allowed("push", events_filter, exact=False)
    assert not webhooks._event_key_allowed("push", events_filter, exact=True)


@pytest.mark.parametrize("events_filter", [[None, 3], 5, True])
def test_allow_list_that_raised_on_main_matches_nothing_in_both_modes(events_filter):
    assert not webhooks._event_key_allowed("push", events_filter, exact=False)
    assert not webhooks._event_key_allowed("push", events_filter, exact=True)


def test_a_legacy_widening_is_logged_once_per_assignment(caplog):
    assignment_id = uuid.uuid4()
    with caplog.at_level(logging.WARNING, logger=webhooks.logger.name):
        for _ in range(3):
            assert webhooks._event_selected(
                "pull_request_review.submitted",
                "pull_request",
                exact=False,
                assignment_id=assignment_id,
            )
            assert webhooks._event_key_allowed(
                "pull_request_review.submitted",
                ["pull_request"],
                exact=False,
                assignment_id=assignment_id,
            )
    reports = [r.getMessage() for r in caplog.records if str(assignment_id) in r.getMessage()]
    assert len(reports) == 1
    assert "legacy event matching" in reports[0]
    assert "WEBHOOK_EXACT_EVENT_MATCHING" in reports[0]


def test_the_legacy_report_reads_the_event_name_whole(caplog):
    """``reprlib.repr`` cut it at 30 characters: 'pull_request...iew.submitted'."""
    assignment_id = uuid.uuid4()
    with caplog.at_level(logging.WARNING, logger=webhooks.logger.name):
        webhooks._event_selected(
            "pull_request_review.submitted", "pull_request", exact=False, assignment_id=assignment_id
        )
    [report] = [r.getMessage() for r in caplog.records if str(assignment_id) in r.getMessage()]
    assert "ran it on 'pull_request_review.submitted', which trigger_config.event ('pull_request')" in report
    assert "..." not in report


def test_an_event_name_from_a_request_stays_bounded_and_on_one_line(caplog):
    assignment_id = uuid.uuid4()
    forged = "pull_request_review." + "a" * 5000 + "\r\nINFO forged line"
    with caplog.at_level(logging.WARNING, logger=webhooks.logger.name):
        webhooks._event_selected(forged, "pull_request", exact=False, assignment_id=assignment_id)
    [report] = [r.getMessage() for r in caplog.records if str(assignment_id) in r.getMessage()]
    assert len(report) < 1000
    assert "\r" not in report
    assert "\n" not in report


async def test_a_legacy_widening_is_said_once_a_day_for_the_whole_platform(monkeypatch, caplog):
    """Every uvicorn worker, recycle and replica routes the deliveries."""
    notices = share_notices(monkeypatch)
    assignment_id = uuid.uuid4()
    with caplog.at_level(logging.WARNING, logger=webhooks.logger.name):
        for _ in range(3):
            new_process()
            webhooks._event_selected(
                "pull_request_review.submitted", "pull_request", exact=False, assignment_id=assignment_id
            )
            await log_once.settle()
    assert len([r for r in caplog.records if str(assignment_id) in r.getMessage()]) == 1
    [claim] = notices.claims()
    assert claim.endswith(f":webhook_selection:{assignment_id}:legacy")
    assert notices.ttls[claim] == 24 * 60 * 60


def test_an_exact_match_is_never_reported_as_a_widening(caplog):
    assignment_id = uuid.uuid4()
    with caplog.at_level(logging.WARNING, logger=webhooks.logger.name):
        assert webhooks._event_selected(
            "pull_request.opened", "pull_request", exact=False, assignment_id=assignment_id
        )
    assert not [r for r in caplog.records if str(assignment_id) in r.getMessage()]


# ---------------------------------------------------------------------------
# The write side: only a new trigger, or new events, are written exact
# ---------------------------------------------------------------------------


def _model_listeners(event_name: str) -> list:
    """The ``app.models.assignment`` functions behind a mapper event's listeners.

    SQLAlchemy registers a wrapper per listener; the function it calls is in
    the wrapper's closure.
    """
    found = []
    for wrapper in getattr(Assignment.__mapper__.dispatch, event_name):
        for fn in (wrapper, *(cell.cell_contents for cell in wrapper.__closure__ or ())):
            if callable(fn) and getattr(fn, "__module__", None) == assignment_model.__name__:
                found.append(fn)
    return found


def test_the_save_listener_is_registered_and_nothing_marks_an_inserted_row():
    assert sa_event.contains(Assignment, "before_update", _saved_trigger_keeps_its_matching)
    assert _model_listeners("before_update") == [_saved_trigger_keeps_its_matching]
    # A copy (duplicate, import, catalog clone) is an insert: marking inserts
    # would switch a legacy source's copy to exact matching.
    assert _model_listeners("before_insert") == []


@pytest.mark.parametrize("trigger_type", ["integration_event", "webhook"])
def test_a_new_event_trigger_is_written_exact(trigger_type):
    config = new_trigger_config(trigger_type, {"provider": "github", "event": "issues"})
    assert config[EVENT_MATCHING_KEY] == EVENT_MATCHING_EXACT


def test_a_new_trigger_names_its_event_under_the_key_every_reader_reads():
    config = new_trigger_config("integration_event", {"provider": "microsoft", "event_type": "new_email"})
    assert config == {"provider": "microsoft", "event": "new_email", EVENT_MATCHING_KEY: EVENT_MATCHING_EXACT}


def test_a_trigger_that_selects_no_event_is_left_as_is():
    config = {"cron": "0 9 * * 1-5"}
    assert new_trigger_config("schedule", config) is config
    assert mark_exact_event_matching("manual", {}) == {}


def _source_row(config: dict, trigger_type: str = "integration_event") -> Assignment:
    return Assignment(name="Review PRs", prompt="p", trigger_type=trigger_type, trigger_config=config)


@pytest.mark.parametrize(
    "build", [build_assignment_copy_kwargs, build_assignment_catalog_clone_kwargs], ids=["duplicate", "clone"]
)
def test_a_copy_keeps_its_source_trigger_as_it_is(build):
    """The copy paths carry the source config whole, and nothing adds the marker."""
    legacy = {"provider": "github", "event": "pull_request"}
    exact = {**legacy, EVENT_MATCHING_KEY: EVENT_MATCHING_EXACT}
    for source_config in (legacy, exact):
        kwargs = build(_source_row(source_config), trigger_config=dict(source_config))
        assert Assignment(**kwargs).trigger_config == source_config


def _loaded_row(config: dict, trigger_type: str = "integration_event") -> Assignment:
    row = Assignment()
    set_committed_value(row, "trigger_type", trigger_type)
    set_committed_value(row, "trigger_config", config)
    return row


def _save(row: Assignment) -> dict:
    _saved_trigger_keeps_its_matching(None, None, row)
    return row.trigger_config


LEGACY = {"provider": "github", "event": "pull_request", "integration_id": "i-1"}


def test_a_row_written_back_unchanged_keeps_its_legacy_matching():
    row = _loaded_row(dict(LEGACY))
    row.name = "Renamed"
    assert EVENT_MATCHING_KEY not in _save(row)


@pytest.mark.parametrize(
    "sent",
    [
        # The trigger card rebuilds an integration-event config on every save:
        # a new integration or filter is not a new selection.
        {**LEGACY, "integration_id": "i-2"},
        {**LEGACY, "filter": {"scope": {"repository": ["filigran/xtm-one"]}}},
        {"provider": "github", "event": "pull_request"},
        # Absent, null and empty select nothing alike.
        {**LEGACY, "events": []},
        {**LEGACY, "path": None},
        # The legacy key is not read on this trigger: dropping or sending it
        # chooses nothing.
        {**LEGACY, "event_type": "issues"},
    ],
)
def test_a_save_that_leaves_the_events_alone_keeps_the_prefix_match(sent):
    row = _loaded_row(dict(LEGACY))
    row.trigger_config = sent
    assert _save(row) == sent


@pytest.mark.parametrize(
    "sent",
    [
        {**LEGACY, "event": "issues"},
        {**LEGACY, "provider": "hubspot"},
        {**LEGACY, "events": ["pull_request.opened"]},
        {**LEGACY, "event": ""},
        {**LEGACY, EVENT_MATCHING_KEY: EVENT_MATCHING_EXACT},
    ],
)
def test_choosing_the_events_again_or_asking_for_it_writes_exact(sent):
    row = _loaded_row(dict(LEGACY))
    row.trigger_config = sent
    assert _save(row) == {**sent, EVENT_MATCHING_KEY: EVENT_MATCHING_EXACT}


def test_an_exact_trigger_stays_exact_when_the_config_sent_lost_the_marker():
    row = _loaded_row({**LEGACY, EVENT_MATCHING_KEY: EVENT_MATCHING_EXACT})
    row.trigger_config = {**LEGACY, "integration_id": "i-2"}
    assert _save(row)[EVENT_MATCHING_KEY] == EVENT_MATCHING_EXACT


def test_turning_another_trigger_type_into_an_event_trigger_writes_exact():
    row = _loaded_row({"cron": "0 9 * * *"}, trigger_type="schedule")
    row.trigger_type = "webhook"
    row.trigger_config = {"path": "github"}
    assert _save(row) == {"path": "github", EVENT_MATCHING_KEY: EVENT_MATCHING_EXACT}


def test_a_type_change_alone_switches_between_event_trigger_types():
    row = _loaded_row({"path": "github", "events": ["issues"]}, trigger_type="webhook")
    row.trigger_type = "integration_event"
    assert _save(row)[EVENT_MATCHING_KEY] == EVENT_MATCHING_EXACT


def test_a_non_event_trigger_is_never_marked():
    row = _loaded_row({"path": "github"}, trigger_type="webhook")
    row.trigger_type = "schedule"
    row.trigger_config = {"cron": "0 9 * * *"}
    assert _save(row) == {"cron": "0 9 * * *"}


def test_a_save_meeting_the_attribute_expired_compares_with_what_the_row_holds():
    """No history of the old value (the attribute was expired): the row as loaded."""
    row = Assignment()
    set_committed_value(row, "trigger_type", "integration_event")
    row.__dict__["_stored_trigger_config"] = dict(LEGACY)
    row.trigger_config = {**LEGACY, "integration_id": "i-2"}
    assert EVENT_MATCHING_KEY not in _save(row)


def test_settle_event_matching_is_copy_on_write():
    sent = {**LEGACY, "event": "issues"}
    settled = settle_event_matching("integration_event", LEGACY, "integration_event", sent)
    assert settled is not sent and EVENT_MATCHING_KEY not in sent
    kept = {**LEGACY, "delay": "x"}
    assert settle_event_matching("integration_event", LEGACY, "integration_event", kept) is kept


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._rows[0]

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))

    def all(self):
        return list(self._rows)


def _serve(monkeypatch, *, integration=None, agents=(), assignments=()):
    class _DB:
        async def execute(self, stmt, *args, **kwargs):
            text = str(stmt)
            if "FROM agents" in text and "AS unreadable" in text:
                # The bound-agents query's ``(agent, unreadable)`` rows.
                return _Result([(agent, False) for agent in agents])
            if "FROM agents" in text:
                return _Result(list(agents))
            if "FROM assignments" in text:
                return _Result(list(assignments))
            return _Result([integration] if integration is not None else [])

        async def commit(self):
            return None

    class _Ctx:
        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(webhooks, "async_session", lambda: _Ctx())
    monkeypatch.setattr(webhooks, "enforce_rate_limit", AsyncMock(return_value=None))
    monkeypatch.setattr(
        webhooks, "_claim_webhook_dedup_slot", AsyncMock(return_value=(False, None, None))
    )
    monkeypatch.setattr(webhooks, "_try_verify_webhook_signature", lambda *a, **kw: None)
    monkeypatch.setattr(webhooks, "_verify_assignment_webhook_secret", lambda *a, **kw: None)
    monkeypatch.setattr(webhooks, "_clear_direct_webhook_drift_state", AsyncMock())
    monkeypatch.setattr(webhooks.settings, "assignment_queue_enabled", False)
    trigger = AsyncMock(return_value="run-1")
    monkeypatch.setattr(webhooks, "trigger_assignment", trigger)
    return trigger


class _Request(SimpleNamespace):
    async def body(self):
        return self._body


def _github(event: str, action: str = "") -> _Request:
    return _Request(
        headers={"Content-Type": "application/json", "X-GitHub-Event": event},
        _body=b'{"action": "%s"}' % action.encode(),
        method="POST",
        url="http://t/api/hooks/x",
        client=SimpleNamespace(host="203.0.113.7"),
        query_params={},
    )


def _agent():
    return SimpleNamespace(
        id=uuid.uuid4(), name="Reviewer", enabled=True, user_id=None, integration_bindings={}
    )


def _assignment(agent, trigger_type: str, config: dict):
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=None,
        is_company_managed=False,
        name="Review PRs",
        enabled=True,
        trigger_type=trigger_type,
        trigger_config=config,
        ignore_self_triggered_events=False,
    )


# ---------------------------------------------------------------------------
# Every matching path, in every mode
# ---------------------------------------------------------------------------


def test_cached_provider_route(mode):
    agent_id = uuid.uuid4()
    selected = webhooks._assignment_matcher(
        "github",
        uuid.uuid4(),
        agent_id,
        "integration_event",
        _config(mode, provider="github", event="pull_request"),
    )
    allow_list = webhooks._assignment_matcher(
        "github",
        uuid.uuid4(),
        agent_id,
        "webhook",
        _config(mode, path="github", events=["pull_request"]),
    )
    routing = webhooks._ProviderRouting(
        oauth_default_exists=False,
        extra_config_enc=None,
        client_secret_enc=None,
        has_agents=True,
        matchers=(selected, allow_list),
    )
    sibling = webhooks._match_provider_assignments(
        routing, "github", "pull_request_review.submitted"
    )
    own = webhooks._match_provider_assignments(routing, "github", "pull_request.opened")
    assert len(sibling) == (2 if _runs_on_sibling(mode) else 0)
    assert len(own) == 2


async def test_integration_url(mode, monkeypatch):
    agent = _agent()
    integration = SimpleNamespace(id=uuid.uuid4(), provider="github", extra_config_enc=None)
    wf = _assignment(
        agent, "integration_event", _config(mode, provider="github", event="pull_request")
    )
    trigger = _serve(monkeypatch, integration=integration, agents=[agent], assignments=[wf])

    await webhooks.receive_integration_webhook(
        str(integration.id), _github("pull_request_review", "submitted")
    )
    assert trigger.await_count == (1 if _runs_on_sibling(mode) else 0)

    trigger.reset_mock()
    await webhooks.receive_integration_webhook(
        str(integration.id), _github("pull_request", "opened")
    )
    assert trigger.await_count == 1


async def test_integration_url_legacy_webhook_allow_list(mode, monkeypatch):
    agent = _agent()
    integration = SimpleNamespace(id=uuid.uuid4(), provider="github", extra_config_enc=None)
    wf = _assignment(agent, "webhook", _config(mode, path="github", events=["pull_request"]))
    trigger = _serve(monkeypatch, integration=integration, agents=[agent], assignments=[wf])

    await webhooks.receive_integration_webhook(
        str(integration.id), _github("pull_request_review", "submitted")
    )
    assert trigger.await_count == (1 if _runs_on_sibling(mode) else 0)


async def test_hubspot_fan_out(mode, monkeypatch):
    agent = _agent()
    integration = SimpleNamespace(id=uuid.uuid4(), provider="hubspot", extra_config_enc=None)
    wf = _assignment(agent, "integration_event", _config(mode, provider="hubspot", event="entity"))
    trigger = _serve(monkeypatch, integration=integration, agents=[agent], assignments=[wf])

    await webhooks._handle_hubspot_webhook(
        str(integration.id),
        integration,
        b"[]",
        [{"subscriptionType": "contact.creation", "objectId": 1}],
        _github("unused"),
    )
    assert trigger.await_count == (1 if _runs_on_sibling(mode) else 0)


async def test_hubspot_per_assignment(mode, monkeypatch):
    agent = _agent()
    integration = SimpleNamespace(id=uuid.uuid4(), provider="hubspot", extra_config_enc=None)
    wf = _assignment(agent, "integration_event", _config(mode, provider="hubspot", event="entity"))
    trigger = _serve(monkeypatch, integration=integration, agents=[agent], assignments=[wf])

    await webhooks._handle_hubspot_assignment_webhook(
        str(integration.id),
        integration,
        b"[]",
        [{"subscriptionType": "contact.creation", "objectId": 1}],
        str(wf.id),
        _github("unused"),
    )
    assert trigger.await_count == (1 if _runs_on_sibling(mode) else 0)


@pytest.mark.parametrize(
    "event,action", [("pull_request_review", "submitted"), ("ping", "")]
)
async def test_per_assignment_url(mode, monkeypatch, event, action):
    """A legacy trigger ran on every GitHub delivery to its dedicated URL,
    the ``ping`` sent when the webhook is created included, and still does."""
    agent = _agent()
    integration = SimpleNamespace(id=uuid.uuid4(), provider="github", extra_config_enc=None)
    wf = _assignment(
        agent, "integration_event", _config(mode, provider="github", event="pull_request")
    )
    trigger = _serve(monkeypatch, integration=integration, agents=[agent], assignments=[wf])

    response = await webhooks.receive_assignment_webhook(
        str(integration.id), str(wf.id), _github(event, action)
    )
    if _runs_on_sibling(mode):
        assert response["count"] == 1
    else:
        assert response.get("reason") == "event not selected"
        trigger.assert_not_awaited()


async def test_per_assignment_url_reports_the_legacy_widening_once(monkeypatch, caplog):
    monkeypatch.setattr(settings, "webhook_exact_event_matching", False)
    agent = _agent()
    integration = SimpleNamespace(id=uuid.uuid4(), provider="github", extra_config_enc=None)
    wf = _assignment(agent, "integration_event", {"provider": "github", "event": "pull_request"})
    _serve(monkeypatch, integration=integration, agents=[agent], assignments=[wf])

    with caplog.at_level(logging.WARNING, logger=webhooks.logger.name):
        for _ in range(2):
            await webhooks.receive_assignment_webhook(
                str(integration.id), str(wf.id), _github("ping")
            )
    reports = [r for r in caplog.records if str(wf.id) in r.getMessage()]
    assert len(reports) == 1


async def test_direct_url(mode, monkeypatch):
    agent = _agent()
    wf = _assignment(agent, "webhook", _config(mode, events=["pull_request"]))
    trigger = _serve(monkeypatch, agents=[agent], assignments=[wf])

    await webhooks.receive_direct_assignment_webhook(
        str(wf.id), _github("pull_request_review", "submitted")
    )
    assert trigger.await_count == (1 if _runs_on_sibling(mode) else 0)


async def test_legacy_agent_url(mode, monkeypatch):
    agent = _agent()
    wf = _assignment(agent, "webhook", _config(mode, path="inbound", events=["pull_request"]))
    trigger = _serve(monkeypatch, agents=[agent], assignments=[wf])

    await webhooks.receive_webhook(
        str(agent.id), "inbound", _github("pull_request_review", "submitted")
    )
    assert trigger.await_count == (1 if _runs_on_sibling(mode) else 0)


# ---------------------------------------------------------------------------
# Visibility: the security audit counts legacy triggers
# ---------------------------------------------------------------------------


class _AuditDb:
    """Answers the count query (enabled event triggers, unmarked, unread event_type), then
    one stored-text read per event trigger type: the rows holding a lone surrogate."""

    def __init__(self, triggers, unmarked=0, unread=0, unparsed=None):
        self.row = (triggers, unmarked, unread)
        self.unparsed = [
            [SimpleNamespace(trigger_config_text=text) for text in (unparsed or {}).get(kind, [])]
            for kind in sorted(EVENT_MATCHING_TRIGGER_TYPES)
        ]
        self.queries = 0

    async def execute(self, _stmt):
        self.queries += 1
        if self.queries == 1:
            return SimpleNamespace(one=lambda: self.row)
        rows = self.unparsed[self.queries - 2]
        return SimpleNamespace(all=lambda: rows)


async def _audit(monkeypatch, db, *, everywhere=False) -> dict:
    monkeypatch.setattr(settings, "webhook_exact_event_matching", everywhere)
    findings: list = []
    await security_audit._check_event_matching(db, findings)
    assert db.queries == 1 + len(EVENT_MATCHING_TRIGGER_TYPES)
    return {finding.id: finding for finding in findings}


async def test_the_audit_counts_a_trigger_holding_a_lone_surrogate_from_its_stored_text(monkeypatch):
    unparsed = {
        "integration_event": ['{"provider": "github", "event_type": "push", "note": "\\ud83d"}'],
        "webhook": ['{"path": "github", "event_matching": "exact", "note": "\\ud800"}'],
    }
    findings = await _audit(monkeypatch, _AuditDb(3, unmarked=1, unparsed=unparsed))
    assert findings["assignments.legacy_event_matching"].title.startswith("2 event trigger(s)")
    assert findings["assignments.legacy_event_type_key"].title.startswith("1 integration-event trigger(s)")


async def test_the_audit_counts_legacy_event_triggers_and_names_the_switch(monkeypatch):
    findings = await _audit(monkeypatch, _AuditDb(5, unmarked=3))
    assert set(findings) == {"assignments.legacy_event_matching"}
    finding = findings["assignments.legacy_event_matching"]
    assert (finding.severity, finding.category, finding.control) == (
        "info",
        "assignments",
        "assignments.exact_event_matching",
    )
    assert finding.title.startswith("3 event trigger(s)")
    assert '"Exact event matching for every trigger"' in finding.recommendation
    assert "WEBHOOK_EXACT_EVENT_MATCHING=true" in finding.recommendation


async def test_the_audit_reports_its_pass_when_every_trigger_matches_exactly(monkeypatch):
    for everywhere, unmarked in ((False, 0), (True, 3)):
        findings = await _audit(monkeypatch, _AuditDb(5, unmarked=unmarked), everywhere=everywhere)
        assert set(findings) == {"assignments.event_matching_exact"}
        assert findings["assignments.event_matching_exact"].severity == "pass"
        assert findings["assignments.event_matching_exact"].control == "assignments.exact_event_matching"


async def test_the_audit_says_nothing_without_event_triggers(monkeypatch):
    assert await _audit(monkeypatch, _AuditDb(0)) == {}


async def test_the_audit_names_the_unread_legacy_event_type_key_whatever_the_switch(monkeypatch):
    for everywhere in (False, True):
        findings = await _audit(monkeypatch, _AuditDb(4, unmarked=2, unread=2), everywhere=everywhere)
        finding = findings["assignments.legacy_event_type_key"]
        assert finding.severity == "info" and finding.control is None
        assert finding.title.startswith("2 integration-event trigger(s)")
