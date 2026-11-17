"""The evaluation family: the platform catalog pinned to the package, family selection on the
evaluation API, the run-trigger family gate, and the token-F1 retirement.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException

import app.models  # noqa: F401 - register model metadata
from app.api.v1 import evaluations as evaluations_api
from app.api.v1.schemas.evaluation import (
    CreateEvaluationRequest,
    UpdateEvaluationRequest,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token

# Importing the suite directly is allowed HERE: this is a test-only guard, and
# the encapsulation scan covers app/ modules, not tests.
from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality import Family, FamilyConfig, run_for_family
from app.models.agent import Agent
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.dataset_version import DatasetVersion, DatasetVersionEntry
from app.models.evaluation import Evaluation, EvaluationAgent, EvaluationResult, EvaluationRun
from app.models.flow import Flow
from app.models.user import User
from app.services.evaluation.metric_package import families
from app.services.evaluation.scorers.registry import invalid_keys
from tests.scratch_database import scratch_database

# ── The platform family catalog ──
# The guard tests pin the hardcoded adapter catalog to the package: if the package adds,
# removes or renames a family, these fail rather than letting the platform catalog drift.


def test_declared_ids_match_package_enum() -> None:
    """The adapter's declared id set is exactly the package's Family enum."""
    assert set(families.FAMILY_IDS) == {m.value for m in Family}


@pytest.mark.parametrize("family_id", families.FAMILY_IDS)
def test_every_declared_family_is_routed_to_its_own_metrics(family_id: str) -> None:
    """Every declared family has package metrics, routed to its own runner.

    On an empty config each runner raises for the configuration it requires; a family
    without a runner would raise that no metrics are implemented for it instead, and a
    fall-through would raise another family's requirement.
    """
    with pytest.raises(ConfigurationError, match=f"family {family_id} requires"):
        run_for_family("", "", Family(family_id), FamilyConfig())


def test_resolve_family_maps_and_rejects() -> None:
    for fid in families.FAMILY_IDS:
        assert families.resolve_family(fid) is Family(fid)
    with pytest.raises(ValueError):
        families.resolve_family("NOPE")
    with pytest.raises(ValueError):
        families.resolve_family(None)


def test_family_is_valid() -> None:
    assert families.family_is_valid("STIX_BUNDLE")
    assert not families.family_is_valid("stix_bundle")  # case-sensitive
    assert not families.family_is_valid("UNKNOWN")
    # Families folded into STRUCTURED_JSON are not runnable any more.
    assert not families.family_is_valid("DETECTION_RULE")
    assert not families.family_is_valid("JSON_ANALYSIS")


def test_prose_lists_its_metrics_in_the_family_picker() -> None:
    """The picker's prose entry lists its metrics: the description and the
    metric-key tuple are authored strings that must agree."""
    prose = next(f for f in families.list_families() if f["id"] == "PROSE")

    # The phrase metrics stay hidden until phrase lists are configurable.
    assert set(prose["metric_keys"]) == {
        "prose.exact_match",
        "prose.token_prf",
        "prose.rouge_l",
    }
    assert "not yet implemented" not in prose["description"]
    # The only place an administrator learns the normalization before running anything.
    assert "ignores case" in prose["description"]
    assert "punctuation around a word" in prose["description"]
    # Prose reads no caller-supplied schema, so the output-schema input stays hidden.
    assert prose["structured_output"] is False


def test_comparison_and_baseline_never_branch_on_the_evaluation_family() -> None:
    """Prose ranks agents and designates a baseline like any other family.

    Asserted structurally: the thing to prevent is a family-conditional branch added
    to aggregation later, which a behavioural test cannot foresee.
    """
    import inspect

    from app.services.evaluation import aggregation

    source = inspect.getsource(aggregation)
    for family_id in families.FAMILY_IDS:
        assert family_id not in source, (
            f"{family_id} is referenced in aggregation.py. Headline composition, ranking "
            "and baseline comparison must be family-agnostic; per-family behaviour "
            "belongs in the metric_package adapter."
        )


def test_list_families_shape() -> None:
    catalog = families.list_families()
    assert {entry["id"] for entry in catalog} == {m.value for m in Family}
    assert {entry["id"] for entry in catalog} == {"STIX_BUNDLE", "STRUCTURED_JSON", "CLASSIFICATION", "PROSE"}
    # Exactly one JSON family carries the schema input: with two, the picker's
    # choice silently decided whether the evaluation produced any deterministic metric.
    json_families = [entry for entry in catalog if entry["structured_output"]]
    assert [entry["id"] for entry in json_families] == ["STRUCTURED_JSON"]
    assert json_families[0]["label"] == "JSON"
    for entry in catalog:
        assert set(entry) == {
            "id",
            "label",
            "description",
            "metric_keys",
            "structured_output",
        }
        assert isinstance(entry["structured_output"], bool)
        # Every family advertises its metric keys.
        assert isinstance(entry["metric_keys"], list)
        assert entry["metric_keys"]


# ── Family selection on the evaluation API ──
# The real create/update handlers and ``GET /evaluations/families`` against an isolated
# PostgreSQL database: an unknown or removed family is rejected with 400, Judge on or off;
# every declared family is persisted and echoed with the Judge off; ``GET /families`` is
# readable by a non-admin through the router and its real auth dependency.


_TABLES = [
    User.__table__,
    Agent.__table__,
    Evaluation.__table__,
    Flow.__table__,
    EvaluationAgent.__table__,
    Dataset.__table__,
    DatasetEntry.__table__,
    EvaluationDataset.__table__,
    EvaluationRun.__table__,
]


@pytest.fixture
def _stub_audit_and_groups(monkeypatch):
    async def _no_groups(_db, _uid):
        return []

    async def _no_audit(*_a, **_k):
        return None

    monkeypatch.setattr(evaluations_api, "get_user_group_ids", _no_groups)
    monkeypatch.setattr(evaluations_api, "log_audit", _no_audit)


async def _make_db():
    """An isolated database with the tables this module needs (``tests/scratch_database.py``).

    Returns ``(sessionmaker, cleanup)``, the shape the tests here take: the database
    is dropped once ``cleanup`` is awaited. Skips if PostgreSQL is unavailable.
    """
    stack = contextlib.AsyncExitStack()
    db = await stack.enter_async_context(scratch_database("family", tables=_TABLES))
    return db.sessions, stack.aclose


async def _admin(session):
    admin = User(email="a@x.io", full_name="Admin", hashed_password="x", role="admin")
    session.add(admin)
    await session.flush()
    await session.commit()
    return admin


async def _create(session, admin, **kwargs):
    body = CreateEvaluationRequest(name="Eval", **kwargs)
    return await evaluations_api.create_evaluation(
        body=body, user=admin, db=session, _agentic_gate=None
    )


_DECLARED_FAMILIES = ("STIX_BUNDLE", "STRUCTURED_JSON", "CLASSIFICATION", "PROSE")


@pytest.mark.usefixtures("_stub_audit_and_groups")
@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("family", _DECLARED_FAMILIES, ids=str.lower)
async def test_create_persists_every_declared_family_without_the_judge(family):
    """No declared family needs the Judge to pass the create gate; the family is stored."""
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            # An explicit empty selection is judge-off.
            resp = await _create(s, admin, family=family, scorer_selection=[])
            assert resp["family"] == family
            ev = await s.get(Evaluation, uuid.UUID(resp["id"]))
            assert ev.family == family
    finally:
        await cleanup()


@pytest.mark.usefixtures("_stub_audit_and_groups")
@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("judge", [True, False], ids=["judge_on", "judge_off"])
@pytest.mark.parametrize("family", ["NOPE", "DETECTION_RULE", "JSON_ANALYSIS"], ids=str.lower)
async def test_create_rejects_an_unknown_or_removed_family(family, judge):
    """An unrecognised family is refused with 400 whatever the Judge says.

    `DETECTION_RULE` and `JSON_ANALYSIS` are not declared families: `STRUCTURED_JSON`
    covers both, and an unrecognised family cannot be resolved at read time either.
    """
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            with pytest.raises(HTTPException) as exc:
                await _create(
                    s, admin, family=family, scorer_selection=None if judge else []
                )
            assert exc.value.status_code == 400
    finally:
        await cleanup()


@pytest.mark.usefixtures("_stub_audit_and_groups")
@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_rejects_unknown_family():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create(s, admin, family="STIX_BUNDLE")
            with pytest.raises(HTTPException) as exc:
                await evaluations_api.update_evaluation(
                    evaluation_id=created["id"],
                    body=UpdateEvaluationRequest(family="NOPE"),
                    user=admin,
                    db=s,
                )
            assert exc.value.status_code == 400
    finally:
        await cleanup()


@pytest.mark.usefixtures("_stub_audit_and_groups")
@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_changes_family():
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create(s, admin, family="STIX_BUNDLE")
            resp = await evaluations_api.update_evaluation(
                evaluation_id=created["id"],
                body=UpdateEvaluationRequest(family="CLASSIFICATION"),
                user=admin,
                db=s,
            )
            assert resp["family"] == "CLASSIFICATION"
    finally:
        await cleanup()


@pytest.mark.usefixtures("_stub_audit_and_groups")
@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_families_is_readable_by_non_admin(monkeypatch):
    """`GET /families` serves a non-admin session through the real auth dependency (rule 19).

    Only the database is injected: the bearer token is decoded and the user loaded by
    `get_current_user`, and the payload goes through `FamilyInfoResponse`.
    """
    monkeypatch.setattr(settings, "terms_of_use_enabled", False)
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            non_admin = User(email="u@x.io", full_name="User", hashed_password="x", role="user")
            s.add(non_admin)
            await s.commit()
            token = create_access_token(str(non_admin.id))

        app = FastAPI()
        app.include_router(evaluations_api.router, prefix="/api/v1/evaluations")

        async def _get_db():
            async with sm() as db:
                yield db

        app.dependency_overrides[get_db] = _get_db
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            anonymous = await client.get("/api/v1/evaluations/families")
            resp = await client.get(
                "/api/v1/evaluations/families", headers={"Authorization": f"Bearer {token}"}
            )
    finally:
        await cleanup()

    assert anonymous.status_code in (401, 403)
    assert resp.status_code == 200, resp.text
    served = resp.json()
    assert {f["id"] for f in served} == set(_DECLARED_FAMILIES)
    assert [f["id"] for f in served if f["structured_output"]] == ["STRUCTURED_JSON"]


# ── Run-trigger family gate ──
# Legacy family-less experiments stay viewable but cannot be re-run until a family is set
# (fail-closed, no default): the pure ``_require_runnable_family`` guard, the real
# ``trigger_evaluation`` handler, and a family-less evaluation's history staying readable.


_GATE_TABLES = [
    User.__table__,
    Agent.__table__,
    Evaluation.__table__,
    Flow.__table__,
    EvaluationAgent.__table__,
    Dataset.__table__,
    DatasetEntry.__table__,
    DatasetVersion.__table__,
    DatasetVersionEntry.__table__,
    EvaluationDataset.__table__,
    EvaluationRun.__table__,
    EvaluationResult.__table__,
]


# --- Pure guard unit tests (no DB) -----------------------------------------------------


def _eval(family=None, scorer_selection=None):
    return SimpleNamespace(family=family, scorer_selection=scorer_selection)


@pytest.mark.usefixtures("_stub_audit_and_groups")
@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [[], None, ["judge"]], ids=["no_scorer", "unset", "judge"])
@pytest.mark.parametrize(
    "family", [None, "", "NOPE", "DETECTION_RULE"], ids=["null", "blank", "unknown", "removed"]
)
async def test_guard_refuses_a_missing_or_unrecognised_family(family, selection):
    """No family, a blank one, an unknown one or a retired one is refused, Judge or not.

    An unrecognised family cannot be resolved at read time either, so the Judge does not
    unblock it.
    """
    with pytest.raises(HTTPException) as exc:
        evaluations_api._require_runnable_family(_eval(family=family, scorer_selection=selection))
    assert exc.value.status_code == 400
    assert "family" in exc.value.detail.lower()


@pytest.mark.usefixtures("_stub_audit_and_groups")
@pytest.mark.integration
@pytest.mark.asyncio
async def test_guard_allows_every_declared_family_without_the_judge():
    """No declared family depends on the Judge to be runnable."""
    for family in ("STIX_BUNDLE", "STRUCTURED_JSON", "CLASSIFICATION", "PROSE"):
        evaluations_api._require_runnable_family(
            _eval(family=family, scorer_selection=[])
        )


# --- Endpoint integration (admin-gated) ------------------------------------------------


async def _make_gate_db():
    """An isolated database with the tables this module needs (``tests/scratch_database.py``).

    Returns ``(sessionmaker, cleanup)``, the shape the tests here take: the database
    is dropped once ``cleanup`` is awaited. Skips if PostgreSQL is unavailable.
    """
    stack = contextlib.AsyncExitStack()
    db = await stack.enter_async_context(scratch_database("gate", tables=_GATE_TABLES))
    return db.sessions, stack.aclose


@pytest.mark.usefixtures("_stub_audit_and_groups")
@pytest.mark.integration
@pytest.mark.asyncio
async def test_trigger_refused_without_family():
    sm, cleanup = await _make_gate_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create(s, admin, family=None)
            with pytest.raises(HTTPException) as exc:
                await evaluations_api.trigger_evaluation(
                    evaluation_id=created["id"], body=None, user=admin, db=s
                )
            assert exc.value.status_code == 400
            assert "family" in exc.value.detail.lower()
    finally:
        await cleanup()


@pytest.mark.usefixtures("_stub_audit_and_groups")
@pytest.mark.integration
@pytest.mark.asyncio
async def test_trigger_proceeds_past_family_gate_once_set():
    # With a family set, the family gate passes and the handler reaches the next
    # check (no agents linked) — a DIFFERENT 400 that proves the gate cleared.
    sm, cleanup = await _make_gate_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create(s, admin, family="STIX_BUNDLE")
            with pytest.raises(HTTPException) as exc:
                await evaluations_api.trigger_evaluation(
                    evaluation_id=created["id"], body=None, user=admin, db=s
                )
            assert exc.value.status_code == 400
            assert "agent" in exc.value.detail.lower()
            assert "family" not in exc.value.detail.lower()
    finally:
        await cleanup()


@pytest.mark.usefixtures("_stub_audit_and_groups")
@pytest.mark.integration
@pytest.mark.asyncio
async def test_historical_run_readable_without_family():
    # A legacy family-less experiment with a completed run stays fully readable;
    # only *running* it is gated.
    sm, cleanup = await _make_gate_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create(s, admin, family=None)
            eval_id = uuid.UUID(created["id"])
            agent = Agent(
                user_id=admin.id,
                name="A",
                slug=f"a-{uuid.uuid4().hex[:8]}",
                persona="p",
            )
            s.add(agent)
            await s.flush()
            run = EvaluationRun(
                evaluation_id=eval_id,
                agent_id=agent.id,
                model_id="gpt-x",
                status="completed",
                trigger_reason="manual",
                total_entries=3,
                completed_entries=3,
            )
            s.add(run)
            await s.commit()

            detail = await evaluations_api.get_evaluation(
                evaluation_id=created["id"], user=admin, db=s
            )
            assert detail["family"] is None

            runs = await evaluations_api.list_runs(
                evaluation_id=created["id"],
                agent_id=None,
                model_id=None,
                limit=20,
                offset=0,
                user=admin,
                db=s,
            )
            assert [(r["id"], r["status"], r["completed_entries"]) for r in runs] == [
                (str(run.id), "completed", 3)
            ]
    finally:
        await cleanup()


# ── Token-F1 retirement ──
# The scorer registry does not resolve ``token_f1`` / ``structured_output``;
# ``quality_source == "f1"`` is rejected on create and update, while ``"judge"`` is accepted;
# ``EvaluationResult.f1_score`` stays on the model and the response, as stored rows carry
# real values.


# ── Registry (unit) ──────────────────────────────────────────────────────


@pytest.mark.usefixtures("_stub_audit_and_groups")
def test_registry_no_longer_resolves_retired_scorers():
    for retired in ("token_f1", "structured_output"):
        assert invalid_keys([retired]) == [retired]


# ── Write-path rejection (integration) ───────────────────────────────────


@pytest.mark.usefixtures("_stub_audit_and_groups")
@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_rejects_f1_quality_source():
    sm, cleanup = await _make_gate_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            with pytest.raises(HTTPException) as exc:
                await _create(s, admin, quality_source="f1")
            assert exc.value.status_code == 422
    finally:
        await cleanup()


@pytest.mark.usefixtures("_stub_audit_and_groups")
@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_allows_judge_quality_source():
    sm, cleanup = await _make_gate_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            resp = await _create(s, admin, quality_source="judge")
            assert resp["quality_source"] == "judge"
    finally:
        await cleanup()


@pytest.mark.usefixtures("_stub_audit_and_groups")
@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_rejects_f1_quality_source():
    sm, cleanup = await _make_gate_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            created = await _create(s, admin, quality_source="judge")
            with pytest.raises(HTTPException) as exc:
                await evaluations_api.update_evaluation(
                    evaluation_id=created["id"],
                    body=UpdateEvaluationRequest(quality_source="f1"),
                    user=admin,
                    db=s,
                )
            assert exc.value.status_code == 422
    finally:
        await cleanup()


# --- f1_score is kept --------------------------------------------------------


@pytest.mark.usefixtures("_stub_audit_and_groups")
def test_f1_score_is_not_removed_with_them():
    """`f1_score` stays: stored rows carry it.

    A sweep deleting every unwritten legacy scorer field would take it too and
    destroy real data.
    """
    from app.api.v1.schemas.evaluation import EvaluationResultResponse

    assert hasattr(EvaluationResult, "f1_score")
    assert "f1_score" in EvaluationResultResponse.model_fields
