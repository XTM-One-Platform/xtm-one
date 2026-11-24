"""Email signature settings through the real routers and tool build, on PostgreSQL, as non-admins.

An email-sending integration's Email signature setting and an agent's
override per binding are written through ``PUT /integrations/{id}`` and
``PUT /agents/{id}``: a signature integration someone sets is one they can
see, holding signatures; an override names a bound integration that sends
email. The two cards say what the settings resolve to, and a run's Outlook
tools sign - or not - exactly as they say. Graph and the signature service are
answered in memory.
"""

from __future__ import annotations

import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.integrations.providers.microsoft as ms
import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.deps import get_current_user
from app.api.v1 import agents as agents_api
from app.api.v1 import email_signatures as cards_api
from app.api.v1 import integrations as integrations_api
from app.core.database import get_db
from app.core.security import encrypt_value
from app.integrations.providers.microsoft import mail
from app.models.agent import Agent
from app.models.integration import Integration
from app.models.user import User
from app.services import email_signatures
from app.services.email_signature_settings import SIGNATURE_CONTEXT_KEY
from app.services.email_signatures import EmailSignature
from app.services.integration_tools import BindingBasis, ToolBindings, build_integration_tools
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

SOURCE = "fake_signatures"


def _scratch(name: str):
    return scratch_database(name, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


@pytest.fixture(autouse=True)
def _services(monkeypatch):
    """A signature service and Graph, answered in memory; every Graph request recorded."""
    sent: list[dict] = []

    async def _read(credential: str, address: str):
        assert credential == "signature-key"
        return EmailSignature(html=f"<p>Signed for {address}</p>", reply_html="<p>Reply</p>")

    class _Ok:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    async def _graph(_client, method, url, _provider, _action, **kwargs):
        if url.endswith("/me"):
            return _Ok({"mail": "a@example.com"}), None
        if url.endswith("/sendMail"):
            sent.append(kwargs["json"])
        if "sentitems" in url:
            return _Ok({"value": []}), None
        return _Ok({}), None

    async def _quiet(*_args, **_kwargs):
        return None

    monkeypatch.setitem(email_signatures._READERS, SOURCE, _read)
    monkeypatch.setattr(email_signatures, "_cache", OrderedDict())
    monkeypatch.setattr(ms._common, "safe_request", _graph)
    monkeypatch.setattr(mail, "record_mail_send", _quiet)
    return sent


class Caller:
    def __init__(self) -> None:
        self.user: User | None = None


@asynccontextmanager
async def _client(sessions: async_sessionmaker, caller: Caller) -> AsyncIterator[httpx.AsyncClient]:
    application = FastAPI()
    application.include_router(integrations_api.router, prefix="/api/v1/integrations")
    application.include_router(cards_api.integration_router, prefix="/api/v1/integrations")
    application.include_router(cards_api.agent_router, prefix="/api/v1/agents")
    application.include_router(agents_api.router, prefix="/api/v1/agents")

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    application.dependency_overrides[get_db] = _get_db
    application.dependency_overrides[get_current_user] = lambda: caller.user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        yield client


async def _user(db, name: str) -> User:
    user = User(email=f"{name}-{uuid.uuid4().hex[:6]}@example.com", hashed_password="x", full_name=name, role="user")
    db.add(user)
    await db.flush()
    return user


async def _integration(db, owner: User, provider: str, name: str, **columns) -> Integration:
    integration = Integration(user_id=owner.id, provider=provider, name=name, status="connected", **columns)
    db.add(integration)
    await db.flush()
    return integration


async def _world(db) -> dict:
    owner, other = await _user(db, "Owner"), await _user(db, "Other")
    mailbox = await _integration(
        db, owner, "microsoft", "Mailbox", auth_type="oauth", access_token_enc=encrypt_value("graph-token")
    )
    signatures = await _integration(
        db, owner, SOURCE, "Signatures", auth_type="api_key", api_key_enc=encrypt_value("signature-key")
    )
    private = await _integration(
        db, other, SOURCE, "Other's signatures", auth_type="api_key", api_key_enc=encrypt_value("signature-key")
    )
    chat = await _integration(db, owner, "slack", "Chat", auth_type="oauth", access_token_enc=encrypt_value("t"))
    bindings = {"microsoft": [str(mailbox.id)], SOURCE: [str(signatures.id)], "slack": [str(chat.id)]}
    first = Agent(name="A", slug=f"a-{uuid.uuid4().hex[:8]}", user_id=owner.id, integration_bindings=bindings)
    second = Agent(name="B", slug=f"b-{uuid.uuid4().hex[:8]}", user_id=owner.id, integration_bindings=bindings)
    db.add_all([first, second])
    await db.commit()
    return {
        "owner": owner, "other": other, "mailbox": mailbox, "signatures": signatures,
        "private": private, "chat": chat, "first": first, "second": second,
    }


async def _send_as(scratch, agent_id, owner: User, sent: list[dict]) -> str:
    """Send one email with the agent's Outlook tools; the body Graph received."""
    async with scratch.sessions() as db:
        agent = await db.get(Agent, agent_id)
        _definitions, executor = await build_integration_tools(
            db,
            ToolBindings(agent.integration_bindings, basis=BindingBasis.CALLER_CHECKED, reason="test"),
            context={"user_id": str(owner.id), SIGNATURE_CONTEXT_KEY: agent.integration_signatures},
        )
    sent.clear()
    result = await executor.execute_tool(
        ToolCall(id="c1", name="send_microsoft_email", arguments={"to": "b@example.com", "subject": "Hi", "body": "Hello"})
    )
    assert not result.is_error, result.content
    (payload,) = sent
    return payload["message"]["body"]["content"]


async def test_the_settings_are_written_checked_and_shown_to_a_non_admin():
    async with _scratch("email_signature_settings_rest") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
        caller = Caller()
        caller.user = world["owner"]
        mailbox, path = world["mailbox"], f"/api/v1/integrations/{world['mailbox'].id}"
        async with _client(scratch.sessions, caller) as client:
            listed = (await client.get(path)).json()
            assert listed["signature"] == {"enabled": True, "integration_id": None}
            card = (await client.get(f"{path}/email-signature")).json()
            assert card["resolved"]["integration_name"] == "Signatures" and card["can_manage"] is True
            assert [o["name"] for o in card["signature_integrations"]] == ["Signatures"]

            refused = await client.put(path, json={"signature_integration_id": str(world["private"].id)})
            assert refused.status_code == 400, refused.text
            refused = await client.put(path, json={"signature_integration_id": str(world["chat"].id)})
            assert refused.status_code == 400 and "does not hold email signatures" in refused.text
            refused = await client.put(f"/api/v1/integrations/{world['chat'].id}", json={"signature_enabled": False})
            assert refused.status_code == 400 and "send email" in refused.text
            assert (await client.get(f"/api/v1/integrations/{world['chat'].id}/email-signature")).status_code == 404

            saved = await client.put(
                path, json={"signature_enabled": False, "signature_integration_id": str(world["signatures"].id)}
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["signature"] == {"enabled": False, "integration_id": str(world["signatures"].id)}
            card = (await client.get(f"{path}/email-signature")).json()
            assert card["enabled"] is False and card["integration_name"] == "Signatures"
            assert card["resolved"]["off_by"] == "integration"
            back = await client.put(path, json={"signature_enabled": True, "signature_integration_id": None})
            assert back.json()["signature"] == {"enabled": True, "integration_id": None}

            agent_path = f"/api/v1/agents/{world['second'].id}"
            unbound = await client.put(
                agent_path, json={"integration_signatures": {str(uuid.uuid4()): {"signature_mode": "off"}}}
            )
            assert unbound.status_code == 422 and "does not bind" in unbound.text
            silent = await client.put(agent_path, json={"integration_signatures": {str(world["chat"].id): "off"}})
            assert silent.status_code == 422 and "send no email" in silent.text
            hidden = await client.put(
                agent_path,
                json={
                    "integration_signatures": {
                        str(mailbox.id): {"signature_mode": "on", "signature_integration_id": str(world["private"].id)}
                    }
                },
            )
            assert hidden.status_code == 400, hidden.text
            saved = await client.put(agent_path, json={"integration_signatures": {str(mailbox.id): "off"}})
            assert saved.status_code == 200, saved.text
            assert saved.json()["integration_signatures"] == {str(mailbox.id): {"signature_mode": "off"}}

            rows = (await client.get(f"{agent_path}/email-signatures")).json()["bindings"]
            assert [(r["integration_name"], r["signature_mode"]) for r in rows] == [("Mailbox", "off")]
            assert rows[0]["inherited"]["integration_name"] == "Signatures" and rows[0]["inherited"]["enabled"]
            assert rows[0]["effective"] == {
                "enabled": False, "integration_id": None, "integration_name": None, "off_by": "binding", "missing": None,
            }
            first_rows = (await client.get(f"/api/v1/agents/{world['first'].id}/email-signatures")).json()["bindings"]
            assert first_rows[0]["signature_mode"] == "inherit" and first_rows[0]["effective"]["enabled"]

            # Unbinding the mailbox drops its override.
            kept = {k: v for k, v in world["second"].integration_bindings.items() if k != "microsoft"}
            unbound = await client.put(agent_path, json={"integration_bindings": kept})
            assert unbound.status_code == 200, unbound.text
            assert unbound.json()["integration_signatures"] is None

            caller.user = world["other"]
            assert (await client.get(f"{path}/email-signature")).status_code == 404


async def test_a_run_signs_as_the_settings_say(_services):
    sent = _services
    async with _scratch("email_signature_settings_run") as scratch:
        async with scratch.sessions() as db:
            world = await _world(db)
            second = await db.get(Agent, world["second"].id)
            second.integration_signatures = {str(world["mailbox"].id): {"signature_mode": "off"}}
            await db.commit()
        owner, block = world["owner"], '<div id="Signature"><p>Signed for a@example.com</p></div>'

        # Nothing configured, the signature integration bound alongside: signed as before.
        assert block in await _send_as(scratch, world["first"].id, owner, sent)
        # The same integrations, the binding override off on the other agent.
        assert "Signature" not in await _send_as(scratch, world["second"].id, owner, sent)

        async with scratch.sessions() as db:
            (await db.get(Integration, world["mailbox"].id)).signature_enabled = False
            second = await db.get(Agent, world["second"].id)
            second.integration_signatures = {str(world["mailbox"].id): {"signature_mode": "on"}}
            await db.commit()
        # The integration setting off: an agent that inherits signs nothing, one that overrides it on signs.
        assert "Signature" not in await _send_as(scratch, world["first"].id, owner, sent)
        assert block in await _send_as(scratch, world["second"].id, owner, sent)

        async with scratch.sessions() as db:
            (await db.get(Integration, world["mailbox"].id)).signature_enabled = True
            first = await db.get(Agent, world["first"].id)
            first.integration_bindings = {"microsoft": [str(world["mailbox"].id)]}
            await db.delete(await db.get(Integration, world["signatures"].id))
            await db.delete(await db.get(Integration, world["private"].id))
            await db.commit()
        # No signature integration at all: the send succeeds unsigned, and the lookup says how to add one.
        assert "Signature" not in await _send_as(scratch, world["first"].id, owner, sent)
        async with scratch.sessions() as db:
            agent = await db.get(Agent, world["first"].id)
            _definitions, executor = await build_integration_tools(
                db, ToolBindings(agent.integration_bindings, basis=BindingBasis.CALLER_CHECKED, reason="test"), context={"user_id": str(owner.id)}
            )
        answer = await executor.execute_tool(ToolCall(id="c2", name="get_microsoft_email_signature", arguments={}))
        assert "no signature integration is available" in answer.content
        assert "Email signature setting" in answer.content
