"""An evaluation's settings survive every path that writes them.

Create, copy, export and import read one list of settings, and an update tells
an explicit ``null`` from an omitted field.
"""

from __future__ import annotations

import io
import uuid
import zipfile

import pytest
from fastapi import UploadFile
from sqlalchemy import select

import app.models  # noqa: F401 - register model metadata
from app.api.v1 import evaluations as evaluations_api
from app.api.v1.schemas.evaluation import (
    CreateEvaluationRequest,
    UpdateEvaluationRequest,
)
from app.models.agent import Agent
from app.models.dataset import Dataset
from app.models.evaluation import Evaluation, EvaluationAgent
from app.services.evaluation.resume_secret import without_resume_secret
from app.services.evaluation.settings import (
    EVALUATION_AGENT_NOT_SETTINGS,
    EVALUATION_AGENT_SETTINGS,
    EVALUATION_NOT_SETTINGS,
    EVALUATION_SETTINGS,
    evaluation_agent_settings,
    evaluation_settings,
)
from tests.evaluation_database import (
    _admin,
    _make_db_full,
    _stub_audit_and_groups,  # noqa: F401 - fixture
)
from tests.evaluation_helpers import seed_provider
from tests.test_evaluation_export_import import _asgi_request

# ── One list ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "settings", "not_settings"),
    [
        pytest.param(Evaluation, EVALUATION_SETTINGS, EVALUATION_NOT_SETTINGS, id="evaluation"),
        pytest.param(EvaluationAgent, EVALUATION_AGENT_SETTINGS, EVALUATION_AGENT_NOT_SETTINGS, id="agent_link"),
    ],
)
def test_every_column_is_a_setting_or_named_as_not_one(model, settings, not_settings):
    """A new column fails here until it is placed: then every path carries it, or none."""
    columns = {column.name for column in model.__table__.columns}
    assert set(settings).isdisjoint(not_settings)
    assert columns == set(settings) | set(not_settings)


@pytest.mark.parametrize("request_model", [CreateEvaluationRequest, UpdateEvaluationRequest])
def test_create_and_update_take_every_setting(request_model):
    assert set(EVALUATION_SETTINGS) <= set(request_model.model_fields)


def test_an_import_reads_every_setting():
    # Key equality: a setting the import does not read by name is missing here.
    assert set(evaluations_api._imported_settings({}, "full_loop")) == set(EVALUATION_SETTINGS)


@pytest.mark.parametrize(
    ("package", "expected"),
    [
        pytest.param({"speed_tolerance": 7.5}, 7.5, id="older-package"),
        pytest.param({"speed_tolerance": 7.5, "performance_tolerance": 2.0}, 2.0, id="current-name-wins"),
        pytest.param({"speed_tolerance": None}, 5.0, id="null-takes-the-default"),
    ],
)
def test_an_import_reads_an_older_packages_speed_tolerance(package, expected):
    assert evaluations_api._imported_settings(package, "full_loop")["performance_tolerance"] == expected


def test_an_export_writes_every_setting():
    exported = evaluations_api._exported_settings(Evaluation(name="E", trigger_config={}))
    assert set(exported) == set(EVALUATION_SETTINGS)


# ── create → copy → export → import ──────────────────────────────────────────

_EXTRACTOR = {
    "source": {"kind": "agent_output"},
    "operations": [{"op": "find_json"}, {"op": "parse_json"}],
    "output_kind": "object",
}

# Every setting away from its default, so a path that drops one is seen.
_SETTINGS = {
    "description": "Every setting set",
    "family": "STRUCTURED_JSON",
    "trigger_config": {
        "schedule": "0 0 * * 1",
        "on_agent_change": True,
        "await_callback": True,
        "callback_timeout_seconds": 120,
        "resume_secret": "s3cret",
    },
    "use_agent_default_model": False,
    "model_ids": ["model-a", "model-b"],
    "disabled_model_ids": ["model-b"],
    "execution_mode": "llm_only",
    "apply_prompt_guards": True,
    "require_tool_approval": True,
    "run_mode": "sequential",
    "evaluator_model": "judge-model",
    "evaluator_prompt": "Judge strictly.",
    "evaluation_instructions": "Ignore the casing.",
    "judge_max_tokens": 1024,
    "performance_tolerance": 7.5,
    "sub_score_weights": {
        "tool_selection_quality": 0.1,
        "response_completeness": 0.2,
        "response_accuracy": 0.4,
        "instruction_adherence": 0.2,
        "coherence": 0.1,
    },
    "quality_source": "judge",
    "penalty_factor": 0.25,
    "timeout_seconds": 900,
    "custom_params": {"suite": "nightly"},
    "tags": {"team": "red"},
    "field_scoring_overrides": {"field_modes": {"/severity": "exact"}, "field_weights": {"/summary": 2.0}},
    "scorer_selection": [],
    "reuse_default": "off",
    "overall_quality_metrics": ["json.field_accuracy"],
    "overall_performance_metrics": [],
    # Persona only: the mode above is llm_only. Stored as the API stores it.
    "placebo_config": {
        "targets": [{"kind": "persona", "skill_id": None}],
        "include_no_target": False,
        "trials_per_entry": 7,
        "custom_text": "Neutral notes about files.",
    },
}


def test_the_fixture_sets_every_setting():
    assert set(_SETTINGS) == set(EVALUATION_SETTINGS)


async def _evaluation_with_every_setting(s):
    admin = await _admin(s)
    agents = [Agent(user_id=admin.id, name=f"Agent {i}", slug=f"agent-{uuid.uuid4().hex[:8]}") for i in range(2)]
    dataset = Dataset(name="ds", family="STRUCTURED_JSON")
    s.add_all([*agents, dataset])
    # A model a save adds must be one a provider of the owner offers:
    # these and the ones the update below sets.
    await seed_provider(s, [*_SETTINGS["model_ids"], *_OTHER_SETTINGS["model_ids"]])
    await s.commit()
    created = await evaluations_api.create_evaluation(
        body=CreateEvaluationRequest(
            name="Settings",
            agent_ids=[str(a.id) for a in agents],
            dataset_ids=[str(dataset.id)],
            agent_extractors={str(agents[0].id): _EXTRACTOR},
            **_SETTINGS,
        ),
        user=admin,
        db=s,
        _agentic_gate=None,
    )
    links = {link.agent_id: link for link in (await s.execute(select(EvaluationAgent))).scalars()}
    links[agents[0].id].tags = {"variant": "a"}
    links[agents[1].id].is_baseline = True
    await s.commit()
    return admin, uuid.UUID(created["id"]), agents


async def _settings_of(s, evaluation_id):
    evaluation = await s.get(Evaluation, evaluation_id)
    await s.refresh(evaluation)
    links = list(
        (
            await s.execute(
                select(EvaluationAgent)
                .where(EvaluationAgent.evaluation_id == evaluation_id)
                .order_by(EvaluationAgent.created_at, EvaluationAgent.id)
            )
        ).scalars()
    )
    return evaluation, evaluation_settings(evaluation), links


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
async def test_every_setting_survives_create_copy_export_and_import():
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, source_id, agents = await _evaluation_with_every_setting(s)
            source, source_settings, source_links = await _settings_of(s, source_id)

            # Create kept every setting, the secret encrypted.
            expected = {**_SETTINGS, "trigger_config": source.trigger_config}
            assert source_settings == expected
            assert without_resume_secret(source.trigger_config) == without_resume_secret(_SETTINGS["trigger_config"])

            copied = await evaluations_api.copy_evaluation(evaluation_id=str(source_id), user=admin, db=s)
            _copy, copy_settings, copy_links = await _settings_of(s, uuid.UUID(copied["id"]))
            # A copy has no trigger (FR-023), and every other setting.
            assert copy_settings == {**source_settings, "trigger_config": {}}
            assert {link.agent_id: evaluation_agent_settings(link) for link in copy_links} == {
                link.agent_id: evaluation_agent_settings(link) for link in source_links
            }

            # The export calls the real agent export, which needs the request.
            response = await evaluations_api.export_evaluation(
                evaluation_id=str(source_id), request=_asgi_request(), user=admin, db=s
            )
            package = await evaluations_api._streaming_response_to_bytes(response)
            assert zipfile.ZipFile(io.BytesIO(package)).testzip() is None

            imported = await evaluations_api.import_evaluation(
                request=_asgi_request(),
                file=UploadFile(filename="e.zip", file=io.BytesIO(package)),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            imported_row, imported_settings, imported_links = await _settings_of(s, uuid.UUID(imported["id"]))

        # An import has no resume secret, and its automatic runs paused.
        assert imported_settings == {
            **source_settings,
            "trigger_config": without_resume_secret(source.trigger_config),
        }
        assert imported_row.enabled is False
        # Its agents are new ones, in the source's order, with their settings.
        assert [evaluation_agent_settings(link) for link in imported_links] == [
            evaluation_agent_settings(link) for link in source_links
        ]
        assert {link.agent_id for link in imported_links}.isdisjoint(a.id for a in agents)
    finally:
        await cleanup()


# ── An update: an explicit null clears, an omitted field is kept ─────────────


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
async def test_an_update_clears_a_setting_sent_as_null_and_keeps_one_left_out():

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, evaluation_id, _agents = await _evaluation_with_every_setting(s)

            # The page's save: the judge model back to the platform default, no
            # field override left, its schedule and on-agent-change only.
            await evaluations_api.update_evaluation(
                evaluation_id=str(evaluation_id),
                body=UpdateEvaluationRequest(
                    evaluator_model=None,
                    field_scoring_overrides=None,
                    trigger_config={"schedule": None, "on_agent_change": False},
                ),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            evaluation, settings, _links = await _settings_of(s, evaluation_id)

        assert settings["evaluator_model"] is None
        assert settings["field_scoring_overrides"] is None
        # The callback settings the page does not edit are kept, and the secret;
        # the schedule sent as null is removed.
        assert without_resume_secret(evaluation.trigger_config) == {
            "on_agent_change": False,
            "await_callback": True,
            "callback_timeout_seconds": 120,
        }
        assert "resume_secret_enc" in evaluation.trigger_config
        # Every setting not sent is unchanged.
        untouched = set(EVALUATION_SETTINGS) - {"evaluator_model", "field_scoring_overrides", "trigger_config"}
        assert {k: settings[k] for k in untouched} == {k: _SETTINGS[k] for k in untouched}
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
async def test_null_on_a_setting_that_cannot_be_null_changes_nothing():

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, evaluation_id, _agents = await _evaluation_with_every_setting(s)
            await evaluations_api.update_evaluation(
                evaluation_id=str(evaluation_id),
                body=UpdateEvaluationRequest(run_mode=None, timeout_seconds=None, model_ids=None),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            _evaluation, settings, _links = await _settings_of(s, evaluation_id)

        assert (settings["run_mode"], settings["timeout_seconds"], settings["model_ids"]) == (
            "sequential",
            900,
            ["model-a", "model-b"],
        )
    finally:
        await cleanup()


# ── An import is refused as create refuses it, before anything is imported ──


async def _counts(s) -> tuple[int, int, int]:
    from sqlalchemy import func

    return tuple(
        [await s.scalar(select(func.count()).select_from(model)) for model in (Evaluation, Agent, Dataset)]
    )


def _with_evaluation(package: bytes, agent: dict | None = None, **changes) -> UploadFile:
    """*package* with its ``evaluation.json`` settings changed, and its first agent's with *agent*."""
    import json

    source = zipfile.ZipFile(io.BytesIO(package))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for name in source.namelist():
            data = source.read(name)
            if name == "evaluation.json":
                definition = json.loads(data)
                definition["evaluation"].update(changes)
                definition["dependencies"]["agents"][0].update(agent or {})
                data = json.dumps(definition).encode()
            out.writestr(name, data)
    return UploadFile(filename="e.zip", file=io.BytesIO(buf.getvalue()))


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
@pytest.mark.parametrize(
    ("changes", "field"),
    [
        pytest.param({"reuse_default": "sometimes"}, "reuse_default", id="reuse_default_not_on_or_off"),
        pytest.param({"timeout_seconds": 10}, "timeout_seconds", id="timeout_below_its_minimum"),
        pytest.param({"timeout_seconds": "an hour"}, "timeout_seconds", id="timeout_not_a_number"),
        pytest.param({"judge_max_tokens": 10}, "judge_max_tokens", id="judge_budget_below_its_minimum"),
        pytest.param({"performance_tolerance": -1}, "performance_tolerance", id="negative_tolerance"),
        pytest.param({"run_mode": "batch"}, "run_mode", id="unknown_run_mode"),
        pytest.param({"penalty_factor": -0.5}, "penalty_factor", id="negative_penalty_factor"),
        pytest.param({"sub_score_weights": {"coherence": 2.0}}, "sub_score_weights", id="weight_above_one"),
        pytest.param({"tags": ["red"]}, "tags", id="tags_not_an_object"),
        pytest.param({"custom_params": "nightly"}, "custom_params", id="custom_params_not_an_object"),
        pytest.param({"scorer_selection": ["oracle"]}, "oracle", id="unknown_scorer"),
        pytest.param({"family": "NOPE"}, "NOPE", id="unknown_family"),
        pytest.param({"agent": {"tags": "red"}}, "agents[0]: tags", id="agent_tags_not_an_object"),
        pytest.param({"agent": {"output_extractor": "find_json"}}, "agents[0]: output_extractor", id="extractor_not_an_object"),
        pytest.param({"agent": {"is_baseline": "yes"}}, "agents[0]: is_baseline", id="baseline_not_a_boolean"),
    ],
)
async def test_a_package_create_would_refuse_is_a_422_that_imports_nothing(changes, field):
    """Checked by create's own schema and checks before the agents and datasets are imported (FR-114)."""
    from fastapi import HTTPException

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, source_id, _agents = await _evaluation_with_every_setting(s)
            response = await evaluations_api.export_evaluation(
                evaluation_id=str(source_id), request=_asgi_request(), user=admin, db=s
            )
            package = await evaluations_api._streaming_response_to_bytes(response)
            before = await _counts(s)

            with pytest.raises(HTTPException) as exc:
                await evaluations_api.import_evaluation(
                    request=_asgi_request(),
                    file=_with_evaluation(package, **changes),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
            await s.rollback()
            after = await _counts(s)

        assert exc.value.status_code in (400, 422)
        assert field in exc.value.detail
        assert after == before
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
async def test_an_import_keeps_a_model_no_provider_of_the_importer_offers_and_warns():
    """Not refused, unlike create: the model is kept, as a saved one whose
    provider went away is, flagged in the response, and named in its warnings."""
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, source_id, _agents = await _evaluation_with_every_setting(s)
            response = await evaluations_api.export_evaluation(
                evaluation_id=str(source_id), request=_asgi_request(), user=admin, db=s
            )
            package = await evaluations_api._streaming_response_to_bytes(response)

            imported = await evaluations_api.import_evaluation(
                request=_asgi_request(),
                file=_with_evaluation(
                    package,
                    model_ids=["model-a", "gone-model", "model-b"],
                    disabled_model_ids=["model-b", "gone-model"],
                ),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            row, _settings, _links = await _settings_of(s, uuid.UUID(imported["id"]))

        assert (row.model_ids, row.disabled_model_ids) == (
            ["model-a", "gone-model", "model-b"],
            ["model-b", "gone-model"],
        )
        assert imported["unavailable_model_ids"] == ["gone-model"]
        assert imported["warnings"] == ["Model gone-model is kept but cannot run: no provider you can use offers it"]
    finally:
        await cleanup()


# ── trigger_config is merged ─────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
async def test_trigger_config_keys_left_out_are_kept_sent_as_null_removed_and_an_empty_one_changes_nothing():
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, evaluation_id, _agents = await _evaluation_with_every_setting(s)

            async def _update(trigger_config):
                await evaluations_api.update_evaluation(
                    evaluation_id=str(evaluation_id),
                    body=UpdateEvaluationRequest(trigger_config=trigger_config),
                    user=admin,
                    db=s,
                    _ai_gate=None,
                )
                evaluation, _settings, _links = await _settings_of(s, evaluation_id)
                return without_resume_secret(evaluation.trigger_config)

            assert await _update({}) == without_resume_secret(_SETTINGS["trigger_config"])
            assert await _update({"callback_timeout_seconds": None, "schedule": None}) == {
                "on_agent_change": True,
                "await_callback": True,
            }
            assert await _update({"await_callback": False}) == {"on_agent_change": True, "await_callback": False}
    finally:
        await cleanup()


def test_create_refuses_a_reuse_default_the_column_would_refuse():
    """The column's CHECK takes ``on`` or ``off`` only: anything else is a 422, not a failed insert."""
    import pydantic

    for request_model in (CreateEvaluationRequest, UpdateEvaluationRequest):
        with pytest.raises(pydantic.ValidationError):
            request_model(name="E", reuse_default="sometimes")


# ── run_mode, penalty_factor and sub_score_weights are checked (#685) ────────

_ALL_ON_COHERENCE = {"coherence": 1.0}


@pytest.mark.parametrize("request_model", [CreateEvaluationRequest, UpdateEvaluationRequest])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("run_mode", "batch", id="unknown_run_mode"),
        pytest.param("penalty_factor", -0.1, id="negative_penalty_factor"),
        pytest.param("penalty_factor", 1.6, id="penalty_factor_above_the_slider"),
        pytest.param("sub_score_weights", {"coherence": 0.5, "charm": 0.5}, id="unknown_criterion"),
        pytest.param("sub_score_weights", {"coherence": 1.5, "response_accuracy": -0.5}, id="weight_outside_0_1"),
        pytest.param("sub_score_weights", {"coherence": 0.5, "response_accuracy": 0.2}, id="weights_not_summing_to_1"),
        pytest.param("sub_score_weights", {}, id="no_weight"),
    ],
)
def test_a_setting_that_would_run_or_score_wrong_is_refused_naming_it(request_model, field, value):
    """A 422 naming the field on create and update, never a value stored as is."""
    import pydantic

    with pytest.raises(pydantic.ValidationError) as exc:
        request_model(name="E", **{field: value})
    assert {error["loc"][0] for error in exc.value.errors()} == {field}


@pytest.mark.parametrize("request_model", [CreateEvaluationRequest, UpdateEvaluationRequest])
def test_the_bounds_and_a_partial_weighting_are_accepted(request_model):
    for penalty_factor in (0.0, 1.5):
        assert request_model(name="E", penalty_factor=penalty_factor).penalty_factor == penalty_factor
    # Weights the creation page normalised, and a single criterion carrying all of
    # it: stored as sent, the criteria left out weighing 0 and not written.
    thirds = {"coherence": 1 / 3, "response_accuracy": 1 / 3, "instruction_adherence": 1 / 3}
    for weights in (thirds, _ALL_ON_COHERENCE):
        assert request_model(name="E", sub_score_weights=weights).sub_score_weights.stored() == weights


def test_an_export_writes_the_run_mode_a_stored_unknown_one_runs_in():
    exported = evaluations_api._exported_settings(Evaluation(name="E", trigger_config={}, run_mode="batch"))
    assert exported["run_mode"] == "parallel"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
@pytest.mark.parametrize(
    ("stored", "field"),
    [
        pytest.param({"penalty_factor": -1.0}, "penalty_factor", id="negative_penalty_factor"),
        pytest.param({"sub_score_weights": {"coherence": 3.0}}, "sub_score_weights", id="weights_above_one"),
    ],
)
async def test_a_copy_refuses_a_stored_setting_create_would_refuse_and_copies_nothing(stored, field):
    """A value stored before its rule existed is a 422 naming it, never copied."""
    from fastapi import HTTPException

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, source_id, _agents = await _evaluation_with_every_setting(s)
            source = await s.get(Evaluation, source_id)
            for name, value in stored.items():
                setattr(source, name, value)
            await s.commit()
            before = await _counts(s)

            with pytest.raises(HTTPException) as exc:
                await evaluations_api.copy_evaluation(evaluation_id=str(source_id), user=admin, db=s)
            await s.rollback()
            after = await _counts(s)

        assert exc.value.status_code == 422
        # The field, or the key of it at fault (``sub_score_weights.coherence``).
        assert exc.value.detail.split(":")[0].split(".")[0] == field
        assert after == before
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
async def test_a_copy_writes_the_run_mode_a_stored_unknown_one_runs_in():
    """Any run_mode but ``sequential`` runs in parallel: the copy stores that, which it can then edit."""
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, source_id, _agents = await _evaluation_with_every_setting(s)
            source = await s.get(Evaluation, source_id)
            source.run_mode = "batch"
            await s.commit()

            copied = await evaluations_api.copy_evaluation(evaluation_id=str(source_id), user=admin, db=s)
            copy, _settings, _links = await _settings_of(s, uuid.UUID(copied["id"]))

        assert copy.run_mode == "parallel"
    finally:
        await cleanup()


# ── quality_weight is retired ────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
async def test_create_and_update_take_the_retired_quality_weight_and_ignore_it():
    """Main's pages send ``quality_weight`` on every save: a page opened before the upgrade
    still saves, and the weight it sends is never stored nor served."""
    import httpx
    from fastapi import FastAPI

    from app.api import deps
    from app.core.database import get_db

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, evaluation_id, _agents = await _evaluation_with_every_setting(s)
            # A row from main keeps the weight it was saved with.
            (await s.get(Evaluation, evaluation_id)).quality_weight = 0.3
            await s.commit()

            app = FastAPI()
            app.include_router(evaluations_api.router, prefix="/api/v1/evaluations")

            async def _db():
                yield s

            app.dependency_overrides.update(
                {
                    get_db: _db,
                    deps.get_current_user: lambda: admin,
                    deps.require_ai_enabled: lambda: None,
                    deps.require_agentic_ai: lambda: None,
                }
            )
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                created = await client.post("/api/v1/evaluations", json={"name": "New", "quality_weight": 0.9})
                updated = await client.put(
                    f"/api/v1/evaluations/{evaluation_id}", json={"description": "Changed", "quality_weight": 0.9}
                )

            assert (created.status_code, updated.status_code) == (201, 200), (created.text, updated.text)
            new, _settings, _links = await _settings_of(s, uuid.UUID(created.json()["id"]))
            edited, settings, _links = await _settings_of(s, evaluation_id)

        assert "quality_weight" not in created.json()
        assert "quality_weight" not in updated.json()
        # The rest of the save applies; the weight does not.
        assert settings["description"] == "Changed"
        assert new.quality_weight == Evaluation.__table__.c.quality_weight.default.arg
        assert edited.quality_weight == 0.3
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
async def test_quality_weight_is_served_by_nothing_and_an_older_package_naming_it_still_imports():
    """An evaluation from main keeps its weight as data, but neither its response, its copy
    nor its export carries it; an older exported package names it and imports, the
    weight ignored."""
    import json

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, source_id, _agents = await _evaluation_with_every_setting(s)
            source = await s.get(Evaluation, source_id)
            source.quality_weight = 0.5
            await s.commit()

            served = await evaluations_api.get_evaluation(evaluation_id=str(source_id), user=admin, db=s)
            copied = await evaluations_api.copy_evaluation(evaluation_id=str(source_id), user=admin, db=s)
            response = await evaluations_api.export_evaluation(
                evaluation_id=str(source_id), request=_asgi_request(), user=admin, db=s
            )
            package = await evaluations_api._streaming_response_to_bytes(response)
            imported = await evaluations_api.import_evaluation(
                request=_asgi_request(),
                file=_with_evaluation(package, quality_weight=0.5),
                user=admin,
                db=s,
                _ai_gate=None,
            )

        exported = json.loads(zipfile.ZipFile(io.BytesIO(package)).read("evaluation.json"))["evaluation"]
        bodies = {"served": served, "copied": copied, "exported": exported, "imported": imported}
        assert {name: "quality_weight" in body for name, body in bodies.items()} == dict.fromkeys(bodies, False)
    finally:
        await cleanup()


# ── An update applies every setting ──────────────────────────────────────────

# Every setting away from the fixture's value, but the one whose only valid value it already holds.
_OTHER_SETTINGS = {
    "description": "Changed",
    "family": "PROSE",
    "trigger_config": {"schedule": "0 1 * * *", "on_agent_change": False, "await_callback": False, "callback_timeout_seconds": 60},
    "use_agent_default_model": True,
    "model_ids": ["model-c"],
    "disabled_model_ids": [],
    "execution_mode": "full_loop",
    "apply_prompt_guards": False,
    "require_tool_approval": False,
    "run_mode": "parallel",
    "evaluator_model": "another-judge",
    "evaluator_prompt": "Judge kindly.",
    "evaluation_instructions": "Mind the casing.",
    "judge_max_tokens": 2048,
    "performance_tolerance": 2.5,
    "sub_score_weights": {
        "tool_selection_quality": 0.2,
        "response_completeness": 0.2,
        "response_accuracy": 0.2,
        "instruction_adherence": 0.2,
        "coherence": 0.2,
    },
    "quality_source": "judge",
    "penalty_factor": 0.75,
    "timeout_seconds": 1200,
    "custom_params": {"suite": "weekly"},
    "tags": {"team": "blue"},
    "field_scoring_overrides": {"field_weights": {"/summary": 3.0}},
    "scorer_selection": ["judge"],
    "reuse_default": "on",
    "overall_quality_metrics": ["prose.token_prf"],
    "overall_performance_metrics": ["performance.latency.wall_clock"],
    "placebo_config": {
        "targets": [{"kind": "skills", "skill_id": None}],
        "include_no_target": True,
        "trials_per_entry": 3,
        "custom_text": None,
    },
}


def test_the_update_fixture_changes_every_setting():
    assert set(_OTHER_SETTINGS) == set(EVALUATION_SETTINGS)
    assert {k for k in EVALUATION_SETTINGS if _OTHER_SETTINGS[k] == _SETTINGS[k]} == {"quality_source"}


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
async def test_an_update_applies_every_setting():
    """A setting added to the list and the schemas but skipped by the update fails here."""
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, evaluation_id, agents = await _evaluation_with_every_setting(s)
            await evaluations_api.update_evaluation(
                evaluation_id=str(evaluation_id),
                # The extractor is an object, which PROSE does not take: cleared in the same update.
                body=UpdateEvaluationRequest(**_OTHER_SETTINGS, agent_extractors={str(agents[0].id): None}),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            evaluation, settings, _links = await _settings_of(s, evaluation_id)

        assert {**settings, "trigger_config": without_resume_secret(evaluation.trigger_config)} == _OTHER_SETTINGS
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_audit_and_groups")
async def test_null_keeps_the_family_and_the_judge_choice():
    """Null on these is no reset anyone would mean: no family cannot run, a null selection runs the judge."""
    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin, evaluation_id, _agents = await _evaluation_with_every_setting(s)
            await evaluations_api.update_evaluation(
                evaluation_id=str(evaluation_id),
                body=UpdateEvaluationRequest(family=None, scorer_selection=None),
                user=admin,
                db=s,
                _ai_gate=None,
            )
            _evaluation, settings, _links = await _settings_of(s, evaluation_id)

        assert (settings["family"], settings["scorer_selection"]) == ("STRUCTURED_JSON", [])
    finally:
        await cleanup()
