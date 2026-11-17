"""The stale CTEM states of the lab (#12381): ``LAB_CTEM_STALE=on`` ages the stored briefing, stage assessments and
cyber posture past their windows, and the stub model completes a briefing or assessment regeneration.

``seed_ctem_actions.py`` and ``lab_asgi`` are loaded by path (``lab_asgi`` with a stand-in for ``app.main``, as
test_lab_seed_declarations.py does); the stub's answer is compiled from ``lab.py``'s syntax tree.
dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import sys
import tomllib
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB_DIR = ROOT / ".xtm-one" / "lab"
CHECKS = ROOT / ".xtm-one" / "checks.toml"
LAB = LAB_DIR / "lab.py"
DOCS = ROOT / "dev-docs" / "reference" / "agentic-labs.md"

pytestmark = pytest.mark.skipif(not CHECKS.is_file() or not LAB.is_file(), reason="no .xtm-one next to the backend")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _lab_asgi(monkeypatch):
    async def backend(scope, receive, send):
        return None

    monkeypatch.setitem(sys.modules, "app.main", types.SimpleNamespace(application=backend))
    monkeypatch.syspath_prepend(str(LAB_DIR))
    return _load("lab_asgi_stale_under_test", LAB_DIR / "lab_asgi.py")


def _stub(env: dict[str, str]) -> dict:
    """The stub's CTEM artifact answer and its helpers, compiled from lab.py with *env* as the environment."""
    names = {
        "LAB_CTEM_STALE_STATES",
        "LAB_CTEM_ARTIFACT_HOLD_SECONDS",
        "LAB_CTEM_STAGE_NAMES",
        "lab_switch",
        "ctem_artifact_answer",
        "ctem_artifact_hold_seconds",
    }
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    nodes = [
        n
        for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in names)
        or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets))
    ]
    namespace: dict = {"os": types.SimpleNamespace(environ=env), "json": json, "log": lambda _line: None}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(LAB), "exec"), namespace)  # noqa: S102 - the repo's own file
    return namespace


def _request(system: str, vertical: str = "cyber") -> dict:
    return {
        "messages": [
            {"role": "system", "content": f"{system}\nActive CTEM vertical: {vertical} (Cyber)."},
            {"role": "user", "content": "Assess."},
        ]
    }


def test_the_stale_seed_stores_every_artifact_past_its_window_and_the_default_stays_fresh():
    from app.api.v1 import ctem_metrics

    seed = _load("lab_seed_ctem_actions_stale", LAB_DIR / "seed_ctem_actions.py")
    now = datetime.now(UTC)
    assert seed.stored_age({}) == timedelta(0)
    assert seed.stored_age({"LAB_CTEM_STALE": "off"}) == timedelta(0)
    assert seed.stored_age({"LAB_CTEM_STALE": " On "}) == seed.STALE_AGE
    agents = {key: f"agent-{key}" for key in (*seed.STAGE_ACTIONS, "briefing")}
    stale = seed.plan(agents, now - seed.stored_age({"LAB_CTEM_STALE": "on"}))
    fresh = seed.plan(agents, now - seed.stored_age({}))
    for row in stale:
        assert ctem_metrics._assessment_is_stale(row["created_at"], now), row["key"]
        assert ctem_metrics._briefing_is_stale(row["created_at"], now), row["key"]
    for row in fresh:
        assert not ctem_metrics._assessment_is_stale(row["created_at"], now), row["key"]
        assert not ctem_metrics._briefing_is_stale(row["created_at"], now), row["key"]


def test_the_stale_posture_is_cached_past_the_soft_ttl_until_a_force_sync(monkeypatch):
    from app.api.v1 import ctem_metrics

    module = _lab_asgi(monkeypatch)
    built: list[bool] = []
    stored: dict[str, dict] = {}

    async def build(db, user, vertical, client, *, force=False):
        built.append(force)
        return {"generated_at": datetime.now(UTC).isoformat(), "shape": ctem_metrics._POSTURE_SHAPE}, {}

    async def cache_set(client, key, value, ttl):
        stored[key] = value

    monkeypatch.setattr(ctem_metrics, "_build_and_cache", build)
    monkeypatch.setattr(ctem_metrics, "_cache_set_json", cache_set)
    assert module.stale_ctem_posture({}) is False
    assert ctem_metrics._build_and_cache is build
    assert module.stale_ctem_posture({"LAB_CTEM_STALE": "on"}) is True
    aged = ctem_metrics._build_and_cache
    user = types.SimpleNamespace(id="u1")
    posture_key, _ = ctem_metrics._posture_cache_keys("u1", "cyber")

    payload, _ = asyncio.run(aged(None, user, "cyber", None))
    assert ctem_metrics._payload_age_seconds(payload) > ctem_metrics._SOFT_TTL
    assert stored[posture_key] is payload
    assert not ctem_metrics._cached_is_fresh({**payload, "profile_version": None}, profile_stamp=None)

    stored.clear()
    payload, _ = asyncio.run(aged(None, user, "cyber", None, force=True))
    assert ctem_metrics._payload_age_seconds(payload) < ctem_metrics._SOFT_TTL
    assert stored == {}
    payload, _ = asyncio.run(aged(None, user, "cyber", None))
    assert ctem_metrics._payload_age_seconds(payload) < ctem_metrics._SOFT_TTL, "a synced posture stays fresh"
    assert built == [False, True, False]


def test_the_stub_completes_a_briefing_and_every_stage_assessment_in_the_analysts_schema():
    from jsonschema import validate

    from app.agents.builtin_agents import _EXPOSURE_BRIEFING_SCHEMA, _STAGE_AGENTS, _STAGE_ASSESSMENT_SCHEMA
    from app.api.v1 import ctem_metrics

    stub = _stub({"LAB_CTEM_STALE": "on"})
    briefing = json.loads(stub["ctem_artifact_answer"](_request("You are the Exposure Briefing Analyst for XTM One")))
    validate(briefing, _EXPOSURE_BRIEFING_SCHEMA)
    assert briefing["vertical"] == "cyber"
    assert ctem_metrics._parse_briefing(json.dumps(briefing))["vertical"] == "cyber"
    stages = set()
    for agent in _STAGE_AGENTS:
        answer = json.loads(stub["ctem_artifact_answer"](_request(agent["persona"], "fimi")))
        validate(answer, _STAGE_ASSESSMENT_SCHEMA)
        assert answer["vertical"] == "fimi"
        assert answer["recommended_action"]
        stages.add(answer["stage"])
    assert stages == set(stub["LAB_CTEM_STAGE_NAMES"].values())
    assert stub["ctem_artifact_answer"](_request("You are a helpful assistant.")) is None


def test_the_stub_answer_is_off_by_default_and_its_hold_is_bounded():
    assert _stub({})["ctem_artifact_answer"](_request("You are the Exposure Briefing Analyst")) is None
    assert (
        _stub({"LAB_CTEM_STALE": "off"})["ctem_artifact_answer"](_request("You are the CTEM Scoping Analyst")) is None
    )
    assert _stub({})["ctem_artifact_hold_seconds"]() == 8
    assert _stub({"LAB_CTEM_REGEN_SECONDS": "30"})["ctem_artifact_hold_seconds"]() == 30
    assert _stub({"LAB_CTEM_REGEN_SECONDS": "601"})["ctem_artifact_hold_seconds"]() == 8
    assert _stub({"LAB_CTEM_REGEN_SECONDS": "soon"})["ctem_artifact_hold_seconds"]() == 8


def test_the_stub_answers_ctem_artifacts_only_after_lab_up():
    source = ast.unparse(
        next(
            n
            for n in ast.walk(ast.parse(LAB.read_text(encoding="utf-8")))
            if isinstance(n, ast.FunctionDef) and n.name == "do_POST"
        )
    )
    assert "if drafted is None and LAB_UP.is_set():" in source
    assert "ctem_artifact_answer(request)" in source
    assert "time.sleep(ctem_artifact_hold_seconds())" in source


def test_the_stale_labs_are_declared_with_the_worker_and_documented():
    services = tomllib.loads(CHECKS.read_text(encoding="utf-8"))["service"]
    for name, theme in (("lab-ctem-stale", None), ("lab-ctem-stale-light", "light")):
        env = services[name]["env"]
        assert env["LAB_CTEM_STALE"] == "on"
        assert env["LAB_CTEM_ACTIONS"] == "on"
        assert env["LAB_WORKER"] == "1"
        assert env["PLATFORM_MODE"] == "xtm_one"
        assert env.get("LAB_THEME") == theme
    assert not any(
        "LAB_CTEM_STALE" in (service.get("env") or {})
        for name, service in services.items()
        if not name.startswith("lab-ctem-stale")
    )
    docs = DOCS.read_text(encoding="utf-8")
    assert "[service.lab-ctem-stale]" in docs and "LAB_CTEM_REGEN_SECONDS" in docs
