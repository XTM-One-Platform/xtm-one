"""Data sources refreshed on the server, against PostgreSQL (``dashboard_refresh``).

A source saved with ``refreshMode: server`` gets a snapshot the scheduler reads
as its publisher (the person who set it, decision D1), whoever has the page
open; viewers are shown that snapshot, with who it was read as and when; the
open pages are told; a ``manual`` source is read only when asked; a source the
writer cannot read is refused at save. A knowledge base document's table is
the source here: no platform is reached.

``integration``-marked; the temporary database is ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.custom_dashboard import CustomDashboardSourceSnapshot
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("dashboard_studio_enabled")]

TABLE = "# Weekly\n\n| severity | count |\n|---|---|\n| high | 3 |\n| low | 7 |\n\n# Notes\n\nNothing else.\n"


def _definition(kb_id, doc_id, **options) -> dict:
    source = {"type": "knowledge", "options": {"knowledge_base_id": str(kb_id), "document_id": str(doc_id), "section": "Weekly",
                                               "refresh": "15m", "refreshMode": "server", **options}}  # fmt: skip
    return {
        "version": 2, "title": "SOC", "dataSources": {"sev": source},
        "visualizations": {"chart": {"type": "xtm.table", "dataSources": {"primary": "sev"}}},
        "layout": {"tabs": {"items": [{"layoutId": "main", "label": "Main"}]},
                   "layoutDefinitions": {"main": {"structure": [{"item": "chart", "position": {"x": 0, "y": 0, "w": 6, "h": 4}}]}}},
    }  # fmt: skip


@asynccontextmanager
async def _world():
    async with scratch_database(
        "dashboard_refresh", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="owner@example.com", hashed_password="x", full_name="Owner", role="user")
            viewer = User(email="viewer@example.com", hashed_password="x", full_name="Viewer", role="user")
            db.add_all([owner, viewer])
            await db.flush()
            kb = KnowledgeBase(name="SOC data", user_id=owner.id)
            db.add(kb)
            await db.flush()
            doc = KnowledgeDocument(knowledge_base_id=kb.id, filename="weekly.md", source_type="text", content=TABLE)
            db.add(doc)
            await db.commit()
        yield scratch, owner, viewer, kb, doc


async def test_a_source_refreshed_on_the_server_is_read_as_its_publisher_and_shown_to_viewers(monkeypatch) -> None:
    import app.core.database as database
    from app.services import dashboard_refresh_events
    from app.services.custom_dashboards import edit_dashboard, save_dashboard
    from app.services.dashboard_definition import upgrade
    from app.services.dashboard_refresh import NEVER, refresh_due
    from app.services.dashboard_sources import read_source

    told: list[tuple[str, str]] = []

    async def _publish(dashboard_id: str, source_id: str) -> None:
        told.append((dashboard_id, source_id))

    monkeypatch.setattr(dashboard_refresh_events, "publish_source_updated", _publish)
    async with _world() as (scratch, owner, viewer, kb, doc):
        monkeypatch.setattr(database, "async_session", scratch.sessions)
        async with scratch.sessions() as db:
            dashboard = await save_dashboard(db, user_id=owner.id, spec_payload=_definition(kb.id, doc.id))
            dashboard.shared_user_ids = [str(viewer.id)]
            await db.commit()
            board = dashboard.id
        async with scratch.sessions() as db:
            [row] = (await db.execute(select(CustomDashboardSourceSnapshot))).scalars().all()
            assert (row.source_id, row.publisher_id, row.result) == ("sev", owner.id, None)
            definition = upgrade((await db.get(type(dashboard), board)).spec)
            before = await read_source(db, viewer.id, definition, "sev", dashboard_id=board)
        # Saved, not read yet; the viewer, who cannot read the knowledge base, is shown what its publisher reads.
        assert before["status"] == "error" and before["code"] == "running" and before["snapshot"]["mode"] == "server"
        assert await refresh_due(scratch.sessions) == 1
        assert told == [(str(board), "sev")]
        async with scratch.sessions() as db:
            row = (await db.execute(select(CustomDashboardSourceSnapshot))).scalar_one()
            assert row.result["status"] == "ok" and row.due_at > datetime.now(UTC) + timedelta(minutes=14)
            shown = await read_source(db, viewer.id, definition, "sev", dashboard_id=board)
            live = await read_source(db, viewer.id, definition, "sev")
        assert [r["severity"] for r in shown["data"]["rows"]] == ["high", "low"]
        assert shown["snapshot"]["publisher"] == "Owner" and shown["snapshot"]["every"] == "15m"
        # Read live (no dashboard: a preview), the viewer's own access applies: they cannot read the base.
        assert live == {"status": "error", "code": "access_denied"}
        # Nothing is due: a second run reads nothing.
        assert await refresh_due(scratch.sessions) == 0
        # Read only when asked: a manual source is never due.
        async with scratch.sessions() as db:
            current = await db.get(type(dashboard), board)
            await edit_dashboard(db, current, operations=[{"op": "set", "path": "dataSources.sev",
                                                          "value": _definition(kb.id, doc.id, refreshMode="manual")["dataSources"]["sev"]}],
                                 acting_user_id=owner.id, source="manual")  # fmt: skip
            await db.commit()
        assert await refresh_due(scratch.sessions) == 1
        async with scratch.sessions() as db:
            row = (await db.execute(select(CustomDashboardSourceSnapshot))).scalar_one()
            assert row.due_at == NEVER
            # A source no longer refreshed on the server drops its snapshot.
            current = await db.get(type(dashboard), board)
            live_source = _definition(kb.id, doc.id)["dataSources"]["sev"]
            live_source["options"] = {k: v for k, v in live_source["options"].items() if k != "refreshMode"}
            await edit_dashboard(db, current, operations=[{"op": "set", "path": "dataSources.sev", "value": live_source}],
                                 acting_user_id=owner.id, source="manual")  # fmt: skip
            await db.commit()
            assert (await db.execute(select(CustomDashboardSourceSnapshot))).scalars().all() == []


async def test_a_writer_may_only_point_a_source_at_a_document_they_read() -> None:
    from app.services.custom_dashboard_spec import DashboardSpecError
    from app.services.custom_dashboards import save_dashboard

    async with _world() as (scratch, _owner, viewer, kb, doc), scratch.sessions() as db:
        with pytest.raises(DashboardSpecError, match="not a document of a knowledge base you read"):
            await save_dashboard(db, user_id=viewer.id, spec_payload=_definition(kb.id, doc.id))


async def test_an_event_source_is_read_on_its_event_once_a_minute_at_most(monkeypatch) -> None:
    import app.core.database as database
    from app.models.custom_dashboard import CustomDashboard
    from app.services import dashboard_refresh_events
    from app.services.custom_dashboards import save_dashboard
    from app.services.dashboard_refresh import NEVER, _reading_assignment, hear_platform_event, refresh_due

    async def _publish(*_a) -> None:
        return None

    monkeypatch.setattr(dashboard_refresh_events, "publish_source_updated", _publish)
    async with _world() as (scratch, owner, _viewer, kb, doc):
        monkeypatch.setattr(database, "async_session", scratch.sessions)
        definition = _definition(kb.id, doc.id, refreshMode="event", on={"platformEvent": "run.completed"})
        definition["dataSources"]["sev"]["options"].pop("refresh")
        async with scratch.sessions() as db:
            await save_dashboard(db, user_id=owner.id, spec_payload=definition)
            await db.commit()
        assert await refresh_due(scratch.sessions) == 1  # read once when saved
        async with scratch.sessions() as db:
            row = (await db.execute(select(CustomDashboardSourceSnapshot))).scalar_one()
            assert (row.trigger, row.due_at) == ("platform:run.completed", NEVER)
            read_at = row.refreshed_at
        await hear_platform_event("run.completed", {"event": "run.completed"})
        await hear_platform_event("run.completed", {"event": "run.completed"})
        async with scratch.sessions() as db:
            row = (await db.execute(select(CustomDashboardSourceSnapshot))).scalar_one()
            # Two events within the minute: one read, a minute after the last one.
            assert row.due_at == read_at + timedelta(seconds=60)
        assert await refresh_due(scratch.sessions, now=read_at + timedelta(seconds=30)) == 0
        assert await refresh_due(scratch.sessions, now=read_at + timedelta(seconds=61)) == 1
        # The pages drawing an assignment's answers are found by the assignment's id.
        target = uuid.uuid4()
        async with scratch.sessions() as db:
            reader = CustomDashboard(user_id=owner.id, title="Feed", widget_count=0, revision=1, spec={
                "version": 2, "title": "Feed", "dataSources": {"feed": {"type": "assignment", "options": {"assignment_id": str(target)}}},
                "visualizations": {}, "layout": {"tabs": {"items": [{"layoutId": "m", "label": "M"}]}, "layoutDefinitions": {"m": {"structure": []}}},
            })  # fmt: skip
            db.add(reader)
            await db.commit()
            reader_id = reader.id
        assert await _reading_assignment(scratch.sessions, str(target)) == [(reader_id, "feed")]
        assert await _reading_assignment(scratch.sessions, str(uuid.uuid4())) == []
