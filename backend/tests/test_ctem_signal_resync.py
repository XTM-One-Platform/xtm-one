# SPDX-License-Identifier: Apache-2.0

"""#2528 step 3: the resync feeds OpenCTI entities to the live listener and runs the passes once."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.services import ctem_signal_listeners as listeners
from app.services import ctem_signal_resync as resync

NOW = datetime(2026, 11, 26, 17, 0, tzinfo=UTC)


def test_a_vulnerability_becomes_the_live_listeners_delivery() -> None:
    payload = resync.delivery(
        {
            "id": "internal-1",
            "standard_id": "vulnerability--aaa",
            "entity_type": "Vulnerability",
            "name": "CVE-2026-0001",
            "x_opencti_cvss_base_score": 9.8,
            "x_opencti_epss_score": None,
        }
    )
    assert payload is not None
    found = listeners.match("opencti", "update", payload)
    assert found is not None
    row, fields = found
    assert row.trigger_path == "new_vulnerability" and row.lens == "world"
    # Dedupe on the OpenCTI internal id, as the live delivery.
    assert fields["external_id"] == "internal-1" and fields["ref"] == "CVE-2026-0001"
    assert fields["evidence"]["cvss_base_score"] == 9.8 and "epss_score" not in fields["evidence"]


@pytest.mark.parametrize(
    ("entity_type", "node", "path"),
    [
        ("Intrusion-Set", {"name": "APT0"}, "relevant_threat"),
        ("Threat-Actor-Individual", {"name": "Someone"}, "relevant_threat"),
        ("Malware", {"name": "Loader"}, "relevant_threat"),
        ("Email-Addr", {"value": "alice@corp.example"}, "credential_leak"),
    ],
)
def test_each_world_type_lands_on_its_path(entity_type: str, node: dict[str, Any], path: str) -> None:
    payload = resync.delivery({"id": "i", "standard_id": "x--1", "entity_type": entity_type, **node})
    found = listeners.match("opencti", "update", payload or {})
    assert found is not None and found[0].trigger_path == path


def test_a_type_the_listener_does_not_read_is_left_out() -> None:
    assert resync.delivery({"id": "i", "entity_type": "Report", "name": "r"}) is None


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def rollback(self) -> None:
        return None


@pytest.mark.asyncio
async def test_world_resync_pages_every_source_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [
        {
            "stixCoreObjects": {
                "edges": [{"node": {"id": "a", "entity_type": "Vulnerability", "name": "CVE-1"}}],
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
            }
        },
        {
            "stixCoreObjects": {
                "edges": [
                    {"node": {"id": "b", "entity_type": "Malware", "name": "M"}},
                    {"node": {"id": "c", "entity_type": "Report", "name": "R"}},
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        },
    ]
    asked: list[dict[str, Any]] = []

    async def _gql(query: str, variables: dict[str, Any]) -> tuple[dict[str, Any], None]:
        asked.append(variables)
        return pages[len(asked) - 1], None

    async def _sources(db: Any) -> list[Any]:
        from types import SimpleNamespace

        return [SimpleNamespace(integration_id="i1", run_as_user_id="u1")]

    async def _conn(db: Any, integration_id: str, user_id: Any) -> Any:
        return _gql, integration_id, "secret"

    noted: list[str] = []

    async def _note(provider: str, event: str, payload: dict[str, Any], *, verified: bool) -> bool:
        noted.append(payload["data"]["name"])
        return payload["data"]["name"] != "M"  # one already recorded: deduped

    monkeypatch.setattr(resync, "_opencti_sources", _sources)
    monkeypatch.setattr("app.services.opencti_trigger_provisioning.integration_connection", _conn)
    result = await resync.resync_world(lambda: _Session(), now=NOW, note=_note)
    assert result == {"read": 2, "recorded": 1, "errors": []}
    assert noted == ["CVE-1", "M"] and asked[1]["after"] == "c1"
    assert asked[0]["filters"]["filters"][0]["values"] == ["2026-10-27T17:00:00+00:00"]


@pytest.mark.asyncio
async def test_each_pass_runs_once_with_its_slot_given_back(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import ctem_attack_surface, ctem_evidence_health

    calls: list[str] = []

    def _patch(module: Any, name: str, answer: Any) -> None:
        async def _release() -> None:
            calls.append(f"release {name}")

        async def _pass(db: Any) -> Any:
            calls.append(f"pass {name}")
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(module, "release_pass_slot", _release)
        monkeypatch.setattr(module, "scheduled_pass", _pass)

    _patch(ctem_attack_surface, "surface", {"recorded": 3})
    _patch(ctem_evidence_health, "evidence", RuntimeError("OpenCRQ down"))
    out = await resync.resync_passes(lambda: _Session())
    assert calls == ["release surface", "pass surface", "release evidence", "pass evidence"]
    assert out["attack_surface"] == {"recorded": 3}
    assert out["evidence_health"] == {"error": "RuntimeError: OpenCRQ down"}
