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
from typing import Any

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
        "proven_exploitable",
        "mitigation_regression",
        "credential_leak",
        "stale_asset",
        "new_exposure",
        # #2436: the OpenAEV new asset source (off by default).
        "surface_drift",
    }
    assert all(row.trigger_path in TRIGGER_PATHS for row in listeners.LISTENERS)


OCTI_EXT = "extension-definition--ea279b3e-5c71-4632-ac08-831c66a786ba"


def test_a_pir_playbook_delivery_records_pir_ids_and_score_only() -> None:
    # The body of a PIR playbook delivery as read in the lab on OpenCTI master.
    payload = {
        "type": "update",
        "xtm_source": "pir_playbook",
        "message": "Update 2 elements in `x` internal-relationship",
        "data": {
            "type": "malware",
            "id": "malware--1",
            "name": "x",
            "extensions": {
                OCTI_EXT: {
                    "pir_information": [
                        {"pir_id": "pir-b", "pir_score": 33, "last_pir_score_date": "2026-11-21T17:49:39Z"},
                        {"pir_id": "pir-a", "pir_score": 100, "last_pir_score_date": "2026-11-21T17:48:43Z"},
                        {"pir_id": "pir-c", "pir_score": True},
                    ],
                    "pir_explanation": [{"criterion": "not kept"}],
                }
            },
        },
    }
    found = listeners.match("opencti", "update", payload)
    assert found is not None
    row, fields = found
    assert row.trigger_path == "relevant_threat"
    assert fields["evidence"]["pir"] == [
        {"pir_id": "pir-a", "score_at_delivery": 100},
        {"pir_id": "pir-b", "score_at_delivery": 33},
    ]
    assert "not kept" not in json.dumps(fields)


def test_merge_evidence_keeps_pir_and_floor_and_takes_the_latest_facts() -> None:
    """#2543: the merge a known signal's evidence goes through at each new sighting. Pure."""
    stored = {
        "pir": [{"pir_id": "a", "score_at_delivery": 75}],
        "pir_floor_met": True,
        "name": "old",
        signals.SOURCE_KEY_EVIDENCE: "opencti:pir_playbook",
    }
    merged = signals.merge_evidence(stored, {"name": "new", signals.SOURCE_KEY_EVIDENCE: "opencti:listener"})
    assert merged["pir"] == stored["pir"] and merged["pir_floor_met"] is True and merged["name"] == "new"
    assert merged[signals.SOURCES_EVIDENCE] == ["opencti:pir_playbook", "opencti:listener"]
    # A sighting that names a PIR floor of its own never downgrades a stored true one either.
    assert signals.merge_evidence(stored, {"pir_floor_met": False})["pir_floor_met"] is True
    # A floor reached later is taken; one never written stays absent.
    assert signals.merge_evidence({"pir_floor_met": False}, {"pir_floor_met": True})["pir_floor_met"] is True
    assert "pir_floor_met" not in signals.merge_evidence({"a": 1}, {"b": 2})
    # One source only: no list written.
    same = {signals.SOURCE_KEY_EVIDENCE: "opencti:listener"}
    assert signals.SOURCES_EVIDENCE not in signals.merge_evidence(same, dict(same))


def test_merge_evidence_keeps_the_highest_score_per_pir_and_stays_under_the_cap() -> None:
    low = {"pir": [{"pir_id": "a", "score_at_delivery": 40}, {"pir_id": "b", "score_at_delivery": 90}]}
    high = {"pir": [{"pir_id": "a", "score_at_delivery": 80}, {"pir_id": "c", "score_at_delivery": 10}]}
    assert signals.merge_evidence(low, high)["pir"] == [
        {"pir_id": "a", "score_at_delivery": 80},
        {"pir_id": "b", "score_at_delivery": 90},
        {"pir_id": "c", "score_at_delivery": 10},
    ]
    many = {"pir": [{"pir_id": f"p{i:03d}", "score_at_delivery": i} for i in range(200)], "pir_floor_met": True}
    merged = signals.merge_evidence(many, {"pir": [{"pir_id": "z" * 40, "score_at_delivery": 1}], "x": "y" * 2000})
    assert len(repr(merged)) <= signals.MAX_EVIDENCE_CHARS
    assert merged["pir_floor_met"] is True and merged["x"] == "y" * 2000
    assert {"pir_id": "p199", "score_at_delivery": 199} in merged["pir"]


def test_pir_information_outside_a_pir_playbook_delivery_is_not_read() -> None:
    payload = {
        "type": "update",
        "data": {
            "type": "malware",
            "id": "malware--1",
            "name": "x",
            "extensions": {OCTI_EXT: {"pir_information": [{"pir_id": "pir-a", "pir_score": 100}]}},
        },
    }
    found = listeners.match("opencti", "update", payload)
    assert found is not None
    assert "pir" not in found[1]["evidence"]


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
    assert fields["evidence"]["target_ref"] == "vulnerability--1"
    assert fields["label"] is None and "cve" not in fields["evidence"]


def _has_relationship(*, target_value: str | None = None, message: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "type": "relationship",
        "relationship_type": "has",
        "id": "relationship--81176c5c",
        "source_ref": "software--86a9d0f5",
        "target_ref": "vulnerability--c0cdd6e3",
    }
    if target_value is not None:
        data["extensions"] = {
            listeners.OPENCTI_EXTENSION: {
                "id": "rel-internal",
                "source_value": "Veeam Plug-In for AWS",
                "target_value": target_value,
            }
        }
    payload: dict[str, Any] = {"type": "create", "data": data}
    if message is not None:
        payload["_opencti_event"] = {"message": message}
    return payload


def test_a_has_relationship_is_named_by_the_target_value_cve() -> None:
    """#2524: the subject is the target's CVE, the STIX id stays in evidence, the CVE reaches the gate."""
    from app.services.ctem_scope_gate import signal_cve_ids

    _row, fields = _matched("create", _has_relationship(target_value="CVE-2026-123634"))
    assert (fields["ref"], fields["label"]) == ("CVE-2026-123634", "CVE-2026-123634")
    assert fields["evidence"]["target_ref"] == "vulnerability--c0cdd6e3"
    assert fields["evidence"]["cve"] == "CVE-2026-123634"
    assert fields["external_id"] == "rel-internal"
    assert signal_cve_ids(fields["ref"], fields["label"], fields["evidence"]) == ["CVE-2026-123634"]


def test_a_has_relationship_without_target_value_takes_the_cve_of_the_message() -> None:
    message = "[relationship] Veeam Plug-In for AWS has cve-2026-123634"
    _row, fields = _matched("create", _has_relationship(message=message))
    assert (fields["ref"], fields["label"]) == ("CVE-2026-123634", "CVE-2026-123634")
    assert fields["evidence"]["cve"] == "CVE-2026-123634"
    assert fields["evidence"]["message"] == message


def test_a_has_relationship_with_neither_keeps_the_stix_id() -> None:
    _row, fields = _matched("create", _has_relationship(message="[relationship] Veeam has a vulnerability"))
    assert (fields["ref"], fields["label"]) == ("vulnerability--c0cdd6e3", None)
    assert "cve" not in fields["evidence"]


def test_a_vulnerability_object_and_a_relationship_on_one_cve_share_the_subject_and_the_case_key() -> None:
    """#2524: one CVE delivered both ways is one subject, so related signals and the case key match."""
    from app.services.ctem_case_loop import fix_of

    vuln = {
        "type": "create",
        "data": {"type": "vulnerability", "id": "vulnerability--c0cdd6e3", "name": "CVE-2026-123634"},
    }
    signals_seen = []
    for payload in (vuln, _has_relationship(target_value="CVE-2026-123634")):
        row, fields = _matched("create", payload)
        signals_seen.append(
            SimpleNamespace(
                trigger_path=row.trigger_path,
                subject_kind=row.subject_kind,
                subject_ref=fields["ref"],
                subject_label=fields["label"],
                evidence=fields["evidence"],
            )
        )
    obj, rel = signals_seen
    assert (obj.subject_kind, obj.subject_ref.lower()) == (rel.subject_kind, rel.subject_ref.lower())
    assert fix_of(obj).key == fix_of(rel).key  # type: ignore[arg-type]


def test_an_opencti_actor_is_a_path_2_signal_and_a_report_is_nothing():
    actor = {"type": "update", "data": {"type": "intrusion-set", "id": "intrusion-set--1", "name": "ShinyHunters"}}
    assert listeners.match("opencti", "update", actor)[0].trigger_path == "relevant_threat"
    report = {"type": "create", "data": {"type": "report", "id": "report--1", "name": "A report"}}
    assert listeners.match("opencti", "create", report) is None
    other = {"type": "create", "data": {"type": "relationship", "relationship_type": "has", "target_ref": "x--1"}}
    assert listeners.match("opencti", "create", other) is None


def _malware(stix_id: str, name: str, *, internal: str | None = "5b7f1c2e-internal") -> dict[str, Any]:
    data: dict[str, Any] = {"type": "malware", "id": stix_id, "name": name}
    if internal is not None:
        data["extensions"] = {listeners.OPENCTI_EXTENSION: {"id": internal}}
        data["x_opencti_stix_ids"] = ["malware--older"]
    return {"type": "update", "data": data}


def _matched(event: str, payload: dict[str, Any]) -> tuple[listeners.Listener, dict[str, Any]]:
    found = listeners.match("opencti", event, payload)
    assert found is not None
    return found


async def test_an_opencti_entity_whose_standard_id_changes_stays_one_signal(store: Any) -> None:
    """#2505: two updates of one entity (same internal id, new STIX id and name) record one signal."""
    no_db: Any = None
    recorded: list[tuple[CtemSignal, bool]] = []
    for stix_id, name in (("malware--b0b05453", "AxolotLoader"), ("malware--fdb464ad", "AxolotlLoader")):
        row, fields = _matched("update", _malware(stix_id, name))
        assert fields["external_id"] == "5b7f1c2e-internal"
        assert fields["evidence"]["stix_id"] == stix_id
        assert fields["evidence"]["stix_ids"] == ["malware--older"]
        recorded.append(
            await signals.record_signal(
                no_db,
                organization_id=ORG,
                signal=signals.SignalInput(
                    trigger_path=row.trigger_path,
                    source_provider="opencti",
                    subject_kind=row.subject_kind,
                    subject_ref=fields["ref"],
                    source_external_id=fields["external_id"],
                    subject_label=fields["label"],
                    evidence=fields["evidence"],
                ),
                recorded_via="listener",
                recorded_by=signals.SYSTEM_ACTOR,
            )
        )
    (first, created_first), (second, created_second) = recorded
    assert created_first is True and created_second is False
    assert first is second and len(store.rows) == 1
    assert second.subject_label == "AxolotlLoader"
    assert second.dedupe_key == "relevant_threat:opencti:5b7f1c2e-internal"


def test_an_opencti_delivery_without_the_extension_keeps_the_stix_id_as_key() -> None:
    _row, fields = _matched("update", _malware("malware--b0b05453", "AxolotLoader", internal=None))
    assert fields["external_id"] == "malware--b0b05453"
    assert "stix_ids" not in fields["evidence"]
    _row, blank = _matched("update", _malware("malware--b0b05453", "AxolotLoader", internal="  "))
    assert blank["external_id"] == "malware--b0b05453"


def test_opencti_relationships_sightings_and_identities_are_keyed_on_the_internal_id() -> None:
    ext = {listeners.OPENCTI_EXTENSION: {"id": "rel-internal"}}
    relationship: dict[str, Any] = {
        "type": "create",
        "data": {
            "type": "relationship",
            "relationship_type": "has",
            "id": "relationship--1",
            "target_ref": "vulnerability--1",
            "extensions": ext,
        },
    }
    assert _matched("create", relationship)[1]["external_id"] == "rel-internal"
    sighting: dict[str, Any] = {
        "type": "create",
        "data": {"type": "sighting", "id": "sighting--1", "sighting_of_ref": "indicator--1", "extensions": ext},
    }
    assert _matched("create", sighting)[1]["external_id"] == "rel-internal"
    account: dict[str, Any] = {
        "type": "create",
        "data": {"type": "email-addr", "id": "email-addr--1", "value": "a@example.com", "extensions": ext},
    }
    _row, fields = _matched("create", account)
    assert fields["external_id"] == "rel-internal"
    assert fields["evidence"]["stix_id"] == "email-addr--1"


async def test_a_close_at_source_on_the_internal_id_reaches_the_signal_from_before_the_id_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2505: the stored key is the internal id, so a close for it finds the first signal."""
    from collections.abc import AsyncIterator

    _row, before = _matched("update", _malware("malware--b0b05453", "AxolotLoader"))
    _row, after = _matched("update", _malware("malware--fdb464ad", "AxolotlLoader"))
    assert before["external_id"] == after["external_id"]
    asked: list[tuple[str, str]] = []

    @asynccontextmanager
    async def _session() -> AsyncIterator[_Db]:
        yield _Db()

    async def _primary(_db: Any) -> SimpleNamespace:
        return SimpleNamespace(id=ORG)

    async def _close(_db: Any, *, organization_id: Any, provider: str, external_id: str, events: Any) -> int:
        asked.append((provider, external_id))
        return 1 if external_id == before["external_id"] else 0

    async def _publish(_pending: Any) -> None:
        return None

    monkeypatch.setattr("app.core.database.async_session", _session)
    monkeypatch.setattr("app.services.organizations.get_primary", _primary)
    monkeypatch.setattr("app.services.ctem_case_loop.close_signal_members", _close)
    monkeypatch.setattr("app.services.ctem_case_loop.publish_pending", _publish)
    assert await listeners._close_at_source("opencti", after["external_id"]) == 1
    assert asked == [("opencti", "5b7f1c2e-internal")]


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


def _aikido(event: str, **body) -> dict:
    return {"event_type": event, "payload": body}


def test_an_aikido_dependency_issue_names_its_repository_and_fixed_version():
    """CTEM-25: the repository goes to evidence.assets for the scope gate, the fixed version to the case."""
    payload = _aikido(
        "issue.open.created",
        issue_id=7,
        type="open_source",
        affected_package="lodash",
        installed_version="4.17.20",
        patched_version="4.17.21",
        code_repo_name="filigran/portal",
        code_repo_id=991,
        severity="critical",
    )
    row, fields = listeners.match("aikido", "issue_open_created", payload)
    assert row.trigger_path == "code_flaw" and row.lens == "inside"
    assert fields["evidence"]["assets"] == ["filigran/portal", "991"]
    assert fields["evidence"]["fixed_version"] == "4.17.21"
    assert fields["evidence"]["installed_version"] == "4.17.20"


def test_an_aikido_leaked_secret_is_a_path_10_signal_and_keeps_no_secret():
    payload = _aikido(
        "issue.open.created",
        issue_id=8,
        type="leaked_secret",
        affected_file=".env",
        code_repo_name="filigran/portal",
        secret="AKIA-NEVER-STORED",
    )
    row, fields = listeners.match("aikido", "issue_open_created", payload)
    assert row.trigger_path == "credential_leak" and row.lens == "world"
    assert fields["external_id"] == "8"
    assert "AKIA-NEVER-STORED" not in json.dumps(fields)


def test_an_aikido_sla_breach_resights_the_signal_of_the_same_issue():
    opened = listeners.match("aikido", "issue_open_created", _aikido("issue.open.created", issue_id=9, title="XSS"))
    breached = listeners.match("aikido", "issue_sla_breached", _aikido("issue.sla.breached", issue_id=9, title="XSS"))
    assert breached[0].trigger_path == opened[0].trigger_path == "code_flaw"
    assert breached[1]["external_id"] == opened[1]["external_id"] == "9"
    assert breached[1]["evidence"]["sla_breached"] is True


def test_an_aikido_gate_or_image_scan_is_about_its_repository_and_an_empty_scan_is_nothing():
    gate = _aikido("ci.gate.failed", scan_id="s-1", code_repo_name="filigran/portal", new_issues=2)
    row, fields = listeners.match("aikido", "ci_gate_failed", gate)
    assert row.trigger_path == "code_flaw"
    assert (fields["external_id"], fields["ref"]) == ("s-1", "filigran/portal")
    image = _aikido("scan.image.finished", scan_id="s-2", container_repo_name="ghcr.io/filigran/app", issue_count=0)
    assert listeners.match("aikido", "scan_image_finished", image) is None
    image["payload"]["issue_count"] = 3
    assert listeners.match("aikido", "scan_image_finished", image)[1]["ref"] == "ghcr.io/filigran/app"


async def test_an_aikido_closed_issue_closes_its_members_and_records_nothing(monkeypatch):
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)
    closed: list = []

    async def _close(provider, external_id):
        closed.append((provider, external_id))
        return 1

    monkeypatch.setattr(listeners, "_close_at_source", _close)
    payload = _aikido("issue.closed", issue_id=42)
    assert await listeners.note_event("aikido", "issue_closed", payload, verified=False) is False
    assert closed == []
    assert await listeners.note_event("aikido", "issue_closed", payload, verified=True) is False
    assert closed == [("aikido", "42")]


def _dependabot(action: str, **alert) -> dict:
    base = {
        "number": 12,
        "html_url": "https://github.com/filigran/portal/security/dependabot/12",
        "dependency": {"package": {"ecosystem": "npm", "name": "lodash"}, "manifest_path": "package-lock.json"},
        "security_advisory": {"ghsa_id": "GHSA-x", "cve_id": "CVE-2021-23337", "severity": "high"},
        "security_vulnerability": {
            "severity": "high",
            "vulnerable_version_range": "< 4.17.21",
            "first_patched_version": {"identifier": "4.17.21"},
        },
    }
    base.update(alert)
    return {"action": action, "alert": base, "repository": {"id": 991, "full_name": "filigran/portal"}}


def test_a_dependabot_alert_is_a_path_3_signal_on_its_repository():
    """#1331: the repository and its identity key go to evidence.assets, the fix to the case."""
    row, fields = listeners.match("github", "dependabot_alert.created", _dependabot("created"))
    assert (row.trigger_path, row.lens, row.subject_kind) == ("code_flaw", "inside", "code")
    assert (fields["external_id"], fields["ref"]) == ("991:12", "lodash")
    evidence = fields["evidence"]
    assert evidence["assets"] == ["filigran/portal", "991", "github.com:repository:991"]
    assert evidence["fixed_version"] == "4.17.21" and evidence["repository"] == "filigran/portal"
    assert evidence["cve_id"] == "CVE-2021-23337" and evidence["vulnerable_range"] == "< 4.17.21"
    reopened = listeners.match("github", "dependabot_alert.reopened", _dependabot("reopened"))
    assert reopened[1]["external_id"] == "991:12"


def test_other_github_events_and_an_alert_without_its_repository_are_nothing():
    assert listeners.match("github", "issues.opened", _dependabot("opened")) is None
    assert listeners.match("github", "dependabot_alert.fixed", _dependabot("fixed")) is None
    orphan = _dependabot("created")
    orphan.pop("repository")
    assert listeners.match("github", "dependabot_alert.created", orphan) is None


async def test_a_fixed_or_dismissed_dependabot_alert_closes_its_members(monkeypatch):
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)
    closed: list = []

    async def _close(provider, external_id):
        closed.append((provider, external_id))
        return 1

    monkeypatch.setattr(listeners, "_close_at_source", _close)
    for action in ("fixed", "dismissed"):
        assert (
            await listeners.note_event("github", f"dependabot_alert.{action}", _dependabot(action), verified=True)
            is False
        )
    assert closed == [("github", "991:12"), ("github", "991:12")]
    assert await listeners.note_event("github", "dependabot_alert.fixed", _dependabot("fixed"), verified=False) is False
    assert len(closed) == 2


def _advisory(state: str = "published", **advisory) -> dict:
    base = {
        "ghsa_id": "GHSA-aaaa-bbbb-cccc",
        "cve_id": "cve-2026-1234",
        "state": state,
        "severity": "High",
        "summary": "Template sandbox escape",
        "html_url": "https://github.com/filigran/portal/security/advisories/GHSA-aaaa-bbbb-cccc",
        "cvss": {"score": 8.1},
        "vulnerabilities": [
            {
                "package": {"ecosystem": "npm", "name": "portal"},
                "vulnerable_version_range": "< 2.4.0",
                "patched_versions": ">= 2.4.0",
            }
        ],
    }
    base.update(advisory)
    return {
        "action": "published",
        "repository_advisory": base,
        "repository": {"id": 991, "full_name": "filigran/portal"},
    }


def test_a_published_repository_advisory_is_a_path_1_signal_on_its_cve():
    """#1410: a vulnerability in a product the organization ships, keyed on the CVE like OpenCTI's."""
    row, fields = listeners.match("github", "repository_advisory.published", _advisory())
    assert (row.trigger_path, row.lens, row.subject_kind) == ("new_vulnerability", "inside", "vulnerability")
    assert (fields["external_id"], fields["ref"]) == ("991:GHSA-aaaa-bbbb-cccc", "CVE-2026-1234")
    assert fields["label"] == "Template sandbox escape"
    evidence = fields["evidence"]
    assert evidence["assets"] == ["filigran/portal", "991", "github.com:repository:991"]
    assert evidence["severity"] == "high" and evidence["cvss_base_score"] == 8.1
    assert evidence["cve"] == "CVE-2026-1234" and evidence["ghsa_id"] == "GHSA-aaaa-bbbb-cccc"
    assert evidence["affected_packages"] == [
        {"name": "portal", "ecosystem": "npm", "vulnerable_range": "< 2.4.0", "patched_versions": ">= 2.4.0"}
    ]
    assert evidence["advisory_url"].endswith("GHSA-aaaa-bbbb-cccc")


def test_an_advisory_without_a_cve_is_keyed_on_its_ghsa_id():
    _, fields = listeners.match("github", "repository_advisory.published", _advisory(cve_id=None))
    assert fields["ref"] == "GHSA-aaaa-bbbb-cccc" and "cve" not in fields["evidence"]


def test_an_advisory_in_triage_a_draft_or_without_its_repository_is_nothing():
    for state in ("triage", "draft", "closed", "withdrawn", ""):
        assert listeners.match("github", "repository_advisory.published", _advisory(state)) is None
    reported = _advisory("triage")
    reported["action"] = "reported"
    assert listeners.match("github", "repository_advisory.reported", reported) is None
    orphan = _advisory()
    orphan.pop("repository")
    assert listeners.match("github", "repository_advisory.published", orphan) is None
    # A Dependabot delivery never reads as an advisory, nor the reverse.
    assert listeners.match("github", "dependabot_alert.created", _advisory()) is None
    assert listeners.match("github", "repository_advisory.published", _dependabot("created")) is None


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
            row.evidence = signals.merge_evidence(row.evidence, clean.evidence)
            row.subject_label = clean.subject_label or row.subject_label
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

    async def counts_by_path(self, _db, *, organization_id, recorded_by):
        counts = {p: {s: 0 for s in SIGNAL_STATES} for p in TRIGGER_PATHS}
        for r in self._mine(organization_id, recorded_by):
            counts[r.trigger_path][r.state] += 1
        return counts

    async def activity_by_path(
        self, _db: Any, *, organization_id: Any, recorded_by: Any, since: datetime
    ) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {p: {"count_30d": 0, "last_signal_at": None} for p in TRIGGER_PATHS}
        for r in self._mine(organization_id, recorded_by):  # type: ignore[no-untyped-call]
            row = out[r.trigger_path]
            if r.last_seen_at >= since:
                row["count_30d"] = int(row["count_30d"]) + 1
            last = row["last_signal_at"]
            if last is None or r.last_seen_at.isoformat() > last:
                row["last_signal_at"] = r.last_seen_at.isoformat()
        return out

    async def activity_by_source(
        self, _db: Any, *, organization_id: Any, recorded_by: Any, since: datetime
    ) -> dict[str, dict[str, dict[str, Any]]]:
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for r in self._mine(organization_id, recorded_by):  # type: ignore[no-untyped-call]
            own = str((r.evidence or {}).get(signals.SOURCE_KEY_EVIDENCE) or r.source_provider)
            for key in (r.evidence or {}).get(signals.SOURCES_EVIDENCE) or [own]:
                row = out.setdefault(r.trigger_path, {}).setdefault(key, {"count_30d": 0, "last_signal_at": None})
                if r.last_seen_at >= since:
                    row["count_30d"] = int(row["count_30d"]) + 1
                last = row["last_signal_at"]
                if last is None or r.last_seen_at.isoformat() > last:
                    row["last_signal_at"] = r.last_seen_at.isoformat()
        return out


class _Db:
    commits = 0

    async def commit(self):
        _Db.commits += 1

    async def get(self, _model, ident):
        return SimpleNamespace(is_admin=str(ident) == ADMIN)

    @asynccontextmanager
    async def begin_nested(self):
        yield self


ADMIN = str(uuid.uuid4())
ALICE = str(uuid.uuid4())
BOB = str(uuid.uuid4())
CAROL = str(uuid.uuid4())  # no CTEM right (#2441)


@pytest.fixture
def store(monkeypatch):
    fake = _Store()
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)
    monkeypatch.setattr(signals, "record_signal", fake.record)
    monkeypatch.setattr(signals, "list_signals", fake.list)
    monkeypatch.setattr(signals, "get_signal", fake.get)
    monkeypatch.setattr(signals, "counts_by_path", fake.counts_by_path)
    monkeypatch.setattr(signals, "activity_by_path", fake.activity_by_path)
    monkeypatch.setattr(signals, "activity_by_source", fake.activity_by_source)
    fake.bound = set()  # type: ignore[attr-defined]

    async def _bound(_db: Any, _user_id: Any) -> set[tuple[str, str]]:
        return set(fake.bound)  # type: ignore[attr-defined]

    monkeypatch.setattr("app.services.ctem_signal_sources.bound_sources", _bound)

    async def _no_webhooks(_db):
        return []

    # #2506: no webhook source on the paths unless a test adds one.
    monkeypatch.setattr("app.services.ctem_webhook_sources.list_sources", _no_webhooks)
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

    async def _right(_db: Any, user_id: Any) -> str | None:
        return "decide" if str(user_id) in (ALICE, BOB) else None  # no CTEM without a right (#2441)

    monkeypatch.setattr("app.services.ctem_access.group_right", _right)
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


async def test_a_right_reads_the_organization_and_no_right_reads_nothing(client):
    """#2441: a CTEM right (or an administrator) reads every signal; without one, even their own is refused."""
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
        carol = await http.get("/api/v1/ctem-signals", headers={"X-User": CAROL})
        carol_records = await http.post(
            "/api/v1/ctem-signals", json=_body("stale_asset", "manual", "asset", "c"), headers={"X-User": CAROL}
        )
    assert sorted(s["subject"]["ref"] for s in alice["signals"]) == ["a", "b"]
    assert bob_reads_alice.status_code == 200
    assert admin["total"] == 2
    assert carol.status_code == 403 and carol_records.status_code == 403


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
        "proven_exploitable",
        "mitigation_regression",
        "credential_leak",
        "stale_asset",
        "new_exposure",
        "surface_drift",
    }


async def test_the_paths_count_only_the_signals_the_caller_reads_and_say_not_configured(client):
    """CTEM-30 (#937): per-path counts on the caller's visible signals; a path with no listener and no signal is not configured."""
    async with client as http:
        await http.post("/api/v1/ctem-signals", json=_body("stale_asset", "manual", "asset", "a"))
        await http.post(
            "/api/v1/ctem-signals", json=_body("stale_asset", "manual", "asset", "b"), headers={"X-User": BOB}
        )
        alice = {p["id"]: p for p in (await http.get("/api/v1/ctem-signals/paths")).json()["paths"]}
        admin = {
            p["id"]: p
            for p in (await http.get("/api/v1/ctem-signals/paths", headers={"X-User": ADMIN})).json()["paths"]
        }
    # A CTEM right reads the organization's signals (#2441).
    assert alice["stale_asset"]["total"] == 2 and alice["stale_asset"]["counts"]["new"] == 2
    assert admin["stale_asset"]["total"] == 2
    assert alice["stale_asset"]["configured"] is True
    assert alice["blind_spot"] == {**alice["blind_spot"], "total": 0, "configured": False}
    assert alice["new_vulnerability"]["configured"] is True and alice["new_vulnerability"]["total"] == 0


async def test_the_paths_name_their_sources_status_and_30_day_activity(client: Any, store: Any) -> None:
    """#1189: sources with their bound flag, a status that never reads connected without a bound source, 30-day counts."""
    store.bound = {("opencti", "listener")}
    async with client as http:
        await http.post("/api/v1/ctem-signals", json=_body("stale_asset", "manual", "asset", "a"))  # type: ignore[no-untyped-call]
        paths = {p["id"]: p for p in (await http.get("/api/v1/ctem-signals/paths")).json()["paths"]}
    vuln = paths["new_vulnerability"]
    assert vuln["status"] == "enabled" and vuln["description"]
    assert {(s["provider"], s["kind"], s["bound"]) for s in vuln["sources"]} == {
        ("opencti", "listener", True),
        ("opencti", "pir_playbook", False),
        ("opencti", "cases_reader", False),
        ("github", "listener", False),
    }
    assert paths["code_flaw"]["status"] == "not_connected"
    assert paths["stale_asset"]["count_30d"] == 1 and paths["stale_asset"]["last_signal_at"]
    assert paths["surface_drift"]["count_30d"] == 0 and paths["surface_drift"]["last_signal_at"] is None


def _watchlisted(store, owner, *, code="below_scope_threshold", criticality="medium"):
    row = next(r for r in store.rows.values() if r.recorded_by == owner)
    row.state = "watchlist"
    row.decision = {"reason_code": code, "scope": {"criticality": criticality, "threshold": "high"}}
    return row


async def test_propose_scope_stores_a_proposal_for_a_readable_watchlisted_signal_only(client, store, monkeypatch):
    """CTEM-30 (#937): "propose to raise the scope" stores a proposal, never a profile write; another user's signal is 404."""
    from app.services import organization_context_proposals as proposals

    stored: list[dict] = []

    async def _propose(_db, *, author_user_id, changes=None, **_kw):
        stored.append({"author": str(author_user_id), "changes": changes})
        return SimpleNamespace(id=uuid.uuid4(), status="pending")

    monkeypatch.setattr(proposals, "propose", _propose)
    async with client as http:
        await http.post("/api/v1/ctem-signals", json=_body("stale_asset", "manual", "asset", "a"))
        await http.post(
            "/api/v1/ctem-signals", json=_body("stale_asset", "manual", "asset", "b"), headers={"X-User": BOB}
        )
        mine = _watchlisted(store, ALICE)
        bobs = _watchlisted(store, BOB)
        ok = await http.post(f"/api/v1/ctem-signals/{mine.id}/propose-scope")
        other = await http.post(f"/api/v1/ctem-signals/{bobs.id}/propose-scope", headers={"X-User": CAROL})
        mine.decision = {"reason_code": "criticality_unknown", "scope": {"criticality": None}}
        unknown = await http.post(f"/api/v1/ctem-signals/{mine.id}/propose-scope")
    assert ok.status_code == 200
    assert ok.json()["value"] == "medium"
    assert stored == [
        {
            "author": ALICE,
            "changes": [
                {
                    "field": "prioritization_policy.scope_min_criticality",
                    "value": "medium",
                    "basis": "Watchlisted CTEM signal on a: criticality medium below the scope threshold",
                }
            ],
        }
    ]
    assert other.status_code == 403  # no CTEM right (#2441)
    assert unknown.status_code == 409


def test_the_scope_threshold_is_a_proposable_field_of_the_primary_profile_only():
    from app.services import organization_context_proposals as proposals

    assert "prioritization_policy.scope_min_criticality" in proposals._contract_paths("primary")
    assert "prioritization_policy.scope_min_criticality" not in proposals._contract_paths("tracked")


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


async def test_the_bulk_tool_records_a_sweep_in_one_call_and_one_commit(tools, monkeypatch):
    """#1726: a list in, one result per row out, idempotent on the dedupe key, a bad row refused alone."""
    from app.services import ctem_scope_gate_store

    async def _no_gate(_db, _row, *, created):
        return None

    monkeypatch.setattr(ctem_scope_gate_store, "evaluate_after_record", _no_gate)
    defs, call = tools(ALICE)
    bulk = next(d for d in defs if d.name == "record_ctem_signals")
    assert bulk.input_schema["required"] == ["signals"]
    rows = [
        {"trigger_path": path, "source_provider": provider, "subject_kind": kind, "subject_ref": ref}
        for path, provider, kind, ref in ONE_PER_PATH
    ]
    rows.append(dict(rows[0]))  # the same signal twice in one sweep
    rows.append({"trigger_path": "nope", "source_provider": "m", "subject_kind": "asset", "subject_ref": "a"})
    rows.append({**rows[1], "observed_at": "yesterday"})
    rows.append("not a row")
    before = _Db.commits
    out = json.loads(await call("record_ctem_signals", signals=rows))
    assert _Db.commits == before + 1
    assert (out["recorded"], out["created"], out["refused"]) == (13, 12, 3)
    assert [r["index"] for r in out["results"]] == list(range(len(rows)))
    assert out["results"][12]["created"] is False and out["results"][12]["seen_count"] == 2
    assert out["results"][12]["id"] == out["results"][0]["id"]
    assert out["results"][13]["error"].startswith("trigger_path")
    assert out["results"][14]["error"].startswith("observed_at")
    assert out["results"][15]["error"] == "not an object"
    # A second sweep of the same rows creates nothing.
    again = json.loads(await call("record_ctem_signals", signals=rows[:12]))
    assert (again["recorded"], again["created"]) == (12, 0)
    assert json.loads(await call("list_ctem_signals", limit=50))["total"] == 12


async def test_the_bulk_tool_refuses_an_empty_or_oversized_list_before_any_write(tools):
    from app.services.builtin_tools.ctem_signals import MAX_BULK_SIGNALS

    _, call = tools(ALICE)
    assert (await call("record_ctem_signals", signals=[])).startswith("signals must be a non-empty list")
    row = {"trigger_path": "blind_spot", "source_provider": "m", "subject_kind": "asset", "subject_ref": "a"}
    too_many = await call("record_ctem_signals", signals=[row] * (MAX_BULK_SIGNALS + 1))
    assert too_many.startswith(f"Refused: at most {MAX_BULK_SIGNALS}") and "Nothing was recorded" in too_many
    assert json.loads(await call("list_ctem_signals"))["total"] == 0


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


async def _two_signals_of_alice(tools) -> None:
    _, alice = tools(ALICE)
    for ref in ("a", "b"):
        await alice(
            "record_ctem_signal", trigger_path="blind_spot", source_provider="m", subject_kind="asset", subject_ref=ref
        )


async def test_a_person_with_no_own_signal_is_told_how_many_the_scope_hides(tools):
    """#1359: never a silent zero; a count only, no content of the hidden signals."""
    await _two_signals_of_alice(tools)
    defs, bob = tools(BOB)
    body = json.loads(await bob("list_ctem_signals"))
    assert body["total"] == 0 and body["signals"] == []
    assert body["scope"]["applied"] == "own"
    assert body["scope"]["acting_user"] == BOB and body["scope"]["is_admin"] is False
    assert body["scope"]["reason"] == "not an administrator"
    assert body["scope"]["hidden_by_scope"] == 2
    assert body["scope"]["note"].startswith("2 signals exist that this run's person did not record")
    listing = next(d for d in defs if d.name == "list_ctem_signals")
    assert listing.description.startswith("Your CTEM signals") and "admin" in listing.description
    # The same filters count the hidden ones: another path hides nothing.
    other_path = json.loads(await bob("list_ctem_signals", trigger_path="credential_leak"))
    assert "hidden_by_scope" not in other_path["scope"] and "note" not in other_path["scope"]


async def test_an_admin_with_strict_agent_access_reads_their_own_and_is_told_why(tools, monkeypatch):
    await _two_signals_of_alice(tools)
    monkeypatch.setattr(
        "app.core.security_options.security_option", lambda key: key == "agent_tools_strict_admin_access"
    )
    _, admin = tools(ADMIN)
    body = json.loads(await admin("list_ctem_signals"))
    assert body["total"] == 0
    assert body["scope"]["applied"] == "own" and body["scope"]["is_admin"] is True
    assert body["scope"]["reason"] == "strict admin access for agent tools"
    assert body["scope"]["hidden_by_scope"] == 2


async def test_an_admin_in_a_platform_health_reach_is_told_why(tools, monkeypatch):
    await _two_signals_of_alice(tools)
    monkeypatch.setattr("app.services.health.identity.in_health_reach", lambda: True)
    _, admin = tools(ADMIN)
    body = json.loads(await admin("list_ctem_signals"))
    assert body["scope"]["applied"] == "own" and body["scope"]["reason"] == "Platform Health reach"


async def test_an_admin_reads_the_organization_and_nothing_is_hidden(tools, monkeypatch):
    await _two_signals_of_alice(tools)
    monkeypatch.setattr("app.core.security_options.security_option", lambda _key: False)
    _, admin = tools(ADMIN)
    body = json.loads(await admin("list_ctem_signals"))
    assert body["total"] == 2
    assert body["scope"] == {
        "applied": "organization",
        "acting_user": ADMIN,
        "is_admin": True,
        "reason": "administrator",
    }
