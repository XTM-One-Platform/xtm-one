"""The CTEM briefing's risk movers (#11547, dev-docs/ctem-stage-viz.md row B3).

The snapshot keeps per-object values from one user's own posture build, and the
movers compare two of that user's snapshots: never a guessed value, never
another user's row.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from app.services import ctem_movers
from app.services.ctem_movers import compute_movers, snapshot_objects

BACKEND = Path(__file__).resolve().parent.parent


def _facts(
    *, technique: tuple[int, int] = (3, 4), domain_contracts: int = 12, epss: float = 0.5
) -> dict[str, dict[str, Any]]:
    return {
        "validation": {
            "outcome_matrix": {
                "chains": [],
                "items": [
                    {
                        "id": "ap-1",
                        "code": "T1059",
                        "label": "Command and Scripting Interpreter",
                        "prevention": {"success": technique[0], "total": technique[1]},
                    },
                    {"id": "ap-2", "code": "T1003", "label": "Untested", "prevention": {"success": 0, "total": 0}},
                ],
            }
        },
        "scoping": {"attack_domains": [{"name": "Endpoint", "covered": True, "contracts": domain_contracts}]},
        "prioritization": {"top_vulnerabilities": [{"name": "CVE-2026-0001", "epss": epss}]},
    }


def test_snapshot_objects_keep_only_measured_values() -> None:
    objects = snapshot_objects(_facts())
    assert objects == {
        "technique:T1059": {"label": "Command and Scripting Interpreter", "value": 75.0},
        "attack_domain:Endpoint": {"label": "Endpoint", "value": 12.0},
        "vulnerability:CVE-2026-0001": {"label": "CVE-2026-0001", "value": 50.0},
    }


def test_snapshot_objects_are_none_when_nothing_was_measured() -> None:
    assert snapshot_objects({}) is None
    assert snapshot_objects({"prioritization": {"top_vulnerabilities": [{"name": "CVE-1", "epss": None}]}}) is None


def test_snapshot_objects_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ctem_movers, "MAX_SNAPSHOT_OBJECTS", 2)
    assert len(snapshot_objects(_facts()) or {}) == 2


def test_movers_need_two_snapshots() -> None:
    current = snapshot_objects(_facts())
    empty: list[dict[str, dict[str, Any]] | None] = [None, {}]
    for previous in empty:
        assert compute_movers(previous, current) == {"status": "insufficient_history", "since": None, "items": []}
    assert compute_movers(current, None)["status"] == "insufficient_history"


def test_movers_rank_by_absolute_change_with_signed_deltas() -> None:
    previous = snapshot_objects(_facts(technique=(3, 4), domain_contracts=12, epss=0.5))
    current = snapshot_objects(_facts(technique=(1, 4), domain_contracts=20, epss=0.52))
    movers = compute_movers(previous, current, since="2026-11-12", limit=5)
    assert movers["status"] == "ready" and movers["since"] == "2026-11-12"
    assert [(m["id"], m["delta"], m["unit"], m["change"]) for m in movers["items"]] == [
        ("T1059", -50.0, "%", "changed"),
        ("Endpoint", 8.0, "contracts", "changed"),
        ("CVE-2026-0001", 2.0, "% EPSS", "changed"),
    ]
    assert movers["items"][0]["value"] == 25.0 and movers["items"][0]["previous"] == 75.0
    assert len(compute_movers(previous, current, limit=1)["items"]) == 1


def test_a_technique_that_appears_or_goes_is_a_mover_a_vulnerability_leaving_the_top_list_is_not() -> None:
    previous = {
        "technique:T1": {"label": "Old", "value": 40.0},
        "vulnerability:CVE-A": {"label": "CVE-A", "value": 90.0},
    }
    current = {
        "technique:T2": {"label": "New", "value": 60.0},
        "vulnerability:CVE-B": {"label": "CVE-B", "value": 80.0},
    }
    items = compute_movers(previous, current)["items"]
    assert [(m["id"], m["delta"], m["change"], m["value"], m["previous"]) for m in items] == [
        ("T2", 60.0, "new", 60.0, None),
        ("T1", -40.0, "gone", None, 40.0),
    ]


def test_nothing_moved_is_ready_and_empty() -> None:
    objects = snapshot_objects(_facts())
    assert compute_movers(objects, objects) == {"status": "ready", "since": None, "items": []}


def test_an_unknown_kind_is_ignored() -> None:
    assert compute_movers({"case:1": {"value": 1}}, {"case:1": {"value": 5}})["items"] == []


def _function(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
    return ast.unparse(node)


def test_the_movers_route_reads_only_the_current_users_rows() -> None:
    """Per-user segregation, as the posture cache: the query filters on the caller."""
    source = _function(BACKEND / "app" / "api" / "v1" / "ctem_metrics.py", "ctem_posture_movers")
    assert "CtemPostureSnapshot.user_id == user.id" in source
    assert "Depends(get_current_user)" in source


def test_the_snapshot_upsert_writes_objects_and_keeps_them_on_an_empty_build() -> None:
    source = _function(BACKEND / "app" / "api" / "v1" / "ctem_metrics.py", "_record_and_load_history")
    assert "objects = snapshot_objects(stage_facts)" in source
    assert "if objects is not None:" in source
    assert "updates['objects'] = objects" in source


def test_an_empty_build_stores_sql_null_not_json_null() -> None:
    """A day first written by a build that measured nothing must not match IS NOT NULL."""
    from app.models.ctem_posture_snapshot import CtemPostureSnapshot

    assert CtemPostureSnapshot.__table__.c.objects.type.none_as_null is True
