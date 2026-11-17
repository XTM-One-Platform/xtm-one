"""Emailing a dashboard's export (``dashboard_export.send_dashboard_export``), against PostgreSQL.

What a scheduled export's run calls through the ``send_dashboard_export``
tool. Only people with an active account who may open the dashboard receive
it, each a render made with THEIR access; an address the platform does not
know, a person who cannot open the dashboard, an inactive account or a
malformed address is reported and never sent to. A sender who cannot open
the dashboard sends nothing; neither does a platform without email.

``integration``-marked; the temporary database is ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.custom_dashboard import CustomDashboard
from app.models.user import User
from app.services import dashboard_export, email
from app.services.dashboard_definition import validate_definition
from app.services.presentation_export import ExportedFile
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_DEFINITION = {
    "version": 2,
    "title": "Ops",
    "dataSources": {"open": {"type": "static", "options": {"shape": "number", "data": {"value": 37}}}},
    "visualizations": {"kpi": {"type": "xtm.singlevalue", "dataSources": {"primary": "open"}}},
    "layout": {
        "tabs": {"items": [{"layoutId": "main", "label": "Main"}]},
        "layoutDefinitions": {"main": {"structure": [{"item": "kpi", "position": {"x": 0, "y": 0, "w": 6, "h": 6}}]}},
    },
}


@pytest.fixture
def outbox(monkeypatch) -> SimpleNamespace:
    """Renders and emails recorded, not made; email configured."""
    sent = SimpleNamespace(renders=[], emails=[])

    async def fake_export(**kwargs) -> ExportedFile:
        sent.renders.append(kwargs)
        return ExportedFile("ops.pdf", "application/pdf", f"pdf for {kwargs['user_id']}".encode())

    async def fake_send(**kwargs) -> bool:
        sent.emails.append(kwargs)
        return True

    monkeypatch.setattr(dashboard_export, "export_dashboard", fake_export)
    monkeypatch.setattr(email, "send_email", fake_send)
    monkeypatch.setattr(email, "is_email_configured", lambda: True)
    return sent


async def _world(monkeypatch):
    scratch_cm = scratch_database("dashboard_send", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)
    scratch = await scratch_cm.__aenter__()
    monkeypatch.setattr("app.core.database.async_session", scratch.sessions)
    async with scratch.sessions() as db:
        people = {
            name: User(email=f"{name}@example.com", hashed_password="x", full_name=name.title(), role="user", is_active=name != "gone")
            for name in ("owner", "viewer", "stranger", "gone")
        }
        db.add_all(people.values())
        await db.flush()
        share = {
            "shared_user_ids": [str(people["viewer"].id), str(people["gone"].id)],
            "shared_user_permissions": {str(people["viewer"].id): "read_only", str(people["gone"].id): "read_only"},
        }
        dashboard = CustomDashboard(user_id=people["owner"].id, title="Ops", widget_count=1, revision=1, spec=validate_definition(_DEFINITION), **share)  # fmt: skip
        db.add(dashboard)
        await db.commit()
        ids = SimpleNamespace(**{name: person.id for name, person in people.items()}, dashboard=dashboard.id)
    return scratch_cm, ids


async def test_each_reader_receives_their_own_render_and_nobody_else_receives_anything(monkeypatch, outbox) -> None:
    scratch_cm, ids = await _world(monkeypatch)
    try:
        report = await dashboard_export.send_dashboard_export(
            dashboard_id=ids.dashboard,
            sender_id=ids.owner,
            recipients=["Viewer@Example.com", "owner@example.com", "stranger@example.com", "gone@example.com", "nobody@example.com", "not an address", "viewer@example.com"],
            subject="Weekly\nops",
            message="Here is this week's view.",
        )
    finally:
        await scratch_cm.__aexit__(None, None, None)
    assert report.sent == ["viewer@example.com", "owner@example.com"]
    assert report.not_sent == {
        "not an address": "invalid",
        "stranger@example.com": "no_access",
        "gone@example.com": "no_account",
        "nobody@example.com": "no_account",
    }
    # Each reader's file is rendered with their own access.
    assert [(render["user_id"], render["export_format"]) for render in outbox.renders] == [(ids.viewer, "pdf"), (ids.owner, "pdf")]
    assert [(mail["to"], mail["subject"]) for mail in outbox.emails] == [
        ("viewer@example.com", "Weekly ops"),
        ("owner@example.com", "Weekly ops"),
    ]
    first = outbox.emails[0]
    assert [(a.filename, a.content_type, a.data) for a in first["attachments"]] == [("ops.pdf", "application/pdf", f"pdf for {ids.viewer}".encode())]
    assert "Here is this week&#x27;s view." in first["html"]
    assert f"/dashboards/{ids.dashboard}" in first["text"]


async def test_a_sender_who_cannot_open_the_dashboard_sends_nothing(monkeypatch, outbox) -> None:
    scratch_cm, ids = await _world(monkeypatch)
    try:
        with pytest.raises(LookupError):
            await dashboard_export.send_dashboard_export(dashboard_id=ids.dashboard, sender_id=ids.stranger, recipients=["viewer@example.com"])
    finally:
        await scratch_cm.__aexit__(None, None, None)
    assert (outbox.renders, outbox.emails) == ([], [])


async def test_without_email_nothing_is_rendered(monkeypatch, outbox) -> None:
    monkeypatch.setattr(email, "is_email_configured", lambda: False)
    with pytest.raises(RuntimeError):
        await dashboard_export.send_dashboard_export(dashboard_id=uuid.uuid4(), sender_id=uuid.uuid4(), recipients=["a@example.com"])
    assert outbox.renders == []


async def test_at_most_ten_people_are_sent_to(monkeypatch, outbox) -> None:
    scratch_cm, ids = await _world(monkeypatch)
    try:
        many = [f"x{n}@example.com" for n in range(12)]
        report = await dashboard_export.send_dashboard_export(dashboard_id=ids.dashboard, sender_id=ids.owner, recipients=many)
    finally:
        await scratch_cm.__aexit__(None, None, None)
    assert [report.not_sent[a] for a in many] == ["no_account"] * 10 + ["too_many"] * 2
