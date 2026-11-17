"""The admin app's provider webhook says when it hands out deliveries it cannot verify.

``POST /api/hooks/providers/{provider}`` verifies a delivery with the webhook secret of the
provider's OAuth default (LinkedIn: its client secret). Without one it accepts the delivery as
before signatures were checked, so anyone who can reach the URL can trigger the assignments it
reaches. Nothing here changes what is delivered: the delivery logs a rate-limited warning once
it triggered assignments, and the Security Audit names the providers, why they cannot verify
and how many assignments they reach.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
import time
import uuid
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.api.v1 import security_audit, webhooks
from app.api.v1.webhooks import (
    _AssignmentMatcher,
    _ProviderRouting,
    _verify_provider_signatures,
    provider_webhook_unverified_reason,
)
from app.core import log_once
from app.core.config import settings
from app.core.security import encrypt_value
from app.services import integration_notification_auth
from app.services.integration_notification_auth import (
    NO_CLIENT_SECRET,
    NO_OAUTH_DEFAULT,
    NO_WEBHOOK_SECRET,
    warn_unverified_provider_delivery,
)
from tests.notice_store import new_process, share_notices

CONTROL = "integrations.notifications_require_verification"
AUTH_LOGGER = integration_notification_auth.__name__
UNVERIFIED = "a delivery the platform cannot verify"


@pytest.fixture(autouse=True)
def _default_mode(monkeypatch):
    integration_notification_auth._log_state.clear()
    webhooks._invalidate_provider_routing_cache()
    monkeypatch.setattr(settings, "integration_notifications_require_verification", False)
    yield
    integration_notification_auth._log_state.clear()
    webhooks._invalidate_provider_routing_cache()


def _default(provider="github", *, config=None, client_secret=None, raw_config=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider=provider,
        client_id_enc=None,
        extra_config_enc=raw_config
        if raw_config is not None
        else (encrypt_value(json.dumps(config)) if config is not None else None),
        client_secret_enc=encrypt_value(client_secret) if client_secret else None,
    )


def _request(body: bytes, headers: dict | None = None):
    async def _body() -> bytes:
        return body

    return SimpleNamespace(
        headers={"X-GitHub-Event": "issues", **(headers or {})},
        body=_body,
        url="https://copilot.example/api/hooks/providers/github",
        method="POST",
        query_params={},
        client=SimpleNamespace(host="203.0.113.7"),
    )


# ---------------------------------------------------------------------------
# Why a delivery cannot be verified: the reading of the signature gate
# ---------------------------------------------------------------------------


def test_without_an_oauth_default_nothing_can_be_verified():
    assert provider_webhook_unverified_reason("github", None) == NO_OAUTH_DEFAULT


@pytest.mark.parametrize("config", [None, {}, {"webhook_secret": ""}, {"webhook_secret": None}])
def test_an_oauth_default_without_a_webhook_secret_verifies_nothing(config):
    assert provider_webhook_unverified_reason("github", _default(config=config)) == NO_WEBHOOK_SECRET


@pytest.mark.parametrize("secret", ["s3cret", 1234])
def test_a_webhook_secret_verifies_every_delivery(secret):
    assert provider_webhook_unverified_reason("github", _default(config={"webhook_secret": secret})) is None


@pytest.mark.parametrize(
    "default",
    [
        _default(config={"webhook_secret": ["not", "a", "secret"]}),
        _default(raw_config="not-a-fernet-token"),
    ],
    ids=["unusable-secret", "unreadable-config"],
)
async def test_a_configuration_the_gate_refuses_accepts_nothing_unverified(default):
    # The gate refuses every delivery then, so none is accepted unverified.
    assert provider_webhook_unverified_reason("github", default) is None
    with pytest.raises(HTTPException) as refused:
        await _verify_provider_signatures(
            b"{}", "github", default.extra_config_enc, default.client_secret_enc, _request(b"{}")
        )
    assert refused.value.status_code in (401, 503)


def test_linkedin_is_verified_by_its_client_secret():
    assert provider_webhook_unverified_reason("linkedin", _default("linkedin", client_secret="cs")) is None
    assert provider_webhook_unverified_reason("linkedin", _default("linkedin")) == NO_CLIENT_SECRET
    signed = _default("linkedin", config={"webhook_secret": "s3cret"})
    assert provider_webhook_unverified_reason("linkedin", signed) is None


async def test_an_oauth_default_without_a_secret_really_accepts_an_unsigned_delivery():
    # The reading is the gate's: what it calls unverified, the gate lets through.
    default = _default(config={})
    await _verify_provider_signatures(b"{}", "github", default.extra_config_enc, None, _request(b"{}"))
    signed = _default(config={"webhook_secret": "s3cret"})
    with pytest.raises(HTTPException):
        await _verify_provider_signatures(b"{}", "github", signed.extra_config_enc, None, _request(b"{}"))


# ---------------------------------------------------------------------------
# The Security Audit
# ---------------------------------------------------------------------------


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _AuditDb:
    """The route's count per provider, the rows SQL cannot parse, then the OAuth defaults it names."""

    def __init__(self, counted, defaults=(), unparsed=()):
        self._results = [_Rows(counted), _Rows(unparsed), _Rows(defaults)]
        self.statements = []

    async def execute(self, statement, *_args, **_kwargs):
        self.statements.append(statement)
        return self._results.pop(0)


async def _audit(counted, defaults=(), *, strict=False, unparsed=()):
    db = _AuditDb(list(counted.items()) if isinstance(counted, dict) else counted, defaults, unparsed)
    findings: list = []
    await security_audit._check_provider_webhook_verification(db, findings, strict=strict)
    return findings, db


def _unparsed(trigger_type, config, bindings):
    """A row as the stored-text read returns it (``json.dumps`` writes a lone surrogate as its escape)."""
    return SimpleNamespace(
        trigger_type=trigger_type,
        config_text=config if isinstance(config, str) else json.dumps(config),
        bindings_text=json.dumps(bindings),
    )


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.mark.asyncio
async def test_the_audit_names_every_provider_that_hands_out_unverified_deliveries():
    findings, _db = await _audit(
        {"github": 2, "microsoft": 1, "notion": 1, "jira": 1},
        [
            _default("github", config={}),
            _default("microsoft", config={}),
            _default("notion", config={"webhook_secret": "s3cret"}),
        ],
    )
    [finding] = findings
    assert finding.id == "integrations.provider_webhook_unverified"
    assert finding.category == "integrations"
    assert finding.severity == "warning"
    assert finding.control == CONTROL
    assert "4 active assignment(s)" in finding.description
    assert (
        "github: 2 assignment(s), no webhook secret on its OAuth default; "
        "jira: 1 assignment(s), no OAuth default; "
        "microsoft: 1 assignment(s), no webhook secret on its OAuth default"
    ) in finding.description
    assert "notion" not in finding.description
    assert "/api/hooks/providers/<provider>" in finding.description
    # What each provider can do: sign (its OAuth default holds a webhook secret), be blocked
    # (it cannot sign, and nothing sends its events here), or reach nothing under the option.
    assert "For github, set a webhook secret" in finding.recommendation
    assert "microsoft cannot sign a delivery to this URL" in finding.recommendation
    assert "block /api/hooks/providers/microsoft at your reverse proxy" in finding.recommendation
    assert "No admin app of jira" in finding.recommendation
    assert "INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION=true" in finding.recommendation


@pytest.mark.asyncio
async def test_the_rows_sql_cannot_parse_are_counted_from_their_text_as_the_router_reads_them():
    surrogate = "\ud800"
    findings, _db = await _audit(
        [("github", 1), (None, 4)],
        unparsed=[
            _unparsed("integration_event", {"provider": "github", "note": surrogate}, {"github": []}),
            _unparsed("webhook", {"path": "github", "note": surrogate}, ["github"]),
            # A webhook is routed by its path, an integration event by its provider.
            _unparsed("webhook", {"provider": "github", "note": surrogate}, {"github": []}),
            _unparsed("integration_event", {"filter": {"provider": "github"}, "note": surrogate}, {"github": []}),
            # Not bound to the provider it names, or no provider at all.
            _unparsed("integration_event", {"provider": "jira", "note": surrogate}, {"github": []}),
            _unparsed("integration_event", {"provider": "", "note": surrogate}, {"": []}),
            _unparsed("integration_event", "not json", {"github": []}),
            _unparsed("integration_event", [surrogate], {"github": []}),
        ],
    )
    [finding] = findings
    assert finding.description.startswith("Up to 3 active assignment(s)")
    assert "github: 3 assignment(s), no OAuth default" in finding.description
    assert "jira" not in finding.description


@pytest.mark.parametrize(
    ("bindings", "bound"),
    [
        ({"github": []}, True),
        (["github", "gmail"], True),
        ("github", True),
        ({"gmail": ["github"]}, False),
        ([["github"]], False),
        (None, False),
    ],
)
def test_the_binding_test_on_stored_text_is_postgresqls_key_test(bindings, bound):
    from app.core.json_sql import jsonb_has_key

    assert jsonb_has_key(bindings, "github") is bound


@pytest.mark.asyncio
async def test_under_the_option_a_provider_without_an_admin_app_reaches_nothing():
    findings, _db = await _audit(
        {"github": 1, "gmail": 1}, [_default("gmail", config={})], strict=True
    )
    [finding] = findings
    assert finding.id == "integrations.provider_webhook_unverified"
    assert "1 active assignment(s)" in finding.description
    assert "github" not in finding.description
    assert "Under the verification option" in finding.description
    assert "INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION" not in finding.recommendation


@pytest.mark.asyncio
async def test_under_the_option_no_admin_app_at_all_is_a_pass():
    findings, _db = await _audit({"github": 1}, [], strict=True)
    [finding] = findings
    assert finding.id == "integrations.provider_webhook_verified"
    assert finding.severity == "pass"
    assert finding.control == CONTROL
    assert "without an OAuth default it reaches no assignment" in finding.description


@pytest.mark.asyncio
async def test_signed_providers_are_a_pass():
    findings, _db = await _audit(
        {"github": 1, "linkedin": 1},
        [_default(config={"webhook_secret": "s3cret"}), _default("linkedin", client_secret="cs")],
    )
    [finding] = findings
    assert finding.id == "integrations.provider_webhook_verified"
    assert finding.severity == "pass"
    assert "github, linkedin" in finding.description


@pytest.mark.asyncio
async def test_linkedin_without_a_client_secret_is_told_to_configure_it():
    findings, _db = await _audit({"linkedin": 1}, [_default("linkedin")])
    [finding] = findings
    assert "linkedin: 1 assignment(s), no client secret on its OAuth default" in finding.description
    assert "For linkedin, configure the app's client secret" in finding.recommendation


@pytest.mark.asyncio
async def test_nothing_the_route_reaches_reports_nothing_and_reads_no_default():
    findings, db = await _audit({})
    assert findings == []
    assert len(db.statements) == 2


@pytest.mark.asyncio
async def test_the_audit_counts_what_the_route_can_run_in_one_grouped_query():
    _findings, db = await _audit({})
    sql = _sql(db.statements[0])
    assert "count(assignments.id)" in sql
    assert "JOIN agents ON agents.id = assignments.agent_id" in sql
    assert "assignments.enabled IS true" in sql
    assert "agents.enabled IS true" in sql
    assert "assignments.trigger_type IN ('integration_event', 'webhook')" in sql
    assert "users.is_active IS false" in sql
    # The router's own test of the binding, on the provider the trigger names.
    assert "CAST(agents.integration_bindings AS JSONB) ?" in sql
    assert "->> 'path'" in sql and "->> 'provider'" in sql
    assert sql.rstrip().endswith("GROUP BY route_provider")


@pytest.mark.asyncio
async def test_a_row_postgresql_cannot_parse_is_skipped_before_it_is_read():
    _findings, db = await _audit({})
    sql = _sql(db.statements[0])
    where = sql[sql.index("WHERE") :]
    surrogate = "[dD][89a-fA-F]"
    # The trigger config first, then the bindings, each before any JSON operator reads it.
    assert where.index("CAST(assignments.trigger_config AS TEXT)") < where.index(surrogate)
    assert where.index(surrogate) < where.index("CAST(agents.integration_bindings AS TEXT)")
    assert where.index("CAST(agents.integration_bindings AS TEXT)") < where.index("AS JSONB)")
    assert where.index("CAST(assignments.trigger_config AS TEXT)") < where.index("->>")


@pytest.mark.asyncio
async def test_the_rows_postgresql_cannot_parse_are_read_as_text_under_the_counts_predicates():
    _findings, db = await _audit({})
    sql = _sql(db.statements[1])
    assert "CAST(assignments.trigger_config AS TEXT)" in sql
    assert "CAST(agents.integration_bindings AS TEXT)" in sql
    # No JSON operator: PostgreSQL refuses to parse these rows.
    assert "->" not in sql and "JSONB" not in sql
    assert "JOIN agents ON agents.id = assignments.agent_id" in sql
    assert "assignments.enabled IS true" in sql and "agents.enabled IS true" in sql
    assert "assignments.trigger_type IN ('integration_event', 'webhook')" in sql
    assert "users.is_active IS false" in sql
    # The exact test of core/json_sql.py, once per column: config unreadable, bindings readable.
    assert sql.count("regexp_replace(") == 2
    assert "AND (CAST(assignments.trigger_config AS TEXT) ~ " in sql
    assert "AND NOT (CAST(agents.integration_bindings AS TEXT) ~ " in sql
    # ...and the out-of-range number arm, once per column.
    assert sql.count("[eE][+-]?[0-9]{5}") == 2


def test_the_check_is_part_of_the_integration_notifications_audit():
    source = inspect.getsource(security_audit._check_integration_notifications)
    assert "_check_provider_webhook_verification(db, findings, strict=strict)" in source


# ---------------------------------------------------------------------------
# The delivery says it
# ---------------------------------------------------------------------------


def test_the_warning_names_the_reason_and_the_remedy_once_per_window(caplog):
    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        warn_unverified_provider_delivery("github", NO_OAUTH_DEFAULT, 2)
        warn_unverified_provider_delivery("github", NO_OAUTH_DEFAULT, 5)
        warn_unverified_provider_delivery("gmail", NO_WEBHOOK_SECRET, 1)
        warn_unverified_provider_delivery("notion", NO_WEBHOOK_SECRET, 0)
    lines = [r.getMessage() for r in caplog.records if UNVERIFIED in r.getMessage()]
    assert len(lines) == 2
    github, gmail = lines
    assert "Provider webhook github" in github
    assert "triggered 2 assignment(s)" in github
    assert "/api/hooks/providers/github" in github
    assert "INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION=true" in github
    assert "The Security Audit (Settings > Access and security) says how to verify or block them." in gmail
    assert "notion" not in caplog.text


async def test_the_warning_is_said_once_per_window_for_the_whole_platform(monkeypatch, caplog):
    """Every uvicorn worker, recycle and replica receives deliveries: 3 lines in 90 s.

    The next line counts the deliveries every process held back since.
    """
    notices = share_notices(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        for _ in range(3):
            new_process()
            warn_unverified_provider_delivery("github", NO_WEBHOOK_SECRET, 1)
            await log_once.settle()
        notices.interval_elapsed()
        new_process()
        warn_unverified_provider_delivery("github", NO_WEBHOOK_SECRET, 1)
        await log_once.settle()
    lines = [r.getMessage() for r in caplog.records if UNVERIFIED in r.getMessage()]
    assert len(lines) == 2
    assert lines[1].endswith("(2 more since the last one)")
    assert {notices.ttls[claim] for claim in notices.claims()} == {900}


async def test_the_warning_is_said_at_once_when_the_reason_changes(monkeypatch, caplog):
    """An OAuth default stored without its webhook secret leaves another gap, said without waiting the window."""
    share_notices(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        for reason in (NO_OAUTH_DEFAULT, NO_OAUTH_DEFAULT, NO_WEBHOOK_SECRET):
            new_process()
            warn_unverified_provider_delivery("github", reason, 1)
            await log_once.settle()
    lines = [r.getMessage() for r in caplog.records if UNVERIFIED in r.getMessage()]
    assert len(lines) == 2
    assert f"({NO_WEBHOOK_SECRET})" in lines[1]


class _Result:
    def __init__(self, *, scalar_one=None, scalars_all=None):
        self._scalar_one = scalar_one
        self._scalars_all = scalars_all or []

    def scalar_one_or_none(self):
        return self._scalar_one

    def all(self):
        return []

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._scalars_all))


class _Session:
    def __init__(self, results):
        self._results = list(results)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, _stmt):
        return self._results.pop(0)

    async def commit(self):
        return None

    def begin_nested(self):
        class _Savepoint:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *_exc):
                return False

        return _Savepoint()


async def _deliver(oauth_default, *, headers=None, trigger_result="run-1", trigger=None, on_admin_app=False):
    """One ``issues`` delivery to an agent with one matching assignment.

    *on_admin_app*: the assignment's integration runs on the admin app its owner may use, so
    the verification option reaches it whenever there is an admin app (the rule itself is
    covered by the app-scope suites).
    """
    owner, agent_id, wf_id, integ = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Repo Agent",
        enabled=True,
        user_id=owner,
        integration_bindings={"github": [str(integ)]},
    )
    wf = SimpleNamespace(
        id=wf_id,
        agent_id=agent_id,
        name="On issue",
        enabled=True,
        trigger_type="integration_event",
        trigger_config={"provider": "github", "event": "issues"},
    )
    webhooks._provider_routing_cache["github"] = (
        time.monotonic(),
        _ProviderRouting(
            oauth_default_exists=oauth_default is not None,
            extra_config_enc=getattr(oauth_default, "extra_config_enc", None),
            client_secret_enc=getattr(oauth_default, "client_secret_enc", None),
            has_agents=True,
            matchers=(
                _AssignmentMatcher(
                    agent_id=str(agent_id),
                    assignment_id=str(wf_id),
                    trigger_type="integration_event",
                    provider="github",
                    event="issues",
                    path="",
                    events_filter=(),
                    integrations=((str(integ), None),),
                ),
            ),
        ),
    )
    session = _Session(
        [
            _Result(scalar_one=oauth_default),
            _Result(scalars_all=[agent]),
            _Result(scalars_all=[wf]),
        ]
    )
    body = json.dumps({"action": "opened"}).encode()
    trigger = trigger or AsyncMock(return_value=trigger_result)

    async def _on_admin_app(_db, _provider, oauth_default, pairs):
        if oauth_default is None:
            return {}
        return {str(assignment.id): ((str(integ), None),) for assignment, _agent in pairs}

    # The seeded snapshot is the one every delivery reads, however long a loaded worker takes
    # between two deliveries: the TTL and the signature-retry age are pinned, not wall time.
    with (
        patch.object(webhooks, "_PROVIDER_ROUTING_TTL_SECONDS", float("inf")),
        patch.object(webhooks, "_PROVIDER_ROUTING_SIG_RETRY_MIN_AGE_SECONDS", float("inf")),
        patch.object(webhooks, "_claim_webhook_dedup_slot", AsyncMock(return_value=(False, None, None))),
        patch.object(webhooks, "_finalize_dedup", AsyncMock(side_effect=lambda _k, r: r)),
        patch.object(webhooks, "_release_webhook_dedup_slot", AsyncMock()),
        patch.object(webhooks, "queue_busy_pushed_event", AsyncMock(return_value=None)),
        patch.object(webhooks, "async_session", new=lambda: session),
        patch.object(webhooks, "trigger_assignment", new=trigger),
        patch.object(
            webhooks,
            "_admin_app_integrations_or_report",
            new=_on_admin_app if on_admin_app else webhooks._admin_app_integrations_or_report,
        ),
    ):
        response = await webhooks.receive_provider_webhook(
            "github", _request(body, headers=headers(body) if headers else None)
        )
    return response, trigger


def _signed(secret: str):
    def headers(body: bytes) -> dict:
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return {"X-Hub-Signature-256": f"sha256={digest}"}

    return headers


@pytest.mark.asyncio
async def test_by_default_a_delivery_without_an_admin_app_still_runs_and_says_so(caplog):
    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        response, trigger = await _deliver(None)
    assert response["count"] == 1
    trigger.assert_awaited_once()
    assert f"Provider webhook github: {UNVERIFIED} (no OAuth default) triggered 1" in caplog.text


@pytest.mark.asyncio
async def test_an_unsigned_delivery_to_an_admin_app_without_a_secret_still_runs_and_says_so(caplog):
    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        response, trigger = await _deliver(_default(config={}))
    assert response["count"] == 1
    trigger.assert_awaited_once()
    assert "(no webhook secret on its OAuth default) triggered 1" in caplog.text


@pytest.mark.asyncio
async def test_a_signed_delivery_is_not_reported(caplog):
    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        response, trigger = await _deliver(
            _default(config={"webhook_secret": "s3cret"}), headers=_signed("s3cret")
        )
    assert response["count"] == 1
    trigger.assert_awaited_once()
    assert UNVERIFIED not in caplog.text


@pytest.mark.asyncio
async def test_a_delivery_that_triggers_nothing_is_not_reported(caplog):
    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        response, _trigger = await _deliver(None, trigger_result=None)
    assert response["count"] == 0
    assert UNVERIFIED not in caplog.text


@pytest.mark.asyncio
async def test_under_the_option_a_delivery_without_an_admin_app_reaches_nothing(monkeypatch, caplog):
    monkeypatch.setattr(settings, "integration_notifications_require_verification", True)
    with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER):
        response, trigger = await _deliver(None)
    assert response["count"] == 0
    trigger.assert_not_awaited()
    assert UNVERIFIED not in caplog.text


# ---------------------------------------------------------------------------
# Report only: every delivery gets the answer and the runs it got before
# ---------------------------------------------------------------------------


async def _outcome(oauth_default, headers, *, report: bool, caplog):
    """What the route answers, which assignments it runs, and whether it warned.

    ``report=False`` is the route without reporting: whether a delivery can be verified
    is never read, so nothing is logged. The route returns a plain dict (FastAPI's 200 JSON,
    with no header of its own) or raises an ``HTTPException``: comparing those compares the
    status, the body and the headers of the response.
    """
    integration_notification_auth._log_state.clear()
    caplog.clear()
    trigger = AsyncMock(return_value="run-1")
    unreported = patch.object(webhooks, "provider_webhook_unverified_reason", lambda _provider, _default: None)
    try:
        with caplog.at_level(logging.WARNING, logger=AUTH_LOGGER), (nullcontext() if report else unreported):
            response = await _deliver(oauth_default, headers=headers, trigger=trigger, on_admin_app=True)
        answer = (200, response[0], None)
    except HTTPException as refused:
        answer = (refused.status_code, refused.detail, refused.headers)
    ran = [
        (call.kwargs["assignment"].name, call.kwargs["agent"].name, call.kwargs["payload"])
        for call in trigger.await_args_list
    ]
    return answer, ran, UNVERIFIED in caplog.text


_DELIVERIES = {
    "signed": (lambda: _default(config={"webhook_secret": "s3cret"}), _signed("s3cret")),
    "wrongly-signed": (lambda: _default(config={"webhook_secret": "s3cret"}), _signed("another")),
    "unsigned-no-secret": (lambda: _default(config={}), None),
    "no-oauth-default": (lambda: None, None),
}

# (status, runs, warned) with the report on, per option state; with it off nothing is warned.
_EXPECTED = {
    (False, "signed"): (200, 1, False),
    (False, "wrongly-signed"): (401, 0, False),
    (False, "unsigned-no-secret"): (200, 1, True),
    (False, "no-oauth-default"): (200, 1, True),
    (True, "signed"): (200, 1, False),
    (True, "wrongly-signed"): (401, 0, False),
    (True, "unsigned-no-secret"): (200, 1, True),
    (True, "no-oauth-default"): (200, 0, False),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True], ids=["option-off", "option-on"])
@pytest.mark.parametrize("delivery", list(_DELIVERIES))
async def test_the_report_changes_no_answer_and_no_run(delivery, strict, monkeypatch, caplog):
    monkeypatch.setattr(settings, "integration_notifications_require_verification", strict)
    make_default, headers = _DELIVERIES[delivery]

    before = await _outcome(make_default(), headers, report=False, caplog=caplog)
    after = await _outcome(make_default(), headers, report=True, caplog=caplog)

    answer, ran, warned = after
    assert answer == before[0]
    assert ran == before[1]
    assert before[2] is False
    status, runs, expect_warned = _EXPECTED[(strict, delivery)]
    assert answer[0] == status
    assert len(ran) == runs
    if status == 200:
        assert (answer[1]["status"], answer[1]["count"]) == ("ok", runs)
    assert warned is expect_warned
