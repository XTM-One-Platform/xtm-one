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
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.services.health import alerts
from app.services.health.settings import HealthAlertEmailSettings, HealthSettings, _read

NOW = datetime(2026, 11, 21, 8, 0, tzinfo=UTC)


def _finding(fid: str, severity: str = "critical", module: str = "opencti") -> Any:
    return SimpleNamespace(
        id=fid,
        check_id=f"{module}.connector_stale",
        module=module,
        category="availability",
        severity=severity,
        recommendation="Restart the connector.",
        first_seen=NOW,
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
async def test_one_email_per_new_critical_finding_and_never_twice(sent: list[dict[str, Any]]) -> None:
    claimed: set[str] = set()

    async def claim(key: str) -> bool:
        if key in claimed:
            return False
        claimed.add(key)
        return True

    config = HealthAlertEmailSettings(enabled=True, recipients=["ops@example.com", "soc@example.com"])
    db = _db([_finding("f1")])
    assert await alerts.send_immediate_alerts(db, ["f1"], config, claim=claim) == 1
    assert [mail["to"] for mail in sent] == ["ops@example.com", "soc@example.com"]
    assert "critical finding" in sent[0]["subject"]
    # The next pass, another replica or a restart: the dedupe key holds.
    assert await alerts.send_immediate_alerts(db, ["f1"], config, claim=claim) == 0
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_no_immediate_email_when_nothing_opened_or_immediate_is_off(sent: list[dict[str, Any]]) -> None:
    async def claim(_key: str) -> bool:
        return True

    on = HealthAlertEmailSettings(enabled=True, recipients=["ops@example.com"])
    assert await alerts.send_immediate_alerts(_db([_finding("f1")]), [], on, claim=claim) == 0
    off = on.model_copy(update={"immediate": False})
    assert await alerts.send_immediate_alerts(_db([_finding("f1")]), ["f1"], off, claim=claim) == 0
    assert sent == []


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
        assert await alerts.send_alerts(_db([_finding("f1")]), ["f1"], now=NOW) == {"immediate": 0, "digest": 0}
    assert sent == []


def test_the_emails_escape_what_a_finding_carries() -> None:
    finding = _finding("f1")
    finding.recommendation = "<script>alert(1)</script>"
    _subject, html, text = alerts.finding_email(finding)
    assert "<script>" not in html
    assert "<script>alert(1)</script>" in text
