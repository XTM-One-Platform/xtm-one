"""The Security Audit answers when an assignment holds a lone surrogate.

A ``json`` column keeps a lone surrogate escape (``"\\ud800"``) a client sent in
``trigger_config``, and every PostgreSQL JSON operator refuses to parse that
row: each audit count reading ``trigger_config`` with ``->>`` (the secretless
webhook assignments, the Slack and LinkedIn integration-event assignments)
failed the whole ``GET /api/v1/security-audit`` for every admin. Each count
skips a row holding a surrogate escape before its key check, and those rows are
read as stored text and counted in Python with the same top-level reading. The
real-PostgreSQL half is ``test_security_audit_postgres.py``.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.api.v1 import security_audit
from app.core import json_sql
from app.core.config import settings
from app.services.integration_notification_auth import SlackSigningSecrets


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})).lower()


def _is_unparsed_read(sql: str) -> bool:
    """``_unparsed_trigger_configs``: the rows SQL cannot parse, read as stored text on their own."""
    return "count(" not in sql and "cast(assignments.trigger_config as text)" in sql and "join agents" not in sql


class _AuditDb:
    """Every query of the checks: the counts (``one`` / ``scalar``) and the unparsed rows (``all``).

    Only the stored-text read of the rows SQL cannot parse lists them; every other list
    (the provider webhook's count per provider and its own read of those rows, joined to
    their agents, the OAuth defaults) is empty.
    """

    def __init__(self, counts: tuple[int, int], texts: list[str]):
        self._counts = counts
        self._rows = [SimpleNamespace(trigger_config_text=text) for text in texts]
        self.statements: list[object] = []

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(statement)
        sql = _sql(statement)
        rows = self._rows if _is_unparsed_read(sql) else []
        return SimpleNamespace(
            one=lambda: self._counts,
            scalar=lambda: 0,
            scalar_one_or_none=lambda: None,
            all=lambda: rows,
        )


def _titles(findings: list[security_audit.AuditFinding]) -> list[str]:
    return [finding.title for finding in findings]


@pytest.fixture
def _no_slack_secrets(monkeypatch):
    async def _loader(_db):
        return SlackSigningSecrets()

    monkeypatch.setattr(security_audit, "load_slack_signing_secrets", _loader)


async def test_the_count_skips_rows_sql_cannot_parse_before_reading_the_secret():
    db = _AuditDb((1, 0), [])
    await security_audit._check_assignments(db, [])
    counts, unparsed = (_sql(statement) for statement in db.statements)
    assert "case when" in counts
    assert counts.index("[dd][89a-fa-f]") < counts.index("->> 'webhook_secret'")
    # Read as stored text: PostgreSQL's JSON operators refuse a lone surrogate escape.
    assert "cast(assignments.trigger_config as text)" in unparsed
    assert "->" not in unparsed
    assert "[dd][89a-fa-f]" in unparsed
    assert "assignments.enabled" in unparsed and "'webhook'" in unparsed


@pytest.mark.parametrize(
    ("config", "secretless"),
    [
        pytest.param({"path": "\ud800"}, True, id="absent"),
        pytest.param({"path": "\ud800", "webhook_secret": None}, True, id="null"),
        pytest.param({"path": "\ud800", "webhook_secret": ""}, True, id="empty"),
        pytest.param({"filter": {"webhook_secret": "s3cr3t"}, "path": "\ud800"}, True, id="nested-only"),
        pytest.param(["\ud800"], True, id="not-an-object"),
        pytest.param({"webhook_secret": "\ud800"}, False, id="lone-surrogate-secret"),
        pytest.param({"path": "\ud800", "webhook_secret": "s3cr3t"}, False, id="text"),
        pytest.param({"path": "\ud800", "webhook_secret": 12345}, False, id="integer"),
        pytest.param({"path": "\ud800", "webhook_secret": False}, False, id="false"),
        pytest.param({"path": "\ud800", "webhook_secret": []}, False, id="empty-list"),
        pytest.param({"path": "\U0001f600", "webhook_secret": "s3cr3t"}, False, id="paired-escape"),
    ],
)
async def test_an_unparsed_webhook_row_is_secretless_exactly_as_the_count_reads_it(monkeypatch, config, secretless):
    """``coalesce(trigger_config->>'webhook_secret', '') = ''``: only the top-level key, none when absent, null or empty."""
    monkeypatch.setattr(settings, "webhook_require_secret", False)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_assignments(_AuditDb((1, 0), [json.dumps(config)]), findings)
    titles = _titles(findings)
    assert ("1 webhook-triggered assignment(s) without a webhook secret" in titles) is secretless
    assert ("All webhook-triggered assignments have a webhook secret" in titles) is not secretless


@pytest.mark.parametrize(
    ("config", "slack", "linkedin"),
    [
        pytest.param({"provider": "slack", "event": "new_slack_message", "channel": "\ud800"}, 1, 0, id="slack"),
        pytest.param({"provider": "slack", "event": "reaction_added", "channel": "\ud800"}, 0, 0, id="other-event"),
        pytest.param({"provider": "linkedin", "note": "\udc00"}, 0, 1, id="linkedin"),
        pytest.param({"filter": {"provider": "slack"}, "event": "new_slack_message", "x": "\ud800"}, 0, 0, id="nested"),
        pytest.param(["\ud800"], 0, 0, id="not-an-object"),
    ],
)
async def test_an_unparsed_integration_event_row_is_counted_as_the_counts_read_it(
    monkeypatch, _no_slack_secrets, config, slack, linkedin
):
    """``trigger_config->>'provider'`` / ``->>'event'``: top-level keys only, as the SQL counts read them."""
    monkeypatch.setattr(settings, "integration_notifications_require_verification", False)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_integration_notifications(_AuditDb((0, 0), [json.dumps(config)]), findings)
    by_id = {finding.id: finding for finding in findings}
    slack_finding = by_id.get("integrations.slack_events_unverified")
    linkedin_finding = by_id.get("integrations.linkedin_notifications_unverified")
    assert (slack_finding is not None) is bool(slack)
    assert (linkedin_finding is not None) is bool(linkedin)
    for finding in (slack_finding, linkedin_finding):
        if finding is not None:
            assert finding.description.startswith("1 active assignment(s)")


async def test_the_integration_event_counts_skip_rows_sql_cannot_parse(_no_slack_secrets):
    db = _AuditDb((0, 0), [])
    await security_audit._check_integration_notifications(db, [])
    compiled = [_sql(statement) for statement in db.statements]
    for provider in ("'slack'", "'linkedin'"):
        [count] = [sql for sql in compiled if "count(" in sql and provider in sql]
        assert count.index("[dd][89a-fa-f]") < count.index("->> 'provider'")
    [unparsed] = [sql for sql in compiled if _is_unparsed_read(sql)]
    assert "->" not in unparsed and "'integration_event'" in unparsed


@pytest.mark.parametrize(
    ("stored", "skipped"),
    [
        pytest.param('{"webhook_secret": "\\ud800"}', True, id="lone-high"),
        pytest.param('{"path": "\\udc00", "webhook_secret": "s3cr3t"}', True, id="lone-low"),
        pytest.param('{"webhook_secret": "\\u0000"}', True, id="nul"),
        pytest.param('{"webhook_secret": "\\uD83D\\uDE00"}', False, id="valid-pair"),
        pytest.param('{"webhook_secret": "\\\\ud800"}', False, id="escaped-backslash"),
        pytest.param('{"webhook_secret": "s3cr3t"}', False, id="text"),
        pytest.param('{"webhook_secret": "caf\\u00e9"}', False, id="accent"),
        pytest.param('{"events": ["push"]}', False, id="array"),
    ],
)
def test_the_skip_test_matches_exactly_the_rows_postgresql_cannot_parse(stored, skipped):
    """The Python reading of ``json_sql.json_unparseable``: PostgreSQL ARE and ``re`` read the patterns alike.

    A valid pair is parsed by PostgreSQL and counted in SQL like any other row.
    """
    real_escapes = stored.replace(json_sql.ESCAPED_BACKSLASH, "_")
    unpaired = re.sub(json_sql.SURROGATE_PAIR_PATTERN, "_", real_escapes)
    assert bool(re.search(json_sql.REFUSED_ESCAPE_PATTERN, unpaired)) is skipped
