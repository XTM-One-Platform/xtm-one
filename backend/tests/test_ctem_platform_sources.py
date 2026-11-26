"""Platform signal sources (#2434): off by default, provisioned on enable with a run-as, never by an assignment."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import ctem_platform_sources as sources
from app.services import ctem_signal_sources as catalog
from app.services import openaev_trigger_provisioning as oaev

from .test_openaev_trigger_provisioning import FakeOpenAEV

NOW = datetime(2026, 11, 26, 1, 0, tzinfo=UTC)
INTEGRATION = uuid.uuid4()
RUN_AS = uuid.uuid4()


class FakeDb:
    """The session calls the source service makes: get, add, flush, execute (one select of the row)."""

    def __init__(self, integration_provider: str = "openaev") -> None:
        self.integration = SimpleNamespace(id=INTEGRATION, provider=integration_provider)
        self.rows: list = []

    async def get(self, model, key):
        if model.__name__ == "Integration":
            return self.integration if key == INTEGRATION else None
        return None

    def add(self, row) -> None:
        self.rows.append(row)

    async def flush(self) -> None:
        return None

    async def execute(self, _statement):
        rows = self.rows
        return SimpleNamespace(scalar_one_or_none=lambda: rows[0] if rows else None)


@pytest.fixture
def openaev(monkeypatch):
    api = FakeOpenAEV()

    async def connection(_db, integration_id, user_id):
        assert user_id == RUN_AS, "the source provisions as its run-as, never another identity"
        return api, str(integration_id), "s3cret"

    async def run_as_ok(_db, _provider, user_id):
        return None if user_id == RUN_AS else "the run-as user is deactivated or no longer exists: pick another run-as"

    monkeypatch.setattr(oaev, "integration_connection", connection)
    monkeypatch.setattr(sources, "_run_as_reason", run_as_ok)
    return api


def test_a_source_never_switched_on_reads_off():
    row = SimpleNamespace(enabled=False, state={})
    assert sources.status_of(row) == ("off", None)


async def test_enable_with_a_run_as_provisions_and_reads_connected(openaev):
    db = FakeDb()
    row = await sources.enable(
        db, provider="openaev", kind="listener", integration_id=INTEGRATION, run_as_user_id=RUN_AS, now=NOW
    )
    assert row.enabled is True and row.run_as_user_id == RUN_AS
    assert sources.status_of(row) == ("connected", None)
    assert len(openaev.notifiers) == 1 and len(openaev.triggers) == len(row.state["trigger_ids"])
    url = next(iter(openaev.notifiers.values()))["notifier_configuration"]["url"]
    # The platform receiver, never an assignment route.
    assert url.endswith(f"/api/hooks/integrations/{INTEGRATION}/signal-sources/{row.id}")
    assert "/assignments/" not in url


async def test_disable_deprovisions(openaev):
    db = FakeDb()
    row = await sources.enable(
        db, provider="openaev", kind="listener", integration_id=INTEGRATION, run_as_user_id=RUN_AS, now=NOW
    )
    await sources.disable(db, row, now=NOW)
    assert row.enabled is False and row.state == {}
    assert not openaev.notifiers and not openaev.triggers
    assert sources.status_of(row) == ("off", None)


async def test_enable_is_refused_with_the_exact_reason_and_leaves_nothing(openaev):
    db = FakeDb()
    with pytest.raises(sources.SourceRefusedError, match="deactivated"):
        await sources.enable(
            db, provider="openaev", kind="listener", integration_id=INTEGRATION, run_as_user_id=uuid.uuid4(), now=NOW
        )
    row = db.rows[0]
    assert row.enabled is False
    assert sources.status_of(row)[0] == "error" and "deactivated" in sources.status_of(row)[1]
    assert not openaev.notifiers and not openaev.triggers


async def test_a_provider_error_is_the_answer_and_nothing_stays(monkeypatch, openaev):
    async def refuses(method, path, body):
        if method == "post" and path == "/api/notification-triggers":
            raise oaev.OpenAEVApiError("403 Forbidden: missing capability")
        return await openaev(method, path, body)

    async def connection(_db, integration_id, _user_id):
        return refuses, str(integration_id), "s3cret"

    monkeypatch.setattr(oaev, "integration_connection", connection)
    with pytest.raises(sources.SourceRefusedError, match="403 Forbidden"):
        await sources.enable(
            FakeDb(), provider="openaev", kind="listener", integration_id=INTEGRATION, run_as_user_id=RUN_AS, now=NOW
        )
    assert not openaev.notifiers, "the notifier created before the refusal is removed"


async def test_a_wrong_integration_is_refused():
    with pytest.raises(sources.SourceRefusedError, match="no OpenAEV integration"):
        await sources.enable(
            FakeDb("opencti"), provider="openaev", kind="listener", integration_id=INTEGRATION, run_as_user_id=RUN_AS
        )


async def test_the_recheck_marks_a_lost_run_as_in_error(openaev, monkeypatch):
    db = FakeDb()
    row = await sources.enable(
        db, provider="openaev", kind="listener", integration_id=INTEGRATION, run_as_user_id=RUN_AS, now=NOW
    )

    async def gone(_db, _provider, _user_id):
        return "the run-as user is deactivated or no longer exists: pick another run-as"

    monkeypatch.setattr(sources, "_run_as_reason", gone)
    row.state = await sources._reconcile(db, row, NOW + timedelta(hours=1))
    status, reason = sources.status_of(row)
    assert status == "error" and "deactivated" in reason, "never a silent stop"


async def test_the_recheck_repairs_a_deleted_object(openaev):
    db = FakeDb()
    row = await sources.enable(
        db, provider="openaev", kind="listener", integration_id=INTEGRATION, run_as_user_id=RUN_AS, now=NOW
    )
    event, trigger_id = next(iter(row.state["trigger_ids"].items()))
    openaev.triggers.pop(trigger_id)
    row.state = await sources._reconcile(db, row, NOW + timedelta(hours=1))
    assert sources.status_of(row) == ("connected", None)
    assert row.state["trigger_ids"][event] in openaev.triggers


def test_no_assignment_holds_a_platform_source():
    """Disabling a fleet, an agent or an assignment never reaches a source: the row names none."""
    from app.models.ctem_signal_source import CtemSignalSource

    columns = set(CtemSignalSource.__table__.columns.keys())
    assert not {c for c in columns if "assignment" in c or "agent" in c or "fleet" in c}


@pytest.mark.parametrize(
    "state, connected",
    [
        ({}, False),
        ({"status": "error", "error": "x", "trigger_ids": {"a": "1"}}, False),
        ({"status": "provisioned", "notifier_id": "n1"}, False),
        ({"status": "provisioned", "notifier_id": "n1", "trigger_ids": {"a": "1"}}, True),
        ({"status": "provisioned", "notifier_id": "n1", "playbook_id": "p1"}, True),
        ({"status": "provisioned", "notifier_id": "n1", "trigger_id": "t1"}, True),
    ],
    ids=["nothing", "error", "notifier-only", "openaev", "pir-playbook", "opencti-trigger"],
)
def test_connected_only_when_objects_exist_and_the_last_check_passed(state, connected):
    assert sources.is_connected(state) is connected


def test_a_connected_integration_alone_never_binds_a_provisioned_source():
    """Sam's report of 26 Nov 2026: the OpenAEV and OpenCTI listener rows read Connected with nothing provisioned."""
    assert catalog.provisioned_pair("openaev", {}) is None
    assert catalog.provisioned_pair("openaev", {"status": "provisioned", "notifier_id": "n1"}) is None
    assert catalog.provisioned_pair("openaev", {"status": "provisioned", "trigger_ids": {"a": "1"}}) == (
        "openaev",
        "listener",
    )
    assert catalog.provisioned_pair("opencti", {"status": "provisioned", "trigger_id": "t"}) == ("opencti", "listener")
    assert catalog.provisioned_pair("opencti", {"status": "provisioned", "playbook_id": "p"}) == (
        "opencti",
        "pir_playbook",
    )
    for pair in [("openaev", "listener"), ("opencti", "listener"), ("opencti", "pir_playbook")]:
        assert pair in catalog.PROVISIONED_PAIRS


def test_the_openaev_source_listens_to_the_loop_events():
    from app.agents.builtin_agents import CTEM_OPENAEV_LOOP_EVENTS

    spec = sources.PROVISIONED[("openaev", "listener")].spec()
    assert oaev.read_provision_spec({"provision": spec}) == spec
    assert set(spec["events"]) == set(CTEM_OPENAEV_LOOP_EVENTS)


def test_the_opencti_live_listener_source_covers_every_opencti_listener_row():
    """The live listener source delivers what the OpenCTI rows of the listener table record."""
    from app.services import opencti_trigger_provisioning as prov
    from app.services.ctem_signal_listeners import LISTENERS

    spec = sources.PROVISIONED[("opencti", "listener")].spec()
    assert spec["mode"] == prov.TRIGGER_MODE and set(spec["event_types"]) == {"create", "update"}
    wanted = {t.lower() for t in spec["entity_types"]}
    for row in LISTENERS:
        if row.provider != "opencti":
            continue
        for kind in row.object_types:
            name = kind.split(":")[-1]
            assert name in wanted or (name == "threat-actor" and "threat-actor-group" in wanted), kind


async def test_enable_the_opencti_live_listener_provisions_one_trigger(monkeypatch):
    from app.services import opencti_trigger_provisioning as prov

    from .test_opencti_trigger_provisioning import FakeOpenCTI

    octi = FakeOpenCTI()

    async def connection(_db, integration_id, user_id):
        assert user_id == RUN_AS
        return octi, str(integration_id), "s3cret"

    async def run_as_ok(_db, _provider, _user_id):
        return None

    monkeypatch.setattr(prov, "integration_connection", connection)
    monkeypatch.setattr(sources, "_run_as_reason", run_as_ok)
    db = FakeDb("opencti")
    row = await sources.enable(
        db, provider="opencti", kind="listener", integration_id=INTEGRATION, run_as_user_id=RUN_AS, now=NOW
    )
    assert sources.status_of(row) == ("connected", None)
    assert len(octi.notifiers) == 1 and list(octi.triggers) == [row.state["trigger_id"]]
    await sources.disable(db, row, now=NOW)
    assert not octi.notifiers and not octi.triggers and sources.status_of(row) == ("off", None)


def test_the_pir_playbook_source_reads_the_profile_pirs():
    spec = sources.PROVISIONED[("opencti", "pir_playbook")].spec()
    assert spec["mode"] == "pir_playbook" and spec["organization_context"] == ["pir_ids"]
    assert "Vulnerability" in spec["entity_types"] and "Intrusion-Set" in spec["entity_types"]
