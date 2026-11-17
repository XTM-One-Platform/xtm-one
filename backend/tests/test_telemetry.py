"""Tests for anonymous usage telemetry helpers.

Focus on the snapshot-refresh contract the count helpers must honour: a failed
read returns ``None`` so the previous snapshot value is preserved (never
clobbered with a bogus ``0``), and ``_apply`` only updates the snapshot on a
successful read.
"""

import logging
import sys
from types import SimpleNamespace

import pytest

import app.services.telemetry as telemetry
from app.models.user import User


class _RaisingSession:
    """Async-session double whose ``execute`` always raises."""

    async def execute(self, *_args, **_kwargs):
        raise RuntimeError("simulated DB failure")


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _OkSession:
    """Async-session double whose ``execute`` returns a fixed scalar."""

    def __init__(self, value):
        self._value = value

    async def execute(self, *_args, **_kwargs):
        return _ScalarResult(self._value)


async def test_scalar_count_returns_none_on_error():
    # ``None`` (not 0) lets refresh_telemetry_data keep the previous value.
    assert await telemetry._scalar_count(_RaisingSession(), User) is None


async def test_scalar_count_returns_int_on_success():
    assert await telemetry._scalar_count(_OkSession(7), User) == 7


async def test_scalar_count_coerces_null_to_zero():
    # COUNT(*) of an empty table comes back as 0, not None.
    assert await telemetry._scalar_count(_OkSession(None), User) == 0


async def test_filtered_count_returns_none_on_error():
    assert (
        await telemetry._filtered_count(
            _RaisingSession(), User, User.is_active.is_(True)
        )
        is None
    )


async def test_filtered_count_returns_int_on_success():
    assert (
        await telemetry._filtered_count(_OkSession(3), User, User.is_active.is_(True))
        == 3
    )


def test_apply_preserves_previous_value_on_none():
    telemetry._state.users_count = 42
    telemetry._apply("users_count", None)  # failed read
    assert telemetry._state.users_count == 42  # preserved, not clobbered


def test_apply_updates_snapshot_on_value():
    telemetry._state.users_count = 42
    telemetry._apply("users_count", 100)
    assert telemetry._state.users_count == 100

    telemetry._apply("users_count", 0)  # a real zero must still apply
    assert telemetry._state.users_count == 0


def test_register_gauges_collects_all_metrics():
    """The observable gauges must register and collect against the pinned OTel
    SDK — guards the init path (the instrument-class imports + gauge wiring)
    that runs lazily inside ``init_telemetry`` and is therefore not exercised
    by app startup under the test runner.
    """
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter(telemetry.TELEMETRY_SERVICE_NAME)

    telemetry._state.users_count = 5
    telemetry._state.agents_count = 9
    telemetry._state.llm_providers_by_type = {"openai": 2, "anthropic": 1}
    telemetry._state.edition = "EE"

    telemetry._register_gauges(meter, telemetry._state)

    data = reader.get_metrics_data()
    names = [
        m.name
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    ]
    # All single-value gauges + the LLM-by-type and platform_info gauges.
    assert len(names) >= 23
    assert any("platform_info" in n for n in names)
    assert any("llm_providers_by_type" in n for n in names)
    assert all(n.startswith(telemetry._PREFIX) for n in names)
    provider.shutdown()


def _collect_metrics(reader):
    """Flatten an InMemoryMetricReader read into ``{name: metric}``."""
    data = reader.get_metrics_data()
    return {
        m.name: m
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }


def test_register_gauges_exports_llm_analytics():
    """The LLM analytics gauges (totals + by-day with a ``date`` label) must
    all export.  Averages with no data yet (``None``) emit no
    observation.
    """
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter(telemetry.TELEMETRY_SERVICE_NAME)

    telemetry._state.llm_total_cost = 4041.51
    telemetry._state.llm_total_tokens = 1_400_000
    telemetry._state.llm_avg_latency_ms = 35_200.0
    telemetry._state.llm_avg_feedback = None  # no feedback yet — omitted
    telemetry._state.llm_total_cost_by_day = {"2026-07-01": 1.5, "2026-07-02": 2.25}
    telemetry._state.llm_total_tokens_by_day = {"2026-07-01": 100, "2026-07-02": 250}
    telemetry._state.llm_avg_latency_ms_by_day = {"2026-07-01": 900.0}
    telemetry._state.llm_avg_feedback_by_day = {}

    telemetry._register_gauges(meter, telemetry._state)
    metrics = _collect_metrics(reader)
    prefix = telemetry._PREFIX

    def _points(name):
        return list(metrics[f"{prefix}{name}"].data.data_points)

    assert _points("llm_total_cost")[0].value == pytest.approx(4041.51)
    assert _points("llm_total_tokens")[0].value == 1_400_000
    assert _points("llm_avg_latency_ms")[0].value == pytest.approx(35_200.0)

    # A None average emits no observation at all.
    assert f"{prefix}llm_avg_feedback" not in metrics

    cost_points = {
        dict(p.attributes)["date"]: p.value
        for p in _points("llm_total_cost_by_day")
    }
    assert cost_points == {"2026-07-01": pytest.approx(1.5), "2026-07-02": pytest.approx(2.25)}
    token_points = {
        dict(p.attributes)["date"]: p.value
        for p in _points("llm_total_tokens_by_day")
    }
    assert token_points == {"2026-07-01": 100, "2026-07-02": 250}

    info_attrs = dict(_points("platform_info")[0].attributes)
    assert info_attrs["platform_mode"] == telemetry._state.platform_mode
    assert "is_saas" not in info_attrs  # replaced by TELEMETRY_TAGS
    provider.shutdown()


def test_register_gauges_exports_provider_breakdowns():
    """Integrations and channel bots must export one datapoint per catalog
    provider with the count as value and the provider as attribute.
    """
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter(telemetry.TELEMETRY_SERVICE_NAME)

    telemetry._state.integrations_by_provider = {"github": 3, "opencti": 1}
    # Channel-bot provider keys carry the catalog ``_bot`` suffix
    # (ChannelBot.provider / CHANNEL_TO_BOT_PROVIDER) — use the real
    # identifier so the test documents the actual telemetry contract.
    telemetry._state.channel_bots_by_provider = {"slack_bot": 2}

    telemetry._register_gauges(meter, telemetry._state)
    metrics = _collect_metrics(reader)
    prefix = telemetry._PREFIX

    integration_points = {
        dict(p.attributes)["provider"]: p.value
        for p in metrics[f"{prefix}integrations_by_provider"].data.data_points
    }
    assert integration_points == {"github": 3, "opencti": 1}

    bot_points = {
        dict(p.attributes)["provider"]: p.value
        for p in metrics[f"{prefix}channel_bots_by_provider"].data.data_points
    }
    assert bot_points == {"slack_bot": 2}
    provider.shutdown()


def test_telemetry_tags_normalization(monkeypatch):
    """TELEMETRY_TAGS must normalize to a canonical string: trim, lowercase,
    drop empties, dedupe, sort — so the same tag set always produces the same
    warehouse value regardless of how the operator typed it.
    """
    cases = {
        "": "",
        "   ": "",
        "saas": "saas",
        "saas,eu-west": "eu-west,saas",
        "  EU-West ,SAAS, saas,, ": "eu-west,saas",
        "b,a,c,a": "a,b,c",
    }
    for raw, expected in cases.items():
        monkeypatch.setattr(telemetry.settings, "telemetry_tags", raw)
        assert telemetry.settings.telemetry_tags_normalized == expected


def test_resource_attributes_carry_normalized_tags(monkeypatch):
    """When TELEMETRY_TAGS is set, the resource attributes must carry exactly
    one ``filigran.telemetry.tags`` attribute with the NORMALIZED value (the
    warehouse lands it as a ``tags`` column on every metric row).
    """
    monkeypatch.setattr(telemetry.settings, "telemetry_tags", "  SAAS, eu-west ,saas")

    attrs = telemetry._build_resource_attributes("instance-1", "2026-01-01T00:00:00")

    assert attrs["filigran.telemetry.tags"] == "eu-west,saas"
    assert attrs["service.instance.id"] == "instance-1"
    assert attrs["service.instance.creation"] == "2026-01-01T00:00:00"
    assert attrs["service.name"] == telemetry.TELEMETRY_SERVICE_NAME
    assert attrs["xtm.platform_mode"] == telemetry.settings.platform_mode


def test_resource_attributes_omit_tags_when_unconfigured(monkeypatch):
    """With no TELEMETRY_TAGS (or only whitespace/empties), the tags attribute
    must be omitted ENTIRELY — never exported as an empty string.
    """
    for raw in ("", "  ", ",,, ,"):
        monkeypatch.setattr(telemetry.settings, "telemetry_tags", raw)
        attrs = telemetry._build_resource_attributes("instance-1", "")
        assert "filigran.telemetry.tags" not in attrs


def test_llm_by_day_window_start_is_utc_midnight_bounded(monkeypatch):
    """The per-day window anchor must be a UTC midnight spanning today plus the
    previous ``_LLM_BY_DAY_WINDOW_DAYS - 1`` days, so the DATE-grouped query can
    never yield more than ``_LLM_BY_DAY_WINDOW_DAYS`` distinct ``date`` labels
    (bounded-cardinality contract).  The clock is frozen (one second before a
    UTC midnight — the worst case for an unanchored window) so the assertions
    are deterministic and can never straddle a midnight rollover mid-test.
    """
    from datetime import UTC, datetime

    frozen_now = datetime(2026, 7, 2, 23, 59, 59, tzinfo=UTC)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_now if tz is not None else frozen_now.replace(tzinfo=None)

    monkeypatch.setattr(telemetry, "datetime", _FrozenDatetime)

    start = telemetry._llm_by_day_window_start()

    assert start.tzinfo is UTC
    assert (start.hour, start.minute, start.second, start.microsecond) == (0, 0, 0, 0)
    # Distinct UTC dates in [start, frozen now] == exactly the window size.
    assert (frozen_now.date() - start.date()).days == telemetry._LLM_BY_DAY_WINDOW_DAYS - 1


class _AnalyticsResult:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows

    def one(self):
        return self._row

    def all(self):
        return self._rows


class _AnalyticsSession:
    """Async-session double serving the totals row then the by-day rows."""

    def __init__(self, totals_row, day_rows):
        self._results = [
            _AnalyticsResult(row=totals_row),
            _AnalyticsResult(rows=day_rows),
        ]

    async def execute(self, *_args, **_kwargs):
        return self._results.pop(0)


async def test_refresh_llm_analytics_commits_snapshot():
    """A successful refresh commits totals + by-day series to the snapshot,
    keeping ``None`` averages as ``None`` (gauge omitted) and skipping by-day
    entries whose average has no data.
    """
    from datetime import date
    from decimal import Decimal

    totals = SimpleNamespace(
        total_cost=Decimal("12.5"),
        total_tokens=100,
        avg_latency_ms=250.0,
        avg_feedback=None,
    )
    day_rows = [
        SimpleNamespace(
            day=date(2026, 7, 1),
            total_cost=Decimal("1.5"),
            total_tokens=40,
            avg_latency_ms=200.0,
            avg_feedback=0.8,
        ),
        SimpleNamespace(
            day=date(2026, 7, 2),
            total_cost=Decimal("11.0"),
            total_tokens=60,
            avg_latency_ms=None,
            avg_feedback=None,
        ),
    ]

    await telemetry._refresh_llm_analytics(_AnalyticsSession(totals, day_rows))

    assert telemetry._state.llm_total_cost == 12.5
    assert telemetry._state.llm_total_tokens == 100
    assert telemetry._state.llm_avg_latency_ms == 250.0
    assert telemetry._state.llm_avg_feedback is None
    assert telemetry._state.llm_total_cost_by_day == {
        "2026-07-01": 1.5,
        "2026-07-02": 11.0,
    }
    assert telemetry._state.llm_total_tokens_by_day == {
        "2026-07-01": 40,
        "2026-07-02": 60,
    }
    # Days without latency/feedback data are absent, not zeroed.
    assert telemetry._state.llm_avg_latency_ms_by_day == {"2026-07-01": 200.0}
    assert telemetry._state.llm_avg_feedback_by_day == {"2026-07-01": 0.8}


async def test_refresh_llm_analytics_preserves_snapshot_on_failure():
    """A failed trace-store read must preserve the previous LLM analytics
    snapshot — never clobber it with zeros/empties (same contract as every
    other telemetry refresh step).
    """
    telemetry._state.llm_total_cost = 42.0
    telemetry._state.llm_total_tokens = 1234
    telemetry._state.llm_avg_latency_ms = 100.0
    telemetry._state.llm_avg_feedback = 0.9
    telemetry._state.llm_total_cost_by_day = {"2026-07-01": 42.0}

    await telemetry._refresh_llm_analytics(_RaisingSession())

    assert telemetry._state.llm_total_cost == 42.0
    assert telemetry._state.llm_total_tokens == 1234
    assert telemetry._state.llm_avg_latency_ms == 100.0
    assert telemetry._state.llm_avg_feedback == 0.9
    assert telemetry._state.llm_total_cost_by_day == {"2026-07-01": 42.0}


class _LoginAnalyticsSession:
    """Async-session double serving the login total then the by-day rows."""

    def __init__(self, total, day_rows):
        self._results = [
            _ScalarResult(total),
            _AnalyticsResult(rows=day_rows),
        ]

    async def execute(self, *_args, **_kwargs):
        return self._results.pop(0)


async def test_refresh_login_analytics_commits_snapshot():
    """A successful refresh commits the all-time login total and the per-day
    series (keyed by ISO date) to the snapshot.
    """
    from datetime import date

    day_rows = [
        SimpleNamespace(day=date(2026, 7, 1), login_count=12),
        SimpleNamespace(day=date(2026, 7, 2), login_count=5),
    ]

    await telemetry._refresh_login_analytics(_LoginAnalyticsSession(347, day_rows))

    assert telemetry._state.user_login_count == 347
    assert telemetry._state.logins_by_day == {"2026-07-01": 12, "2026-07-02": 5}


async def test_refresh_login_analytics_preserves_snapshot_on_failure():
    """A failed audit-log read must preserve the previous login snapshot —
    never clobber it with zeros/empties (same contract as every other
    telemetry refresh step).
    """
    telemetry._state.user_login_count = 99
    telemetry._state.logins_by_day = {"2026-07-01": 7}

    await telemetry._refresh_login_analytics(_RaisingSession())

    assert telemetry._state.user_login_count == 99
    assert telemetry._state.logins_by_day == {"2026-07-01": 7}


class _CapturingLoginSession:
    """Async-session double that records every executed statement."""

    def __init__(self):
        self.statements = []
        self._results = [_ScalarResult(0), _AnalyticsResult(rows=[])]

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(statement)
        return self._results.pop(0)


async def test_refresh_login_analytics_excludes_mcp_oauth_grants():
    """Both login queries (total + by-day) must exclude MCP OAuth client
    authorization grants — audited with the same ``action="login"`` but
    marked by ``details.login_method`` in ``_MCP_OAUTH_LOGIN_METHODS`` — and
    the exclusion must be NULL-safe (COALESCE) so real sign-ins that carry no
    ``login_method`` at all (OIDC/SAML rows) still count.
    """
    from sqlalchemy.dialects import postgresql

    session = _CapturingLoginSession()
    await telemetry._refresh_login_analytics(session)

    assert len(session.statements) == 2
    for statement in session.statements:
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "'login'" in sql
        # NULL-safe: COALESCE keeps rows whose details/login_method is NULL.
        assert "coalesce" in sql.lower()
        assert "login_method" in sql
        assert "NOT IN" in sql
        for method in telemetry._MCP_OAUTH_LOGIN_METHODS:
            assert f"'{method}'" in sql


class _ActivitySession:
    """Async-session double serving the three activity by-day row sets
    (assignment runs, conversations, messages — in refresh order)."""

    def __init__(self, run_rows, conversation_rows, message_rows):
        self._results = [
            _AnalyticsResult(rows=run_rows),
            _AnalyticsResult(rows=conversation_rows),
            _AnalyticsResult(rows=message_rows),
        ]

    async def execute(self, *_args, **_kwargs):
        return self._results.pop(0)


async def test_refresh_activity_by_day_commits_snapshot():
    """A successful refresh commits the per-day activity series (rows created
    per UTC day, keyed by ISO date) for assignment runs, conversations, and
    messages.
    """
    from datetime import date

    run_rows = [
        SimpleNamespace(day=date(2026, 7, 1), row_count=4),
        SimpleNamespace(day=date(2026, 7, 2), row_count=9),
    ]
    conversation_rows = [SimpleNamespace(day=date(2026, 7, 2), row_count=3)]
    message_rows = [
        SimpleNamespace(day=date(2026, 7, 1), row_count=25),
        SimpleNamespace(day=date(2026, 7, 2), row_count=41),
    ]

    await telemetry._refresh_activity_by_day(
        _ActivitySession(run_rows, conversation_rows, message_rows)
    )

    assert telemetry._state.assignment_runs_by_day == {
        "2026-07-01": 4,
        "2026-07-02": 9,
    }
    assert telemetry._state.conversations_by_day == {"2026-07-02": 3}
    assert telemetry._state.messages_by_day == {
        "2026-07-01": 25,
        "2026-07-02": 41,
    }


async def test_refresh_activity_by_day_preserves_snapshot_on_failure():
    """A failed read must preserve the previous per-day activity snapshot —
    never clobber it with empties (same contract as every other telemetry
    refresh step).
    """
    telemetry._state.assignment_runs_by_day = {"2026-07-01": 4}
    telemetry._state.conversations_by_day = {"2026-07-01": 2}
    telemetry._state.messages_by_day = {"2026-07-01": 10}

    await telemetry._refresh_activity_by_day(_RaisingSession())

    assert telemetry._state.assignment_runs_by_day == {"2026-07-01": 4}
    assert telemetry._state.conversations_by_day == {"2026-07-01": 2}
    assert telemetry._state.messages_by_day == {"2026-07-01": 10}


class _CapturingActivitySession:
    """Async-session double that records every executed statement."""

    def __init__(self):
        self.statements = []
        self._results = [
            _AnalyticsResult(rows=[]),
            _AnalyticsResult(rows=[]),
            _AnalyticsResult(rows=[]),
        ]

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(statement)
        return self._results.pop(0)


async def test_refresh_activity_by_day_is_window_bounded():
    """Every activity by-day query must be bounded to the rolling window
    (``created_at >= window_start``) and grouped by UTC date — the series
    covers ONLY the span, never the whole table.
    """
    from sqlalchemy.dialects import postgresql

    session = _CapturingActivitySession()
    await telemetry._refresh_activity_by_day(session)

    assert len(session.statements) == 3
    for statement in session.statements:
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "created_at >=" in sql
        assert "timezone('UTC'" in sql
        assert "GROUP BY" in sql


def test_register_gauges_exports_activity_by_day():
    """The activity by-day gauges must export one datapoint per day with the
    UTC ISO date as the ``date`` attribute — the same contract as
    ``logins_by_day``.
    """
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter(telemetry.TELEMETRY_SERVICE_NAME)

    telemetry._state.assignment_runs_by_day = {"2026-07-01": 4, "2026-07-02": 9}
    telemetry._state.conversations_by_day = {"2026-07-02": 3}
    telemetry._state.messages_by_day = {"2026-07-01": 25}

    telemetry._register_gauges(meter, telemetry._state)
    metrics = _collect_metrics(reader)
    prefix = telemetry._PREFIX

    def _day_points(name):
        return {
            dict(p.attributes)["date"]: p.value
            for p in metrics[f"{prefix}{name}"].data.data_points
        }

    assert _day_points("assignment_runs_by_day") == {"2026-07-01": 4, "2026-07-02": 9}
    assert _day_points("conversations_by_day") == {"2026-07-02": 3}
    assert _day_points("messages_by_day") == {"2026-07-01": 25}
    provider.shutdown()


class _MonthSession:
    """Async-session double serving the run rows then the template rows."""

    def __init__(self, run_rows, template_rows):
        self.statements = []
        self._results = [
            _AnalyticsResult(rows=run_rows),
            _AnalyticsResult(rows=template_rows),
        ]

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(statement)
        return self._results.pop(0)


def _freeze_month_starts(monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.setattr(
        telemetry,
        "_month_starts_utc",
        lambda: (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)),
    )


async def test_refresh_assignments_ran_by_month_classifies_each_origin(monkeypatch):
    """One assignment of each origin is counted under its origin, per month
    and once per month, and the other month is zero-filled."""
    _freeze_month_starts(monkeypatch)
    run_rows = [
        SimpleNamespace(month="2026-11", assignment_id=1, name="Daily digest", is_builtin=True, template_slug=None),
        SimpleNamespace(month="2026-11", assignment_id=2, name="Triage inbox", is_builtin=False, template_slug="email-triage"),
        SimpleNamespace(month="2026-11", assignment_id=3, name="My own flow", is_builtin=False, template_slug="email-triage"),
        SimpleNamespace(month="2026-11", assignment_id=4, name="Hand written", is_builtin=False, template_slug=None),
        SimpleNamespace(month="2026-10", assignment_id=4, name="Hand written", is_builtin=False, template_slug=None),
    ]
    template_rows = [
        SimpleNamespace(
            slug="email-triage",
            default_assignments=[{"name": "Triage inbox"}, "not an entry"],
        )
    ]
    session = _MonthSession(run_rows, template_rows)

    await telemetry._refresh_assignments_ran_by_month(session)

    assert telemetry._state.assignments_ran_by_month == {
        "2026-10": {"builtin": 0, "template_default": 0, "custom": 1},
        "2026-11": {"builtin": 1, "template_default": 1, "custom": 2},
    }


async def test_refresh_assignments_ran_by_month_is_bounded_and_distinct(monkeypatch):
    """The run query starts at the previous month and groups by assignment
    and UTC month, so an assignment counts once per month."""
    from sqlalchemy.dialects import postgresql

    _freeze_month_starts(monkeypatch)
    session = _MonthSession([], [])

    await telemetry._refresh_assignments_ran_by_month(session)

    assert len(session.statements) == 1
    sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "assignment_runs.created_at >= '2026-10-01" in sql
    assert "timezone('UTC'" in sql
    assert "GROUP BY" in sql
    assert telemetry._state.assignments_ran_by_month == {
        "2026-10": {"builtin": 0, "template_default": 0, "custom": 0},
        "2026-11": {"builtin": 0, "template_default": 0, "custom": 0},
    }


async def test_refresh_assignments_ran_by_month_preserves_snapshot_on_failure():
    previous = {"2026-11": {"builtin": 1, "template_default": 0, "custom": 3}}
    telemetry._state.assignments_ran_by_month = previous

    await telemetry._refresh_assignments_ran_by_month(_RaisingSession())

    assert telemetry._state.assignments_ran_by_month == previous


def test_month_starts_utc_cross_the_year(monkeypatch):
    from datetime import UTC, datetime

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2027, 1, 15, 10, 30, tzinfo=UTC)

    monkeypatch.setattr(telemetry, "datetime", _FrozenDatetime)
    previous, current = telemetry._month_starts_utc()

    assert previous == datetime(2026, 12, 1, tzinfo=UTC)
    assert current == datetime(2027, 1, 1, tzinfo=UTC)


def test_register_gauges_exports_assignments_ran_by_month():
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter(telemetry.TELEMETRY_SERVICE_NAME)
    telemetry._state.assignments_ran_by_month = {
        "2026-11": {"builtin": 1, "template_default": 2, "custom": 3},
    }

    telemetry._register_gauges(meter, telemetry._state)
    metrics = _collect_metrics(reader)
    points = {
        (dict(p.attributes)["month"], dict(p.attributes)["origin"]): p.value
        for p in metrics[f"{telemetry._PREFIX}assignments_ran_by_month"].data.data_points
    }

    assert points == {
        ("2026-11", "builtin"): 1,
        ("2026-11", "template_default"): 2,
        ("2026-11", "custom"): 3,
    }
    provider.shutdown()


def test_register_gauges_exports_agentic_executions():
    """The agentic-execution gauges must export: the cumulative total as a
    plain gauge and the per-day series with a ``date`` label — the same
    contract as the login / activity by-day series.

    This is the volume-of-agentic-work metric: neither ``assignment_runs_*``
    (automations only) nor ``messages_*`` (conversation turns) can stand in
    for it.
    """
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter(telemetry.TELEMETRY_SERVICE_NAME)

    telemetry._state.agentic_executions_count = 8421
    telemetry._state.agentic_executions_by_day = {"2026-07-01": 118, "2026-07-02": 96}

    telemetry._register_gauges(meter, telemetry._state)
    metrics = _collect_metrics(reader)
    prefix = telemetry._PREFIX

    total_points = list(metrics[f"{prefix}agentic_executions_count"].data.data_points)
    assert total_points[0].value == 8421

    day_points = {
        dict(p.attributes)["date"]: p.value
        for p in metrics[f"{prefix}agentic_executions_by_day"].data.data_points
    }
    assert day_points == {"2026-07-01": 118, "2026-07-02": 96}
    provider.shutdown()


async def test_refresh_agentic_executions_preserves_snapshot_on_error():
    """A failed read must keep the previous values rather than clobbering them
    with zeros — the graceful-degradation contract every refresh step shares.
    """
    telemetry._state.agentic_executions_count = 777
    telemetry._state.agentic_executions_by_day = {"2026-07-01": 5}

    await telemetry._refresh_agentic_executions(_RaisingSession())

    assert telemetry._state.agentic_executions_count == 777
    assert telemetry._state.agentic_executions_by_day == {"2026-07-01": 5}


def test_register_gauges_exports_login_metrics():
    """The login gauges must export: the cumulative total as a plain gauge and
    the per-day series with a ``date`` label — the same contract as the
    sibling products' login telemetry (opencti_user_login_count,
    openaev_count_logins_per_day, opencrq_user_login_count).
    """
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter(telemetry.TELEMETRY_SERVICE_NAME)

    telemetry._state.user_login_count = 347
    telemetry._state.logins_by_day = {"2026-07-01": 12, "2026-07-02": 5}

    telemetry._register_gauges(meter, telemetry._state)
    metrics = _collect_metrics(reader)
    prefix = telemetry._PREFIX

    total_points = list(metrics[f"{prefix}user_login_count"].data.data_points)
    assert total_points[0].value == 347

    day_points = {
        dict(p.attributes)["date"]: p.value
        for p in metrics[f"{prefix}logins_by_day"].data.data_points
    }
    assert day_points == {"2026-07-01": 12, "2026-07-02": 5}
    provider.shutdown()


def test_otlp_exporter_accepts_class_keyed_delta_temporality():
    """The OTLP exporter's ``preferred_temporality`` is keyed by instrument
    *classes* (``Counter``/``Histogram``/``ObservableGauge``/…) imported from
    ``opentelemetry.sdk.metrics`` — confirm both the imports resolve and the
    class-keyed DELTA map is accepted and applied by the pinned SDK.
    """
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.sdk.metrics import (
        Counter,
        Histogram,
        ObservableCounter,
        ObservableGauge,
        ObservableUpDownCounter,
        UpDownCounter,
    )
    from opentelemetry.sdk.metrics.export import AggregationTemporality

    delta = AggregationTemporality.DELTA
    preferred = {
        Counter: delta,
        UpDownCounter: delta,
        Histogram: delta,
        ObservableCounter: delta,
        ObservableUpDownCounter: delta,
        ObservableGauge: delta,
    }
    exporter = OTLPMetricExporter(
        endpoint="https://example.invalid/v1/metrics",
        timeout=15,
        preferred_temporality=preferred,
    )
    applied = exporter._preferred_temporality
    assert applied.get(ObservableGauge) == AggregationTemporality.DELTA
    assert all(v == AggregationTemporality.DELTA for v in applied.values())


def test_dev_mode_pairs_staging_endpoint_with_tight_cadence(monkeypatch):
    """The export cadence and the endpoint must share one dev gate: in
    ``platform_mode == "dev"`` (even with ``debug=False``) telemetry must use the
    staging collector AND the tight export interval — never the staging endpoint
    paired with the production 6h cadence.
    """
    monkeypatch.setattr(telemetry.settings, "telemetry_endpoint", "")

    # platform_mode=dev, debug off — the case the old debug-only interval missed.
    monkeypatch.setattr(telemetry.settings, "debug", False)
    monkeypatch.setattr(telemetry.settings, "platform_mode", "dev")
    assert telemetry._is_dev_mode() is True
    assert telemetry._export_interval_ms() == telemetry._DEV_EXPORT_INTERVAL_MS
    assert telemetry._resolve_endpoint() == telemetry._STAGING_ENDPOINT

    # debug on, mode production — also dev cadence + staging endpoint.
    monkeypatch.setattr(telemetry.settings, "debug", True)
    monkeypatch.setattr(telemetry.settings, "platform_mode", "production")
    assert telemetry._is_dev_mode() is True
    assert telemetry._export_interval_ms() == telemetry._DEV_EXPORT_INTERVAL_MS
    assert telemetry._resolve_endpoint() == telemetry._STAGING_ENDPOINT


def test_production_mode_uses_prod_endpoint_and_six_hour_cadence(monkeypatch):
    monkeypatch.setattr(telemetry.settings, "telemetry_endpoint", "")
    monkeypatch.setattr(telemetry.settings, "debug", False)
    monkeypatch.setattr(telemetry.settings, "platform_mode", "production")
    assert telemetry._is_dev_mode() is False
    assert telemetry._export_interval_ms() == telemetry._SIX_HOURS_MS
    assert telemetry._resolve_endpoint() == telemetry._PROD_ENDPOINT


class _Non200Response:
    status_code = 503


class _Non200Client:
    """httpx.AsyncClient double whose probe POST returns a non-200 response."""

    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, *_args, **_kwargs):
        return _Non200Response()


async def test_probe_logs_status_and_disables_on_non_200(monkeypatch, caplog):
    """A reachable-but-rejecting collector (non-200) must disable telemetry and
    log the status, so operators can tell it apart from an unreachable host.
    """
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _Non200Client)
    with caplog.at_level(logging.INFO, logger=telemetry.logger.name):
        ok = await telemetry._probe_endpoint("https://collector.invalid/v1/metrics")
    assert ok is False
    messages = [r.getMessage() for r in caplog.records]
    assert any("rejected the probe" in m and "503" in m for m in messages)


class _PSOnlySession:
    """Async-session double for `_refresh_edition`: the `PlatformSettings`
    load succeeds (returns no row), so the only failure injected is the
    monkeypatched `ee_platform_status` query.
    """

    async def execute(self, *_args, **_kwargs):
        return _ScalarOneOrNone(None)


class _ScalarOneOrNone:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


async def test_refresh_edition_preserves_snapshot_on_ee_query_failure(monkeypatch):
    """`_refresh_edition` must commit the edition snapshot (is_enterprise_edition
    / edition / agentic_source) atomically: if the EE-platform DB query raises
    mid-refresh, the previous snapshot must be fully preserved, never left
    half-updated (new edition flag paired with a stale source). The caller's
    try/except only "preserves the previous edition snapshot" if no field was
    mutated before the failing read.
    """
    import app.services.xtm_license as xtm_license_module

    # Previous snapshot we expect to survive untouched.
    telemetry._state.is_enterprise_edition = 1
    telemetry._state.edition = "EE"
    telemetry._state.agentic_source = "xtm_license"

    # `is_xtm_one` is a computed property (platform_mode == "xtm_one"), so drive
    # it via platform_mode to exercise the `ee_platform_status` branch.
    monkeypatch.setattr(telemetry.settings, "platform_mode", "xtm_one")

    # License resolves to CE — so a partial update would flip edition to "CE".
    fake_info = SimpleNamespace(license_validated=False)
    monkeypatch.setattr(xtm_license_module, "resolve_xtm_license", lambda _ps: fake_info)

    async def _boom(_db, _info):
        raise RuntimeError("simulated EE-status DB failure")

    monkeypatch.setattr(xtm_license_module, "ee_platform_status", _boom)

    with pytest.raises(RuntimeError):
        await telemetry._refresh_edition(_PSOnlySession())

    # Nothing half-applied: all three edition fields keep their previous values.
    assert telemetry._state.is_enterprise_edition == 1
    assert telemetry._state.edition == "EE"
    assert telemetry._state.agentic_source == "xtm_license"


async def test_refresh_edition_counts_an_ee_platform_as_enterprise(monkeypatch):
    """Enterprise Edition in telemetry is THE licence answer — a connected EE
    platform with no Filigran license is Enterprise, under the included allowance
    (``agentic_source == "ee_platform"``). The retired rule counted only an
    Filigran license carrying the ``agentic_ai`` flag, so a deployment whose whole
    agentic surface was unlocked reported itself as Community."""
    import app.services.xtm_license as xtm_license_module

    monkeypatch.setattr(telemetry.settings, "platform_mode", "xtm_one")
    fake_info = SimpleNamespace(license_validated=False)
    monkeypatch.setattr(xtm_license_module, "resolve_xtm_license", lambda _ps: fake_info)

    async def _one_ee(_db, _info):
        return (True, 1)

    monkeypatch.setattr(xtm_license_module, "ee_platform_status", _one_ee)

    await telemetry._refresh_edition(_PSOnlySession())

    assert telemetry._state.is_enterprise_edition == 1
    assert telemetry._state.edition == "EE"
    assert telemetry._state.agentic_source == "ee_platform"


class _RecordingClient:
    """httpx.AsyncClient double that records the args/kwargs of the probe POST."""

    last_kwargs: dict | None = None

    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, *_args, **kwargs):
        _RecordingClient.last_kwargs = kwargs
        return SimpleNamespace(status_code=200)


async def test_probe_sends_empty_json_object(monkeypatch):
    """The connectivity probe must POST an empty JSON object (``json={}``, which
    httpx serialises with ``Content-Type: application/json``) — exact parity with
    the OpenCTI/OpenAEV startup gates.  A bodiless POST without a content type is
    rejected by the OTLP/HTTP receiver with 415, which previously self-disabled
    telemetry on every install.
    """
    _RecordingClient.last_kwargs = None
    monkeypatch.setattr(telemetry.httpx, "AsyncClient", _RecordingClient)

    ok = await telemetry._probe_endpoint("https://collector.invalid/v1/metrics")

    assert ok is True
    assert _RecordingClient.last_kwargs is not None
    # The probe body is exactly the empty JSON object the sibling products send.
    assert _RecordingClient.last_kwargs.get("json") == {}
    # No competing body kwargs (httpx derives the content type from ``json=``).
    assert "content" not in _RecordingClient.last_kwargs
    assert "data" not in _RecordingClient.last_kwargs
    assert "headers" not in _RecordingClient.last_kwargs


# ---------------------------------------------------------------------------
# Agent portable-format adoption
# ---------------------------------------------------------------------------


class _FormatAdoptionSession:
    """Async-session double for the three format-adoption queries, in order."""

    def __init__(self, export_rows, import_rows, rejected):
        self._results = [
            _AnalyticsResult(rows=export_rows),
            _AnalyticsResult(rows=import_rows),
            _ScalarResult(rejected),
        ]

    async def execute(self, *_args, **_kwargs):
        return self._results.pop(0)


async def test_refresh_agent_format_adoption_commits_snapshot():
    """Exports and successful imports are bucketed by format; rejections counted."""
    await telemetry._refresh_agent_format_adoption(
        _FormatAdoptionSession(
            export_rows=[
                SimpleNamespace(format="native", count=42),
                SimpleNamespace(format="agentschema", count=7),
            ],
            import_rows=[SimpleNamespace(format="agentschema", count=3)],
            rejected=5,
        )
    )

    assert telemetry._state.agent_exports_by_format == {"native": 42, "agentschema": 7}
    assert telemetry._state.agent_imports_by_format == {"agentschema": 3}
    assert telemetry._state.agent_imports_rejected_count == 5


async def test_refresh_agent_format_adoption_preserves_snapshot_on_failure():
    """A failed audit-log read preserves the previous snapshot, as everywhere else."""
    telemetry._state.agent_exports_by_format = {"native": 11}
    telemetry._state.agent_imports_by_format = {"native": 4}
    telemetry._state.agent_imports_rejected_count = 2

    await telemetry._refresh_agent_format_adoption(_RaisingSession())

    assert telemetry._state.agent_exports_by_format == {"native": 11}
    assert telemetry._state.agent_imports_by_format == {"native": 4}
    assert telemetry._state.agent_imports_rejected_count == 2


def test_register_gauges_exports_agent_format_adoption():
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter(telemetry.TELEMETRY_SERVICE_NAME)

    telemetry._state.agent_exports_by_format = {"native": 42, "agentschema": 7}
    telemetry._state.agent_imports_by_format = {"agentschema": 3}
    telemetry._state.agent_imports_rejected_count = 5

    telemetry._register_gauges(meter, telemetry._state)

    metrics = _collect_metrics(reader)
    prefix = telemetry._PREFIX
    exports = {
        point.attributes["format"]: point.value
        for point in metrics[f"{prefix}agent_exports_by_format"].data.data_points
    }
    assert exports == {"native": 42, "agentschema": 7}
    imports = {
        point.attributes["format"]: point.value
        for point in metrics[f"{prefix}agent_imports_by_format"].data.data_points
    }
    assert imports == {"agentschema": 3}
    rejected = list(metrics[f"{prefix}agent_imports_rejected_count"].data.data_points)
    assert [point.value for point in rejected] == [5]
    provider.shutdown()


# -- One export per install --------------------------------------------


class _CapturingExporter:
    """A MetricExporter recording every export (what the collector would get).

    ``outcome``: ``"success"``, ``"failure"`` (the collector refused it) or
    ``"raise"`` (the request itself failed).
    """

    def __new__(cls, outcome: str = "success"):
        from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult

        class Capture(MetricExporter):
            def __init__(self):
                super().__init__()
                self.batches = []

            def export(self, metrics_data, timeout_millis=10_000, **kwargs):
                self.batches.append(metrics_data)
                if outcome == "raise":
                    raise ConnectionError("collector unreachable")
                if outcome == "failure":
                    return MetricExportResult.FAILURE
                return MetricExportResult.SUCCESS

            def force_flush(self, timeout_millis=10_000):
                return True

            def shutdown(self, timeout_millis=30_000, **kwargs):
                return None

        return Capture()


def _process(exporter):
    """One platform process's pipeline, wired like ``init_telemetry`` wires it."""
    from opentelemetry.sdk.metrics import MeterProvider

    recorder = telemetry._recording_exporter(exporter)
    provider = MeterProvider(metric_readers=[telemetry._new_reader(recorder)])
    provider.recorder = recorder
    telemetry._register_gauges(provider.get_meter(telemetry.TELEMETRY_SERVICE_NAME), telemetry._state)
    return provider


def test_a_process_never_exports_on_its_own():
    # No timer and no final collect at shutdown: a worker recycle or a
    # rollout used to add one export per process.
    exporter = _CapturingExporter()
    provider = _process(exporter)
    reader = provider._sdk_config.metric_readers[0]
    assert reader._daemon_thread is None
    provider.shutdown()
    assert exporter.batches == []


def test_a_flush_exports_the_whole_snapshot_once():
    exporter = _CapturingExporter()
    provider = _process(exporter)
    telemetry._state.users_count = 11
    provider.force_flush()
    assert len(exporter.batches) == 1
    names = {m.name for rm in exporter.batches[0].resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
    assert f"{telemetry._PREFIX}users_count" in names
    provider.shutdown()


class _LeaseRedis:
    """The window-lease subset of Redis: SET NX EX, and expiry on demand."""

    def __init__(self) -> None:
        self.keys: dict[str, int] = {}

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = ex
        return True

    def expire_all(self) -> None:
        self.keys.clear()


@pytest.fixture
def install(monkeypatch):
    """N processes of one install sharing one Redis; returns (redis, add_process)."""
    from unittest.mock import AsyncMock

    redis = _LeaseRedis()
    monkeypatch.setattr("app.core.redis.redis_client", AsyncMock(return_value=redis))
    refresh = AsyncMock()
    monkeypatch.setattr(telemetry, "refresh_telemetry_data", refresh)
    processes = []

    def add_process(outcome="success"):
        exporter = _CapturingExporter(outcome)
        provider = _process(exporter)
        processes.append(provider)
        return exporter, provider

    yield redis, add_process, refresh
    for provider in processes:
        provider.shutdown()


async def _tick(monkeypatch, provider):
    """The scheduler tick's telemetry step, run in the process owning ``provider``."""
    monkeypatch.setattr(telemetry, "_provider", provider)
    monkeypatch.setattr(telemetry, "_recorder", provider.recorder)
    monkeypatch.setattr(telemetry, "_initialized", True)
    return await telemetry.export_telemetry_if_due(object())


async def test_one_process_per_install_exports_per_interval(monkeypatch, install):
    redis, add_process, refresh = install
    workers = [add_process() for _ in range(4)]  # 2 workers x 2 replicas
    exported = []
    for _minute in range(3):  # every tick of the interval, in any process
        for _exporter, provider in workers:
            exported.append(await _tick(monkeypatch, provider))
    assert exported.count(True) == 1
    assert sum(len(exporter.batches) for exporter, _ in workers) == 1
    # The lease lasts the export interval, and the snapshot was read from the
    # database right before the export.
    assert redis.keys == {telemetry._EXPORT_LEASE_KEY: telemetry._export_interval_ms() // 1000}
    refresh.assert_awaited_once()


async def test_the_next_interval_is_exported_by_any_process(monkeypatch, install):
    # The lease is a window: when it expires (the holder may have died), the
    # first process to ask exports the next interval.
    redis, add_process, _refresh = install
    (first, first_provider), (second, second_provider) = add_process(), add_process()
    assert await _tick(monkeypatch, first_provider) is True
    redis.expire_all()
    assert await _tick(monkeypatch, second_provider) is True
    assert len(first.batches) == 1
    assert len(second.batches) == 1


async def test_a_single_process_install_still_exports(monkeypatch, install):
    _redis, add_process, _refresh = install
    exporter, provider = add_process()
    assert await _tick(monkeypatch, provider) is True
    assert len(exporter.batches) == 1


async def test_a_redis_outage_skips_the_export_and_never_raises(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr("app.core.redis.redis_client", AsyncMock(side_effect=ConnectionError("redis down")))
    refresh = AsyncMock()
    monkeypatch.setattr(telemetry, "refresh_telemetry_data", refresh)
    exporter = _CapturingExporter()
    provider = _process(exporter)
    try:
        assert await _tick(monkeypatch, provider) is False
    finally:
        provider.shutdown()
    assert exporter.batches == []
    refresh.assert_not_awaited()


async def test_a_process_without_telemetry_never_takes_the_lease(monkeypatch, install):
    # A process whose collector probe failed must not consume the interval's
    # lease, or the install would skip that export.
    redis, _add_process, _refresh = install
    monkeypatch.setattr(telemetry, "_provider", None)
    monkeypatch.setattr(telemetry, "_initialized", False)
    assert await telemetry.export_telemetry_if_due(object()) is False
    assert redis.keys == {}


@pytest.mark.parametrize("outcome", ["failure", "raise"])
async def test_an_export_the_collector_did_not_take_is_reported_as_failed(monkeypatch, install, outcome):
    # force_flush says True either way; the exporter's own result decides.
    redis, add_process, _refresh = install
    exporter, provider = add_process(outcome)
    assert await _tick(monkeypatch, provider) is False
    assert len(exporter.batches) == 1  # it was attempted
    # The lease stays held: the next attempt is the next interval, not every
    # tick against a collector that is down.
    assert telemetry._EXPORT_LEASE_KEY in redis.keys
    assert await _tick(monkeypatch, provider) is False
    assert len(exporter.batches) == 1


async def test_a_failed_refresh_does_not_raise(monkeypatch, install):
    _redis, add_process, refresh = install
    refresh.side_effect = RuntimeError("db down")
    exporter, provider = add_process()
    assert await _tick(monkeypatch, provider) is False
    assert exporter.batches == []


# ── The admin's switch and the docs page ─────────────────────────────


async def test_telemetry_enabled_false_makes_no_connection(monkeypatch, install, caplog):
    # TELEMETRY_ENABLED=false: no startup probe, no pipeline, and the
    # scheduler tick never takes the export lease. The test-runner and CI
    # guards are lifted for the call, so only the switch stands between
    # init_telemetry and the probe.
    redis, _add_process, refresh = install

    async def _no_probe(_endpoint):
        raise AssertionError("the collector must not be contacted")

    monkeypatch.setattr(telemetry.settings, "telemetry_enabled", False)
    monkeypatch.setattr(telemetry, "_probe_endpoint", _no_probe)
    monkeypatch.setattr(telemetry, "_provider", None)
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_is_ci_environment", lambda: False)
    with caplog.at_level(logging.INFO, logger=telemetry.logger.name), monkeypatch.context() as no_runner:
        no_runner.delitem(sys.modules, "pytest")
        await telemetry.init_telemetry(object())

    assert telemetry.is_telemetry_active() is False
    assert any("TELEMETRY_ENABLED=false" in r.getMessage() for r in caplog.records)
    assert await telemetry.export_telemetry_if_due(object()) is False
    assert redis.keys == {}
    refresh.assert_not_awaited()


def test_telemetry_is_on_by_default():
    from app.core.config import Settings

    assert Settings.model_fields["telemetry_enabled"].default is True


def _registered_gauge_names() -> set[str]:
    class _NameMeter:
        def __init__(self) -> None:
            self.names: set[str] = set()

        def create_observable_gauge(self, name, **_kwargs):
            self.names.add(name)

    meter = _NameMeter()
    telemetry._register_gauges(meter, telemetry._TelemetryState())
    return meter.names


def test_the_docs_page_lists_every_gauge_and_nothing_else():
    # docs/docs/deployment/telemetry.md is what an admin reads before deciding
    # to keep telemetry on: a gauge added or removed here must be reflected
    # there in the same change.
    import re
    from pathlib import Path

    page = Path(__file__).resolve().parents[2] / "docs" / "docs" / "deployment" / "telemetry.md"
    documented = set(re.findall(r"`(" + telemetry._PREFIX + r"\w+)`", page.read_text(encoding="utf-8")))
    registered = _registered_gauge_names()

    assert registered - documented == set(), "gauges missing from the docs page"
    assert documented - registered == set(), "the docs page lists gauges that do not exist"
