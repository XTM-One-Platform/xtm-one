"""Administrators are notified of critical and warning Security Audit findings, once each.

The scheduler runs the audit once an hour install-wide, in the background, and
sends each active administrator one bell notification naming the findings they
were never told about: once per finding id, ever - not again when it comes
back or escalates, never while dismissed, once more when a dismissal is
re-opened by a higher severity. The memory is PostgreSQL's (the finding state
rows, faked here by ``_Store``; the SQL runs in
``test_security_audit_finding_states_postgres.py``), so losing Redis
re-notifies nothing.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api.v1 import security_audit
from app.services import autonomous
from app.services import security_audit_finding_states as finding_states
from app.services import security_audit_notifications as notifications

AuditFinding = security_audit.AuditFinding
FindingState = finding_states.FindingState
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _finding(finding_id: str, severity: str, title: str | None = None, **markers) -> AuditFinding:
    category = finding_id.partition(".")[0]
    return AuditFinding(
        id=finding_id, category=category, severity=severity, title=title or finding_id, description="d", **markers
    )


# ---------------------------------------------------------------------------
# What is due
# ---------------------------------------------------------------------------


def test_the_first_run_reports_every_critical_and_warning_finding_critical_first():
    findings = [
        _finding("authentication.admins_without_mfa", "warning"),
        _finding("encryption.secret_key_configured", "pass"),
        _finding("infrastructure.redis_unreachable", "critical"),
        _finding("web.cors_origins_wildcard", "info"),
        _finding("assignments.webhook_without_secret", "warning"),
    ]
    assert [finding.id for finding in notifications.due_findings(findings, {})] == [
        "infrastructure.redis_unreachable",
        "authentication.admins_without_mfa",
        "assignments.webhook_without_secret",
    ]


def test_a_notified_finding_is_never_due_again_whatever_it_does():
    """No second notice when it comes back, when its title changes or when it escalates."""
    states = {"infrastructure.quota_fail_open": FindingState("infrastructure.quota_fail_open", notified_at=T0)}
    for severity in ("warning", "critical"):
        assert notifications.due_findings([_finding("infrastructure.quota_fail_open", severity, "5 x")], states) == []


def test_a_dismissed_finding_is_never_due():
    dismissal = security_audit.FindingDismissal(severity="warning")
    assert notifications.due_findings([_finding("agents.guardrails_off", "warning", dismissed=dismissal)], {}) == []
    state = FindingState("agents.guardrails_off", dismissed_at=T0, dismissed_severity="critical")
    assert notifications.due_findings([_finding("agents.guardrails_off", "critical")], {state.finding_id: state}) == []


def test_a_reopened_dismissal_is_due_once():
    reopened = FindingState(
        "agents.guardrails_off", notified_at=T0, reopened_at=T0 + timedelta(days=3), reopened_severity="critical"
    )
    finding = _finding("agents.guardrails_off", "critical")
    assert notifications.due_findings([finding], {reopened.finding_id: reopened}) == [finding]
    told = dataclasses.replace(reopened, notified_at=T0 + timedelta(days=3, hours=1))
    assert notifications.due_findings([finding], {told.finding_id: told}) == []


def test_a_finding_without_an_id_or_seen_twice_is_counted_once_at_its_highest_severity():
    unnamed = AuditFinding(category="mcp", severity="critical", title="t", description="d")
    twice = [
        _finding("mcp.dynamic_client_registration_open", "warning"),
        _finding("mcp.dynamic_client_registration_open", "critical"),
    ]
    due = notifications.due_findings([unnamed, *twice], {})
    assert [(finding.id, finding.severity) for finding in due] == [("mcp.dynamic_client_registration_open", "critical")]


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not json",
        "[]",
        json.dumps({"a.b": {"severity": "warning", "seen": 1}}),
        json.dumps({"run": 5, "findings": []}),
    ],
)
def test_an_unreadable_legacy_memory_reads_as_nothing_notified(raw):
    assert notifications.legacy_notified(raw) == {}


def test_every_finding_the_legacy_memory_held_counts_as_notified():
    findings = {
        "a.b": {"severity": "critical", "seen": 12.5},
        # Absent from runs for a while: still told.
        "c.d": {"severity": "warning", "seen": -9e9},
        "e.f": {"severity": "info", "seen": 1},
        "Not an id": {"severity": "warning", "seen": 1},
        "g.h": "warning",
    }
    assert notifications.legacy_notified(json.dumps({"run": 20, "findings": findings})) == {
        "a.b": "critical",
        "c.d": "warning",
    }


# ---------------------------------------------------------------------------
# What the notification says
# ---------------------------------------------------------------------------


def test_one_new_finding_is_named_by_its_severity_and_title():
    title, summary, extra = notifications.build_notification(
        [
            _finding(
                "authentication.dormant_account_automations", "warning", "4 active automation(s) run as 2 account(s)"
            )
        ]
    )
    assert title == "New warning security audit finding"
    assert summary == "4 active automation(s) run as 2 account(s)"
    assert extra == {
        "notification_source": "security_audit",
        "severity": "warning",
        "finding_ids": ["authentication.dormant_account_automations"],
        "audit_path": "/settings/security?finding=authentication.dormant_account_automations",
    }


def test_several_new_findings_are_counted_and_linked():
    due = [_finding("infrastructure.redis_unreachable", "critical", "Redis is unreachable")] + [
        _finding(f"agents.check_{index}", "warning", f"Warning {index}") for index in range(11)
    ]
    title, summary, extra = notifications.build_notification(due)
    assert title == "12 new security audit findings (1 critical, 11 warning)"
    assert summary.startswith("Redis is unreachable; Warning 0; ")
    assert summary.endswith("Warning 8; and 2 more")
    assert extra["severity"] == "critical"
    assert len(extra["finding_ids"]) == 12
    assert extra["audit_path"].startswith(
        "/settings/security?finding=infrastructure.redis_unreachable&finding=agents.check_0"
    )


def test_a_reopened_finding_says_so():
    reopening = security_audit.FindingReopening(at=T0.isoformat(), severity="critical")
    due = [
        _finding("infrastructure.redis_unreachable", "critical", "Redis is unreachable", reopened=reopening),
        _finding("agents.guardrails_off", "warning", "Guardrails are off"),
    ]
    title, summary, _extra = notifications.build_notification(due)
    assert title == "2 new or re-opened security audit findings (1 critical, 1 warning)"
    assert summary == "Redis is unreachable (re-opened); Guardrails are off"
    title, _summary, _extra = notifications.build_notification(due[:1])
    assert title == "Re-opened critical security audit finding"


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


class _Redis:
    def __init__(self, legacy: str | None = None, lease_taken: bool = False, fail_get: bool = False):
        self.values: dict[str, str] = {} if legacy is None else {notifications.LEGACY_STATE_KEY: legacy}
        if lease_taken:
            self.values[notifications.LEASE_KEY] = "1"
        self.fail_get = fail_get
        self.expiries: dict[str, int] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key):
        if self.fail_get:
            raise ConnectionError("no redis")
        return self.values.get(key)

    async def expire(self, key, seconds):
        self.expiries[key] = seconds


class _Store:
    """The finding state rows: what was committed."""

    def __init__(self):
        self.states: dict[str, FindingState] = {}

    def notified(self) -> set[str]:
        return {finding_id for finding_id, state in self.states.items() if state.notified_at}


class _Db:
    """The session: admin ids, and the rows ``record_notified`` wrote, applied on commit."""

    def __init__(self, store: _Store, admin_ids, *, fail_commit: bool = False):
        self.store = store
        self.admin_ids = admin_ids
        self.fail_commit = fail_commit
        self.commits = 0
        self.pending: list = []

    async def execute(self, _statement):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self.admin_ids)))

    async def commit(self):
        if self.fail_commit:
            self.pending.clear()
            raise RuntimeError("commit failed")
        for write in self.pending:
            write()
        self.pending.clear()
        self.commits += 1


@pytest.fixture
def run(monkeypatch):
    """Wire the run to a fake report, session, state rows and bell; returns what they saw."""
    store = _Store()
    seen = SimpleNamespace(
        findings=[], store=store, db=_Db(store, [uuid.uuid4(), uuid.uuid4()]), notified=[], reports=0, memory_checks=0
    )

    async def _report(_db):
        seen.reports += 1
        return SimpleNamespace(findings=list(seen.findings))

    @asynccontextmanager
    async def _session():
        yield seen.db

    async def _notify(db, user_id, **kwargs):
        seen.notified.append((user_id, kwargs))

    async def _load(_db):
        return dict(store.states)

    async def _empty(_db):
        seen.memory_checks += 1
        return not store.notified()

    async def _persist_reopenings(db, findings, *, now):
        reopened = []
        for finding_id, severity in finding_states.reported_severities(findings).items():
            state = store.states.get(finding_id)
            if finding_states.escalated_past_dismissal(state, severity):
                store.states[finding_id] = dataclasses.replace(
                    state, dismissed_at=None, reopened_at=now, reopened_severity=severity
                )
                reopened.append(finding_id)
        return reopened

    async def _record(db, notified, *, now, only_if_unset=False):
        def _write():
            for finding_id, severity in notified.items():
                state = store.states.get(finding_id, FindingState(finding_id))
                if only_if_unset and state.notified_at:
                    continue
                store.states[finding_id] = dataclasses.replace(state, notified_at=now, notified_severity=severity)

        db.pending.append(_write)

    monkeypatch.setattr(security_audit, "build_security_audit_report", _report)
    monkeypatch.setattr(notifications, "async_session", _session)
    monkeypatch.setattr(notifications, "notify_web_user", _notify)
    monkeypatch.setattr(finding_states, "load_states", _load)
    monkeypatch.setattr(finding_states, "notification_memory_empty", _empty)
    monkeypatch.setattr(finding_states, "record_notified", _record)
    monkeypatch.setattr(finding_states, "persist_reopenings", _persist_reopenings)
    monkeypatch.setattr(notifications, "_legacy_memory_settled", False)
    return seen


async def test_every_active_administrator_is_notified_once_per_run(run):
    run.findings = [
        _finding("infrastructure.redis_unreachable", "critical"),
        _finding("agents.guardrails_off", "warning"),
    ]
    redis = _Redis()
    assert await notifications.notify_new_findings(redis) == 2
    assert [user_id for user_id, _ in run.notified] == [str(admin_id) for admin_id in run.db.admin_ids]
    _user_id, kwargs = run.notified[0]
    assert kwargs["title"] == "2 new security audit findings (1 critical, 1 warning)"
    assert kwargs["source_label"] == "Security audit"
    assert kwargs["extra_data"]["notification_source"] == "security_audit"
    # The notifications and the memory in one commit.
    assert run.db.commits == 1
    assert run.store.notified() == {"infrastructure.redis_unreachable", "agents.guardrails_off"}
    # Nothing of it in Redis: the lease is all Redis holds.
    assert redis.values == {}
    assert await notifications.notify_new_findings(redis) == 0
    assert len(run.notified) == 2


async def test_losing_redis_notifies_nothing_again(run):
    run.findings = [_finding("infrastructure.redis_unreachable", "critical")]
    assert await notifications.notify_new_findings(_Redis()) == 1
    # A new, empty Redis (flushed, failed over, replaced).
    assert await notifications.notify_new_findings(_Redis()) == 0
    assert len(run.notified) == 2


async def test_a_finding_that_escalates_or_comes_back_is_not_notified_again(run):
    run.findings = [_finding("infrastructure.quota_fail_open", "warning")]
    redis = _Redis()
    assert await notifications.notify_new_findings(redis) == 1
    run.findings = []
    assert await notifications.notify_new_findings(redis) == 0
    run.findings = [_finding("infrastructure.quota_fail_open", "critical")]
    assert await notifications.notify_new_findings(redis) == 0
    assert len(run.notified) == 2


async def test_a_dismissed_finding_is_never_notified(run):
    dismissal = security_audit.FindingDismissal(at=T0.isoformat(), severity="critical")
    run.findings = [_finding("infrastructure.redis_unreachable", "critical", dismissed=dismissal)]
    assert await notifications.notify_new_findings(_Redis()) == 0
    assert run.notified == [] and run.store.states == {}


async def test_a_reopened_dismissal_is_notified_once(run):
    run.store.states["agents.guardrails_off"] = FindingState(
        "agents.guardrails_off",
        notified_at=T0,
        notified_severity="warning",
        reopened_at=T0 + timedelta(days=2),
        reopened_severity="critical",
    )
    reopening = security_audit.FindingReopening(at=(T0 + timedelta(days=2)).isoformat(), severity="critical")
    run.findings = [_finding("agents.guardrails_off", "critical", "Guardrails are off", reopened=reopening)]
    redis = _Redis()
    assert await notifications.notify_new_findings(redis) == 1
    assert run.notified[0][1]["title"] == "Re-opened critical security audit finding"
    assert run.store.states["agents.guardrails_off"].notified_severity == "critical"
    assert await notifications.notify_new_findings(redis) == 0


async def test_the_run_records_an_escalated_dismissal_and_notifies_it_once(run):
    run.store.states["agents.guardrails_off"] = FindingState(
        "agents.guardrails_off", notified_at=T0, dismissed_at=T0, dismissed_severity="warning"
    )
    # The report shows it re-opened without writing it (``finding_views``).
    reopening = security_audit.FindingReopening(severity="critical")
    run.findings = [_finding("agents.guardrails_off", "critical", "Guardrails are off", reopened=reopening)]
    redis = _Redis()
    assert await notifications.notify_new_findings(redis) == 1
    state = run.store.states["agents.guardrails_off"]
    assert state.dismissed_at is None and state.reopened_severity == "critical"
    assert await notifications.notify_new_findings(redis) == 0


async def test_the_legacy_memory_is_checked_once_per_process(run):
    run.findings = [_finding("infrastructure.redis_unreachable", "critical")]
    redis = _Redis()
    assert await notifications.notify_new_findings(redis) == 1
    assert await notifications.notify_new_findings(redis) == 0
    assert run.memory_checks == 1


async def test_the_upgrade_imports_the_redis_memory_and_announces_nothing_already_told(run, caplog):
    legacy = json.dumps(
        {
            "run": 1.0,
            "findings": {
                "infrastructure.redis_unreachable": {"severity": "critical", "seen": 1.0},
                # No longer reported: told all the same.
                "mcp.dynamic_client_registration_open": {"severity": "warning", "seen": 0.5},
            },
        }
    )
    run.findings = [
        _finding("infrastructure.redis_unreachable", "critical"),
        _finding("agents.guardrails_off", "warning"),
    ]
    redis = _Redis(legacy)
    with caplog.at_level(logging.INFO, logger=notifications.logger.name):
        assert await notifications.notify_new_findings(redis) == 1
    assert [kwargs["extra_data"]["finding_ids"] for _user, kwargs in run.notified] == [["agents.guardrails_off"]] * 2
    assert run.store.notified() == {
        "infrastructure.redis_unreachable",
        "mcp.dynamic_client_registration_open",
        "agents.guardrails_off",
    }
    assert "2 finding(s) notified before the upgrade carried over from Redis" in caplog.text
    # Kept a week for a rollback, then gone.
    assert redis.expiries == {notifications.LEGACY_STATE_KEY: notifications.LEGACY_KEY_RETENTION_SECONDS}


async def test_the_legacy_memory_is_read_only_while_nothing_is_recorded(run):
    run.store.states["agents.guardrails_off"] = FindingState("agents.guardrails_off", notified_at=T0)
    legacy = json.dumps({"run": 1.0, "findings": {"infrastructure.redis_unreachable": {"severity": "critical"}}})
    run.findings = [_finding("infrastructure.redis_unreachable", "critical")]
    assert await notifications.notify_new_findings(_Redis(legacy)) == 1


async def test_a_legacy_memory_that_cannot_be_read_notifies_nothing(run):
    run.findings = [_finding("infrastructure.redis_unreachable", "critical")]
    with pytest.raises(ConnectionError):
        await notifications.notify_new_findings(_Redis(fail_get=True))
    assert run.notified == [] and run.store.states == {}


async def test_with_no_active_administrator_nothing_is_recorded(run):
    """Nobody was told, so the first administrator is, at the next run."""
    run.findings = [_finding("infrastructure.redis_unreachable", "critical")]
    run.db.admin_ids = []
    redis = _Redis()
    assert await notifications.notify_new_findings(redis) == 0
    assert run.store.states == {}
    run.db.admin_ids = [uuid.uuid4()]
    assert await notifications.notify_new_findings(redis) == 1


async def test_a_failed_delivery_is_retried_by_the_next_run(run):
    run.findings = [_finding("infrastructure.redis_unreachable", "critical")]
    run.db.fail_commit = True
    redis = _Redis()
    with pytest.raises(RuntimeError):
        await notifications.notify_new_findings(redis)
    assert run.store.states == {}
    run.db.fail_commit = False
    assert await notifications.notify_new_findings(redis) == 1


async def test_a_run_with_nothing_to_notify_logs_nothing_at_info(run, caplog):
    run.findings = [_finding("encryption.secret_key_configured", "pass")]
    with caplog.at_level(logging.INFO, logger=notifications.logger.name):
        assert await notifications.notify_new_findings(_Redis()) == 0
    assert [record for record in caplog.records if record.name == notifications.logger.name] == []
    assert run.notified == [] and run.db.commits == 0


async def test_the_audit_runs_only_for_the_process_that_takes_the_lease(run, monkeypatch):
    run.findings = [_finding("infrastructure.redis_unreachable", "critical")]
    redis = _Redis(lease_taken=True)

    async def _client():
        return redis

    monkeypatch.setattr(notifications, "redis_client", _client)
    assert await notifications.notify_new_findings_if_due() is False
    assert run.reports == 0
    del redis.values[notifications.LEASE_KEY]
    assert await notifications.notify_new_findings_if_due() is True
    await asyncio.gather(*notifications._running)
    assert run.reports == 1 and len(run.notified) == 2


async def test_an_unreachable_redis_skips_the_run(run, monkeypatch):
    async def _unreachable():
        raise ConnectionError("no redis")

    monkeypatch.setattr(notifications, "redis_client", _unreachable)
    assert await notifications.notify_new_findings_if_due() is False
    assert run.reports == 0 and not notifications._running


async def test_the_tick_does_not_wait_for_the_audit(monkeypatch):
    """The tick holds a lock the next ticks skip on: it only takes the lease."""
    release = asyncio.Event()
    finished = []

    async def _slow(_redis):
        await release.wait()
        finished.append(True)
        return 0

    async def _client():
        return _Redis()

    monkeypatch.setattr(notifications, "redis_client", _client)
    monkeypatch.setattr(notifications, "notify_new_findings", _slow)
    assert await asyncio.wait_for(notifications.notify_new_findings_if_due(), timeout=1) is True
    assert finished == [] and len(notifications._running) == 1
    release.set()
    await asyncio.gather(*notifications._running)
    assert finished == [True] and not notifications._running


async def test_a_failed_run_is_logged_never_raised(monkeypatch, caplog):
    async def _broken(_redis):
        raise RuntimeError("audit failed")

    async def _client():
        return _Redis()

    monkeypatch.setattr(notifications, "redis_client", _client)
    monkeypatch.setattr(notifications, "notify_new_findings", _broken)
    with caplog.at_level(logging.ERROR, logger=notifications.logger.name):
        assert await notifications.notify_new_findings_if_due() is True
        results = await asyncio.gather(*notifications._running, return_exceptions=True)
    assert results == [None]
    assert "Security audit notification error: audit failed" in caplog.text


def test_the_scheduler_asks_on_every_tick_outside_the_ai_gate():
    source = inspect.getsource(autonomous._run_autonomous_tick_inner)
    ask = source.index("await notify_new_findings_if_due()")
    # After the tick's own session, like the other install-wide sweeps.
    assert ask > source.index("await purge_orphaned_seed_objects_if_due(")
