"""CTEM signals (#826): the input rules, the listener table, the API and the agent tools.

The store's SQL (the idempotent upsert, the filters, organization isolation)
runs against PostgreSQL in ``test_ctem_signals_postgres.py``; here the store
is faked so the routes and tools are checked for their shapes and rules.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request

from app.api.deps import get_current_user
from app.api.v1 import ctem_signals as api_module
from app.core.config import settings
from app.core.database import get_db
from app.models.ctem_signal import SIGNAL_STATES, TRIGGER_PATHS, CtemSignal
from app.services import ctem_signal_listeners as listeners
from app.services import ctem_signals as signals
from app.services.builtin_tools import CTEM_SIGNAL_TOOL_NAMES, build_ctem_signal_tools, offers_ctem_signal_tools
from app.tools.base import ToolCall

ORG = uuid.uuid4()
NOW = datetime(2026, 11, 21, 7, 0, tzinfo=UTC)

#: One plausible signal per trigger path: (path, provider, subject kind, subject ref).
ONE_PER_PATH = (
    ("new_vulnerability", "opencti", "vulnerability", "CVE-2026-123633"),
    ("relevant_threat", "opencti", "threat", "ShinyHunters"),
    ("code_flaw", "aikido", "code", "lodash 4.17.20"),
    ("proven_exploitable", "openaev", "asset", "ingress-nginx.eu-west"),
    ("new_exposure", "manual", "asset", "admin.example.com:8443"),
    ("stale_asset", "manual", "asset", "old-tenant.example.com"),
    ("surface_drift", "shodan", "asset", "203.0.113.10"),
    ("mitigation_regression", "openaev", "asset", "k8s-eu-west"),
    ("control_decay", "opencrq", "asset", "payments-db"),
    ("credential_leak", "manual", "identity", "svc-deploy@example.com"),
    ("third_party_event", "opencti", "third_party", "Example SaaS"),
    ("blind_spot", "opencrq", "asset", "build-runner-7"),
)


def test_every_trigger_path_has_a_label_and_a_default_lens():
    assert tuple(signals.PATH_CATALOG) == TRIGGER_PATHS
    assert {p for p, *_ in ONE_PER_PATH} == set(TRIGGER_PATHS)


def test_normalize_fills_the_lens_and_a_stable_dedupe_key():
    out = signals.normalize(
        signals.SignalInput(
            trigger_path="new_exposure",
            source_provider=" Manual ",
            subject_kind="asset",
            subject_ref="  Admin.Example.com  ",
        )
    )
    assert out.lens == "outside"
    assert out.source_provider == "manual"
    assert out.subject_ref == "Admin.Example.com"
    assert out.dedupe_key == "new_exposure:asset:admin.example.com"
    by_source = signals.normalize(
        signals.SignalInput("new_vulnerability", "opencti", "vulnerability", "CVE-1", source_external_id="vuln--1")
    )
    assert by_source.dedupe_key == "new_vulnerability:opencti:vuln--1"


def test_a_very_long_reference_is_hashed_into_the_key():
    out = signals.normalize(signals.SignalInput("surface_drift", "manual", "asset", "h" * 290))
    assert len(out.dedupe_key) <= 200
    assert out.dedupe_key.startswith("surface_drift:sha256:")


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"trigger_path": "new_cve"}, "trigger_path"),
        ({"lens": "sideways"}, "lens"),
        ({"subject_kind": "planet"}, "subject_kind"),
        ({"source_provider": " "}, "source_provider"),
        ({"subject_ref": ""}, "subject_ref"),
        ({"evidence": {"blob": "x" * 9000}}, "evidence"),
    ],
    ids=["path", "lens", "kind", "provider", "ref", "evidence"],
)
def test_a_bad_field_is_refused_by_name(change, field):
    base = {"trigger_path": "stale_asset", "source_provider": "manual", "subject_kind": "asset", "subject_ref": "a"}
    base.update(change)
    with pytest.raises(signals.SignalError, match=field):
        signals.normalize(signals.SignalInput(**base))


# ── the listener table ────────────────────────────────────────────────────


def test_listeners_cover_the_paths_whose_events_exist_today():
    assert listeners.paths_with_listener() == {
        "new_vulnerability",
        "relevant_threat",
        "code_flaw",
        "mitigation_regression",
    }
    assert all(row.trigger_path in TRIGGER_PATHS for row in listeners.LISTENERS)


def test_an_opencti_vulnerability_delivery_is_a_path_1_signal():
    payload = {
        "type": "create",
        "data": {
            "type": "vulnerability",
            "id": "vulnerability--1",
            "name": "CVE-2026-123633",
            "x_opencti_cvss_base_score": 9.2,
            "x_opencti_epss_score": 0.41,
        },
    }
    row, fields = listeners.match("opencti", "create", payload)
    assert (row.trigger_path, row.subject_kind) == ("new_vulnerability", "vulnerability")
    assert fields["ref"] == "CVE-2026-123633"
    assert fields["external_id"] == "vulnerability--1"
    assert fields["evidence"]["cvss_base_score"] == 9.2


def test_a_has_relationship_to_a_vulnerability_is_a_path_1_signal_on_the_target():
    payload = {
        "type": "create",
        "data": {
            "type": "relationship",
            "relationship_type": "has",
            "id": "relationship--9",
            "source_ref": "software--nginx",
            "target_ref": "vulnerability--1",
        },
    }
    row, fields = listeners.match("opencti", "create", payload)
    assert row.lens == "inside"
    assert fields["ref"] == "vulnerability--1"
    assert fields["evidence"]["source_ref"] == "software--nginx"


def test_an_opencti_actor_is_a_path_2_signal_and_a_report_is_nothing():
    actor = {"type": "update", "data": {"type": "intrusion-set", "id": "intrusion-set--1", "name": "ShinyHunters"}}
    assert listeners.match("opencti", "update", actor)[0].trigger_path == "relevant_threat"
    report = {"type": "create", "data": {"type": "report", "id": "report--1", "name": "A report"}}
    assert listeners.match("opencti", "create", report) is None
    other = {"type": "create", "data": {"type": "relationship", "relationship_type": "has", "target_ref": "x--1"}}
    assert listeners.match("opencti", "create", other) is None


def test_an_aikido_new_issue_is_a_path_3_signal_and_a_closed_one_is_nothing():
    payload = {
        "event_type": "issue.open.created",
        "payload": {"issue_id": 42, "affected_package": "lodash", "severity": "high", "type": "open_source"},
    }
    row, fields = listeners.match("aikido", "issue_open_created", payload)
    assert row.trigger_path == "code_flaw"
    assert (fields["external_id"], fields["ref"]) == ("42", "lodash")
    assert fields["evidence"]["severity"] == "high"
    assert listeners.match("aikido", "issue_closed", payload) is None
    assert listeners.match("slack", "message", payload) is None


async def test_note_event_records_through_the_store_as_system(monkeypatch):
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)
    seen: dict = {}

    class _Db:
        async def commit(self):
            seen["committed"] = True

    @asynccontextmanager
    async def _session():
        yield _Db()

    async def _primary(_db):
        return SimpleNamespace(id=ORG)

    async def _record(_db, **kw):
        seen.update(kw)
        return SimpleNamespace(), True

    monkeypatch.setattr("app.core.database.async_session", _session)
    monkeypatch.setattr("app.services.organizations.get_primary", _primary)
    monkeypatch.setattr(signals, "record_signal", _record)
    payload = {"type": "create", "data": {"type": "vulnerability", "id": "vulnerability--1", "name": "CVE-1"}}
    assert await listeners.note_event("opencti", "create", payload, verified=False) is False
    assert seen == {}
    assert await listeners.note_event("opencti", "create", payload, verified=True) is True
    assert seen["recorded_via"] == "listener"
    assert seen["recorded_by"] == "system"
    assert seen["organization_id"] == ORG
    assert seen["committed"]
    note = {"type": "create", "data": {"type": "note"}}
    assert await listeners.note_event("opencti", "create", note, verified=True) is False


async def test_note_event_never_raises_and_stays_off_in_copilot(monkeypatch):
    payload = {"type": "create", "data": {"type": "vulnerability", "id": "vulnerability--1", "name": "CVE-1"}}
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: True), raising=False)
    assert await listeners.note_event("opencti", "create", payload, verified=True) is False
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)

    @asynccontextmanager
    async def _broken():
        raise RuntimeError("database down")
        yield  # pragma: no cover

    monkeypatch.setattr("app.core.database.async_session", _broken)
    assert await listeners.note_event("opencti", "create", payload, verified=True) is False


# ── a fake store shared by the API and tool tests ─────────────────────────


class _Store:
    """``record_signal`` / ``list_signals`` / ``get_signal`` over a dict, keyed like the table."""

    def __init__(self):
        self.rows: dict[tuple[uuid.UUID, str, str], CtemSignal] = {}

    async def record(self, _db, *, organization_id, signal, recorded_via, recorded_by, now=None):
        clean = signals.normalize(signal)
        key = (organization_id, recorded_by, clean.dedupe_key)
        at = clean.observed_at or now or datetime.now(UTC)
        row = self.rows.get(key)
        if row is not None:
            row.last_seen_at = max(row.last_seen_at, at)
            row.seen_count += 1
            row.evidence = clean.evidence
            return row, False
        row = CtemSignal(
            id=uuid.uuid4(),
            organization_id=organization_id,
            trigger_path=clean.trigger_path,
            lens=clean.lens,
            source_provider=clean.source_provider,
            source_external_id=clean.source_external_id,
            subject_kind=clean.subject_kind,
            subject_ref=clean.subject_ref,
            subject_label=clean.subject_label,
            observed_at=at,
            first_seen_at=at,
            last_seen_at=at,
            seen_count=1,
            evidence=clean.evidence,
            dedupe_key=clean.dedupe_key,
            state="new",
            recorded_via=recorded_via,
            recorded_by=recorded_by,
        )
        self.rows[key] = row
        return row, True

    def _mine(self, organization_id, recorded_by):
        return [
            r
            for (org, _, _), r in self.rows.items()
            if org == organization_id and (recorded_by is None or r.recorded_by == recorded_by)
        ]

    async def list(self, _db, *, organization_id, recorded_by, state=None, trigger_path=None, lens=None, **_kw):
        rows = [
            r
            for r in self._mine(organization_id, recorded_by)
            if (not trigger_path or r.trigger_path == trigger_path) and (not lens or r.lens == lens)
        ]
        counts = {s: sum(1 for r in rows if r.state == s) for s in SIGNAL_STATES}
        page = [r for r in rows if not state or r.state == state]
        return page, len(page), counts

    async def get(self, _db, signal_id, *, organization_id, recorded_by):
        return next((r for r in self._mine(organization_id, recorded_by) if r.id == signal_id), None)


class _Db:
    async def commit(self):
        return None

    async def get(self, _model, ident):
        return SimpleNamespace(is_admin=str(ident) == ADMIN)


ADMIN = str(uuid.uuid4())
ALICE = str(uuid.uuid4())
BOB = str(uuid.uuid4())


@pytest.fixture
def store(monkeypatch):
    fake = _Store()
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)
    monkeypatch.setattr(signals, "record_signal", fake.record)
    monkeypatch.setattr(signals, "list_signals", fake.list)
    monkeypatch.setattr(signals, "get_signal", fake.get)
    return fake


@pytest.fixture
def client(store, monkeypatch):
    """The API mounted alone; the caller is chosen per request with the X-User header."""
    app = FastAPI()
    app.include_router(api_module.router, prefix="/api/v1/ctem-signals")

    async def _primary(_db):
        return SimpleNamespace(id=ORG)

    monkeypatch.setattr(api_module, "get_primary", _primary)

    async def _db():
        yield _Db()

    async def _user(request: Request):
        uid = request.headers.get("X-User", ALICE)
        return SimpleNamespace(id=uuid.UUID(uid), is_admin=uid == ADMIN)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _body(path, provider, kind, ref, **extra):
    return {"trigger_path": path, "source": {"provider": provider}, "subject": {"kind": kind, "ref": ref}, **extra}


async def test_the_api_records_one_signal_for_each_of_the_12_paths(client):
    async with client as http:
        for path, provider, kind, ref in ONE_PER_PATH:
            answer = await http.post("/api/v1/ctem-signals", json=_body(path, provider, kind, ref))
            assert answer.status_code == 200, answer.text
            data = answer.json()
            assert data["created"] is True
            assert data["signal"]["trigger_path"] == path
            assert data["signal"]["state"] == "new"
            assert data["signal"]["recorded_via"] == "api"
        listed = (await http.get("/api/v1/ctem-signals")).json()
    assert listed["total"] == 12
    assert {s["trigger_path"] for s in listed["signals"]} == set(TRIGGER_PATHS)


async def test_the_same_dedupe_key_twice_moves_last_seen_and_keeps_one_row(client):
    first = _body("new_exposure", "manual", "asset", "admin.example.com", observed_at="2026-11-21T06:00:00Z")
    again = _body(
        "new_exposure",
        "manual",
        "asset",
        "admin.example.com",
        observed_at="2026-11-21T08:00:00Z",
        evidence={"status": 200, "link": "https://admin.example.com"},
    )
    async with client as http:
        a = (await http.post("/api/v1/ctem-signals", json=first)).json()
        b = (await http.post("/api/v1/ctem-signals", json=again)).json()
        listed = (await http.get("/api/v1/ctem-signals")).json()
    assert (a["created"], b["created"]) == (True, False)
    assert a["signal"]["id"] == b["signal"]["id"]
    assert b["signal"]["seen_count"] == 2
    assert b["signal"]["last_seen_at"].startswith("2026-11-21T08:00")
    assert b["signal"]["first_seen_at"].startswith("2026-11-21T06:00")
    assert b["signal"]["evidence"]["status"] == 200
    assert listed["total"] == 1


async def test_list_filters_by_state_path_and_lens(client, store):
    async with client as http:
        for path, provider, kind, ref in ONE_PER_PATH:
            await http.post("/api/v1/ctem-signals", json=_body(path, provider, kind, ref))
        next(iter(store.rows.values())).state = "watchlist"
        outside = (await http.get("/api/v1/ctem-signals", params={"lens": "outside"})).json()
        by_path = (await http.get("/api/v1/ctem-signals", params={"trigger_path": "blind_spot"})).json()
        watch = (await http.get("/api/v1/ctem-signals", params={"state": "watchlist"})).json()
        refused = await http.get("/api/v1/ctem-signals", params={"state": "open"})
    assert {s["trigger_path"] for s in outside["signals"]} == {"new_exposure", "stale_asset", "surface_drift"}
    assert [s["subject"]["ref"] for s in by_path["signals"]] == ["build-runner-7"]
    assert watch["total"] == 1 and watch["counts"]["watchlist"] == 1
    assert refused.status_code == 422


async def test_a_user_reads_only_their_signals_and_an_admin_reads_the_organization(client):
    async with client as http:
        mine = (await http.post("/api/v1/ctem-signals", json=_body("stale_asset", "manual", "asset", "a"))).json()
        await http.post(
            "/api/v1/ctem-signals",
            json=_body("stale_asset", "manual", "asset", "b"),
            headers={"X-User": BOB},
        )
        alice = (await http.get("/api/v1/ctem-signals")).json()
        bob_reads_alice = await http.get(f"/api/v1/ctem-signals/{mine['signal']['id']}", headers={"X-User": BOB})
        admin = (await http.get("/api/v1/ctem-signals", headers={"X-User": ADMIN})).json()
    assert [s["subject"]["ref"] for s in alice["signals"]] == ["a"]
    assert bob_reads_alice.status_code == 404
    assert admin["total"] == 2


async def test_another_users_dedupe_key_never_returns_or_touches_their_signal(client, store):
    alice_body = _body(
        "credential_leak", "manual", "identity", "svc@example.com", evidence={"link": "https://a", "note": "alice"}
    )
    bob_body = _body("credential_leak", "manual", "identity", "svc@example.com", evidence={"note": "bob"})
    async with client as http:
        alice = (await http.post("/api/v1/ctem-signals", json=alice_body)).json()
        bob = (await http.post("/api/v1/ctem-signals", json=bob_body, headers={"X-User": BOB})).json()
        alice_again = (await http.get(f"/api/v1/ctem-signals/{alice['signal']['id']}")).json()
    assert alice["signal"]["dedupe_key"] == bob["signal"]["dedupe_key"]
    assert bob["created"] is True and bob["signal"]["id"] != alice["signal"]["id"]
    assert bob["signal"]["evidence"] == {"note": "bob"}
    assert alice_again["evidence"] == {"link": "https://a", "note": "alice"}
    assert alice_again["seen_count"] == 1
    assert len(store.rows) == 2


async def test_the_paths_endpoint_names_the_listened_paths(client):
    async with client as http:
        paths = (await http.get("/api/v1/ctem-signals/paths")).json()["paths"]
    assert [p["id"] for p in paths] == list(TRIGGER_PATHS)
    assert {p["id"] for p in paths if p["listener"]} == {
        "new_vulnerability",
        "relevant_threat",
        "code_flaw",
        "mitigation_regression",
    }


async def test_an_invalid_body_is_422_and_copilot_mode_is_404(client, monkeypatch):
    async with client as http:
        bad = await http.post("/api/v1/ctem-signals", json=_body("new_cve", "manual", "asset", "a"))
        monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: True), raising=False)
        off = await http.get("/api/v1/ctem-signals")
    assert bad.status_code == 422
    assert off.status_code == 404


# ── the agent tools ───────────────────────────────────────────────────────


@pytest.fixture
def tools(store, monkeypatch):
    @asynccontextmanager
    async def _session():
        yield _Db()

    async def _primary(_db):
        return SimpleNamespace(id=ORG)

    monkeypatch.setattr("app.core.database.async_session", _session)
    monkeypatch.setattr("app.services.organizations.get_primary", _primary)

    def _for(user_id: str):
        defs, executor = build_ctem_signal_tools(user_id=user_id)

        async def call(name: str, **args) -> str:
            result = await executor.execute_tool(ToolCall(id="c1", name=name, arguments=args))
            return result.content if isinstance(result.content, str) else str(result.content)

        return defs, call

    return _for


def test_only_the_ctem_trigger_agents_are_offered_the_tools(monkeypatch):
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)
    assert offers_ctem_signal_tools("ctem-vuln-prioritization-orchestrator", is_builtin=True)
    assert offers_ctem_signal_tools("ctem-threat-relevance-analyst", is_builtin=True)
    assert not offers_ctem_signal_tools("ctem-vuln-prioritization-orchestrator", is_builtin=False)
    assert not offers_ctem_signal_tools("ctem-remediation-coordinator", is_builtin=True)
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: True), raising=False)
    assert not offers_ctem_signal_tools("ctem-assistant", is_builtin=True)


async def test_the_tool_records_one_signal_for_each_of_the_12_paths_idempotently(tools):
    defs, call = tools(ALICE)
    assert tuple(d.name for d in defs) == CTEM_SIGNAL_TOOL_NAMES
    for path, provider, kind, ref in ONE_PER_PATH:
        out = json.loads(
            await call(
                "record_ctem_signal", trigger_path=path, source_provider=provider, subject_kind=kind, subject_ref=ref
            )
        )
        assert out["created"] is True and out["signal"]["recorded_via"] == "tool"
    again = json.loads(
        await call(
            "record_ctem_signal",
            trigger_path="credential_leak",
            source_provider="manual",
            subject_kind="identity",
            subject_ref="svc-deploy@example.com",
            observed_at="2030-01-01T00:00:00Z",
        )
    )
    assert again["created"] is False and again["signal"]["seen_count"] == 2
    listed = json.loads(await call("list_ctem_signals", lens="world"))
    assert {s["trigger_path"] for s in listed["signals"]} == {
        "new_vulnerability",
        "relevant_threat",
        "credential_leak",
        "third_party_event",
    }


async def test_the_tool_refuses_a_bad_field_and_scopes_the_list_to_the_user(tools):
    _, alice = tools(ALICE)
    _, bob = tools(BOB)
    _, admin = tools(ADMIN)
    refused = await alice(
        "record_ctem_signal", trigger_path="nope", source_provider="m", subject_kind="asset", subject_ref="a"
    )
    assert refused.startswith("Refused: trigger_path")
    await alice(
        "record_ctem_signal", trigger_path="blind_spot", source_provider="m", subject_kind="asset", subject_ref="a"
    )
    assert json.loads(await bob("list_ctem_signals"))["total"] == 0
    assert json.loads(await admin("list_ctem_signals"))["total"] == 1
