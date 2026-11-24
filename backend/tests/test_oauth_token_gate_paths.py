"""Regression coverage for every reader of a personal OAuth token.

Each path is driven through the same five cases, seen from the acting user
(``COLLEAGUE_ID``): their own OAuth integration, a share its owner
acknowledged and a non-OAuth one keep working; a colleague's company-managed
or group-shared OAuth integration whose owner never shared the token is
refused where a person chooses it (REST, bind time, per-user resolution).
A flow Integration node runs as the run's user, so it uses its integration
only when that person may. A credential a skill source or a KB sync source is
bound to is used by its runs as bound: nothing is withheld at run time.

Three paths (event poller, Graph webhook, chat session override) were gated
but never tested; the others had no check at all.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import true

from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.integration import Integration
from app.models.knowledge_base import KBSyncSource
from app.services.integration_credentials import OAUTH_CREDENTIALS_NOT_SHARED_DETAIL
from app.tools.base import CallableToolExecutor
from tests.integration_credential_fakes import (
    COLLEAGUE_ID,
    OWNER_ID,
    SHARING_CASES,
    FakeSession,
    make_integration,
)

CASES = [pytest.param(case, usable, id=case) for case, (_, usable) in SHARING_CASES.items()]
REFUSED = [c for c in CASES if not c.values[1]]


def _case_integration(case: str, **extra) -> Integration:
    return make_integration(**{**SHARING_CASES[case][0], **extra})


def _user(user_id=COLLEAGUE_ID, *, is_admin: bool = False):
    # A member, not an admin: the gate leaves admins their tooling
    # (pinned by the "admin still" tests below).
    return SimpleNamespace(id=user_id, is_admin=is_admin, role="admin" if is_admin else "user")


def _refused(exc_info) -> None:
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == OAUTH_CREDENTIALS_NOT_SHARED_DETAIL


# ---------------------------------------------------------------------------
# Path 1 - scheduled integration_event poll (acts as the agent's owner)
# ---------------------------------------------------------------------------


def _poller_fixture(monkeypatch, integration: Integration, *, agent_owner):
    from app.services import event_poller
    from tests.agentic_gate_stub import stub_agentic_gate

    stub_agentic_gate(monkeypatch)
    agent = SimpleNamespace(
        id=uuid.uuid4(), user_id=agent_owner, enabled=True, integration_bindings={}
    )
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=agent_owner,
        is_company_managed=agent_owner is None,
        trigger_type="integration_event",
        enabled=True,
        trigger_config={
            "provider": integration.provider,
            "event": "new_email",
            "integration_id": str(integration.id),
        },
    )
    polled: list[str] = []

    async def _poll(_provider, _event, token, *_a, **_k):
        polled.append(token)
        return []

    redis = AsyncMock()
    redis.get.return_value = None
    monkeypatch.setattr(event_poller, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(event_poller, "decrypt_value", lambda value: f"plain:{value}")
    monkeypatch.setattr(event_poller, "_poll_provider", _poll)
    monkeypatch.setattr(
        "app.core.database.release_db_connection_before_loop", AsyncMock()
    )
    db = FakeSession(
        {Assignment: [assignment], Agent: [agent], Integration: [integration]}
    )
    return event_poller, db, polled


@pytest.mark.parametrize(("case", "usable"), CASES)
async def test_event_poller_polls_only_with_a_token_the_agent_owner_may_use(
    monkeypatch, case, usable
):
    integration = _case_integration(case, provider="microsoft")
    event_poller, db, polled = _poller_fixture(
        monkeypatch, integration, agent_owner=COLLEAGUE_ID
    )

    await event_poller.poll_integration_events(db)

    assert polled == (["plain:enc-access-token"] if usable else [])


async def test_event_poller_for_an_ownerless_agent_needs_an_acknowledged_share(monkeypatch):
    unshared = make_integration(provider="microsoft", is_company_managed=True)
    event_poller, db, polled = _poller_fixture(monkeypatch, unshared, agent_owner=None)
    await event_poller.poll_integration_events(db)
    assert polled == []

    shared = make_integration(
        provider="microsoft", is_company_managed=True, oauth_share_acknowledged=True
    )
    event_poller, db, polled = _poller_fixture(monkeypatch, shared, agent_owner=None)
    await event_poller.poll_integration_events(db)
    assert polled == ["plain:enc-access-token"]


# ---------------------------------------------------------------------------
# Path 2 - Microsoft Graph change notification (acts as the agent's owner)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("case", "usable"), CASES)
async def test_graph_notification_enriches_only_with_a_token_the_agent_owner_may_use(
    monkeypatch, case, usable
):
    from app.api.v1 import integration_notifications as notifications

    integration = _case_integration(case, provider="microsoft")
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=COLLEAGUE_ID,
        enabled=True,
        integration_bindings={"microsoft": [str(integration.id)]},
    )
    assignment = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent.id,
        enabled=True,
        name="Triage",
        user_id=COLLEAGUE_ID,
        is_company_managed=False,
    )
    db = FakeSession({Assignment: [assignment], Agent: [agent], Integration: [integration]})

    @contextlib.asynccontextmanager
    async def _session():
        yield db

    redis = AsyncMock()
    # The subscription record as main reads it: the bare assignment id (a legacy
    # record, served as before by default).
    redis.mget.return_value = [str(assignment.id)]
    enriched_with: list[str] = []

    async def _notified(_event, _notif, *, token, **_kwargs):
        # The event a notification names, read with the owner's token (``notified_event``).
        enriched_with.append(token)
        return {}, ""

    trigger = AsyncMock(return_value="run-1")
    monkeypatch.setattr(notifications, "async_session", _session)
    monkeypatch.setattr(notifications, "redis_client", AsyncMock(return_value=redis))
    monkeypatch.setattr(notifications, "decrypt_value", lambda value: f"plain:{value}")
    monkeypatch.setattr(notifications, "notified_event", _notified)
    monkeypatch.setattr(notifications, "trigger_assignment", trigger)
    body = json.dumps(
        {"value": [{"resource": "me/messages/1", "subscriptionId": "sub-1", "changeType": "created"}]}
    ).encode()

    async def _stream():
        yield body

    # The raw body is read, bounded, before it is parsed.
    request = SimpleNamespace(query_params={}, headers={"content-length": str(len(body))}, stream=_stream)

    result = await notifications.microsoft_graph_notification(request)

    assert enriched_with == (["plain:enc-access-token"] if usable else [])
    # The run is still triggered - only the owner's mailbox is not read for it.
    # That holds for an integration the acting user cannot see too (shared
    # with a group they are not in): delivered as before, flagged.
    assert result == {"status": "ok", "processed": 1}
    trigger.assert_awaited_once()


# ---------------------------------------------------------------------------
# Newly gated REST readers (the admin visibility bypass included)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("case", "usable"), CASES)
async def test_listing_integration_elements_needs_a_token_the_caller_may_use(
    monkeypatch, case, usable
):
    from app.api.v1 import integration_elements

    integration = _case_integration(case)
    listed_with: list[str] = []

    async def _list(_provider, _element_type, token):
        listed_with.append(token)
        return []

    monkeypatch.setattr(
        integration_elements, "get_user_integration", AsyncMock(return_value=integration)
    )
    monkeypatch.setattr(integration_elements, "decrypt_value", lambda value: f"plain:{value}")
    monkeypatch.setattr(integration_elements, "_list_provider_elements", _list)

    call = integration_elements.list_integration_elements(
        integration_id=str(integration.id),
        element_type="slack_channel",
        user=_user(),
        db=None,
    )
    if usable:
        await call
        assert listed_with == ["plain:enc-access-token"]
    else:
        with pytest.raises(HTTPException) as exc:
            await call
        _refused(exc)
        assert listed_with == []


class _RecordingGitHub:
    """httpx.AsyncClient stand-in recording every GitHub call."""

    def __init__(self, calls: list[tuple[str, str, dict]]):
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, json=None, headers=None):
        self._calls.append(("POST", url, headers))
        return SimpleNamespace(status_code=201, json=lambda: {"id": 42}, text="")

    async def delete(self, url, headers=None):
        self._calls.append(("DELETE", url, headers))
        return SimpleNamespace(status_code=204, text="")


def _github_webhooks(monkeypatch, integration, extra_config: dict):
    from app.api.v1 import integration_github_webhooks as webhooks

    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(webhooks, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(webhooks, "get_user_integration", AsyncMock(return_value=integration))
    monkeypatch.setattr(webhooks, "decrypt_value", lambda value: f"plain:{value}")
    monkeypatch.setattr(webhooks, "decrypt_json", lambda _value: dict(extra_config))
    monkeypatch.setattr(webhooks, "encrypt_value", lambda value: value)
    monkeypatch.setattr(webhooks, "log_audit", AsyncMock())
    monkeypatch.setattr(webhooks.httpx, "AsyncClient", lambda **_k: _RecordingGitHub(calls))
    return webhooks, calls


@pytest.mark.parametrize(("case", "usable"), CASES)
async def test_creating_a_github_webhook_needs_a_token_the_caller_may_use(
    monkeypatch, case, usable
):
    integration = _case_integration(case, provider="github")
    webhooks, calls = _github_webhooks(monkeypatch, integration, {})

    call = webhooks.create_github_webhook(
        integration_id=str(integration.id),
        body=webhooks.CreateGitHubWebhookRequest(repo="acme/app"),
        request=SimpleNamespace(),
        user=_user(),
        db=SimpleNamespace(flush=AsyncMock(), commit=AsyncMock()),
    )
    if usable:
        await call
        assert calls[0][2]["Authorization"] == "Bearer plain:enc-access-token"
    else:
        with pytest.raises(HTTPException) as exc:
            await call
        _refused(exc)
        assert calls == []


@pytest.mark.parametrize(("case", "usable"), REFUSED)
async def test_deleting_a_github_webhook_needs_a_token_the_caller_may_use(
    monkeypatch, case, usable
):
    integration = _case_integration(case, provider="github")
    hook = {"hook_id": 42, "repo": "acme/app", "events": ["push"], "target": "integration"}
    webhooks, calls = _github_webhooks(monkeypatch, integration, {"managed_webhooks": [hook]})

    with pytest.raises(HTTPException) as exc:
        await webhooks.delete_github_webhook(
            integration_id=str(integration.id),
            hook_id=42,
            request=SimpleNamespace(),
            user=_user(),
            db=SimpleNamespace(flush=AsyncMock(), commit=AsyncMock()),
        )
    _refused(exc)
    assert calls == []


async def test_the_owner_still_deletes_their_github_webhook(monkeypatch):
    integration = make_integration(provider="github", user_id=COLLEAGUE_ID)
    hook = {"hook_id": 42, "repo": "acme/app", "events": ["push"], "target": "integration"}
    webhooks, calls = _github_webhooks(monkeypatch, integration, {"managed_webhooks": [hook]})

    await webhooks.delete_github_webhook(
        integration_id=str(integration.id),
        hook_id=42,
        request=SimpleNamespace(),
        user=_user(),
        db=SimpleNamespace(flush=AsyncMock(), commit=AsyncMock()),
    )

    assert [(verb, headers["Authorization"]) for verb, _url, headers in calls] == [
        ("DELETE", "Bearer plain:enc-access-token")
    ]


@pytest.mark.parametrize(("case", "usable"), CASES)
async def test_the_connectivity_test_needs_a_token_the_caller_may_use(
    monkeypatch, case, usable
):
    from app.api.v1 import integrations as integrations_api

    integration = _case_integration(case)
    tested_with: list[str] = []

    async def _probe(_provider, token, *_a, **_k):
        tested_with.append(token)
        return {"status": "ok", "message": "fine"}

    monkeypatch.setattr(integrations_api, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        integrations_api, "get_user_integration", AsyncMock(return_value=integration)
    )
    monkeypatch.setattr(integrations_api, "decrypt_value", lambda value: f"plain:{value}")
    monkeypatch.setattr(integrations_api, "test_provider_connectivity", _probe)

    call = integrations_api.test_integration(
        integration_id=str(integration.id), user=_user(), db=FakeSession()
    )
    if usable:
        assert (await call)["status"] == "ok"
        assert tested_with == ["plain:enc-access-token"]
    else:
        with pytest.raises(HTTPException) as exc:
            await call
        _refused(exc)
        assert tested_with == []


@pytest.mark.parametrize(("case", "usable"), CASES)
async def test_the_tool_test_needs_a_token_the_caller_may_use(monkeypatch, case, usable):
    from app.api.v1 import integrations as integrations_api

    integration = _case_integration(case)
    build = AsyncMock(return_value=([], CallableToolExecutor()))
    monkeypatch.setattr(integrations_api, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        integrations_api, "get_user_integration", AsyncMock(return_value=integration)
    )
    monkeypatch.setattr("app.services.integration_tools.build_integration_tools", build)

    call = integrations_api.test_integration_tool(
        integration_id=str(integration.id),
        body={"tool_name": "slack_post"},
        user=_user(),
        db=FakeSession(),
    )
    if usable:
        await call
        build.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as exc:
            await call
        _refused(exc)
        build.assert_not_awaited()


# ---------------------------------------------------------------------------
# Flows: the operations picker and the integration node
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("case", "usable"), CASES)
async def test_flow_operations_listing_needs_a_token_the_caller_may_use(
    monkeypatch, case, usable
):
    from app.api.v1 import flows as flows_api

    integration = _case_integration(case)
    build = AsyncMock(return_value=([], None))
    monkeypatch.setattr(flows_api, "user_visible_filter", lambda *_a, **_k: true())
    monkeypatch.setattr(flows_api, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.services.integration_tools.build_integration_tools", build)

    call = flows_api.list_integration_operations(
        integration_id=str(integration.id),
        user=_user(),
        db=FakeSession({Integration: [integration]}),
    )
    if usable:
        await call
        build.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as exc:
            await call
        _refused(exc)
        build.assert_not_awaited()


async def _run_integration_node(monkeypatch, integration, *, present=True):
    from app.services.flow_runner import FlowWalker

    build = AsyncMock(return_value=([], CallableToolExecutor()))
    monkeypatch.setattr("app.services.integration_tools.build_integration_tools", build)
    walker = SimpleNamespace(
        db=FakeSession({Integration: [integration] if present else []}),
        user=SimpleNamespace(id=COLLEAGUE_ID, is_admin=False),
        run=SimpleNamespace(flow_id=uuid.uuid4()),
        _renderer=None,
    )
    result = await FlowWalker._exec_integration(
        walker, {"integration_id": str(integration.id), "operation": "slack_post"}
    )
    return result, build


_NODE_CASES = [
    *CASES,
    pytest.param(
        "strangers_api_key", False, id="an_integration_nobody_on_the_flow_can_see"
    ),
    pytest.param("restricted_company", False, id="a_restricted_company_integration"),
]


@pytest.mark.parametrize(("case", "usable"), _NODE_CASES)
async def test_a_flow_integration_node_runs_only_with_an_integration_its_user_may_use(
    monkeypatch, case, usable
):
    """The node runs as the run's user: it uses the integration it names
    only when that person may, the rule of an agent's binding in chat. The
    graph write checks the writer (``check_integration_references``)."""
    integration = {
        "strangers_api_key": lambda: make_integration(auth_type="api_key", user_id=uuid.uuid4()),
        "restricted_company": lambda: make_integration(
            auth_type="api_key", user_id=None, is_company_managed=True, access_restricted=True
        ),
    }.get(case, lambda: _case_integration(case))()

    result, build = await _run_integration_node(monkeypatch, integration)

    if usable:
        build.assert_awaited_once()
        assert build.await_args.args[1] == {integration.provider: str(integration.id)}
        assert "no longer exists" not in (result.error or "")
    else:
        build.assert_not_awaited()
        assert result.error and result.error.startswith("The run's user may not use")


async def test_a_flow_integration_node_whose_integration_is_gone_says_so(monkeypatch):
    result, build = await _run_integration_node(
        monkeypatch, make_integration(auth_type="api_key"), present=False
    )

    assert result.error == "The configured integration no longer exists."
    build.assert_not_awaited()


# ---------------------------------------------------------------------------
# Skill sources: bind time and refresh
# ---------------------------------------------------------------------------

_SKILL_URL = "https://github.com/acme/skills/blob/main/SKILL.md"


@pytest.mark.parametrize(("case", "usable"), CASES)
async def test_binding_a_skill_source_needs_a_token_the_caller_may_use(
    monkeypatch, case, usable
):
    import app.api.v1.agent_skills as agent_skills

    integration = _case_integration(case, provider="github")
    resolve = AsyncMock(return_value="credential")
    monkeypatch.setattr(agent_skills, "get_user_integration", AsyncMock(return_value=integration))
    monkeypatch.setattr(agent_skills, "resolve_skill_credential", resolve)

    call = agent_skills._resolve_source_credential(
        None, _user(), str(integration.id), _SKILL_URL
    )
    if usable:
        assert await call == "credential"
        resolve.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as exc:
            await call
        _refused(exc)
        resolve.assert_not_awaited()


def _shareable(owner, **overrides) -> SimpleNamespace:
    """A personal shareable entity (skill, knowledge base, flow, agent) stand-in."""
    fields = {
        "id": uuid.uuid4(),
        "user_id": owner,
        "is_company_managed": False,
        "shared_group_ids": None,
        "shared_group_permissions": None,
        "shared_user_ids": None,
        "shared_user_permissions": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.mark.parametrize(
    "integration",
    [
        pytest.param({"user_id": COLLEAGUE_ID}, id="an_editor_s_own_token"),
        pytest.param({"user_id": uuid.uuid4()}, id="a_token_whose_owner_cannot_edit_the_skill"),
    ],
)
async def test_worked_on_main_a_skill_refreshes_with_the_credential_it_is_bound_to(
    monkeypatch, integration
):
    """Whoever's token it is, the bound source keeps refreshing."""
    from app.services import skill_source

    bound = make_integration(provider="github", **integration)
    monkeypatch.setattr(skill_source, "decrypt_value", lambda value: f"plain:{value}")

    credential = await skill_source.resolve_skill_credential(
        FakeSession({Integration: [bound]}), bound.id
    )

    assert credential.token == "plain:enc-access-token"


async def test_worked_on_main_an_admin_binds_any_visible_skill_credential(monkeypatch):
    import app.api.v1.agent_skills as agent_skills

    colleagues = make_integration(provider="github")
    resolve = AsyncMock(return_value="credential")
    monkeypatch.setattr(agent_skills, "get_user_integration", AsyncMock(return_value=colleagues))
    monkeypatch.setattr(agent_skills, "resolve_skill_credential", resolve)

    assert (
        await agent_skills._resolve_source_credential(
            None, _user(is_admin=True), str(colleagues.id), _SKILL_URL
        )
        == "credential"
    )


@pytest.mark.parametrize(("case", "_usable"), CASES)
async def test_re_reading_a_credential_the_skill_already_holds_is_not_re_checked(
    monkeypatch, case, _usable
):
    """A URL edit by a co-manager re-reads the source with the credential the
    skill holds: only a credential the request binds is checked."""
    import app.api.v1.agent_skills as agent_skills

    integration = _case_integration(case, provider="github")
    resolve = AsyncMock(return_value="credential")
    monkeypatch.setattr(agent_skills, "get_user_integration", AsyncMock(return_value=integration))
    monkeypatch.setattr(agent_skills, "resolve_skill_credential", resolve)

    assert (
        await agent_skills._resolve_source_credential(
            None, _user(), str(integration.id), _SKILL_URL, newly_bound=False
        )
        == "credential"
    )


@pytest.mark.parametrize(("case", "_usable"), CASES)
async def test_worked_on_main_a_skill_refresh_reads_with_its_bound_credential(
    monkeypatch, case, _usable
):
    from app.services import skill_source

    integration = _case_integration(case, provider="github")
    monkeypatch.setattr(skill_source, "decrypt_value", lambda value: f"plain:{value}")

    credential = await skill_source.resolve_skill_credential(
        FakeSession({Integration: [integration]}), integration.id
    )

    assert credential.token == "plain:enc-access-token"


@pytest.mark.parametrize(("case", "_usable"), CASES)
async def test_worked_on_main_the_skill_refresh_scheduler_reads_with_the_bound_credential(
    monkeypatch, case, _usable
):
    from app.services import skill_source
    from app.services.autonomous import _build_skill_fetch_plans

    integration = _case_integration(case, provider="github")
    monkeypatch.setattr(skill_source, "decrypt_value", lambda value: f"plain:{value}")
    skill = _shareable(
        COLLEAGUE_ID, url=_SKILL_URL, source_integration_id=integration.id, source_ref=None
    )

    (plan,) = await _build_skill_fetch_plans(FakeSession({Integration: [integration]}), [skill])

    assert plan.headers["Authorization"] == "Bearer plain:enc-access-token"
    assert plan.config_error is None


async def test_worked_on_main_a_manual_skill_refresh_uses_the_bound_credential(monkeypatch):
    import app.api.v1.agent_skills as agent_skills
    from app.services.skill_source import SkillCredentialError

    skill = SimpleNamespace(
        id=uuid.uuid4(),
        url=_SKILL_URL,
        user_id=OWNER_ID,
        source_integration_id=uuid.uuid4(),
        content="old",
        description=None,
        compatibility=None,
    )
    resolve = AsyncMock(side_effect=SkillCredentialError("stop here"))
    monkeypatch.setattr(agent_skills, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_skills, "get_manageable_entity", AsyncMock(return_value=skill))
    monkeypatch.setattr(agent_skills, "resolve_skill_credential", resolve)

    with pytest.raises(HTTPException):
        await agent_skills.refresh_skill(skill_id=str(skill.id), user=_user(), db=None)

    assert resolve.await_args.args[1:] == (skill.source_integration_id,)
    assert resolve.await_args.kwargs == {}


# ---------------------------------------------------------------------------
# KB sync sources: the explicit binding, bind time and re-scoping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("case", "_usable"), CASES)
async def test_worked_on_main_a_kb_sync_explicit_binding_is_used_as_bound(
    monkeypatch, case, _usable
):
    from app.services import kb_sync

    integration = _case_integration(case, provider="notion")
    monkeypatch.setattr(kb_sync, "decrypt_value", lambda value: f"plain:{value}")
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda value: f"plain:{value}")
    if integration.auth_type != "oauth":
        integration.api_key_enc, integration.access_token_enc = "enc-access-token", None
    db = FakeSession({Integration: [integration]})

    token = await kb_sync._get_integration_token(
        db, COLLEAGUE_ID, "notion", integration_id=str(integration.id)
    )

    assert token == "plain:enc-access-token"


async def test_kb_sync_company_fallback_needs_an_acknowledged_share(monkeypatch):
    from app.services import kb_sync

    monkeypatch.setattr(kb_sync, "decrypt_value", lambda value: f"plain:{value}")
    unshared = make_integration(provider="notion", is_company_managed=True)
    with pytest.raises(ValueError, match="No connected notion integration"):
        await kb_sync._get_integration_token(
            FakeSession({Integration: [unshared]}), COLLEAGUE_ID, "notion"
        )

    shared = make_integration(
        provider="notion", is_company_managed=True, oauth_share_acknowledged=True
    )
    token = await kb_sync._get_integration_token(
        FakeSession({Integration: [shared]}), COLLEAGUE_ID, "notion"
    )
    assert token == "plain:enc-access-token"


@pytest.mark.parametrize("bound", [True, False], ids=["bound_source", "auto_source"])
async def test_worked_on_main_sync_now_runs_as_the_user_who_pressed_it(monkeypatch, bound):
    from app.services import kb_sync_scheduler

    source = SimpleNamespace(
        id=uuid.uuid4(),
        source_type="notion",
        config={},
        integration_id=uuid.uuid4() if bound else None,
        user_id=OWNER_ID,
        last_sync_status=None,
        last_synced_at=None,
        last_sync_error=None,
        last_sync_documents=0,
        sync_failure_count=0,
        sync_next_retry_at=None,
    )
    kb = SimpleNamespace(id=uuid.uuid4(), name="KB", user_id=None)
    run_sync = AsyncMock(return_value=(["doc-1"], 0))
    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_in_isolated_session", run_sync)
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())

    await kb_sync_scheduler.execute_sync_source(db, source, kb, str(COLLEAGUE_ID))

    assert run_sync.await_args.kwargs["user_id"] == str(COLLEAGUE_ID)


def _kb_routes(monkeypatch):
    from app.api.v1 import knowledge_bases

    kb = SimpleNamespace(
        id=uuid.uuid4(),
        is_builtin=False,
        user_id=OWNER_ID,
        is_company_managed=False,
        shared_group_ids=None,
        shared_group_permissions=None,
        shared_user_ids=None,
        shared_user_permissions=None,
        access_restricted=False,
    )
    monkeypatch.setattr(knowledge_bases, "get_user_knowledge_base", AsyncMock(return_value=kb))
    monkeypatch.setattr(
        knowledge_bases, "assert_knowledge_base_not_package_origin", AsyncMock()
    )
    monkeypatch.setattr(knowledge_bases, "_assert_sync_sources_licensed", AsyncMock())
    monkeypatch.setattr(knowledge_bases, "_assert_kb_manageable", AsyncMock())
    monkeypatch.setattr(
        knowledge_bases, "_sync_source_to_response", lambda source, **_k: source
    )
    return knowledge_bases, kb


@pytest.mark.parametrize(("case", "usable"), CASES)
async def test_binding_a_kb_sync_source_needs_a_token_the_caller_may_use(
    monkeypatch, case, usable
):
    from app.api.v1.schemas.knowledge_base import CreateSyncSourceRequest

    knowledge_bases, kb = _kb_routes(monkeypatch)
    integration = _case_integration(case, provider="notion")
    body = CreateSyncSourceRequest(
        source_type="notion", config={}, integration_id=str(integration.id)
    )

    call = knowledge_bases.create_sync_source(
        kb_id=str(kb.id),
        body=body,
        user=_user(),
        db=FakeSession({Integration: [integration]}),
        _ai_gate=None,
    )
    if usable:
        assert (await call).integration_id == str(integration.id)
    else:
        # A member binding a colleague's personal integration was already
        # refused on main ("You don't have access"); still refused.
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 403


def _bound_source(kb, integration) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        knowledge_base_id=kb.id,
        integration_id=integration.id,
        config={"page_ids": ["a"]},
        sync_interval="daily",
        enabled=True,
        source_type="notion",
    )


@pytest.mark.parametrize(("case", "usable"), CASES)
async def test_rebinding_a_kb_sync_source_needs_a_token_the_caller_may_use(
    monkeypatch, case, usable
):
    from app.api.v1.schemas.knowledge_base import UpdateSyncSourceRequest

    knowledge_bases, kb = _kb_routes(monkeypatch)
    own = make_integration(provider="notion", user_id=COLLEAGUE_ID)
    target = _case_integration(case, provider="notion")
    source = _bound_source(kb, own)
    db = FakeSession({KBSyncSource: [source], Integration: [own, target]})

    call = knowledge_bases.update_sync_source(
        kb_id=str(kb.id),
        source_id=str(source.id),
        body=UpdateSyncSourceRequest(integration_id=str(target.id)),
        user=_user(),
        db=db,
        _ai_gate=None,
    )
    if usable:
        await call
        assert source.integration_id == target.id
    else:
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 403
        assert source.integration_id == own.id


@pytest.mark.parametrize(("case", "usable"), CASES)
async def test_worked_on_main_a_kb_manager_rescopes_a_bound_source(monkeypatch, case, usable):
    """Editing what a bound source syncs works for a KB manager as on main; the
    binding and the source's owner are unchanged."""
    from app.api.v1.schemas.knowledge_base import UpdateSyncSourceRequest

    knowledge_bases, kb = _kb_routes(monkeypatch)
    integration = _case_integration(case, provider="notion")
    source = _bound_source(kb, integration)
    source.user_id = OWNER_ID
    db = FakeSession({KBSyncSource: [source], Integration: [integration]})

    await knowledge_bases.update_sync_source(
        kb_id=str(kb.id),
        source_id=str(source.id),
        body=UpdateSyncSourceRequest(config={"page_ids": ["b"]}),
        user=_user(),
        db=db,
        _ai_gate=None,
    )

    assert source.config == {"page_ids": ["b"]}
    assert source.integration_id == integration.id
    assert source.user_id == OWNER_ID


async def test_a_schedule_only_edit_of_a_bound_kb_sync_source_is_not_a_rebinding(monkeypatch):
    from app.api.v1.schemas.knowledge_base import UpdateSyncSourceRequest

    knowledge_bases, kb = _kb_routes(monkeypatch)
    integration = make_integration(provider="notion", is_company_managed=True)
    source = _bound_source(kb, integration)
    db = FakeSession({KBSyncSource: [source], Integration: [integration]})

    await knowledge_bases.update_sync_source(
        kb_id=str(kb.id),
        source_id=str(source.id),
        body=UpdateSyncSourceRequest(sync_interval="weekly", config={"page_ids": ["a"]}),
        user=_user(),
        db=db,
        _ai_gate=None,
    )

    assert source.sync_interval == "weekly"


async def test_a_kb_member_binding_their_own_token_keeps_the_source_s_owner(monkeypatch):
    """A co-manager binds their own token: the source keeps its owner
    (``user_id`` cascades on user deletion) and syncs with the bound token."""
    from app.api.v1.schemas.knowledge_base import UpdateSyncSourceRequest
    from app.services import kb_sync

    knowledge_bases, kb = _kb_routes(monkeypatch)
    editors_own = make_integration(provider="notion", user_id=COLLEAGUE_ID)
    company = make_integration(provider="notion", auth_type="api_key", is_company_managed=True)
    source = _bound_source(kb, company)
    source.user_id = OWNER_ID
    db = FakeSession({KBSyncSource: [source], Integration: [editors_own, company]})

    await knowledge_bases.update_sync_source(
        kb_id=str(kb.id),
        source_id=str(source.id),
        body=UpdateSyncSourceRequest(integration_id=str(editors_own.id)),
        user=_user(),
        db=db,
        _ai_gate=None,
    )

    assert source.integration_id == editors_own.id
    assert source.user_id == OWNER_ID
    monkeypatch.setattr(kb_sync, "decrypt_value", lambda value: f"plain:{value}")
    token = await kb_sync._get_integration_token(
        db, str(OWNER_ID), "notion", integration_id=str(source.integration_id)
    )
    assert token == "plain:enc-access-token"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"auth_type": "api_key"}, id="non_oauth"),
        pytest.param({"oauth_share_acknowledged": True}, id="acknowledged_oauth"),
    ],
)
async def test_worked_on_main_rebinding_to_a_usable_integration_keeps_the_sync_identity(
    monkeypatch, overrides
):
    """The identity also decides the source's lifecycle (``ON DELETE CASCADE``):
    it only moves to the binder when it could not use the new credential."""
    from app.api.v1.schemas.knowledge_base import UpdateSyncSourceRequest

    knowledge_bases, kb = _kb_routes(monkeypatch)
    first = make_integration(provider="notion", auth_type="api_key", is_company_managed=True)
    company = make_integration(provider="notion", is_company_managed=True, **overrides)
    source = _bound_source(kb, first)
    source.user_id = OWNER_ID
    db = FakeSession({KBSyncSource: [source], Integration: [first, company]})

    await knowledge_bases.update_sync_source(
        kb_id=str(kb.id),
        source_id=str(source.id),
        body=UpdateSyncSourceRequest(integration_id=str(company.id)),
        user=_user(),
        db=db,
        _ai_gate=None,
    )

    assert source.integration_id == company.id
    assert source.user_id == OWNER_ID


@pytest.mark.parametrize(
    "owner",
    [
        pytest.param(COLLEAGUE_ID, id="a_co_manager_s_own_token"),
        pytest.param(uuid.uuid4(), id="a_token_nobody_on_the_kb_owns"),
    ],
)
async def test_worked_on_main_a_bound_kb_source_keeps_syncing_whoever_s_token_it_is(
    monkeypatch, owner
):
    from app.services import kb_sync

    monkeypatch.setattr(kb_sync, "decrypt_value", lambda value: f"plain:{value}")
    bound = make_integration(provider="notion", user_id=owner)

    token = await kb_sync._get_integration_token(
        FakeSession({Integration: [bound]}),
        str(OWNER_ID),
        "notion",
        integration_id=str(bound.id),
    )

    assert token == "plain:enc-access-token"


async def test_an_interval_edit_keeps_the_identity_a_bound_source_syncs_as(monkeypatch):
    from app.api.v1.schemas.knowledge_base import UpdateSyncSourceRequest

    knowledge_bases, kb = _kb_routes(monkeypatch)
    integration = make_integration(provider="notion", user_id=OWNER_ID)
    source = _bound_source(kb, integration)
    source.user_id = OWNER_ID
    db = FakeSession({KBSyncSource: [source], Integration: [integration]})

    await knowledge_bases.update_sync_source(
        kb_id=str(kb.id),
        source_id=str(source.id),
        body=UpdateSyncSourceRequest(sync_interval="weekly", config={"page_ids": ["a"]}),
        user=_user(),
        db=db,
        _ai_gate=None,
    )

    assert source.user_id == OWNER_ID
