# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Platform Health alert emails (#101 S12, dev-docs/platform-health.md sections 7 and 9): off by
default, recipients validated, one email per new critical finding (deduplicated), one digest a day."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.services.health import alerts
from app.services.health.settings import HealthAlertEmailSettings, HealthSettings, _read

NOW = datetime(2026, 11, 21, 8, 0, tzinfo=UTC)


def _finding(fid: str, severity: str = "critical", module: str = "opencti", *, opened_minutes_ago: int = 20) -> Any:
    opened = NOW - timedelta(minutes=opened_minutes_ago)
    return SimpleNamespace(
        id=fid,
        check_id=f"{module}.connector_stale",
        module=module,
        category="availability",
        severity=severity,
        status="open",
        recommendation="Restart the connector.",
        first_seen=opened,
        opened_at=opened,
        alerted_at=None,
    )


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> list[Any]:
        return list(self._rows)


class _Db:
    def __init__(self, rows: list[Any], mark: Any = None) -> None:
        self.rows, self.mark, self.commits = rows, mark, 0
        self.added: list[Any] = []

    async def execute(self, _stmt: Any) -> _Rows:
        return _Rows(self.rows)

    async def get(self, _model: Any, _key: Any) -> Any:
        return self.mark

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        self.mark = obj

    async def commit(self) -> None:
        self.commits += 1


def _db(rows: list[Any], mark: Any = None) -> Any:
    """A session double answering *rows* to every select (typed Any: it stands for an AsyncSession)."""
    return _Db(rows, mark)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    outbox: list[dict[str, Any]] = []

    async def fake_deliver(**kwargs: Any) -> None:
        outbox.append(kwargs)

    monkeypatch.setattr("app.services.email.deliver_email", fake_deliver)
    return outbox


def test_alert_emails_are_off_by_default_and_read_from_an_older_value() -> None:
    assert HealthSettings().alert_emails == HealthAlertEmailSettings()
    assert HealthAlertEmailSettings().enabled is False
    assert HealthAlertEmailSettings().recipients == []
    assert _read({"enabled": True}).alert_emails.enabled is False


def test_recipients_are_trimmed_lower_cased_and_deduplicated() -> None:
    config = HealthAlertEmailSettings(recipients=[" Ops@Example.com ", "ops@example.com", "soc@example.org"])
    assert config.recipients == ["ops@example.com", "soc@example.org"]


@pytest.mark.parametrize("value", ["", "ops", "ops@", "@example.com", "ops@localhost", "a b@example.com", "x@y@z.com"])
def test_a_value_that_is_not_an_address_is_refused(value: str) -> None:
    with pytest.raises(ValidationError):
        HealthAlertEmailSettings(recipients=[value])


def test_at_or_above_orders_the_severities() -> None:
    assert alerts.at_or_above("critical", "warning")
    assert alerts.at_or_above("warning", "warning")
    assert not alerts.at_or_above("info", "warning")


@pytest.mark.asyncio
async def test_one_email_per_critical_finding_open_for_the_window_and_never_twice(sent: list[dict[str, Any]]) -> None:
    claimed: set[str] = set()

    async def claim(key: str) -> bool:
        if key in claimed:
            return False
        claimed.add(key)
        return True

    config = HealthAlertEmailSettings(enabled=True, recipients=["ops@example.com", "soc@example.com"])
    row = _finding("f1")
    db = _db([row])
    assert await alerts.send_immediate_alerts(db, config, now=NOW, claim=claim) == 1
    assert [mail["to"] for mail in sent] == ["ops@example.com", "soc@example.com"]
    assert "critical finding" in sent[0]["subject"]
    assert row.alerted_at == NOW
    # The next pass: the episode is stamped, nothing again.
    assert await alerts.send_immediate_alerts(db, config, now=NOW + timedelta(minutes=5), claim=claim) == 0
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_no_immediate_email_before_the_window_or_when_immediate_is_off(sent: list[dict[str, Any]]) -> None:
    async def claim(_key: str) -> bool:
        return True

    on = HealthAlertEmailSettings(enabled=True, recipients=["ops@example.com"])
    assert on.alert_after_minutes == 15
    young = _finding("f1", opened_minutes_ago=10)
    assert await alerts.send_immediate_alerts(_db([young]), on, now=NOW, claim=claim) == 0
    assert young.alerted_at is None
    off = on.model_copy(update={"immediate": False})
    assert await alerts.send_immediate_alerts(_db([_finding("f2")]), off, now=NOW, claim=claim) == 0
    assert sent == []


def test_alert_due_reads_the_window_the_severity_and_the_episode_stamp() -> None:
    config = HealthAlertEmailSettings(alert_after_minutes=15)
    assert alerts.alert_due(_finding("f", opened_minutes_ago=15), config, NOW)
    assert not alerts.alert_due(_finding("f", opened_minutes_ago=14), config, NOW)
    assert not alerts.alert_due(_finding("f", "warning"), config, NOW)
    alerted = _finding("f")
    alerted.alerted_at = NOW
    assert not alerts.alert_due(alerted, config, NOW)
    resolved = _finding("f")
    resolved.status = "resolved"
    assert not alerts.alert_due(resolved, config, NOW)


def test_the_dedupe_key_names_the_open_episode() -> None:
    first, second = NOW, NOW + timedelta(days=1)
    assert alerts.dedupe_key("f1", first) != alerts.dedupe_key("f1", second)
    assert alerts.dedupe_key("f1") == "health:alert:f1"


def test_alert_after_minutes_is_bounded_and_read_from_an_older_value() -> None:
    assert _read({"alert_emails": {"enabled": True}}).alert_emails.alert_after_minutes == 15
    with pytest.raises(ValidationError):
        HealthAlertEmailSettings(alert_after_minutes=-1)
    with pytest.raises(ValidationError):
        HealthAlertEmailSettings(alert_after_minutes=24 * 60 + 1)


@pytest.mark.asyncio
async def test_one_digest_a_day_listing_the_findings_at_or_above_the_minimum(sent: list[dict[str, Any]]) -> None:
    config = HealthAlertEmailSettings(enabled=True, recipients=["ops@example.com"], min_severity="warning")
    db = _db([_finding("f1"), _finding("f2", "warning", "openaev"), _finding("f3", "info")])
    assert await alerts.send_daily_digest(db, config, now=NOW) is True
    assert len(sent) == 1
    assert sent[0]["subject"] == "Platform Health: 2 open findings on 2026-11-21"
    assert "openaev.connector_stale" in sent[0]["text"]
    assert "[info]" not in sent[0]["text"]
    # Same day: marked, nothing sent again.
    assert await alerts.send_daily_digest(db, config, now=NOW.replace(hour=20)) is False
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_no_digest_when_nothing_is_open_at_the_minimum(sent: list[dict[str, Any]]) -> None:
    config = HealthAlertEmailSettings(enabled=True, recipients=["ops@example.com"], min_severity="critical")
    db = _db([_finding("f3", "info")])
    assert await alerts.send_daily_digest(db, config, now=NOW) is False
    assert sent == []
    assert db.commits == 1


@pytest.mark.asyncio
async def test_send_alerts_sends_nothing_when_off_without_recipients_or_without_smtp(
    monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
) -> None:
    def loader(config: HealthAlertEmailSettings) -> Callable[[Any], Awaitable[HealthSettings]]:
        async def load(_db: Any) -> HealthSettings:
            return HealthSettings(alert_emails=config)

        return load

    on = HealthAlertEmailSettings(enabled=True, recipients=["ops@example.com"])
    cases = [
        (HealthAlertEmailSettings(), True),
        (HealthAlertEmailSettings(enabled=True), True),
        (on, False),
    ]
    for config, smtp in cases:
        monkeypatch.setattr(alerts, "load_health_settings", loader(config))
        monkeypatch.setattr("app.services.email.is_email_configured", lambda smtp=smtp: smtp)
        row = _finding("f1")
        assert await alerts.send_alerts(_db([row]), now=NOW) == {"immediate": 0, "digest": 0}
        # Stamped without a send: enabling the emails later never mails it.
        assert row.alerted_at == NOW
    assert sent == []


@pytest.mark.asyncio
async def test_enabling_the_emails_later_mails_only_what_opens_after(
    monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
) -> None:
    async def claim(_key: str) -> bool:
        return True

    settings = {"config": HealthAlertEmailSettings()}

    async def load(_db: Any) -> HealthSettings:
        return HealthSettings(alert_emails=settings["config"])

    monkeypatch.setattr(alerts, "load_health_settings", load)
    monkeypatch.setattr("app.services.email.is_email_configured", lambda: True)
    already_open = _finding("f1", opened_minutes_ago=60)
    young = _finding("f2", opened_minutes_ago=5)
    db = _db([already_open, young])
    await alerts.send_alerts(db, now=NOW)
    assert already_open.alerted_at == NOW
    assert young.alerted_at is None
    # The administrator turns the emails on: no burst for the finding already open.
    settings["config"] = HealthAlertEmailSettings(enabled=True, recipients=["ops@example.com"])
    later = NOW + timedelta(minutes=20)
    assert await alerts.send_immediate_alerts(db, settings["config"], now=later, claim=claim) == 1
    assert [mail["to"] for mail in sent] == ["ops@example.com"]
    assert young.alerted_at == later


@pytest.mark.asyncio
async def test_a_restored_finding_starts_a_new_episode_and_waits_for_the_window() -> None:
    from app.models.health_check import STATUS_DISMISSED, STATUS_OPEN
    from app.services.health import engine

    row = _finding("f1", opened_minutes_ago=600)
    row.status, row.dismissal, row.alerted_at = STATUS_DISMISSED, {"reason": "known"}, None

    class _Session:
        async def get(self, _model: Any, _key: Any) -> Any:
            return row

        async def flush(self) -> None:
            return None

    session: Any = _Session()
    before = datetime.now(UTC)
    restored = await engine.restore_finding(session, "f1")
    opened = restored.opened_at
    assert restored.status == STATUS_OPEN
    assert restored.alerted_at is None
    assert opened is not None and opened >= before
    assert not alerts.alert_due(restored, HealthAlertEmailSettings(), opened + timedelta(minutes=1))


@pytest.mark.asyncio
async def test_immediate_off_stamps_the_due_findings_without_sending(sent: list[dict[str, Any]]) -> None:
    off = HealthAlertEmailSettings(enabled=True, recipients=["ops@example.com"], immediate=False)
    row = _finding("f1")
    assert await alerts.send_immediate_alerts(_db([row]), off, now=NOW) == 0
    assert row.alerted_at == NOW
    assert sent == []


def test_the_emails_escape_what_a_finding_carries() -> None:
    finding = _finding("f1")
    finding.recommendation = "<script>alert(1)</script>"
    _subject, html, text = alerts.finding_email(finding)
    assert "<script>" not in html
    assert "<script>alert(1)</script>" in text
