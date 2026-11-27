# SPDX-License-Identifier: Apache-2.0

"""#2528 step 3, #2578: the resync reads every route with its own window and cap, and reports each."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
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
    assert "pir" not in fields["evidence"]


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


def test_a_pir_match_rides_as_the_playbook_delivers_it() -> None:
    """#2578: a PIR-flagged threat carries its PIR score, so the relevance floor is computed as live."""
    payload = resync.delivery(
        {
            "id": "i",
            "standard_id": "intrusion-set--1",
            "entity_type": "Intrusion-Set",
            "name": "APT0",
            "pirInformation": {"pir_score": 80},
        },
        pir_id="pir-1",
    )
    found = listeners.match("opencti", "update", payload or {})
    assert found is not None and found[0].trigger_path == "relevant_threat"
    assert found[1]["evidence"]["pir"] == [{"pir_id": "pir-1", "score_at_delivery": 80}]


def test_a_type_the_listener_does_not_read_is_left_out() -> None:
    assert resync.delivery({"id": "i", "entity_type": "Report", "name": "r"}) is None


# ── windows ─────────────────────────────────────────────────────────────


def test_the_defaults_are_seven_days_for_vulnerabilities_thirty_for_the_world_and_open_inside() -> None:
    windows = resync.default_windows()
    assert windows["vulnerability"] == 7
    assert {windows[k] for k in ("intrusion_set", "campaign", "malware", "pir", "third_party")} == {30}
    assert windows["aikido"] is None and windows["github"] is None
    assert "attack_surface" not in windows  # a pass reads the current state


@pytest.mark.parametrize(
    "raw",
    [
        {"vulnerability": 0},
        {"vulnerability": 366},
        {"vulnerability": None},  # a world route always has a window
        {"vulnerability": 2.5},
        {"vulnerability": True},
        {"attack_surface": 3},
        {"gitlab": 3},  # no listing: the route reads no window
        {"nope": 3},
    ],
)
def test_a_bad_window_is_refused(raw: dict[str, Any]) -> None:
    with pytest.raises(resync.WindowError):
        resync.validate_windows(raw)


def test_a_route_the_resync_does_not_read_offers_no_window() -> None:
    # UX round 1 of #2590 (B3): an unlisted route is drawn read-only with its reason, never as a field.
    described = {r["key"]: r for r in resync.describe_windows()}
    for key in resync.UNLISTED:
        assert described[key]["windowed"] is False and described[key]["note"]
        assert key not in resync.default_windows()
    assert all(r["note"] is None for r in described.values() if r["windowed"])


def test_an_inside_route_may_have_no_age_limit_or_a_cap() -> None:
    windows = resync.validate_windows({"aikido": 30, "github": None, "malware": 365})
    assert windows["aikido"] == 30 and windows["github"] is None and windows["malware"] == 365


# ── the world routes ────────────────────────────────────────────────────


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _one_source(monkeypatch: pytest.MonkeyPatch, gql: Any) -> None:
    async def _sources(db: Any) -> list[Any]:
        return [SimpleNamespace(integration_id="i1", run_as_user_id="u1")]

    async def _conn(db: Any, integration_id: str, user_id: Any) -> Any:
        return gql, integration_id, "secret"

    monkeypatch.setattr(resync, "_opencti_sources", _sources)
    monkeypatch.setattr("app.services.opencti_trigger_provisioning.integration_connection", _conn)


@pytest.mark.asyncio
async def test_no_type_is_cut_by_another_types_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    """#2578 acceptance: 25k vulnerabilities and 50 actors in the window, every actor resynced."""
    asked: list[dict[str, Any]] = []

    async def _gql(query: str, variables: dict[str, Any]) -> tuple[dict[str, Any], None]:
        asked.append(variables)
        kind = variables["types"][0]
        total = {"Vulnerability": 25000, "Intrusion-Set": 50}.get(kind, 0)
        start = int(variables["after"] or 0)
        size = min(variables["first"], total - start)
        edges = [
            {"node": {"id": f"{kind}-{start + n}", "entity_type": kind, "name": f"{kind} {start + n}"}}
            for n in range(size)
        ]
        end = start + size
        return {
            "stixCoreObjects": {"edges": edges, "pageInfo": {"hasNextPage": end < total, "endCursor": str(end)}}
        }, None

    async def _note(provider: str, event: str, payload: dict[str, Any], *, verified: bool) -> bool:
        return True

    _one_source(monkeypatch, _gql)
    routes = await resync.resync_world(lambda: _Session(), now=NOW, note=_note)
    assert routes["intrusion_set"]["read"] == 50 and routes["intrusion_set"]["recorded"] == 50
    # Vulnerabilities stop at their own cap, and say so.
    assert routes["vulnerability"]["read"] == resync.MAX_PER_ROUTE and routes["vulnerability"].get("capped")
    # One query per type, each with its own window.
    vulns = next(v for v in asked if v["types"] == ["Vulnerability"])
    actors = next(v for v in asked if v["types"] == ["Intrusion-Set"])
    assert vulns["filters"]["filters"][0]["values"] == ["2026-11-19T17:00:00+00:00"]
    assert actors["filters"]["filters"][0]["values"] == ["2026-10-27T17:00:00+00:00"]
    assert routes["vulnerability"]["window_days"] == 7 and routes["intrusion_set"]["window_days"] == 30


@pytest.mark.asyncio
async def test_the_world_routes_take_the_chosen_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[dict[str, Any]] = []

    async def _gql(query: str, variables: dict[str, Any]) -> tuple[dict[str, Any], None]:
        asked.append(variables)
        return {"stixCoreObjects": {"edges": [], "pageInfo": {"hasNextPage": False}}}, None

    async def _note(*a: Any, **k: Any) -> bool:
        return True

    _one_source(monkeypatch, _gql)
    windows = resync.validate_windows({"vulnerability": 1})
    await resync.resync_world(lambda: _Session(), windows=windows, now=NOW, note=_note)
    vulns = next(v for v in asked if v["types"] == ["Vulnerability"])
    assert vulns["filters"]["filters"][0]["values"] == ["2026-11-25T17:00:00+00:00"]


@pytest.mark.asyncio
async def test_pir_matches_are_read_per_pir_with_their_score(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _gql(query: str, variables: dict[str, Any]) -> tuple[dict[str, Any], None]:
        if "CtemResyncPirs" in query:
            return {"pirs": {"edges": [{"node": {"id": "pir-1"}}]}}, None
        node = {
            "id": "a",
            "standard_id": "intrusion-set--a",
            "entity_type": "Intrusion-Set",
            "name": "APT0",
            "pirInformation": {"pir_score": 70},
        }
        return {"stixDomainObjects": {"edges": [{"node": node}], "pageInfo": {"hasNextPage": False}}}, None

    seen: list[dict[str, Any]] = []

    async def _note(provider: str, event: str, payload: dict[str, Any], *, verified: bool) -> bool:
        seen.append(payload)
        return True

    async def _scope(sessions: Any) -> dict[str, int]:
        return {"pir-1": 50}

    _one_source(monkeypatch, _gql)
    monkeypatch.setattr(resync, "_pir_scope", _scope)
    out = await resync.resync_pir(lambda: _Session(), now=NOW, note=_note)
    assert out["read"] == 1 and out["recorded"] == 1 and out["window_days"] == 30
    assert seen[0]["xtm_source"] == "pir_playbook"


@pytest.mark.asyncio
async def test_a_pir_the_playbooks_do_not_listen_to_and_a_match_below_its_minimum_are_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of #2590: only the selected PIRs, each at its own minimum score, as the live playbook."""
    asked: list[str] = []

    async def _gql(query: str, variables: dict[str, Any]) -> tuple[dict[str, Any], None]:
        if "CtemResyncPirs" in query:
            return {"pirs": {"edges": [{"node": {"id": "pir-on"}}, {"node": {"id": "pir-off"}}]}}, None
        asked.append(variables["pirId"])
        nodes = [
            {
                "id": f"a{score}",
                "standard_id": f"intrusion-set--{score}",
                "entity_type": "Intrusion-Set",
                "name": f"APT{score}",
                "pirInformation": {"pir_score": score},
            }
            for score in (40, 75)
        ]
        edges = [{"node": n} for n in nodes]
        return {"stixDomainObjects": {"edges": edges, "pageInfo": {"hasNextPage": False}}}, None

    seen: list[dict[str, Any]] = []

    async def _note(provider: str, event: str, payload: dict[str, Any], *, verified: bool) -> bool:
        seen.append(payload)
        return True

    async def _scope(sessions: Any) -> dict[str, int]:
        return {"pir-on": 60}

    _one_source(monkeypatch, _gql)
    monkeypatch.setattr(resync, "_pir_scope", _scope)
    out = await resync.resync_pir(lambda: _Session(), now=NOW, note=_note)
    assert asked == ["pir-on"]  # the PIR no playbook listens to is never read
    assert out["read"] == 1 and out["recorded"] == 1 and out["below_minimum"] == 1
    assert seen[0]["data"]["name"] == "APT75"


@pytest.mark.asyncio
async def test_no_selected_pir_skips_the_route_with_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _scope(sessions: Any) -> dict[str, int]:
        return {}

    monkeypatch.setattr(resync, "_pir_scope", _scope)
    out = await resync.resync_pir(lambda: _Session(), now=NOW, note=None)
    assert out["read"] == 0 and "no enabled PIR" in out["skipped"]


# ── inside state, passes, the whole step ────────────────────────────────


def test_an_inside_item_outside_the_window_is_left_and_an_undated_one_kept() -> None:
    since = datetime(2026, 11, 1, tzinfo=UTC)
    assert resync._in_window({"created_at": "2026-11-20T00:00:00Z"}, since, ("created_at",))
    assert not resync._in_window({"created_at": "2026-10-20T00:00:00Z"}, since, ("created_at",))
    assert resync._in_window({}, since, ("created_at",))
    assert resync._in_window({"created_at": "2020-01-01T00:00:00Z"}, None, ("created_at",))


async def test_github_organizations_pair_each_login_with_its_own_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user account sorted before an organization never takes its place; the column is read through the vault."""
    installs = [
        {"account_login": "zeta-org", "account_type": "Organization"},
        {"account_login": "alice", "account_type": "User"},
        {"account_login": "zeta-org", "account_type": "Organization"},
    ]

    async def _reveal(_entity: object, _column: str, **_kw: object) -> dict[str, object]:
        return {"installations": installs}

    monkeypatch.setattr("app.services.vault.reveal_json_field", _reveal)
    assert await resync.github_organizations(SimpleNamespace(extra_config_enc="x")) == ["zeta-org"]
    assert await resync.github_organizations(SimpleNamespace(extra_config_enc=None)) == []


def test_a_pass_says_why_it_raised_nothing() -> None:
    assert resync.pass_outcome(None)["skipped"].startswith("off in the profile")
    assert (
        resync.pass_outcome({"recorded": 0, "observed": 12})["note"] == "the pass ran and found no difference to raise"
    )
    assert "note" not in resync.pass_outcome({"recorded": 3})
    assert resync.pass_outcome({"skipped": "no Shodan key"})["recorded"] == 0


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
    assert out["evidence_health"] == {"recorded": 0, "error": "RuntimeError: OpenCRQ down"}


@pytest.mark.asyncio
async def test_the_step_reports_every_route_and_survives_a_failed_one(monkeypatch: pytest.MonkeyPatch) -> None:
    actors: dict[str, Any] = {}

    def _route(key: str, recorded: int) -> Any:
        async def _run(sessions: Any, *, windows: Any, **kw: Any) -> dict[str, Any]:
            actors[key] = kw.get("actor_id")
            return {**resync._route(key, windows), "read": recorded, "recorded": recorded}

        return _run

    async def _world(sessions: Any, *, windows: Any) -> dict[str, Any]:
        return {"vulnerability": {**resync._route("vulnerability", windows), "recorded": 4}}

    async def _boom(sessions: Any, *, windows: Any, **kw: Any) -> dict[str, Any]:
        raise RuntimeError("Aikido 503")

    async def _passes(sessions: Any) -> dict[str, Any]:
        return {"attack_surface": {"recorded": 2}, "evidence_health": resync.pass_outcome(None)}

    async def _by_path(sessions: Any) -> dict[str, Any]:
        return {"relevant_threat": {"signals": 1, "cases": 0}}

    monkeypatch.setattr(resync, "resync_world", _world)
    monkeypatch.setattr(resync, "resync_pir", _route("pir", 1))
    monkeypatch.setattr(resync, "resync_third_party", _route("third_party", 0))
    monkeypatch.setattr(resync, "resync_aikido", _boom)
    monkeypatch.setattr(resync, "resync_github", _route("github", 5))
    monkeypatch.setattr(resync, "resync_passes", _passes)
    monkeypatch.setattr(resync, "by_trigger_path", _by_path)
    progress: dict[str, int] = {}

    async def _progress(key: str, n: int) -> None:
        progress[key] = n

    out = await resync.resync(sessions=None, actor_id="admin-1", progress=_progress, windows={"vulnerability": 3})  # type: ignore[arg-type]
    # The inside-state routes read as the administrator who started the purge; the others as their sources.
    assert actors == {"pir": None, "third_party": None, "github": "admin-1"}
    routes = out["routes"]
    assert out["windows"]["vulnerability"] == 3
    assert routes["aikido"]["errors"] == ["RuntimeError: Aikido 503"]
    assert routes["gitlab"]["skipped"] and routes["openaev"]["skipped"]
    assert routes["evidence_health"]["skipped"].startswith("off in the profile")
    assert progress == {
        "vulnerability": 4,
        "pir": 1,
        "third_party": 0,
        "aikido": 0,
        "github": 5,
        "attack_surface": 2,
        "evidence_health": 0,
    }
    assert out["by_path"] == {"relevant_threat": {"signals": 1, "cases": 0}}
