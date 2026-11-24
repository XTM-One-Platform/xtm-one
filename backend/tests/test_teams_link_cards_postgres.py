"""The Teams card of a pasted run link, through the webhook, against PostgreSQL.

Every case goes through ``POST /api/channels/teams/messages`` as Teams sends
it - an ``adaptiveCard/action`` invoke for a viewer's refresh or a click - with
the Bot Framework JWT check and the member lookup stubbed, and the real access
rules, row lock, decision writes and refusal audit underneath. The run is
resumed by a spy: the resume itself is the respond endpoint's, tested there.

The cast: ``owner`` (a non-admin with a private agent), ``other`` (a non-admin
who owns nothing here), ``admin``; a private run of ``owner`` paused on two
tools, a company-managed run paused on one, a private run paused on a question.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from starlette.requests import Request

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.channels.teams import teams_messages
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.audit_log import AuditLog
from app.models.user import User
from app.services import assignment_engine
from app.services import teams_link_cards as cards
from app.services.channel_notices import notice
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@dataclass
class World:
    sessions: object
    users: dict[str, User]
    runs: dict[str, AssignmentRun]
    resumed: list[uuid.UUID]


def _proposal(call_id: str, name: str) -> dict:
    return {
        "tool_call_id": call_id,
        "tool_name": name,
        "tool_description": "",
        "arguments": {"target": "soc@example.com"},
        "input_schema": {"type": "object"},
        "source": "builtin",
    }


@pytest_asyncio.fixture
async def world(monkeypatch) -> AsyncIterator[World]:
    async with scratch_database("teamscards", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as s:
            users = {
                name: User(
                    email=f"{name}-{uuid.uuid4().hex[:8]}@example.com",
                    hashed_password="x",
                    full_name=f"{name.title()} Person",
                    role="admin" if name == "admin" else "user",
                )
                for name in ("owner", "other", "admin")
            }
            s.add_all(users.values())
            await s.flush()
            private_agent = Agent(name="Mail Triage", slug=f"mail-{uuid.uuid4().hex[:8]}", persona="p", user_id=users["owner"].id)
            company_agent = Agent(
                name="Company agent",
                slug=f"co-{uuid.uuid4().hex[:8]}",
                persona="p",
                user_id=users["admin"].id,
                is_company_managed=True,
            )
            s.add_all([private_agent, company_agent])
            await s.flush()
            private_assignment = Assignment(
                agent_id=private_agent.id,
                user_id=users["owner"].id,
                name="Phishing triage",
                trigger_type="manual",
                trigger_config={},
                prompt="Triage.",
                enabled=True,
            )
            company_assignment = Assignment(
                agent_id=company_agent.id,
                user_id=users["admin"].id,
                is_company_managed=True,
                name="Company sweep",
                trigger_type="manual",
                trigger_config={},
                prompt="Sweep.",
                enabled=True,
            )
            s.add_all([private_assignment, company_assignment])
            await s.flush()
            runs = {
                "private": AssignmentRun(
                    assignment_id=private_assignment.id,
                    agent_id=private_agent.id,
                    user_id=users["owner"].id,
                    status="awaiting_human",
                    human_input_question="Approval required before running: send_email, create_indicator",
                    pending_approvals=[_proposal("c1", "send_email"), _proposal("c2", "create_indicator")],
                ),
                "company": AssignmentRun(
                    assignment_id=company_assignment.id,
                    agent_id=company_agent.id,
                    user_id=users["admin"].id,
                    is_company_managed=True,
                    status="awaiting_human",
                    human_input_question="Approval required before running: send_email",
                    pending_approvals=[_proposal("k1", "send_email")],
                ),
                "question": AssignmentRun(
                    assignment_id=private_assignment.id,
                    agent_id=private_agent.id,
                    user_id=users["owner"].id,
                    status="awaiting_human",
                    human_input_question="Deploy to production?",
                ),
            }
            s.add_all(runs.values())
            await s.commit()

        resumed: list[uuid.UUID] = []
        real_resume = assignment_engine.resume_assignment_run

        async def _resume(session, run):
            # The real resume, which writes the run's orchestration rows and
            # closes its action requests; only the reconciler is not woken.
            resumed.append(run.id)
            await real_resume(session, run)

        emails = {f"aad-{name}": user.email for name, user in users.items()}

        async def _email(data):
            return emails.get((data.get("from") or {}).get("aadObjectId"))

        monkeypatch.setattr(assignment_engine, "resume_assignment_run", _resume)
        monkeypatch.setattr(assignment_engine, "notify_orchestration_wake", AsyncMock())
        monkeypatch.setattr("app.channels.teams._invoke_user_email", _email)
        monkeypatch.setattr("app.channels.teams._get_teams_app_id", AsyncMock(return_value="app-123"))
        monkeypatch.setattr("app.channels.teams._verify_botframework_jwt", lambda *_a: True)
        monkeypatch.setattr("app.core.rate_limit.enforce_rate_limit", AsyncMock())
        monkeypatch.setattr("app.channels.teams._configured_teams_tenant", AsyncMock(return_value="tenant-a"))
        monkeypatch.setattr("app.services.notify._notify_web", AsyncMock())
        yield World(sessions=db.sessions, users=users, runs=runs, resumed=resumed)


async def _invoke(viewer: str, verb: str, data: dict) -> dict:
    body = json.dumps(
        {
            "type": "invoke",
            "name": "adaptiveCard/action",
            "from": {"id": f"29:{viewer}", "aadObjectId": f"aad-{viewer}"},
            "conversation": {"id": "19:conv"},
            "serviceUrl": "https://smba.trafficmanager.net/emea/",
            "channelData": {"tenant": {"id": "tenant-a"}},
            "value": {"action": {"type": "Action.Execute", "verb": verb, "data": data}, "trigger": "manual"},
        }
    ).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/channels/teams/messages",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer token")],
        },
        receive,
    )
    response = await teams_messages(request)
    payload = json.loads(response.body)
    assert (payload["statusCode"], payload["type"]) == (200, cards.ADAPTIVE_CARD)
    return payload["value"]


def _target(run_id) -> dict:
    return {"kind": "run", "id": str(run_id)}


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _choices(card: dict) -> list[str]:
    return [node["id"] for node in _walk(card) if node.get("type") == "Input.ChoiceSet"]


def _verbs(card: dict) -> list[str]:
    return [a["verb"] for a in card["actions"] if a["type"] == "Action.Execute"]


def _banner(card: dict) -> str | None:
    for block in card["body"]:
        inlines = block.get("inlines") or []
        if inlines and inlines[0].get("color") == "attention":
            return inlines[0]["text"]
    return None


async def _run(world: World, key: str) -> AssignmentRun:
    async with world.sessions() as s:
        return await s.get(AssignmentRun, world.runs[key].id)


async def _refusals(world: World) -> list[tuple[str, uuid.UUID, dict]]:
    async with world.sessions() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.entity_type == "assignment_run_approval"))).scalars()
        return [(row.action, row.entity_id, row.details) for row in rows]


# --- What each viewer sees -----------------------------------------------------


@pytest.mark.asyncio
async def test_the_owner_sees_the_paused_run_with_a_choice_per_tool(world):
    card = await _invoke("owner", cards.VERB_REFRESH, _target(world.runs["private"].id))

    assert _choices(card) == ["d0", "d1"]
    assert _verbs(card) == [cards.VERB_DECIDE] * 3
    texts = [inline["text"] for node in _walk(card) if node.get("type") == "RichTextBlock" for inline in node["inlines"]]
    assert "Phishing triage" in texts
    assert "target: soc@example.com" in texts


@pytest.mark.asyncio
async def test_someone_else_learns_only_that_they_have_no_access_and_so_does_a_missing_run(world):
    run_id = world.runs["private"].id
    missing = uuid.uuid4()

    forbidden = await _invoke("other", cards.VERB_REFRESH, _target(run_id))
    absent = await _invoke("other", cards.VERB_REFRESH, _target(missing))

    assert forbidden == cards.no_access_card(cards.LinkTarget("run", run_id), "en", linked=True)
    assert json.dumps(absent).replace(str(missing), "ID") == json.dumps(forbidden).replace(str(run_id), "ID")
    assert "Phishing triage" not in json.dumps(forbidden)


@pytest.mark.asyncio
async def test_a_teams_user_the_platform_does_not_know_is_asked_to_sign_in(world):
    card = await _invoke("stranger", cards.VERB_REFRESH, _target(world.runs["private"].id))

    assert card == cards.no_access_card(cards.LinkTarget("run", world.runs["private"].id), "en", linked=False)


@pytest.mark.asyncio
async def test_a_company_managed_run_is_read_by_everyone_and_answered_by_admins(world):
    run_id = world.runs["company"].id

    reader = await _invoke("other", cards.VERB_REFRESH, _target(run_id))
    admin = await _invoke("admin", cards.VERB_REFRESH, _target(run_id))

    assert (_choices(reader), _verbs(reader)) == ([], [cards.VERB_REQUEST])
    assert "soc@example.com" not in json.dumps(reader)
    assert notice("card_waiting_admin", "en") in json.dumps(reader)
    assert (_choices(admin), _verbs(admin)) == (["d0"], [cards.VERB_DECIDE] * 3)


# --- Acting from the card --------------------------------------------------------


@pytest.mark.asyncio
async def test_the_owner_decides_from_the_card_as_on_the_web(world):
    run_id = world.runs["private"].id
    card = await _invoke(
        "owner",
        cards.VERB_DECIDE,
        {**_target(run_id), "calls": ["c1", "c2"], "d0": "approve", "d1": "reject", "reason": "not this one"},
    )

    run = await _run(world, "private")
    assert run.approval_decisions == {
        "c1": {"tool_call_id": "c1", "decision": "approve", "rejection_reason": None},
        "c2": {"tool_call_id": "c2", "decision": "reject", "rejection_reason": "not this one"},
    }
    (entry,) = run.human_input_history
    assert (entry["resolution"], entry["responded_by_id"], entry["surface"]) == (
        "answered",
        str(world.users["owner"].id),
        "teams_card",
    )
    assert world.resumed == [run_id]
    assert _banner(card) == notice("card_answer_sent", "en")
    assert (_choices(card), _verbs(card)) == ([], [])


@pytest.mark.asyncio
async def test_a_second_decision_is_told_the_run_was_answered(world):
    run_id = world.runs["private"].id
    data = {**_target(run_id), "calls": ["c1", "c2"], "all": "approve"}
    await _invoke("owner", cards.VERB_DECIDE, data)

    card = await _invoke("admin", cards.VERB_DECIDE, {**data, "all": "reject"})

    run = await _run(world, "private")
    assert {d["decision"] for d in run.approval_decisions.values()} == {"approve"}
    assert world.resumed == [run_id]
    assert _banner(card) == notice("card_already_answered", "en")


@pytest.mark.asyncio
async def test_a_reader_who_forges_a_decision_is_refused_and_audited(world):
    run_id = world.runs["company"].id
    card = await _invoke("other", cards.VERB_DECIDE, {**_target(run_id), "calls": ["k1"], "all": "approve"})

    run = await _run(world, "company")
    assert run.approval_decisions is None
    assert world.resumed == []
    assert _banner(card) == notice("card_refused", "en")
    assert await _refusals(world) == [("refuse", run_id, {"reason": "forbidden", "surface": "teams_card"})]


@pytest.mark.asyncio
async def test_someone_who_cannot_read_the_run_is_audited_and_a_missing_run_is_not(world):
    run_id = world.runs["private"].id
    data = {"calls": ["c1", "c2"], "all": "approve"}

    await _invoke("other", cards.VERB_DECIDE, {**_target(run_id), **data})
    await _invoke("other", cards.VERB_DECIDE, {**_target(uuid.uuid4()), **data})

    assert (await _run(world, "private")).approval_decisions is None
    assert await _refusals(world) == [("refuse", run_id, {"reason": "forbidden", "surface": "teams_card"})]


@pytest.mark.asyncio
async def test_a_decision_on_calls_the_card_did_not_show_is_refused(world):
    run_id = world.runs["private"].id
    card = await _invoke("owner", cards.VERB_DECIDE, {**_target(run_id), "calls": ["c1"], "all": "approve"})

    assert (await _run(world, "private")).approval_decisions is None
    assert _banner(card) == notice("card_changed", "en")
    assert _choices(card) == ["d0", "d1"]


@pytest.mark.asyncio
async def test_a_partial_decision_is_refused(world):
    run_id = world.runs["private"].id
    card = await _invoke("owner", cards.VERB_DECIDE, {**_target(run_id), "calls": ["c1", "c2"], "d0": "approve"})

    assert (await _run(world, "private")).approval_decisions is None
    assert _banner(card) == notice("card_decide_every", "en")


@pytest.mark.asyncio
async def test_the_owner_answers_a_question_from_the_card(world):
    run_id = world.runs["question"].id
    await _invoke(
        "owner",
        cards.VERB_ANSWER,
        {**_target(run_id), "q": cards.question_digest("Deploy to production?"), "answer": "Yes, go ahead"},
    )

    run = await _run(world, "question")
    assert run.human_input_response == "Yes, go ahead"
    assert [(e["response"], e["surface"]) for e in run.human_input_history] == [("Yes, go ahead", "teams_card")]
    assert world.resumed == [run_id]


@pytest.mark.asyncio
async def test_the_web_answer_keeps_its_history_entry_unchanged(world):
    """The respond endpoint now delegates to the same function: its entry gains no surface."""
    from app.api.v1.assignments import respond_to_human_input
    from app.api.v1.schemas.assignment import RespondToHumanInputRequest

    async with world.sessions() as s:
        owner = await s.get(User, world.users["owner"].id)
        await respond_to_human_input(
            str(world.runs["question"].id), RespondToHumanInputRequest(response="Yes"), user=owner, db=s
        )
        await s.commit()

    (entry,) = (await _run(world, "question")).human_input_history
    assert "surface" not in entry
    assert entry["response"] == "Yes"


@pytest.mark.asyncio
async def test_the_queried_link_reads_nothing_from_the_database(world):
    with patch("app.core.database.async_session") as sessions:
        body = json.dumps(
            {
                "type": "invoke",
                "name": "composeExtension/queryLink",
                "value": {"url": f"{(cards._page_base())}/operations/executions/{world.runs['private'].id}"},
                "from": {"id": "29:owner", "aadObjectId": "aad-owner"},
            }
        ).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        response = await teams_messages(
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/channels/teams/messages",
                    "query_string": b"",
                    "headers": [(b"authorization", b"Bearer token")],
                },
                receive,
            )
        )

    assert sessions.call_count == 0
    attachment = json.loads(response.body)["composeExtension"]["attachments"][0]
    assert "Phishing triage" not in json.dumps(attachment)


# --- Asking whoever may answer to act (D7) --------------------------------------


async def _requests(world: World) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    from app.models.access_request import AccessRequest

    async with world.sessions() as s:
        rows = (await s.execute(select(AccessRequest).order_by(AccessRequest.created_at))).scalars()
        return [(row.entity_id, row.requester_id, row.status) for row in rows]


async def _notified(world: World) -> list[tuple[uuid.UUID, str, dict]]:
    from app.models.activity import Activity

    async with world.sessions() as s:
        rows = (await s.execute(select(Activity).order_by(Activity.created_at))).scalars()
        return [(row.user_id, row.title, row.extra_data) for row in rows]


@pytest.mark.asyncio
async def test_someone_without_access_asks_the_owner_once_and_is_told_the_same_every_time(world):
    run_id = world.runs["private"].id

    first = await _invoke("other", cards.VERB_REQUEST, _target(run_id))
    second = await _invoke("other", cards.VERB_REQUEST, _target(run_id))

    assert await _requests(world) == [(run_id, world.users["other"].id, "open")]
    ((recipient, title, extra),) = await _notified(world)
    assert recipient == world.users["owner"].id
    assert title == "Other Person asks you to act on a run"
    assert (extra["notification_source"], extra["run_id"]) == ("run_action_request", str(run_id))
    assert first == second
    assert _banner(first) == notice("card_request_sent", "en")
    assert "Phishing triage" not in json.dumps(first)


@pytest.mark.asyncio
async def test_a_request_on_a_run_that_waits_for_nobody_or_does_not_exist_writes_nothing(world):
    run_id = world.runs["private"].id
    async with world.sessions() as s:
        (await s.get(AssignmentRun, run_id)).status = "completed"
        await s.commit()
    missing = uuid.uuid4()

    done = await _invoke("other", cards.VERB_REQUEST, _target(run_id))
    absent = await _invoke("other", cards.VERB_REQUEST, _target(missing))

    assert await _requests(world) == []
    assert await _notified(world) == []
    assert json.dumps(absent).replace(str(missing), "ID") == json.dumps(done).replace(str(run_id), "ID")


@pytest.mark.asyncio
async def test_a_reader_of_a_company_run_asks_the_administrators(world):
    run_id = world.runs["company"].id

    await _invoke("other", cards.VERB_REQUEST, _target(run_id))

    assert await _requests(world) == [(run_id, world.users["other"].id, "open")]
    assert [recipient for recipient, _title, _extra in await _notified(world)] == [world.users["admin"].id]


@pytest.mark.asyncio
async def test_someone_who_can_answer_asks_nobody(world):
    await _invoke("owner", cards.VERB_REQUEST, _target(world.runs["private"].id))

    assert await _requests(world) == []
    assert await _notified(world) == []


@pytest.mark.asyncio
async def test_answering_the_run_closes_its_requests(world):
    run_id = world.runs["private"].id
    await _invoke("other", cards.VERB_REQUEST, _target(run_id))

    await _invoke("owner", cards.VERB_DECIDE, {**_target(run_id), "calls": ["c1", "c2"], "all": "approve"})

    assert await _requests(world) == [(run_id, world.users["other"].id, "closed")]


@pytest.mark.asyncio
async def test_the_owner_lists_and_dismisses_a_request_and_nobody_else_can(world):
    from fastapi import HTTPException

    from app.api.v1.assignments import dismiss_run_action_request, list_run_action_requests

    run_id = world.runs["private"].id
    await _invoke("other", cards.VERB_REQUEST, _target(run_id))

    async with world.sessions() as s:
        owner = await s.get(User, world.users["owner"].id)
        other = await s.get(User, world.users["other"].id)
        listed = await list_run_action_requests(str(run_id), user=owner, db=s)
        with pytest.raises(HTTPException) as refused:
            await list_run_action_requests(str(run_id), user=other, db=s)
        with pytest.raises(HTTPException) as refused_dismiss:
            await dismiss_run_action_request(str(run_id), listed[0].id, user=other, db=s)
        await dismiss_run_action_request(str(run_id), listed[0].id, user=owner, db=s)
        with pytest.raises(HTTPException) as gone:
            await dismiss_run_action_request(str(run_id), listed[0].id, user=owner, db=s)

    assert [(r.requester_name, r.source) for r in listed] == [("Other Person", "teams_card")]
    assert (refused.value.status_code, refused_dismiss.value.status_code, gone.value.status_code) == (404, 404, 404)
    assert await _requests(world) == [(run_id, world.users["other"].id, "closed")]
