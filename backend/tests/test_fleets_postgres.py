"""Fleets on PostgreSQL: the API, the membership rules, the boundary.

What is pinned:

* **One home fleet**, by the partial unique index and by the route's 409
  naming the other fleet; a connected link is not a home.
* **A workspace per fleet**: created with the fleet when none is named; a
  workspace hosting a fleet refuses its delete with a reason.
* **Exposure and the card**: the orchestrator is always exposed; the card
  lists the exposed agents only.
* **The boundary**: a NEW handover to a non-exposed member of another fleet
  is refused when it is written; one configured before the exposure changed
  is kept and reported, never broken.
* **Visibility**: every request runs as a NON-admin (``user_visible_filter``
  short-circuits for admins, so an admin-only test proves nothing).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import fleets as fleets_api
from app.api.v1 import workspaces as workspaces_api
from app.api.v1.helpers import user_visible_filter
from app.core.database import get_db
from app.models.agent import Agent
from app.models.fleet import FLEET_ROLE_MEMBER, Fleet, FleetMember
from app.models.user import User
from app.models.workspace import Workspace
from app.services.fleets import (
    FleetActionRefusedError,
    ensure_workspace_can_host,
    fleet_boundary_violations,
    refused_cross_fleet_handovers,
)
from app.services.workspaces import (
    WorkspaceActionRefusedError,
    hosted_fleets_of,
    lock_workspace_for_hosting,
    remove_workspace,
    set_workspace_archived,
)
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

BASE = "/api/v1/fleets"
WORKSPACES = "/api/v1/workspaces"


@asynccontextmanager
async def _database():
    async with scratch_database(
        "fleets", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db, db.sessions() as session:
        yield session


@asynccontextmanager
async def _client(session, user: User) -> AsyncIterator[httpx.AsyncClient]:
    api = FastAPI()
    api.include_router(fleets_api.router, prefix=BASE)
    api.include_router(workspaces_api.router, prefix=WORKSPACES)
    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[deps.require_agentic_ai_licensed] = lambda: None

    async def _get_db():
        yield session
        await session.commit()

    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield client


async def _user(session, name: str) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        full_name=name,
        hashed_password="x",
        role="user",
    )
    session.add(user)
    await session.flush()
    return user


async def _agent(session, owner: User, name: str, *, handover_to: list[uuid.UUID] | None = None) -> Agent:
    agent = Agent(
        id=uuid.uuid4(),
        user_id=owner.id,
        name=name,
        slug=f"{name.lower()}-{uuid.uuid4().hex[:6]}",
        handover_agent_ids=[str(a) for a in handover_to or []],
    )
    session.add(agent)
    await session.flush()
    return agent


async def _fleet(client: httpx.AsyncClient, name: str, **body) -> dict:
    response = await client.post(BASE, json={"name": name, **body})
    assert response.status_code == 201, response.text
    return response.json()


async def _member(client: httpx.AsyncClient, fleet_id: str, agent: Agent, **body) -> httpx.Response:
    return await client.post(f"{BASE}/{fleet_id}/members", json={"agent_id": str(agent.id), **body})


async def test_a_fleet_is_created_with_its_workspace_and_lists_for_its_owner_only() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        bob = await _user(session, "bob")
        await session.commit()
        async with _client(session, alice) as client:
            fleet = await _fleet(client, "CEO Office", description="The desks")
            assert fleet["slug"] == "ceo-office"
            assert fleet["workspace_name"] == "CEO Office"
            assert fleet["member_count"] == 0
            assert fleet["can_manage"] is True
            second = await _fleet(client, "CEO Office")
            assert second["slug"] == "ceo-office-2"
            listed = (await client.get(BASE)).json()
            assert {f["id"] for f in listed} == {fleet["id"], second["id"]}
        async with _client(session, bob) as client:
            assert (await client.get(BASE)).json() == []
            assert (await client.get(f"{BASE}/{fleet['id']}")).status_code == 404


async def test_a_fleet_binds_to_a_named_workspace_and_that_workspace_cannot_be_deleted() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        workspace = Workspace(id=uuid.uuid4(), user_id=alice.id, name="Ops", slug="ops")
        session.add(workspace)
        await session.commit()
        async with _client(session, alice) as client:
            fleet = await _fleet(client, "Ops fleet", workspace_id=str(workspace.id))
            assert fleet["workspace_id"] == str(workspace.id)
            listed = (await client.get(BASE, params={"workspace_id": str(workspace.id)})).json()
            assert [f["id"] for f in listed] == [fleet["id"]]
        with pytest.raises(WorkspaceActionRefusedError) as exc:
            await remove_workspace(session, workspace)
        assert exc.value.status_code == 409
        assert "fleet" in exc.value.detail


async def test_a_workspace_names_the_fleets_it_hosts_to_who_can_see_them() -> None:
    """List and get carry hosted_fleets; a fleet the reader cannot see
    is counted (it still blocks the delete) but never named."""
    async with _database() as session:
        alice = await _user(session, "alice")
        bob = await _user(session, "bob")
        shared = Workspace(
            id=uuid.uuid4(), user_id=alice.id, name="Shared", slug="shared",
            shared_user_ids=[str(bob.id)], shared_user_permissions={str(bob.id): "read_only"},
        )
        session.add(shared)
        await session.commit()
        async with _client(session, alice) as client:
            fleet = await _fleet(client, "Ops fleet", workspace_id=str(shared.id))
            listed = {w["id"]: w for w in (await client.get(WORKSPACES)).json()}
            row = listed[str(shared.id)]
            assert row["hosted_fleets"] == [
                {"id": fleet["id"], "name": "Ops fleet", "slug": "ops-fleet", "is_archived": False}
            ]
            assert (row["hosted_fleet_count"], row["live_hosted_fleet_count"]) == (1, 1)
            # The default workspace, created by the listing, hosts nothing.
            assert all(w["hosted_fleets"] == [] for wid, w in listed.items() if wid != str(shared.id))
            one = (await client.get(f"{WORKSPACES}/{shared.id}")).json()
            assert [f["name"] for f in one["hosted_fleets"]] == ["Ops fleet"]
        async with _client(session, bob) as client:
            seen = (await client.get(f"{WORKSPACES}/{shared.id}")).json()
            assert seen["hosted_fleets"] == []
            assert seen["hosted_fleet_count"] == 1
        # One query for many workspaces, the visibility clause applied in it.
        hosted = await hosted_fleets_of(
            session, [shared.id, uuid.uuid4()], user_visible_filter(Fleet, bob.id, [])
        )
        assert list(hosted) == [shared.id]
        assert hosted[shared.id].total == 1 and hosted[shared.id].visible == ()


async def test_the_hosting_lock_rereads_a_workspace_archived_since_it_was_loaded() -> None:
    """Archive and bind serialise on the workspace row; the bind
    validates the row as it is under the lock, never a stale copy."""
    async with _database() as session:
        alice = await _user(session, "alice")
        workspace = Workspace(id=uuid.uuid4(), user_id=alice.id, name="Ops", slug="ops")
        session.add(workspace)
        await session.commit()
        # Archived behind the ORM's back, as a concurrent archive would.
        await session.execute(
            Workspace.__table__.update().where(Workspace.id == workspace.id).values(is_archived=True)
        )
        assert workspace.is_archived is False  # the stale identity-map copy
        await lock_workspace_for_hosting(session, workspace)
        assert workspace.is_archived is True
        with pytest.raises(FleetActionRefusedError):
            ensure_workspace_can_host(False, workspace)
        async with _client(session, alice) as client:
            refused = await client.post(BASE, json={"name": "Late", "workspace_id": str(workspace.id)})
            assert refused.status_code == 400
            assert refused.json()["detail"] == "That workspace is archived."


async def test_a_workspace_hosting_a_live_fleet_is_not_archived_and_survives_the_fleet() -> None:
    """Archive refused while a live fleet coordinates there (REST and
    the shared rule); allowed once the fleet is archived; deleting the fleet
    keeps the workspace, which then deletes."""
    async with _database() as session:
        alice = await _user(session, "alice")
        await session.commit()
        async with _client(session, alice) as client:
            fleet = await _fleet(client, "CEO Office")
            ws_id = fleet["workspace_id"]
            refused = await client.put(f"{WORKSPACES}/{ws_id}", json={"is_archived": True})
            assert refused.status_code == 409
            assert refused.json()["detail"] == "This workspace hosts 1 fleet: archive or move it first."
            workspace = await session.get(Workspace, uuid.UUID(ws_id))
            assert workspace is not None and workspace.is_archived is False
            with pytest.raises(WorkspaceActionRefusedError):
                await set_workspace_archived(session, workspace, True)

            archived_fleet = await client.put(f"{BASE}/{fleet['id']}", json={"is_archived": True})
            assert archived_fleet.status_code == 200, archived_fleet.text
            row = (await client.get(f"{WORKSPACES}/{ws_id}")).json()
            assert (row["hosted_fleet_count"], row["live_hosted_fleet_count"]) == (1, 0)
            assert row["hosted_fleets"][0]["is_archived"] is True
            allowed = await client.put(f"{WORKSPACES}/{ws_id}", json={"is_archived": True})
            assert allowed.status_code == 200, allowed.text
            restored = await client.put(f"{WORKSPACES}/{ws_id}", json={"is_archived": False})
            assert restored.status_code == 200, restored.text

            # Still hosting (an archived fleet): not deleted.
            assert (await client.delete(f"{WORKSPACES}/{ws_id}")).status_code == 409
            assert (await client.delete(f"{BASE}/{fleet['id']}")).status_code == 204
            kept = await client.get(f"{WORKSPACES}/{ws_id}")
            assert kept.status_code == 200
            assert kept.json()["hosted_fleets"] == []
            assert (await client.delete(f"{WORKSPACES}/{ws_id}")).status_code == 204


async def test_one_home_fleet_per_agent_and_connected_is_not_a_home() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        ea = await _agent(session, alice, "EA")
        await session.commit()
        async with _client(session, alice) as client:
            first = await _fleet(client, "First")
            second = await _fleet(client, "Second")
            added = await _member(client, first["id"], ea)
            assert added.status_code == 201, added.text
            refused = await _member(client, second["id"], ea)
            assert refused.status_code == 409
            assert "First" in refused.json()["detail"]
            connected = await _member(
                client, second["id"], ea, role="connected", collaboration_instruction="Ask for a brief"
            )
            assert connected.status_code == 201, connected.text
            assert connected.json()["role"] == "connected"
            assert connected.json()["name"] == "EA"
            again = await _member(client, first["id"], ea, role="connected")
            assert again.status_code == 409
        # The index is the authority, whatever a route checks first.
        session.add(FleetMember(fleet_id=uuid.UUID(second["id"]), agent_id=ea.id, role=FLEET_ROLE_MEMBER))
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


async def test_hierarchy_orchestrator_exposure_and_card() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        ea = await _agent(session, alice, "EA")
        dev = await _agent(session, alice, "Developer")
        ux = await _agent(session, alice, "UX")
        await session.commit()
        async with _client(session, alice) as client:
            fleet = await _fleet(client, "CEO Office", skills=[{"name": "Run the office"}])
            fid = fleet["id"]
            ea_row = (await _member(client, fid, ea)).json()
            dev_row = (await _member(client, fid, dev, parent_member_id=ea_row["id"])).json()
            ux_row = (await _member(client, fid, ux, parent_member_id=dev_row["id"])).json()
            assert ux_row["parent_member_id"] == dev_row["id"]
            loop = await client.patch(f"{BASE}/{fid}/members/{ea_row['id']}", json={"parent_member_id": ux_row["id"]})
            assert loop.status_code == 400
            # A role label is a label on the membership row: set, trimmed, read back, cleared.
            assert dev_row["role_label"] == ""
            labelled = await client.patch(f"{BASE}/{fid}/members/{dev_row['id']}", json={"role_label": "  Release manager "})
            assert labelled.status_code == 200, labelled.text
            assert labelled.json()["role_label"] == "Release manager"
            listed = (await client.get(f"{BASE}/{fid}/members")).json()
            assert {r["id"]: r["role_label"] for r in listed}[dev_row["id"]] == "Release manager"
            too_long = await client.patch(f"{BASE}/{fid}/members/{dev_row['id']}", json={"role_label": "x" * 101})
            assert too_long.status_code == 422
            cleared = await client.patch(f"{BASE}/{fid}/members/{dev_row['id']}", json={"role_label": ""})
            assert cleared.json()["role_label"] == ""
            updated = await client.put(f"{BASE}/{fid}", json={"orchestrator_agent_id": str(ea.id)})
            assert updated.status_code == 200, updated.text
            assert updated.json()["exposed_agent_ids"] == [str(ea.id)]
            not_member = await client.put(f"{BASE}/{fid}/exposure", json={"agent_ids": [str(uuid.uuid4())]})
            assert not_member.status_code == 400
            exposed = await client.put(f"{BASE}/{fid}/exposure", json={"agent_ids": [str(dev.id)]})
            assert exposed.json() == [str(ea.id), str(dev.id)]
            # The read tells the orchestrator's implicit exposure from a stored flag.
            flags = {
                r["agent_id"]: (r["is_exposed"], r["exposed_explicitly"])
                for r in (await client.get(f"{BASE}/{fid}/members")).json()
            }
            assert flags[str(ea.id)] == (True, False)
            assert flags[str(dev.id)] == (True, True)
            card = (await client.get(f"{BASE}/{fid}/card")).json()
            assert card["entry_agent"]["name"] == "EA"
            assert [a["name"] for a in card["exposed_agents"]] == ["EA", "Developer"]
            assert card["workspace_name"] == "CEO Office"
            assert card["skills"][0]["name"] == "Run the office"
            assert set(card["accepted_handover_types"]) == {"consult", "delegate", "transfer"}
            # Removing the orchestrator clears it; deleting the fleet keeps the agents.
            assert (await client.delete(f"{BASE}/{fid}/members/{ea_row['id']}")).status_code == 204
            assert (await client.get(f"{BASE}/{fid}")).json()["orchestrator_agent_id"] is None
            assert (await client.delete(f"{BASE}/{fid}")).status_code == 204
        assert (await session.execute(select(Agent).where(Agent.id == dev.id))).scalar_one() is not None


async def test_an_archived_fleet_keeps_its_members_until_it_is_restored() -> None:
    """Archiving freezes members, connected rows and exposure (409); the settings stay open to restore it."""
    async with _database() as session:
        alice = await _user(session, "alice")
        lead = await _agent(session, alice, "Lead")
        dev = await _agent(session, alice, "Developer")
        newcomer = await _agent(session, alice, "Newcomer")
        await session.commit()
        async with _client(session, alice) as client:
            fid = (await _fleet(client, "Frozen"))["id"]
            lead_row = (await _member(client, fid, lead)).json()
            dev_row = (await _member(client, fid, dev, parent_member_id=lead_row["id"])).json()
            archived = await client.put(f"{BASE}/{fid}", json={"is_archived": True})
            assert archived.status_code == 200, archived.text
            frozen = "This fleet is archived: restore it to change its members."
            refusals = [
                await _member(client, fid, newcomer, role_label="Analyst"),
                await _member(client, fid, newcomer, role="connected"),
                await client.patch(f"{BASE}/{fid}/members/{dev_row['id']}", json={"role_label": "Release manager"}),
                await client.delete(f"{BASE}/{fid}/members/{dev_row['id']}"),
                await client.put(f"{BASE}/{fid}/exposure", json={"agent_ids": [str(dev.id)]}),
            ]
            for refused in refusals:
                assert refused.status_code == 409, refused.text
                assert refused.json()["detail"] == frozen
            listed = (await client.get(f"{BASE}/{fid}/members")).json()
            assert {r["agent_id"] for r in listed} == {str(lead.id), str(dev.id)}
            assert {r["id"]: r["role_label"] for r in listed}[dev_row["id"]] == ""
            # The settings stay open: renaming and restoring an archived fleet both work.
            renamed = await client.put(f"{BASE}/{fid}", json={"name": "Frozen still"})
            assert renamed.status_code == 200, renamed.text
            restored = await client.put(f"{BASE}/{fid}", json={"is_archived": False})
            assert restored.status_code == 200, restored.text
            labelled = await client.patch(f"{BASE}/{fid}/members/{dev_row['id']}", json={"role_label": "Release manager"})
            assert labelled.status_code == 200, labelled.text
            assert labelled.json()["role_label"] == "Release manager"
            # Deleting an archived fleet stays open as well.
            assert (await client.put(f"{BASE}/{fid}", json={"is_archived": True})).status_code == 200
            assert (await client.delete(f"{BASE}/{fid}")).status_code == 204


async def test_member_defaults_are_inherited_and_a_bulk_call_is_all_or_nothing() -> None:
    """R1 of #9162: a connected row without its own setting reads the fleet default."""
    async with _database() as session:
        alice = await _user(session, "alice")
        lead = await _agent(session, alice, "Lead")
        peers = [await _agent(session, alice, f"Peer{i}") for i in range(10)]
        await session.commit()
        async with _client(session, alice) as client:
            fleet = await _fleet(client, "Defaults")
            fid = fleet["id"]
            assert fleet["member_defaults"] == {
                "context_inclusion": "none",
                "collaboration_instruction": "",
                "organization_scope": {"mode": "primary"},
            }
            home = (await _member(client, fid, lead)).json()
            rows = [(await _member(client, fid, p, role="connected")).json() for p in peers]
            assert all(r["overridden"] == [] and r["context_inclusion"] == "none" for r in rows)

            updated = await client.put(
                f"{BASE}/{fid}",
                json={"member_defaults": {"context_inclusion": "summary", "collaboration_instruction": "Ask"}},
            )
            assert updated.status_code == 200, updated.text
            listed = (await client.get(f"{BASE}/{fid}/members")).json()
            connected = [m for m in listed if m["role"] == "connected"]
            assert {(m["context_inclusion"], m["collaboration_instruction"]) for m in connected} == {("summary", "Ask")}

            ids = [r["id"] for r in rows]
            bulk = await client.post(f"{BASE}/{fid}/members/bulk", json={"member_ids": ids, "set": {"context_inclusion": "full"}})
            assert bulk.status_code == 200, bulk.text
            assert {(m["context_inclusion"], tuple(m["overridden"])) for m in bulk.json()} == {
                ("full", ("context_inclusion",))
            }

            # A home member in the selection refuses the whole call: nothing changes.
            refused = await client.post(
                f"{BASE}/{fid}/members/bulk",
                json={"member_ids": [ids[0], home["id"]], "set": {"context_inclusion": None}},
            )
            assert refused.status_code == 400, refused.text
            assert home["id"] in refused.json()["detail"]
            first = next(m for m in (await client.get(f"{BASE}/{fid}/members")).json() if m["id"] == ids[0])
            assert first["context_inclusion"] == "full"

            # null resets: the row inherits the fleet default again.
            reset = await client.post(
                f"{BASE}/{fid}/members/bulk", json={"member_ids": ids[:1], "set": {"context_inclusion": None}}
            )
            assert reset.status_code == 200, reset.text
            assert (reset.json()[0]["context_inclusion"], reset.json()[0]["overridden"]) == ("summary", [])


async def _rows(session, fleet_id: str) -> dict[uuid.UUID, FleetMember]:
    """The home rows of a fleet by agent, read afresh over the session's identity map."""
    statement = (
        select(FleetMember)
        .where(FleetMember.fleet_id == uuid.UUID(fleet_id), FleetMember.role == FLEET_ROLE_MEMBER)
        .execution_options(populate_existing=True)
    )
    return {r.agent_id: r for r in (await session.execute(statement)).scalars().all()}


async def test_a_member_moves_to_another_fleet_in_one_transaction() -> None:
    """The source row leaves and the target row joins together; the agent is not written."""
    async with _database() as session:
        alice = await _user(session, "alice")
        lead = await _agent(session, alice, "Lead")
        analyst = await _agent(session, alice, "Analyst", handover_to=[lead.id])
        await session.commit()
        async with _client(session, alice) as client:
            source = await _fleet(client, "Source")
            target = await _fleet(client, "Target")
            lead_row = (await _member(client, source["id"], lead)).json()
            analyst_row = (
                await _member(client, source["id"], analyst, parent_member_id=lead_row["id"], is_exposed=True,
                              role_label="Release manager")
            ).json()
            await client.put(f"{BASE}/{source['id']}", json={"orchestrator_agent_id": str(lead.id)})

            moved = await client.post(
                f"{BASE}/{source['id']}/members/{analyst_row['id']}/move", json={"target_fleet_id": target["id"]}
            )
            assert moved.status_code == 200, moved.text
            body = moved.json()
            assert (body["fleet_id"], body["agent_id"], body["role"]) == (target["id"], str(analyst.id), "member")
            # It joins at top level, not exposed, with no role label.
            assert (body["parent_member_id"], body["is_exposed"], body["role_label"]) == (None, False, "")
            assert set(await _rows(session, source["id"])) == {lead.id}
            assert set(await _rows(session, target["id"])) == {analyst.id}
            # Not the orchestrator: the source keeps its own.
            assert (await client.get(f"{BASE}/{source['id']}")).json()["orchestrator_agent_id"] == str(lead.id)

            # The orchestrator moves too: the source's orchestrator is cleared, only now.
            moved_lead = await client.post(
                f"{BASE}/{source['id']}/members/{lead_row['id']}/move", json={"target_fleet_id": target["id"]}
            )
            assert moved_lead.status_code == 200, moved_lead.text
            assert (await client.get(f"{BASE}/{source['id']}")).json()["orchestrator_agent_id"] is None
            assert set(await _rows(session, target["id"])) == {lead.id, analyst.id}
        # The agent's own configuration is untouched: its handovers stay.
        refreshed = (
            await session.execute(select(Agent).where(Agent.id == analyst.id).execution_options(populate_existing=True))
        ).scalar_one()
        assert refreshed.handover_agent_ids == [str(lead.id)]


async def test_a_refused_move_leaves_the_member_where_it_was() -> None:
    """Every refusal leaves the source row, its parent, exposure, role label and the orchestrator."""
    async with _database() as session:
        alice = await _user(session, "alice")
        bob = await _user(session, "bob")
        lead = await _agent(session, alice, "Lead")
        analyst = await _agent(session, alice, "Analyst")
        await session.commit()
        async with _client(session, bob) as client:
            foreign = await _fleet(client, "Bob's")
        async with _client(session, alice) as client:
            source = await _fleet(client, "Source")
            archived = await _fleet(client, "Archived")
            connects = await _fleet(client, "Connects")
            lead_row = (await _member(client, source["id"], lead)).json()
            analyst_row = (
                await _member(client, source["id"], analyst, parent_member_id=lead_row["id"], is_exposed=True,
                              role_label="Release manager")
            ).json()
            await client.put(f"{BASE}/{source['id']}", json={"orchestrator_agent_id": str(lead.id)})
            assert (await client.put(f"{BASE}/{archived['id']}", json={"is_archived": True})).status_code == 200
            # The analyst is a connected agent of `Connects`: joining there is refused AFTER the source row left.
            linked = await _member(client, connects["id"], analyst, role="connected")
            assert linked.status_code == 201, linked.text
            connected_row = (await _member(client, connects["id"], lead, role="connected")).json()

            def move(fleet_id: str, member_id: str, target_id: str):
                return client.post(f"{BASE}/{fleet_id}/members/{member_id}/move", json={"target_fleet_id": target_id})

            cases = [
                (await move(source["id"], analyst_row["id"], archived["id"]), 409, "archived"),
                (await move(source["id"], analyst_row["id"], connects["id"]), 409, "connected agent of this fleet"),
                (await move(source["id"], lead_row["id"], connects["id"]), 409, "connected agent of this fleet"),
                (await move(source["id"], analyst_row["id"], foreign["id"]), 404, "Fleet not found"),
                (await move(source["id"], analyst_row["id"], source["id"]), 400, "already in this fleet"),
                (await move(connects["id"], connected_row["id"], source["id"]), 400, "Only a home member"),
                (await move(source["id"], str(uuid.uuid4()), connects["id"]), 404, "Fleet member not found"),
            ]
            for response, status_code, words in cases:
                assert response.status_code == status_code, response.text
                assert words in response.json()["detail"], response.text
            rows = await _rows(session, source["id"])
            assert set(rows) == {lead.id, analyst.id}
            kept = rows[analyst.id]
            assert (kept.id, kept.parent_member_id, kept.is_exposed, kept.role_label) == (
                uuid.UUID(analyst_row["id"]), uuid.UUID(lead_row["id"]), True, "Release manager"
            )
            assert (await client.get(f"{BASE}/{source['id']}")).json()["orchestrator_agent_id"] == str(lead.id)
            assert await _rows(session, connects["id"]) == {}

            # An archived source refuses too.
            assert (await client.put(f"{BASE}/{source['id']}", json={"is_archived": True})).status_code == 200
            free = await _fleet(client, "Free")
            frozen = await move(source["id"], analyst_row["id"], free["id"])
            assert frozen.status_code == 409, frozen.text
            assert frozen.json()["detail"] == "This fleet is archived: restore it to change its members."
        # A read-only grantee of the source cannot move its members.
        async with _client(session, alice) as client:
            assert (await client.put(f"{BASE}/{source['id']}", json={"is_archived": False})).status_code == 200
            shared = await client.put(
                f"{BASE}/{source['id']}",
                json={"shared_user_ids": [str(bob.id)], "shared_user_permissions": {str(bob.id): "read_only"}},
            )
            assert shared.status_code == 200, shared.text
        async with _client(session, bob) as client:
            refused = await client.post(
                f"{BASE}/{source['id']}/members/{analyst_row['id']}/move", json={"target_fleet_id": foreign["id"]}
            )
            assert refused.status_code == 403, refused.text
        assert set(await _rows(session, source["id"])) == {lead.id, analyst.id}


async def test_a_new_handover_to_a_hidden_member_of_another_fleet_is_refused_an_old_one_is_flagged() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        orchestrator = await _agent(session, alice, "Orchestrator")
        hidden = await _agent(session, alice, "Hidden")
        outsider = await _agent(session, alice, "Outsider", handover_to=[])
        await session.commit()
        async with _client(session, alice) as client:
            fleet = await _fleet(client, "Closed")
            await _member(client, fleet["id"], orchestrator)
            await _member(client, fleet["id"], hidden)
            await client.put(f"{BASE}/{fleet['id']}", json={"orchestrator_agent_id": str(orchestrator.id)})
        # The orchestrator is reachable, the hidden member is not.
        await refused_cross_fleet_handovers(
            session, source_agent_id=outsider.id, handover_agent_ids=[str(orchestrator.id)]
        )
        with pytest.raises(FleetActionRefusedError) as exc:
            await refused_cross_fleet_handovers(
                session, source_agent_id=outsider.id, handover_agent_ids=[str(hidden.id)]
            )
        assert exc.value.status_code == 422
        assert "Hidden" in exc.value.detail and "Closed" in exc.value.detail
        # Inside the fleet, handovers stay free.
        await refused_cross_fleet_handovers(
            session, source_agent_id=orchestrator.id, handover_agent_ids=[str(hidden.id)]
        )
        # A handover configured before is kept: an unrelated edit re-sends it.
        await refused_cross_fleet_handovers(
            session,
            source_agent_id=outsider.id,
            handover_agent_ids=[str(hidden.id)],
            baseline=[str(hidden.id)],
        )
        outsider.handover_agent_ids = [str(hidden.id)]
        await session.flush()
        fleet_row = (await session.execute(select(Fleet).where(Fleet.id == uuid.UUID(fleet["id"])))).scalar_one()
        violations = await fleet_boundary_violations(session, fleet_row)
        assert [(v.source_agent_id, v.target_agent_id) for v in violations] == [(outsider.id, hidden.id)]


async def test_a_read_only_viewer_cannot_change_a_fleet_and_an_unseen_member_is_a_placeholder() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        bob = await _user(session, "bob")
        private = await _agent(session, alice, "Private")
        await session.commit()
        async with _client(session, alice) as client:
            fleet = await _fleet(client, "Shared")
            await _member(client, fleet["id"], private)
            shared = await client.put(
                f"{BASE}/{fleet['id']}",
                json={"shared_user_ids": [str(bob.id)], "shared_user_permissions": {str(bob.id): "read_only"}},
            )
            assert shared.status_code == 200, shared.text
        async with _client(session, bob) as client:
            members = (await client.get(f"{BASE}/{fleet['id']}/members")).json()
            assert [m["name"] for m in members] == [""]
            assert (await client.put(f"{BASE}/{fleet['id']}", json={"name": "Mine"})).status_code == 403
            assert (await _member(client, fleet["id"], private)).status_code == 403


async def test_the_agent_page_reads_its_home_fleet_and_the_fleets_that_connect_it() -> None:
    """``GET /fleets/by-agent/{id}``: home, parent, label, connected; visibility both ways."""
    async with _database() as session:
        alice = await _user(session, "alice")
        bob = await _user(session, "bob")
        ea = await _agent(session, alice, "EA")
        dev = await _agent(session, alice, "Developer")
        loner = await _agent(session, alice, "Loner")
        await session.commit()
        async with _client(session, alice) as client:
            office = await _fleet(client, "CEO Office")
            ea_row = (await _member(client, office["id"], ea)).json()
            dev_row = (await _member(client, office["id"], dev, parent_member_id=ea_row["id"])).json()
            await client.patch(f"{BASE}/{office['id']}/members/{dev_row['id']}", json={"role_label": "Builder"})
            await client.put(f"{BASE}/{office['id']}", json={"orchestrator_agent_id": str(ea.id)})
            design = await _fleet(client, "Design")
            linked = await _member(
                client, design["id"], dev, role="connected",
                collaboration_instruction="Ask for builds", context_inclusion="summary",
            )
            assert linked.status_code == 201, linked.text

            seen = (await client.get(f"{BASE}/by-agent/{dev.id}")).json()
            assert seen["agent_id"] == str(dev.id)
            home = seen["home"]
            assert (home["fleet_id"], home["fleet_name"]) == (office["id"], "CEO Office")
            assert (home["parent_member_id"], home["parent_name"]) == (ea_row["id"], "EA")
            assert home["role_label"] == "Builder"
            assert (home["is_orchestrator"], home["is_exposed"], home["can_manage"]) == (False, False, True)
            links = [
                (c["fleet_name"], c["collaboration_instruction"], c["context_inclusion"]) for c in seen["connected_to"]
            ]
            assert links == [("Design", "Ask for builds", "summary")]
            top = (await client.get(f"{BASE}/by-agent/{ea.id}")).json()["home"]
            assert (top["is_orchestrator"], top["is_exposed"], top["parent_member_id"]) == (True, True, None)
            assert (top["has_orchestrator"], home["has_orchestrator"]) == (True, True)
            playground = (await client.get(f"{BASE}/by-agent/{loner.id}")).json()
            assert (playground["home"], playground["home_hidden"], playground["connected_to"]) == (None, False, [])
            assert seen["home_hidden"] is False
            # Bob sees the Developer agent but no fleet: nothing is named to him.
            dev.shared_user_ids = [str(bob.id)]
            dev.shared_user_permissions = {str(bob.id): "read_only"}
            await session.flush()
        async with _client(session, bob) as client:
            unseen = (await client.get(f"{BASE}/by-agent/{dev.id}")).json()
            # Its home fleet is flagged, never named: not "in the playground".
            assert (unseen["home"], unseen["home_hidden"], unseen["connected_to"]) == (None, True, [])
            # An agent the caller cannot see answers 404, like the agent itself.
            assert (await client.get(f"{BASE}/by-agent/{ea.id}")).status_code == 404
            assert (await client.get(f"{BASE}/by-agent/not-a-uuid")).status_code == 404


async def test_the_fleet_views_draw_the_graph_tree_health_and_activity() -> None:
    """Phase 1b: the four reads on one fleet, as a non-admin owner."""
    async with _database() as session:
        alice = await _user(session, "alice")
        bob = await _user(session, "bob")
        dev = await _agent(session, alice, "Developer")
        ea = await _agent(session, alice, "EA", handover_to=[dev.id])
        outsider = await _agent(session, alice, "Outsider", handover_to=[dev.id])
        await session.commit()
        async with _client(session, alice) as client:
            fleet = await _fleet(client, "CEO Office")
            fid = fleet["id"]
            ea_row = (await _member(client, fid, ea)).json()
            await _member(client, fid, dev, parent_member_id=ea_row["id"])
            await client.put(f"{BASE}/{fid}", json={"orchestrator_agent_id": str(ea.id)})

            graph = (await client.get(f"{BASE}/{fid}/graph")).json()
            assert graph["orchestrator_agent_id"] == str(ea.id)
            kinds = {n["name"]: n["kind"] for n in graph["nodes"]}
            assert kinds == {"EA": "member", "Developer": "member", "Outsider": "external"}
            edges = {(e["source"], e["target"]): e for e in graph["edges"]}
            assert edges[(str(ea.id), str(dev.id))]["scope"] == "internal"
            inbound = edges[(str(outsider.id), str(dev.id))]
            # A handover from outside to a member the fleet does not expose:
            # kept, drawn ungoverned and flagged.
            assert (inbound["scope"], inbound["governed"], inbound["allowed"]) == ("inbound", False, False)

            tree = (await client.get(f"{BASE}/{fid}/hierarchy")).json()
            assert [(n["name"], n["depth"], n["is_orchestrator"]) for n in tree] == [
                ("EA", 0, True),
                ("Developer", 1, False),
            ]
            assert tree[1]["parent_agent_id"] == str(ea.id)

            health = (await client.get(f"{BASE}/{fid}/health")).json()
            assert health["status"] == "idle"
            assert [m["name"] for m in health["members"]] == ["EA", "Developer"]
            assert health["workspace_open_work_items"] == 0

            activity = (await client.get(f"{BASE}/{fid}/activity")).json()
            assert activity == {"fleet_id": fid, "runs": [], "delegations": []}
        # A fleet the caller cannot see answers 404 on every view.
        async with _client(session, bob) as client:
            for view in ("graph", "hierarchy", "health", "activity"):
                assert (await client.get(f"{BASE}/{fid}/{view}")).status_code == 404
