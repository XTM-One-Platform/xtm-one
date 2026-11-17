"""Dismissed Security Audit findings: the rules, the report and the routes.

The SQL runs on real PostgreSQL in ``test_security_audit_finding_states_postgres.py``;
here the pure rules of ``services/security_audit_finding_states.py``, how the
report counts a dismissed finding, and what the routes refuse before any row is
written.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api import deps
from app.api.v1 import security_audit
from app.core.database import get_db
from app.models.security_audit_finding_state import DISMISSAL_REASON_MAX_CHARS, FINDING_ID_MAX_CHARS
from app.services import security_audit_finding_states as finding_states

AuditFinding = security_audit.AuditFinding
FindingState = finding_states.FindingState
T0 = datetime(2026, 9, 1, tzinfo=UTC)
_FRONTEND_HELPERS = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "settings" / "security-audit.ts"
)


def _finding(finding_id: str | None, severity: str, title: str = "t") -> AuditFinding:
    return AuditFinding(id=finding_id, category="agents", severity=severity, title=title, description="d")


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("finding_id", "valid"),
    [
        ("agents.guardrails_off", True),
        ("network.client_addresses_undeclared_proxy", True),
        ("agents", False),
        ("Agents.guardrails_off", False),
        ("agents.guardrails-off", False),
        ("agents.guardrails_off.more", False),
        ("agents.guardrails_off\n", False),
        ("agents." + "x" * 200, False),
        (None, False),
        (42, False),
    ],
)
def test_a_finding_id_is_the_audit_s_own_shape(finding_id, valid):
    assert finding_states.valid_finding_id(finding_id) is valid


def test_the_route_pattern_is_the_id_rule():
    anchored = f"^{finding_states._FINDING_ID_RE.pattern}$"
    assert anchored == finding_states.FINDING_ID_PATTERN


def test_the_settings_screen_offers_a_dismissal_exactly_where_the_route_accepts_one():
    source = _FRONTEND_HELPERS.read_text(encoding="utf-8")
    severities = re.search(r"DISMISSABLE_SEVERITIES: readonly string\[\] = \[([^\]]*)\]", source)
    assert severities is not None
    assert set(re.findall(r'"([a-z]+)"', severities.group(1))) == set(finding_states.DISMISSABLE_SEVERITIES)
    assert f"const DISMISSABLE_ID = /{finding_states.FINDING_ID_PATTERN}/;" in source
    assert f"const FINDING_ID_MAX_CHARS = {FINDING_ID_MAX_CHARS};" in source
    assert f"export const DISMISSAL_REASON_MAX_CHARS = {DISMISSAL_REASON_MAX_CHARS};" in source


@pytest.mark.parametrize(
    ("reason", "stored"),
    [(None, None), ("", None), ("   ", None), ("  accepted risk \n", "accepted risk"), ("x" * 1200, "x" * 1000)],
)
def test_a_reason_is_trimmed_bounded_and_blank_is_none(reason, stored):
    assert finding_states.clean_reason(reason) == stored


def test_the_run_reports_each_id_once_at_its_highest_severity():
    findings = [
        _finding("agents.a", "warning"),
        _finding("agents.a", "critical"),
        _finding("agents.a", "info"),
        _finding(None, "critical"),
        _finding("agents.b", "pass"),
    ]
    assert finding_states.reported_severities(findings) == {"agents.a": "critical", "agents.b": "pass"}


def test_a_dismissal_re_opens_only_above_the_severity_it_was_made_at():
    dismissed = FindingState("agents.a", dismissed_at=T0, dismissed_severity="warning")
    assert finding_states.escalated_past_dismissal(dismissed, "critical") is True
    assert finding_states.escalated_past_dismissal(dismissed, "warning") is False
    assert finding_states.escalated_past_dismissal(dismissed, "info") is False
    assert (
        finding_states.escalated_past_dismissal(FindingState("agents.a", dismissed_severity="info"), "critical")
        is False
    )
    assert finding_states.escalated_past_dismissal(None, "critical") is False


def test_a_finding_is_notified_once_unless_a_dismissal_re_opened():
    assert finding_states.notification_due(None) is True
    assert finding_states.notification_due(FindingState("agents.a")) is True
    assert finding_states.notification_due(FindingState("agents.a", notified_at=T0)) is False
    assert finding_states.notification_due(FindingState("agents.a", dismissed_at=T0)) is False
    assert finding_states.notification_due(FindingState("agents.a", notified_at=T0, dismissed_at=T0)) is False
    later = T0 + timedelta(days=1)
    assert finding_states.notification_due(FindingState("agents.a", notified_at=T0, reopened_at=later)) is True
    assert finding_states.notification_due(FindingState("agents.a", notified_at=later, reopened_at=T0)) is False
    assert finding_states.notification_due(FindingState("agents.a", reopened_at=T0)) is True


async def test_an_unreadable_state_leaves_every_finding_counted(caplog):
    class _NoSavepoints:
        def begin_nested(self):
            raise RuntimeError("no savepoints here")

    views = await finding_states.finding_views(_NoSavepoints(), [_finding("agents.a", "warning")])
    assert views == {}
    assert "cannot read the finding states" in caplog.text


async def test_an_escalated_dismissal_reads_re_opened_and_the_read_writes_nothing(monkeypatch):
    states = {
        "agents.a": FindingState("agents.a", dismissed_at=T0, dismissed_severity="warning", dismissal_reason="ok"),
        "agents.b": FindingState("agents.b", dismissed_at=T0, dismissed_severity="critical"),
    }

    async def _states(_db):
        return states

    async def _names(_db, _ids):
        return {}

    class _ReadOnly:
        def begin_nested(self):
            return contextlib.nullcontext()

        async def execute(self, *_args, **_kwargs):
            raise AssertionError("the report path must not write")

        async def commit(self):
            raise AssertionError("the report path must not commit")

    monkeypatch.setattr(finding_states, "load_states", _states)
    monkeypatch.setattr(finding_states, "_admin_names", _names)
    views = await finding_states.finding_views(
        _ReadOnly(), [_finding("agents.a", "critical"), _finding("agents.b", "critical")]
    )
    escalated = views["agents.a"]
    assert escalated.dismissed is None and escalated.reopened and escalated.reopened_at is None
    assert escalated.reopened_severity == "critical" and escalated.reopened_dismissal.at == T0
    assert views["agents.b"].dismissed is not None and not views["agents.b"].reopened


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


async def test_a_dismissed_finding_stays_listed_and_leaves_the_counts_and_the_score(monkeypatch):
    admin_id = uuid.uuid4()

    async def _views(_db, _findings):
        return {
            "agents.a": finding_states.FindingView(
                dismissed=finding_states.DismissalView(
                    at=T0, severity="critical", reason="accepted", by_user_id=admin_id, by="Admin"
                )
            ),
            "agents.b": finding_states.FindingView(
                reopened_at=T0,
                reopened_severity="warning",
                reopened_dismissal=finding_states.DismissalView(
                    at=None, severity="info", reason=None, by_user_id=None, by=None
                ),
            ),
        }

    monkeypatch.setattr(finding_states, "finding_views", _views)
    findings = [_finding("agents.a", "critical"), _finding("agents.b", "warning"), _finding("agents.c", "pass")]
    report = await security_audit._finish_report(None, findings, [], {})

    assert [finding.id for finding in report.findings] == ["agents.a", "agents.b", "agents.c"]
    assert report.summary == {"critical": 0, "warning": 1, "info": 0, "pass": 1}
    assert report.overall_score == "needs_attention"
    assert report.dismissed_count == 1
    dismissed = report.findings[0].dismissed
    assert dismissed is not None
    assert (dismissed.at, dismissed.severity, dismissed.reason, dismissed.by_user_id, dismissed.by) == (
        T0.isoformat(),
        "critical",
        "accepted",
        str(admin_id),
        "Admin",
    )
    reopened = report.findings[1].reopened
    assert reopened is not None and reopened.severity == "warning" and reopened.dismissal.severity == "info"
    assert reopened.dismissal.at is None
    assert report.findings[2].dismissed is None and report.findings[2].reopened is None


async def test_every_finding_dismissed_reads_healthy(monkeypatch):
    async def _views(_db, findings):
        view = finding_states.FindingView(
            dismissed=finding_states.DismissalView(at=T0, severity="critical", reason=None, by_user_id=None, by=None)
        )
        return {finding.id: view for finding in findings}

    monkeypatch.setattr(finding_states, "finding_views", _views)
    report = await security_audit._finish_report(None, [_finding("agents.a", "critical")], [], {})
    assert report.overall_score == "healthy" and report.dismissed_count == 1
    assert report.summary == {"critical": 0, "warning": 0, "info": 0, "pass": 0}


def test_the_report_fields_are_additive():
    """An older client reads a report without them; a newer one reads their defaults."""
    report = security_audit.SecurityAuditReport(
        timestamp="t", overall_score="healthy", findings=[], summary={}, controls=[], controls_summary={}
    )
    assert report.dismissed_count == 0
    finding = _finding("agents.a", "warning")
    assert finding.dismissed is None and finding.reopened is None


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


class _Db:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


@pytest.fixture
def api(monkeypatch):
    """The router as an admin, with a fixed report; returns what the routes did."""
    seen = SimpleNamespace(
        db=_Db(),
        admin=SimpleNamespace(id=uuid.uuid4(), is_admin=True),
        findings=[
            _finding("agents.guardrails_off", "warning", "Guardrails are off"),
            _finding("agents.guardrails_off", "critical", "Guardrails are off everywhere"),
            _finding("encryption.secret_key_configured", "pass"),
        ],
        dismissed=[],
        restored=[],
        restorable=True,
        builds=0,
        reopenings=0,
    )

    async def _report(_db):
        seen.builds += 1
        return await security_audit._finish_report(_db, [f.model_copy() for f in seen.findings], [], {})

    async def _persist_reopenings(db, _findings):
        assert db.commits == 0, "re-openings are written before the commit"
        seen.reopenings += 1
        return []

    async def _views(_db, _findings):
        return {}

    async def _dismiss(db, *, admin, finding, reason, request=None):
        seen.dismissed.append((admin.id, finding.id, finding.severity, reason))

    async def _restore(db, *, admin, finding_id, request=None):
        seen.restored.append(finding_id)
        return seen.restorable

    monkeypatch.setattr(security_audit, "build_security_audit_report", _report)
    monkeypatch.setattr(finding_states, "finding_views", _views)
    monkeypatch.setattr(finding_states, "dismiss_finding", _dismiss)
    monkeypatch.setattr(finding_states, "restore_finding", _restore)
    monkeypatch.setattr(finding_states, "persist_reopenings", _persist_reopenings)

    app = FastAPI()
    app.include_router(security_audit.router, prefix="/api/v1/security-audit")
    app.dependency_overrides[deps.get_current_user] = lambda: seen.admin
    app.dependency_overrides[get_db] = lambda: seen.db
    seen.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return seen


async def test_a_reported_finding_is_dismissed_at_its_highest_severity(api):
    async with api.client as client:
        response = await client.put(
            "/api/v1/security-audit/findings/agents.guardrails_off/dismissal", json={"reason": "accepted"}
        )
    assert response.status_code == 200, response.text
    assert api.dismissed == [(api.admin.id, "agents.guardrails_off", "critical", "accepted")]
    assert api.db.commits == 1
    # One audit run: the answer is that report with the states read again.
    assert (api.builds, api.reopenings) == (1, 1)
    assert "findings" in response.json()


async def test_a_dismissal_needs_no_body(api):
    async with api.client as client:
        response = await client.put("/api/v1/security-audit/findings/agents.guardrails_off/dismissal")
    assert response.status_code == 200, response.text
    assert api.dismissed == [(api.admin.id, "agents.guardrails_off", "critical", None)]


@pytest.mark.parametrize(
    ("path", "status"),
    [
        # Not reported by this run: refused, never stored.
        ("agents.never_reported", 404),
        # A passing check has nothing to accept.
        ("encryption.secret_key_configured", 422),
        # Not a finding id at all.
        ("Agents.guardrails_off", 422),
        ("agents", 422),
        ("agents.guardrails-off", 422),
    ],
)
async def test_a_dismissal_the_audit_cannot_stand_behind_is_refused(api, path, status):
    async with api.client as client:
        response = await client.put(f"/api/v1/security-audit/findings/{path}/dismissal", json={})
    assert response.status_code == status, response.text
    assert api.dismissed == [] and api.db.commits == 0


async def test_a_reason_over_the_limit_is_refused(api):
    async with api.client as client:
        response = await client.put(
            "/api/v1/security-audit/findings/agents.guardrails_off/dismissal", json={"reason": "x" * 1001}
        )
    assert response.status_code == 422
    assert api.dismissed == []


async def test_a_dismissed_finding_is_restored(api):
    async with api.client as client:
        response = await client.delete("/api/v1/security-audit/findings/agents.guardrails_off/dismissal")
    assert response.status_code == 200, response.text
    assert api.restored == ["agents.guardrails_off"] and api.db.commits == 1
    assert (api.builds, api.reopenings) == (1, 1)


async def test_restoring_a_finding_that_is_not_dismissed_is_a_404(api):
    api.restorable = False
    async with api.client as client:
        response = await client.delete("/api/v1/security-audit/findings/agents.guardrails_off/dismissal")
    assert response.status_code == 404
    assert api.db.commits == 0


async def test_only_an_administrator_may_dismiss_or_restore(api):
    api.admin.is_admin = False
    async with api.client as client:
        dismissed = await client.put("/api/v1/security-audit/findings/agents.guardrails_off/dismissal")
        restored = await client.delete("/api/v1/security-audit/findings/agents.guardrails_off/dismissal")
    assert (dismissed.status_code, restored.status_code) == (403, 403)
    assert api.dismissed == [] and api.restored == []
