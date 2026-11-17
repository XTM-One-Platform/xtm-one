import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import evaluations as evaluations_api
from app.core.database import get_db
from app.models.evaluation import EvaluationRun


class _ScalarsResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return self._values


class _CountResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _ScalarOneResult:
    """Mimics ``select(...)`` results consumed via ``scalar_one_or_none()``.

    Used to stub the "does a param row already exist for this run/key"
    lookup ``app.services.evaluation.params.upsert_param`` performs — every
    fresh run this endpoint creates captures a ``{"reuse": "off"}``
    manual param, so each created run consumes one of these from
    the fake DB's execute-results queue.
    """

    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDb:
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)
        self.added = []
        self.flush_calls = 0
        self.commits = 0

    async def execute(self, _statement):
        if not self._execute_results:
            raise AssertionError("Unexpected extra execute() call")
        return self._execute_results.pop(0)

    def add(self, obj):
        # Simulate SQLAlchemy identity assignment after add/flush.
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        self.added.append(obj)

    async def flush(self):
        self.flush_calls += 1

    async def commit(self):
        self.commits += 1


@pytest.fixture(autouse=True)
def _stub_count_pinned_entries(monkeypatch):
    # Baselines in this suite carry pinned_dataset_versions, so
    # trigger_evaluation_from_runs's per-baseline total_entries lookup
    # would otherwise issue extra db.execute() calls the strict
    # ``_FakeDb`` queue doesn't provision for. Returning 0 falls back to the
    # live entry_count.
    async def _no_count(_db, _evaluation_id, _pinned):
        return 0

    monkeypatch.setattr(
        "app.services.evaluation.dataset_versioning.count_pinned_entries",
        _no_count,
    )


@pytest.fixture(autouse=True)
def _every_model_served(monkeypatch):
    # Every baseline's model is offered: the refusal of one that is not
    # has its own tests in ``test_evaluation_launch.py``; this suite is about
    # which runs are created.
    async def _served(*_args, **_kwargs):
        return None

    monkeypatch.setattr(evaluations_api, "check_runs_served", _served)


@pytest.fixture(autouse=True)
def _stub_snapshot_check(monkeypatch):
    # Each selected run is replayed with its own versions, so the route
    # checks that they still resolve, as ``replay_run`` does; that lookup reads
    # tables the strict ``_FakeDb`` queue does not provision for. Every snapshot
    # resolves here; a missing one is covered on PostgreSQL in ``test_evaluations.py``.
    async def _all_resolve(_db, runs):
        return {run.id: [] for run in runs}

    monkeypatch.setattr(
        "app.services.evaluation_engine.batch_compute_run_replayability", _all_resolve
    )


@pytest.fixture
def fake_user():
    return SimpleNamespace(id=uuid.uuid4(), is_admin=True, full_name="Admin")


@pytest.fixture
def evaluations_client(fake_user):
    app = FastAPI()
    app.include_router(evaluations_api.router, prefix="/api/v1/evaluations")

    app.dependency_overrides[deps.get_current_user] = lambda: fake_user
    # Triggering an evaluation calls models, so the route now carries the AI
    # gate — stubbed here, since this suite has no resolvable state.
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None

    def _mk_client_with_db(fake_db):
        async def _get_db():
            yield fake_db

        app.dependency_overrides[get_db] = _get_db
        return TestClient(app)

    return _mk_client_with_db


def _run_response(run):
    now = "2026-05-09T12:00:00+00:00"
    return {
        "id": str(run.id),
        "evaluation_id": str(run.evaluation_id),
        "evaluation_name": "Eval",
        "agent_id": str(run.agent_id),
        "agent_name": "Agent",
        "agent_slug": "agent",
        "agent_user_name": None,
        "model_id": run.model_id,
        "is_default_model": bool(run.is_default_model),
        "status": run.status,
        "trigger_reason": run.trigger_reason,
        "score": None,
        "speed_score": None,
        "overall_score": None,
        "f1_score": None,
        "quality_source": "judge",
        "duration_seconds": None,
        "avg_sub_scores": {},
        "completed_entries": 0,
        "total_entries": int(run.total_entries),
        "error": None,
        "started_at": None,
        "completed_at": None,
        "created_at": now,
        "persona_version_number": run.persona_version_number,
        "prompt_versions": run.prompt_versions,
        "agent_config_versions": run.agent_config_versions,
        "can_replay": True,
        "replay_block_reason": None,
    }


@pytest.mark.asyncio
async def test_trigger_from_runs_success_with_variation(monkeypatch, evaluations_client):
    evaluation_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    baseline_run_id = uuid.uuid4()

    evaluation = SimpleNamespace(
        id=evaluation_id,
        name="Eval",
        timeout_seconds=3600,
        last_triggered_at=None,
        # The trigger endpoint gates on a runnable output family. These tests
        # are about run selection, not the gate, which has its own coverage in
        # ``tests/test_evaluation_families.py``.
        family="STIX_BUNDLE",
        scorer_selection=None,
    )
    baseline = SimpleNamespace(
        id=baseline_run_id,
        evaluation_id=evaluation_id,
        agent_id=agent_id,
        flow_id=None,
        model_id="gpt-4.1",
        is_default_model=False,
        persona_version_number=1,
        prompt_versions={str(uuid.uuid4()): 1},
        skill_versions=None,
        placebo_comparison_id=None,
        arm="real",
        agent_config_versions={"temperature": 1},
        pinned_dataset_versions={"11111111-1111-1111-1111-111111111111": 3},
    )

    fake_db = _FakeDb(
        execute_results=[
            _ScalarsResult([]),  # dataset family gate: none missing
            _ScalarsResult([baseline]),  # baseline runs lookup
            _CountResult(5),  # dataset entries count
            _ScalarOneResult(None),  # reuse=off param upsert lookup
        ]
    )

    async def _groups(_db, _uid):
        return []

    async def _get_eval(_db, _uid, _eid, _groups, *, is_admin=False):
        return evaluation


    async def _audit(*_args, **_kwargs):
        return None

    async def _build_resp(_db, run, *, missing_snapshots=None):
        # ``missing_snapshots`` is the precomputed-replayability fast
        # path — the route passes ``[]`` for freshly-created runs to
        # skip the per-run ``resolve_run_snapshot_overrides()`` call.
        # The mock accepts it (and ignores its value) so the response
        # shape stays stable across the refactor.
        return _run_response(run)

    class _Queue:
        async def enqueue(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(evaluations_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(evaluations_api, "_get_user_evaluation", _get_eval)
    monkeypatch.setattr(evaluations_api, "log_audit", _audit)
    monkeypatch.setattr(evaluations_api, "_build_run_response", _build_resp)
    async def _noop_enqueue(*_a, **_k):
        # Must carry an ``id``: the endpoint stamps it onto every run it
        # creates as ``owner_execution_id``, in the same commit.
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(
        "app.services.evaluation_engine.stage_evaluation_execution", _noop_enqueue
    )

    with evaluations_client(fake_db) as client:
        resp = client.post(
            f"/api/v1/evaluations/{evaluation_id}/trigger-from-runs",
            json={"run_ids": [str(baseline_run_id)]},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    # The run's own versions, as the run page's "Re-run full" replays it,
    # never the agent's current ones.
    assert data[0]["persona_version_number"] == baseline.persona_version_number
    assert data[0]["prompt_versions"] == baseline.prompt_versions
    assert data[0]["agent_config_versions"] == baseline.agent_config_versions
    assert fake_db.flush_calls == 1
    assert fake_db.commits == 1
    # 1 EvaluationRun. The "reuse": "off" param is a bulk upsert via
    # ``capture_params_for_runs`` -- one ``db.execute(...)`` per key,
    # not an ORM ``db.add()``, so it is not in ``fake_db.added``.
    assert len(fake_db.added) == 1


@pytest.mark.asyncio
async def test_trigger_from_runs_mixed_selection_creates_run_for_every_baseline(
    monkeypatch, evaluations_client
):
    """A selection mixing a changed-config run and an unchanged-config
    run must create a fresh run for *every* selected baseline,
    each with a captured ``reuse=off`` manual param.
    """
    evaluation_id = uuid.uuid4()
    changed_agent_id = uuid.uuid4()
    unchanged_agent_id = uuid.uuid4()
    changed_run_id = uuid.uuid4()
    unchanged_run_id = uuid.uuid4()
    prompt_id = uuid.uuid4()

    evaluation = SimpleNamespace(
        id=evaluation_id,
        name="Eval",
        timeout_seconds=3600,
        last_triggered_at=None,
        family="STIX_BUNDLE",
        scorer_selection=None,
    )
    changed_baseline = SimpleNamespace(
        id=changed_run_id,
        evaluation_id=evaluation_id,
        agent_id=changed_agent_id,
        flow_id=None,
        model_id="gpt-4.1",
        is_default_model=False,
        persona_version_number=1,
        prompt_versions={str(prompt_id): 1},
        skill_versions=None,
        placebo_comparison_id=None,
        arm="real",
        agent_config_versions={"temperature": 1},
        pinned_dataset_versions={"11111111-1111-1111-1111-111111111111": 3},
    )
    unchanged_baseline = SimpleNamespace(
        id=unchanged_run_id,
        evaluation_id=evaluation_id,
        agent_id=unchanged_agent_id,
        flow_id=None,
        model_id="gpt-4.1",
        is_default_model=True,
        persona_version_number=3,
        prompt_versions={str(prompt_id): 2},
        skill_versions=None,
        placebo_comparison_id=None,
        arm="real",
        agent_config_versions={"temperature": 4},
        # Deliberately a DIFFERENT pin than ``changed_baseline``: each
        # new run must inherit its OWN baseline's pin, not a single
        # evaluation-wide "current" pin.
        pinned_dataset_versions={"11111111-1111-1111-1111-111111111111": 5},
    )

    fake_db = _FakeDb(
        execute_results=[
            _ScalarsResult([]),  # dataset family gate: none missing
            _ScalarsResult([changed_baseline, unchanged_baseline]),
            _CountResult(7),
        ]
    )

    captured_calls = []

    async def _fake_capture_params_for_runs(_db, run_ids, params, source):
        captured_calls.append((list(run_ids), dict(params), source))

    monkeypatch.setattr(
        "app.services.evaluation.params.capture_params_for_runs",
        _fake_capture_params_for_runs,
    )


    async def _groups(_db, _uid):
        return []

    async def _get_eval(_db, _uid, _eid, _groups, *, is_admin=False):
        return evaluation


    async def _audit(*_args, **_kwargs):
        return None

    async def _build_resp(_db, run, *, missing_snapshots=None):
        return _run_response(run)

    monkeypatch.setattr(evaluations_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(evaluations_api, "_get_user_evaluation", _get_eval)
    monkeypatch.setattr(evaluations_api, "log_audit", _audit)
    monkeypatch.setattr(evaluations_api, "_build_run_response", _build_resp)

    async def _noop_enqueue(*_a, **_k):
        # Must carry an ``id``: the endpoint stamps it onto every run it
        # creates as ``owner_execution_id``, in the same commit.
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(
        "app.services.evaluation_engine.stage_evaluation_execution", _noop_enqueue
    )

    with evaluations_client(fake_db) as client:
        resp = client.post(
            f"/api/v1/evaluations/{evaluation_id}/trigger-from-runs",
            json={"run_ids": [str(changed_run_id), str(unchanged_run_id)]},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2, (
        f"every selected baseline must produce a run regardless of "
        f"whether its config changed; got {len(data)} runs"
    )
    created_runs = [obj for obj in fake_db.added if isinstance(obj, EvaluationRun)]
    assert len(created_runs) == 2
    # The "reuse": "off" manual param is one bulk upsert call covering the
    # whole batch via ``capture_params_for_runs``, not one
    # ``db.add(EvaluationRunParam)`` per run.
    assert len(captured_calls) == 1
    run_ids_captured, params_captured, source_captured = captured_calls[0]
    assert set(run_ids_captured) == {run.id for run in created_runs}
    assert params_captured == {"reuse": "off"}
    assert source_captured == "manual"

    # Each new run must carry its OWN baseline's pin, not a uniform
    # "current" pin re-computed for the whole
    # evaluation — otherwise a full re-run would silently score a different
    # dataset version than the baseline it's meant to replay.
    runs_by_agent = {run.agent_id: run for run in created_runs}
    assert (
        runs_by_agent[changed_agent_id].pinned_dataset_versions
        == changed_baseline.pinned_dataset_versions
    )
    assert (
        runs_by_agent[unchanged_agent_id].pinned_dataset_versions
        == unchanged_baseline.pinned_dataset_versions
    )
    assert (
        runs_by_agent[changed_agent_id].pinned_dataset_versions
        != runs_by_agent[unchanged_agent_id].pinned_dataset_versions
    ), "the two baselines' distinct pins must not collapse into one shared pin"


@pytest.mark.asyncio
async def test_trigger_from_runs_total_entries_derived_per_baseline_pin(
    monkeypatch, evaluations_client
):
    """Each new run's total_entries comes from its OWN baseline's pinned
    snapshot, not the single live entry_count shared by the whole request. Two baselines pinned to different versions
    (with different entry counts) must produce runs with different
    total_entries, neither of which is the live count.
    """
    evaluation_id = uuid.uuid4()
    agent_a_id = uuid.uuid4()
    agent_b_id = uuid.uuid4()
    run_a_id = uuid.uuid4()
    run_b_id = uuid.uuid4()
    pin_a = {"11111111-1111-1111-1111-111111111111": 3}
    pin_b = {"11111111-1111-1111-1111-111111111111": 5}

    evaluation = SimpleNamespace(
        id=evaluation_id,
        name="Eval",
        timeout_seconds=3600,
        last_triggered_at=None,
        family="STIX_BUNDLE",
        scorer_selection=None,
    )
    baseline_a = SimpleNamespace(
        id=run_a_id,
        evaluation_id=evaluation_id,
        agent_id=agent_a_id,
        flow_id=None,
        model_id="gpt-4.1",
        is_default_model=False,
        persona_version_number=1,
        prompt_versions=None,
        skill_versions=None,
        placebo_comparison_id=None,
        arm="real",
        agent_config_versions=None,
        pinned_dataset_versions=pin_a,
    )
    baseline_b = SimpleNamespace(
        id=run_b_id,
        evaluation_id=evaluation_id,
        agent_id=agent_b_id,
        flow_id=None,
        model_id="gpt-4.1",
        is_default_model=False,
        persona_version_number=1,
        prompt_versions=None,
        skill_versions=None,
        placebo_comparison_id=None,
        arm="real",
        agent_config_versions=None,
        pinned_dataset_versions=pin_b,
    )

    fake_db = _FakeDb(
        execute_results=[
            _ScalarsResult([]),  # dataset family gate: none missing
            _ScalarsResult([baseline_a, baseline_b]),
            _CountResult(9),  # live entry count -- must NOT end up on either run
            _ScalarOneResult(None),  # reuse=off param upsert (run 1)
            _ScalarOneResult(None),  # reuse=off param upsert (run 2)
        ]
    )

    async def _groups(_db, _uid):
        return []

    async def _get_eval(_db, _uid, _eid, _groups, *, is_admin=False):
        return evaluation


    async def _audit(*_args, **_kwargs):
        return None

    async def _build_resp(_db, run, *, missing_snapshots=None):
        return _run_response(run)

    async def _count_pinned(_db, _evaluation_id, pinned):
        # A distinct, non-live count per pin -- 2 for version 3, 4 for
        # version 5 -- so the assertion below can tell the two apart and
        # confirm neither collapsed to the live count (9).
        return {3: 2, 5: 4}[pinned["11111111-1111-1111-1111-111111111111"]]

    monkeypatch.setattr(evaluations_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(evaluations_api, "_get_user_evaluation", _get_eval)
    monkeypatch.setattr(evaluations_api, "log_audit", _audit)
    monkeypatch.setattr(evaluations_api, "_build_run_response", _build_resp)
    monkeypatch.setattr(
        "app.services.evaluation.dataset_versioning.count_pinned_entries",
        _count_pinned,
    )

    async def _noop_enqueue(*_a, **_k):
        # Must carry an ``id``: the endpoint stamps it onto every run it
        # creates as ``owner_execution_id``, in the same commit.
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(
        "app.services.evaluation_engine.stage_evaluation_execution", _noop_enqueue
    )

    with evaluations_client(fake_db) as client:
        resp = client.post(
            f"/api/v1/evaluations/{evaluation_id}/trigger-from-runs",
            json={"run_ids": [str(run_a_id), str(run_b_id)]},
        )

    assert resp.status_code == 200
    created_runs = [obj for obj in fake_db.added if isinstance(obj, EvaluationRun)]
    assert len(created_runs) == 2
    runs_by_agent = {run.agent_id: run for run in created_runs}
    assert runs_by_agent[agent_a_id].total_entries == 2
    assert runs_by_agent[agent_b_id].total_entries == 4
    assert 9 not in (
        runs_by_agent[agent_a_id].total_entries,
        runs_by_agent[agent_b_id].total_entries,
    ), "neither run may fall back to the live entry_count when its own pin resolves"


@pytest.mark.asyncio
async def test_trigger_from_runs_returns_404_for_unknown_baseline(monkeypatch, evaluations_client):
    evaluation_id = uuid.uuid4()
    unknown_run_id = uuid.uuid4()

    evaluation = SimpleNamespace(
        id=evaluation_id,
        name="Eval",
        timeout_seconds=3600,
        last_triggered_at=None,
        # The trigger endpoint gates on a runnable output family. These tests
        # are about run selection, not the gate, which has its own coverage in
        # ``tests/test_evaluation_families.py``.
        family="STIX_BUNDLE",
        scorer_selection=None,
    )

    fake_db = _FakeDb(
        execute_results=[
            _ScalarsResult([]),  # dataset family gate: none missing
            _ScalarsResult([]),  # no baselines found
        ]
    )

    async def _groups(_db, _uid):
        return []

    async def _get_eval(_db, _uid, _eid, _groups, *, is_admin=False):
        return evaluation

    monkeypatch.setattr(evaluations_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(evaluations_api, "_get_user_evaluation", _get_eval)

    with evaluations_client(fake_db) as client:
        resp = client.post(
            f"/api/v1/evaluations/{evaluation_id}/trigger-from-runs",
            json={"run_ids": [str(unknown_run_id)]},
        )

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_trigger_from_runs_rejects_over_cap_run_ids(monkeypatch, evaluations_client):
    """``run_ids`` is bounded: an over-cap request is rejected by
    schema validation before any row lock or DB query is attempted (the
    strict, zero-provisioned ``_FakeDb`` proves no execute() call happens).
    """
    from app.api.v1.schemas.evaluation import MAX_BULK_RUN_IDS

    evaluation_id = uuid.uuid4()
    over_cap_run_ids = [str(uuid.uuid4()) for _ in range(MAX_BULK_RUN_IDS + 1)]

    fake_db = _FakeDb(execute_results=[])

    with evaluations_client(fake_db) as client:
        resp = client.post(
            f"/api/v1/evaluations/{evaluation_id}/trigger-from-runs",
            json={"run_ids": over_cap_run_ids},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_trigger_from_runs_accepts_uppercase_uuids(monkeypatch, evaluations_client):
    """Uppercase UUIDs in ``run_ids`` must successfully select the
    baseline run instead of being falsely reported as "not found".

    The DB column is a UUID type and ``str(uuid.UUID(...))`` always
    returns the lowercase canonical form, so the existing lookup map
    ``baseline_by_id = {str(run.id): run for run in baseline_runs}``
    is lowercase-keyed.  The previous implementation compared the
    raw uppercase request string against those lowercase keys, which
    produced a spurious 404 even though ``baseline_uuids`` (the
    actual DB query payload) parsed the uppercase string correctly
    and returned the baseline row.

    The fix normalises every request ID to ``str(parse_uuid(...))``
    before deduping/comparing so canonicalisation is consistent
    across all three uses (DB query, dedup, missing-report).  This
    test pins the contract by submitting an uppercase UUID and
    asserting a 200 + 1 created run, NOT a 404.
    """
    evaluation_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    baseline_run_id = uuid.uuid4()

    evaluation = SimpleNamespace(
        id=evaluation_id,
        name="Eval",
        timeout_seconds=3600,
        last_triggered_at=None,
        # The trigger endpoint gates on a runnable output family. These tests
        # are about run selection, not the gate, which has its own coverage in
        # ``tests/test_evaluation_families.py``.
        family="STIX_BUNDLE",
        scorer_selection=None,
    )
    baseline = SimpleNamespace(
        id=baseline_run_id,
        evaluation_id=evaluation_id,
        agent_id=agent_id,
        flow_id=None,
        model_id="gpt-4.1",
        is_default_model=False,
        persona_version_number=1,
        prompt_versions={str(uuid.uuid4()): 1},
        skill_versions=None,
        placebo_comparison_id=None,
        arm="real",
        agent_config_versions={"temperature": 1},
        pinned_dataset_versions={"11111111-1111-1111-1111-111111111111": 3},
    )

    fake_db = _FakeDb(
        execute_results=[
            _ScalarsResult([]),  # dataset family gate: none missing
            _ScalarsResult([baseline]),
            _CountResult(5),
            _ScalarOneResult(None),  # reuse=off param upsert lookup
        ]
    )

    async def _groups(_db, _uid):
        return []

    async def _get_eval(_db, _uid, _eid, _groups, *, is_admin=False):
        return evaluation


    async def _audit(*_args, **_kwargs):
        return None

    async def _build_resp(_db, run, *, missing_snapshots=None):
        return _run_response(run)

    class _Queue:
        async def enqueue(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(evaluations_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(evaluations_api, "_get_user_evaluation", _get_eval)
    monkeypatch.setattr(evaluations_api, "log_audit", _audit)
    monkeypatch.setattr(evaluations_api, "_build_run_response", _build_resp)
    async def _noop_enqueue(*_a, **_k):
        # Must carry an ``id``: the endpoint stamps it onto every run it
        # creates as ``owner_execution_id``, in the same commit.
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(
        "app.services.evaluation_engine.stage_evaluation_execution", _noop_enqueue
    )

    # Submit the UUID in UPPERCASE.  ``str(baseline.id)`` is
    # always lowercase, so without canonicalisation the
    # "missing" check would falsely match this entry.
    uppercase_request_id = str(baseline_run_id).upper()
    assert uppercase_request_id != str(baseline_run_id), (
        "precondition: the uppercase request string must differ "
        "from the canonical str(UUID); otherwise the test "
        "wouldn't exercise the canonicalisation path"
    )

    with evaluations_client(fake_db) as client:
        resp = client.post(
            f"/api/v1/evaluations/{evaluation_id}/trigger-from-runs",
            json={"run_ids": [uppercase_request_id]},
        )

    assert resp.status_code == 200, (
        f"uppercase UUID must canonicalise and find the baseline run "
        f"instead of producing a 404; got {resp.status_code}: "
        f"{resp.json()!r}"
    )
    data = resp.json()
    assert len(data) == 1
    # 1 EvaluationRun -- the "reuse": "off" param is a bulk upsert via
    # ``capture_params_for_runs``, not an ORM ``db.add()``, so it is
    # not in ``fake_db.added``.
    assert len(fake_db.added) == 1


@pytest.mark.asyncio
async def test_trigger_from_runs_dedupes_case_only_duplicates(monkeypatch, evaluations_client):
    """If a caller submits the same UUID twice — once lowercase, once
    uppercase — the route must treat them as a single request, not as
    two separate baselines that produce two runs.

    Without canonicalising before the ``dict.fromkeys`` dedup pass,
    the two case-distinct strings survive as separate entries, the
    iteration produces a duplicate `EvaluationRun` for the same
    baseline, and the response carries two copies of the same fresh
    run — wasteful and confusing.
    """
    evaluation_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    baseline_run_id = uuid.uuid4()

    evaluation = SimpleNamespace(
        id=evaluation_id,
        name="Eval",
        timeout_seconds=3600,
        last_triggered_at=None,
        # The trigger endpoint gates on a runnable output family. These tests
        # are about run selection, not the gate, which has its own coverage in
        # ``tests/test_evaluation_families.py``.
        family="STIX_BUNDLE",
        scorer_selection=None,
    )
    baseline = SimpleNamespace(
        id=baseline_run_id,
        evaluation_id=evaluation_id,
        agent_id=agent_id,
        flow_id=None,
        model_id="gpt-4.1",
        is_default_model=False,
        persona_version_number=1,
        prompt_versions={str(uuid.uuid4()): 1},
        skill_versions=None,
        placebo_comparison_id=None,
        arm="real",
        agent_config_versions={"temperature": 1},
        pinned_dataset_versions={"11111111-1111-1111-1111-111111111111": 3},
    )

    fake_db = _FakeDb(
        execute_results=[
            _ScalarsResult([]),  # dataset family gate: none missing
            _ScalarsResult([baseline]),
            _CountResult(3),
            _ScalarOneResult(None),  # reuse=off param upsert lookup
        ]
    )

    async def _groups(_db, _uid):
        return []

    async def _get_eval(_db, _uid, _eid, _groups, *, is_admin=False):
        return evaluation


    async def _audit(*_args, **_kwargs):
        return None

    async def _build_resp(_db, run, *, missing_snapshots=None):
        return _run_response(run)

    class _Queue:
        async def enqueue(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(evaluations_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(evaluations_api, "_get_user_evaluation", _get_eval)
    monkeypatch.setattr(evaluations_api, "log_audit", _audit)
    monkeypatch.setattr(evaluations_api, "_build_run_response", _build_resp)
    async def _noop_enqueue(*_a, **_k):
        # Must carry an ``id``: the endpoint stamps it onto every run it
        # creates as ``owner_execution_id``, in the same commit.
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(
        "app.services.evaluation_engine.stage_evaluation_execution", _noop_enqueue
    )

    lower = str(baseline_run_id)
    upper = lower.upper()
    assert lower != upper

    with evaluations_client(fake_db) as client:
        resp = client.post(
            f"/api/v1/evaluations/{evaluation_id}/trigger-from-runs",
            json={"run_ids": [lower, upper]},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1, (
        f"case-only-duplicate UUIDs must dedup to a single run; got "
        f"{len(data)} runs in response: {data!r}"
    )
    # 1 EvaluationRun -- the "reuse": "off" param is a bulk upsert via
    # ``capture_params_for_runs``, not an ORM ``db.add()``, so it is
    # not in ``fake_db.added``.
    assert len(fake_db.added) == 1


@pytest.mark.asyncio
async def test_trigger_from_runs_response_skips_per_run_snapshot_resolution(
    monkeypatch, evaluations_client
):
    """The route resolves each selected run's snapshots once, before creating
    its replay, so the response path must NOT re-run
    ``resolve_run_snapshot_overrides()`` per created run — that doubles the
    O(runs × snapshot_refs) DB fan-out.

    The contract is encoded by passing ``missing_snapshots=[]`` to
    ``_build_run_response``.  Lock it by spying on the resolver and
    asserting it is NOT called during the response-building phase.
    """
    evaluation_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    baseline_run_id = uuid.uuid4()

    evaluation = SimpleNamespace(
        id=evaluation_id,
        name="Eval",
        timeout_seconds=3600,
        last_triggered_at=None,
        # The trigger endpoint gates on a runnable output family. These tests
        # are about run selection, not the gate, which has its own coverage in
        # ``tests/test_evaluation_families.py``.
        family="STIX_BUNDLE",
        scorer_selection=None,
    )
    baseline = SimpleNamespace(
        id=baseline_run_id,
        evaluation_id=evaluation_id,
        agent_id=agent_id,
        flow_id=None,
        model_id="gpt-4.1",
        is_default_model=False,
        persona_version_number=1,
        prompt_versions={str(uuid.uuid4()): 1},
        skill_versions=None,
        placebo_comparison_id=None,
        arm="real",
        agent_config_versions={"temperature": 1},
        pinned_dataset_versions={"11111111-1111-1111-1111-111111111111": 3},
    )

    fake_db = _FakeDb(
        execute_results=[
            _ScalarsResult([]),  # dataset family gate: none missing
            _ScalarsResult([baseline]),
            _CountResult(5),
            _ScalarOneResult(None),  # reuse=off param upsert lookup
        ]
    )

    async def _groups(_db, _uid):
        return []

    async def _get_eval(_db, _uid, _eid, _groups, *, is_admin=False):
        return evaluation


    async def _audit(*_args, **_kwargs):
        return None

    # Counts every invocation of the per-run snapshot resolver — must
    # stay at zero across the route call below because the response
    # path takes the precomputed-empty-missing-snapshots fast path.
    resolver_calls = 0

    async def _resolver(_db, _run):
        nonlocal resolver_calls
        resolver_calls += 1
        return (None, [])

    # Build a real-ish ``_build_run_response`` that honours
    # ``missing_snapshots`` exactly like the production implementation
    # — only calling the resolver when ``missing_snapshots is None``.
    async def _build_resp(_db, run, *, missing_snapshots=None):
        if missing_snapshots is None:
            await _resolver(_db, run)
        return _run_response(run)

    class _Queue:
        async def enqueue(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(evaluations_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(evaluations_api, "_get_user_evaluation", _get_eval)
    monkeypatch.setattr(evaluations_api, "log_audit", _audit)
    monkeypatch.setattr(evaluations_api, "_build_run_response", _build_resp)
    async def _noop_enqueue(*_a, **_k):
        # Must carry an ``id``: the endpoint stamps it onto every run it
        # creates as ``owner_execution_id``, in the same commit.
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(
        "app.services.evaluation_engine.stage_evaluation_execution", _noop_enqueue
    )

    with evaluations_client(fake_db) as client:
        resp = client.post(
            f"/api/v1/evaluations/{evaluation_id}/trigger-from-runs",
            json={"run_ids": [str(baseline_run_id)]},
        )

    assert resp.status_code == 200
    assert resolver_calls == 0, (
        f"the response path must not re-resolve snapshots for "
        f"freshly-created runs (the route already resolved each "
        f"run's snapshots); resolver invoked "
        f"{resolver_calls} time(s)"
    )
