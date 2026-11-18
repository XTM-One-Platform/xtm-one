"""External task lifecycle: validation, state projection, artifacts, cancel.

The state a partner sees is projected from the AssignmentRun the task mirrors,
never stored alongside it - so these tests pin the projection rather than any
second copy of the truth.
"""

import uuid

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.a2a import protocol as proto
from app.a2a import tasks as task_svc
from tests import a2a_server_stubs as stubs


class _Agent:
    def __init__(self, output_format="markdown"):
        self.id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.name = "A"
        self.output_format = output_format


class _Run:
    def __init__(self, status="running", result=None):
        self.id = uuid.uuid4()
        self.status = status
        self.result_summary = result
        self.error = None


class _Task:
    def __init__(self, run_id=None, rejected=None, cancelled=None):
        self.id = uuid.uuid4()
        self.context_id = uuid.uuid4()
        self.run_id = run_id
        self.rejected_reason = rejected
        self.cancel_requested_at = cancelled
        self.client_id = "c1"
        self.responding_agent_id = None
        self.caller_question = None
        self.clarification_count = 0


class _Result:
    def __init__(self, v):
        self._v = v
        # The guarded UPDATEs read ``rowcount``; the fake reports one matched
        # row so the happy paths behave as they do against a live database.
        self.rowcount = 1

    def scalar_one_or_none(self):
        return self._v

    def one_or_none(self):
        # ``resolve_state`` reads the task again with its run; the task
        # is refreshed in place, so the row's run is the answer that matters.
        return None if self._v is None else (None, self._v)

    def scalars(self):
        return self

    def all(self):
        # No delegated work, which is what "cancel a task that delegated
        # nothing" looks like to the cascade ``request_cancel`` now runs.
        return []


class _DB:
    def __init__(self, results=None):
        self._r = list(results or [])
        self.flushes = 0

    async def execute(self, _s):
        return _Result(self._r.pop(0) if self._r else None)

    async def flush(self):
        self.flushes += 1

    def add(self, _o):
        pass


@pytest.fixture(autouse=True)
def _no_delegated_work(monkeypatch):
    stubs.silence_delegated_work(monkeypatch, task_svc)

    async def _none(*_a, **_k):
        return None

    # Nor about what leaves the process once a task is terminal: the frames and
    # the callbacks have their own files.
    monkeypatch.setattr("app.a2a.push.notify", _none)


class TestSubmissionValidation:
    def test_text_parts_are_accepted(self):
        out = task_svc.validate_submission([{"text": "hello"}])
        assert out == "hello"

    def test_structured_data_is_accepted_from_any_partner(self):
        out = task_svc.validate_submission([{"structuredData": {"a": 1}}])
        assert '"a": 1' in out

    def test_legacy_data_shape_is_accepted(self):
        out = task_svc.validate_submission([{"data": {"a": 1}}])
        assert '"a": 1' in out

    def test_multiple_parts_are_joined(self):
        out = task_svc.validate_submission([{"text": "one"}, {"text": "two"}])
        assert "one" in out and "two" in out

    def test_file_parts_are_refused_before_a_run_starts(self):
        with pytest.raises(task_svc.TaskRejectedError) as exc:
            task_svc.validate_submission([{"url": {"uri": "x"}}])
        assert exc.value.reason == task_svc.REJECT_CONTENT_TYPE

    def test_empty_message_is_refused(self):
        with pytest.raises(task_svc.TaskRejectedError) as exc:
            task_svc.validate_submission([])
        assert exc.value.reason == task_svc.REJECT_NO_CONTENT

    def test_unrecognised_part_is_refused(self):
        with pytest.raises(task_svc.TaskRejectedError):
            task_svc.validate_submission([{"mystery": 1}])


class TestStateProjection:
    async def test_rejected_task_reports_rejected(self):
        state = await task_svc.resolve_state(_DB(), _Task(rejected="bad_type"))
        assert state is proto.A2AState.REJECTED

    async def test_task_without_a_run_reports_submitted(self):
        assert await task_svc.resolve_state(_DB(), _Task()) is proto.A2AState.SUBMITTED

    @pytest.mark.parametrize(
        "status,expected",
        [
            ("pending", proto.A2AState.SUBMITTED),
            ("running", proto.A2AState.WORKING),
            ("awaiting_human", proto.A2AState.INPUT_REQUIRED),
            ("completed", proto.A2AState.COMPLETED),
            ("failed", proto.A2AState.FAILED),
            ("cancelled", proto.A2AState.CANCELED),
        ],
    )
    async def test_run_status_projects_through(self, status, expected):
        run = _Run(status)
        assert (
            await task_svc.resolve_state(_DB([run]), _Task(run_id=run.id)) is expected
        )

    async def test_vanished_run_fails_closed(self):
        """Reporting 'working' forever for something nothing is executing is
        the worse failure."""
        state = await task_svc.resolve_state(_DB([None]), _Task(run_id=uuid.uuid4()))
        assert state is proto.A2AState.FAILED

    async def test_cancel_request_wins_over_a_live_run(self):
        from datetime import UTC, datetime

        run = _Run("running")
        task = _Task(run_id=run.id, cancelled=datetime.now(UTC))
        assert await task_svc.resolve_state(_DB([run]), task) is proto.A2AState.CANCELED

    async def test_the_task_is_read_in_the_statement_that_reads_its_run(self):
        """The terminal write commits the run and the task together.

        A task loaded before that commit, interpreted with a run read after
        it, answered ``completed`` with no ``respondingAgent``. Both rows come
        from ONE statement, loaded over the caller's instances; what that
        prevents against a live database is in
        ``test_a2a_task_read_postgres.py``.
        """
        from app.models.a2a import A2ATask
        from app.models.assignment import AssignmentRun

        issued: list = []

        class _Recording(_DB):
            async def execute(self, stmt):
                issued.append(stmt)
                return await super().execute(stmt)

        run = _Run("completed")
        await task_svc.resolve_state(_Recording([run]), _Task(run_id=run.id))
        assert len(issued) == 1, issued
        assert [d["entity"] for d in issued[0].column_descriptions] == [
            A2ATask,
            AssignmentRun,
        ]
        assert issued[0].get_execution_options().get("populate_existing") is True


class TestArtifacts:
    async def test_completed_run_yields_a_text_artifact(self):
        run = _Run("completed", result="the answer")
        arts = await task_svc.build_artifacts(
            _DB([run]), _Task(run_id=run.id), _Agent(), "1.0"
        )
        assert len(arts) == 1
        assert arts[0]["parts"][0] == {"text": "the answer"}

    @pytest.mark.parametrize("version", ["1.0", "0.3"])
    async def test_artifact_is_keyed_artifact_id_not_id(self, version):
        """Both versions require ``artifactId`` and label with ``name``.

        Emitting ``id``/``title`` made every completed task's result fail a
        partner's deserialisation - the one payload on this surface that
        carries the actual answer - so the key names are pinned here rather
        than left to the round-trip test below.
        """
        run = _Run("completed", result="x")
        art = (
            await task_svc.build_artifacts(
                _DB([run]), _Task(run_id=run.id), _Agent(), version
            )
        )[0]
        assert set(art) == {"artifactId", "name", "parts"}
        assert uuid.UUID(art["artifactId"])  # opaque, and a fresh one each time

    async def test_completed_task_deserialises_for_a_v03_partner(self):
        """The regression test that actually bites: a full v0.3 ``Task``.

        Asserting on our own key names cannot catch a wrong key name, so the
        shape is put through the reference v0.3 parser instead - which is
        what a partner that asks for v0.3 reads it with.
        """
        from a2a.compat.v0_3 import types as v03

        run = _Run("completed", result="the answer")
        task = _Task(run_id=run.id)
        arts = await task_svc.build_artifacts(
            _DB([run]), task, _Agent(), "0.3"
        )
        parsed = v03.Task.model_validate(
            {
                "id": str(uuid.uuid4()),
                "contextId": str(uuid.uuid4()),
                "status": {"state": "completed"},
                "artifacts": arts,
            }
        )
        assert parsed.artifacts[0].artifact_id
        assert parsed.artifacts[0].name == "result"

    async def test_legacy_version_uses_the_legacy_part_shape(self):
        run = _Run("completed", result="x")
        arts = await task_svc.build_artifacts(
            _DB([run]), _Task(run_id=run.id), _Agent(), "0.3"
        )
        assert arts[0]["parts"][0]["kind"] == "text"


class TestCompletedTaskHistoryMirror:
    """v0.3 completed tasks mirror the result into ``history``.

    Artifacts are where the spec puts the output, but Foundry reads "the
    agent's reply" off the last agent message in history — a completed task
    whose answer lives only in artifacts renders to its user as the tool-call
    name, not the answer.
    """

    async def test_v03_history_carries_the_result_and_deserialises(self):
        from a2a.compat.v0_3 import types as v03

        from app.api.v1.a2a_server import _render_task

        run = _Run("completed", result="the answer")
        rendered = await _render_task(
            _DB([run, run]), _Task(run_id=run.id), _Agent(), "0.3"
        )
        parsed = v03.Task.model_validate(rendered)
        assert parsed.history[0].role.value == "agent"
        # Same parts in all three places: history and status.message are
        # mirrors, never a second rendering that could drift from the
        # artifact.
        assert rendered["history"][0]["parts"] == rendered["artifacts"][0]["parts"]
        assert rendered["history"][0]["parts"][0]["text"] == "the answer"
        assert rendered["status"]["message"] == rendered["history"][0]
        assert parsed.status.message.parts[0].root.text == "the answer"

    async def test_v1_task_mirrors_too_and_survives_the_reference_parser(self):
        """v1.0 mirrors the result exactly like v0.3 does.

        An earlier revision mirrored only for v0.3 because Foundry was the
        client that demonstrably needed it — but where a v1.0 client reads
        the result from is exactly as unverified as Foundry's behaviour was,
        and the proto Task carries both fields. The mirror must be
        proto-shaped: ``ROLE_AGENT``, no ``kind`` discriminator.
        """
        from a2a.types import a2a_pb2
        from google.protobuf import json_format

        from app.api.v1.a2a_server import _render_task

        run = _Run("completed", result="x")
        rendered = await _render_task(
            _DB([run, run]), _Task(run_id=run.id), _Agent(), "1.0"
        )
        assert rendered["history"][0]["parts"] == rendered["artifacts"][0]["parts"]
        assert rendered["status"]["message"] == rendered["history"][0]
        parsed = json_format.ParseDict(rendered, a2a_pb2.Task())
        assert parsed.history[0].role == a2a_pb2.ROLE_AGENT
        assert parsed.history[0].parts[0].text == "x"

    async def test_incomplete_task_carries_no_history(self):
        from app.api.v1.a2a_server import _render_task

        run = _Run("running")
        rendered = await _render_task(
            _DB([run]), _Task(run_id=run.id), _Agent(), "0.3"
        )
        assert "history" not in rendered

    async def test_json_agent_yields_a_structured_artifact(self):
        # ``data`` is the v1.0 proto field; ``structuredData`` was a draft
        # spelling no proto field carries, and the reference parser rejected
        # every artifact shaped with it.
        run = _Run("completed", result='{"verdict": "malicious"}')
        arts = await task_svc.build_artifacts(
            _DB([run]), _Task(run_id=run.id), _Agent("json"), "1.0"
        )
        assert arts[0]["parts"][0] == {"data": {"verdict": "malicious"}}

    async def test_non_json_agent_never_gets_a_structured_artifact(self):
        """Synthesising one would make the card - which advertises output
        modes - a lie."""
        run = _Run("completed", result='{"a": 1}')
        arts = await task_svc.build_artifacts(
            _DB([run]), _Task(run_id=run.id), _Agent("markdown"), "1.0"
        )
        assert arts[0]["parts"][0] == {"text": '{"a": 1}'}

    async def test_json_agent_producing_non_json_degrades_to_text(self):
        run = _Run("completed", result="not json at all")
        arts = await task_svc.build_artifacts(
            _DB([run]), _Task(run_id=run.id), _Agent("json"), "1.0"
        )
        assert arts[0]["parts"][0] == {"text": "not json at all"}

    async def test_no_result_yields_no_artifacts(self):
        run = _Run("completed", result=None)
        assert (
            await task_svc.build_artifacts(
                _DB([run]), _Task(run_id=run.id), _Agent(), "1.0"
            )
            == []
        )


class TestCancellation:
    async def test_live_task_can_be_cancelled(self):
        run = _Run("running")
        db = _DB([run, None])
        assert await task_svc.request_cancel(db, _Task(run_id=run.id)) is True

    async def test_terminal_task_cannot_be_cancelled(self):
        run = _Run("completed")
        assert await task_svc.request_cancel(_DB([run]), _Task(run_id=run.id)) is False

    async def test_cancel_is_recorded_for_cross_replica_pickup(self):
        """The cancelling request rarely lands on the replica running the
        task, so an in-process event alone would silently do nothing."""
        run = _Run("running")
        task = _Task(run_id=run.id)
        await task_svc.request_cancel(_DB([run, None]), task)
        assert task.cancel_requested_at is not None

    async def test_cancel_losing_the_race_to_a_finalize_reports_false(self):
        """The state read and the cancel write are not atomic across replicas.

        The executor can commit ``completed`` (with the result) in the gap;
        the guarded UPDATE then matches nothing, and this cancel must report
        too-late rather than flip a finished, billed run to ``cancelled`` —
        which skipped ``build_artifacts`` on the next poll and cost the
        partner the one payload that carries the actual answer.
        """
        run = _Run("running")  # read says running...
        task = _Task(run_id=run.id)

        class _RacedDB(_DB):
            async def execute(self, stmt):
                from sqlalchemy.sql.dml import Update

                result = await super().execute(stmt)
                if isinstance(stmt, Update):
                    # ...but the executor committed ``completed`` first, so the
                    # guarded UPDATE matched nothing.
                    run.status = "completed"
                    result.rowcount = 0
                return result

        # The state read, the task lock, the guarded UPDATE, the state read again.
        db = _RacedDB([run, None, None, run])
        assert await task_svc.request_cancel(db, task) is False
        assert task.cancel_requested_at is None

    async def test_a_cancel_meeting_a_run_that_asked_withdraws_the_question(self):
        """The same race, lost to a run that ended by asking its caller.

        The run completes and the task gets its question in one commit, between
        the cancel's state read and its guarded write. Read again, the task is
        ``input-required``, and a cancel withdraws the question of such a task
        rather than answering that it is already terminal.
        """
        run = _Run("running")
        task = _Task(run_id=run.id)
        raced = False

        class _RacedDB(_DB):
            async def execute(self, stmt):
                from sqlalchemy.sql.dml import Update

                nonlocal raced
                result = await super().execute(stmt)
                if isinstance(stmt, Update) and not raced:
                    # What the re-read loads into the caller's task: the
                    # executor's commit, made before the guarded UPDATE ran.
                    raced = True
                    run.status = "completed"
                    task.caller_question = "Which region?"
                    result.rowcount = 0
                return result

        db = _RacedDB([run, None, None, run])
        assert await task_svc.request_cancel(db, task) is True
        assert task.cancel_requested_at is not None
        assert await task_svc.resolve_state(_DB([run]), task) is proto.A2AState.CANCELED


class TestTheTaskRowIsTakenBeforeTheRunRow:
    """The cancel and the claim take the task row before the run row.

    The terminal write and the reaper take them in that order, so a cancel or a
    claim writing the run first and then waiting for the task deadlocked with
    them. Which transaction waits for which is asserted against PostgreSQL in
    ``test_a2a_cancel_lock_order_postgres.py``; these pin the order of the
    statements themselves.
    """

    async def test_the_cancel_locks_the_task_before_it_writes_the_run(self):
        from sqlalchemy.dialects import postgresql

        issued: list[str] = []

        class _Recording(_DB):
            async def execute(self, stmt):
                issued.append(str(stmt.compile(dialect=postgresql.dialect())))
                return await super().execute(stmt)

        run = _Run("running")
        assert await task_svc.request_cancel(_Recording([run, None]), _Task(run_id=run.id))
        locked = [
            i
            for i, sql in enumerate(issued)
            if sql.startswith("SELECT a2a_tasks.id") and sql.endswith("FOR NO KEY UPDATE")
        ]
        written = [i for i, sql in enumerate(issued) if sql.startswith("UPDATE assignment_runs")]
        assert locked and written, issued
        assert locked[0] < written[0], "the run row was written before the task row was held"

    async def test_the_claim_writes_the_task_before_it_claims_the_run(self, monkeypatch):
        from sqlalchemy.sql.dml import UpdateBase

        captured = stubs.drive_executor(monkeypatch)
        order: list[str] = []

        class _Recording(stubs.FakeDb):
            async def execute(self, stmt):
                if isinstance(stmt, UpdateBase):
                    order.append(f"update {stmt.table.name}")
                return await super().execute(stmt)

            async def flush(self):
                order.append("flush")
                await super().flush()

        monkeypatch.setattr(
            "app.core.database.async_session",
            lambda: stubs.Ctx(
                _Recording(
                    captured["_task"],
                    captured["_run"],
                    captured["_exposure"],
                    captured["_agent"],
                )
            ),
        )
        await task_svc.execute_task_run(uuid.uuid4())

        assert order[:2] == ["flush", "update assignment_runs"], order
        assert captured["_task"].lease_heartbeat_at is not None


class TestFailureDetail:
    async def test_failed_run_reports_its_stored_error(self):
        run = _Run("failed")
        run.error = "quota_exhausted: allowance spent"
        detail = await task_svc.failure_detail(_DB([run]), _Task(run_id=run.id))
        assert detail == "quota_exhausted: allowance spent"

    async def test_runless_task_has_no_detail(self):
        assert await task_svc.failure_detail(_DB(), _Task()) is None

    async def test_vanished_run_states_that_rather_than_nothing(self):
        detail = await task_svc.failure_detail(
            _DB([None]), _Task(run_id=uuid.uuid4())
        )
        assert detail == "Execution record lost"

    def test_status_message_deserialises_for_a_v03_partner(self):
        """The failed task's stated cause must itself be a valid Message —
        an unparseable explanation is worse than none."""
        from a2a.compat.v0_3 import types as v03

        status = v03.TaskStatus.model_validate(
            {
                "state": "failed",
                "message": proto.status_message(
                    "quota_exhausted: allowance spent", "0.3", message_id="m1"
                ),
            }
        )
        assert status.message.parts[0].root.text.startswith("quota_exhausted")


class TestRetryAfterRejection:
    """A content rejection must not consume the message id.

    Deduplication protects a partner who cannot know whether their submission
    landed. A rejected submission is the opposite case: they were told exactly
    what was wrong. Answering the corrected re-send with the stale rejection
    would describe content they have already replaced.
    """

    async def test_lookup_ignores_rejected_rows(self):
        """The predicate excludes them, so a corrected re-send is free to run."""
        import inspect

        src = inspect.getsource(task_svc.find_existing_task)
        assert "rejected_reason.is_(None)" in src

    async def test_unique_index_is_partial_so_the_retry_can_insert(self):
        from app.models.a2a import A2ATask

        index = next(
            i for i in A2ATask.__table__.indexes
            if i.name == "uq_a2a_tasks_client_message"
        )
        assert index.unique is True
        assert index.dialect_options["postgresql"].get("where") is not None

    async def test_failed_task_still_deduplicates(self):
        """Unchanged on purpose: a failure IS a result for that message, and
        re-running it takes a new message id - the usual idempotency-key
        contract."""
        failed = type(
            "T", (), {"id": uuid.uuid4(), "rejected_reason": None,
                      "run_id": uuid.uuid4()}
        )()
        db = _DB([failed])
        found = await task_svc.find_existing_task(db, "c1", "m-1")
        assert found is failed

    async def test_rejection_releases_the_dedup_claim(self, monkeypatch):
        """Otherwise every corrected retry pays a wasted round trip and the
        key lingers for the whole window."""
        released = []

        class _Redis:
            async def delete(self, key):
                released.append(key)

        async def _client():
            return _Redis()

        monkeypatch.setattr("app.core.redis.redis_client", _client, raising=False)
        await task_svc._release_message_id("c1", "m-1")
        assert released == [task_svc._dedup_key("c1", "m-1")]

    async def test_release_survives_a_redis_outage(self, monkeypatch):
        """A stale claim is harmless - create_task falls through to the
        database - so an outage here must not raise.

        The outage is simulated rather than left to the environment: on a
        machine with Redis running, an unpatched call would release a real key
        and exercise no outage at all."""
        calls: list[int] = []

        async def _down():
            calls.append(1)
            raise ConnectionError("redis down")

        monkeypatch.setattr("app.core.redis.redis_client", _down)
        await task_svc._release_message_id("c1", "m-1")
        assert calls, "the simulated outage was never reached"


class TestExecutingIdentity:
    """Which identity an external run executes as.

    It comes from the exposure, resolved once when the agent was exposed, so
    nothing is inferred at execution time. Deriving it from ``agent.user_id``
    instead would run external work as whoever seeded a company-managed agent,
    since that column is preserved rather than cleared.
    """

    class _Exposure:
        def __init__(self, run_as=None):
            self.run_as_user_id = run_as or uuid.uuid4()

    def test_runs_as_the_exposures_recorded_identity(self):
        exposure = self._Exposure()
        assert task_svc._executing_user_id(exposure) == str(exposure.run_as_user_id)

    def test_identity_never_derives_from_the_agent_at_execution_time(self):
        """The regression that made this necessary: reading agent.user_id here
        silently reintroduces the seeding administrator for company agents."""
        import ast
        import inspect

        # Checks the body, not the text: the docstring explains the old
        # behaviour and necessarily names agent.user_id.
        fn = ast.parse(inspect.getsource(task_svc._executing_user_id)).body[0]
        body = [n for n in fn.body if not isinstance(n, ast.Expr)]
        names = {
            n.value.id
            for stmt in body
            for n in ast.walk(stmt)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
        }
        assert "agent" not in names
        assert "exposure" in names

    def test_always_a_parseable_uuid(self):
        """The old code could yield the literal string "None", which the quota
        gate could not parse - it logged an ERROR per submission and, under the
        default QUOTA_FAIL_MODE=open, let the run through unmetered."""
        value = task_svc._executing_user_id(self._Exposure())
        assert uuid.UUID(value)


class TestInboundSizeCaps:
    """Rate limits bound how MANY submissions a partner makes, never how large
    each one is. Without a cap, one registration can persist multi-MB rows in
    ``inbound_parts`` and push arbitrarily long prompts into the executor
    entirely inside its normal 10/min budget.
    """

    def test_oversized_flattened_message_is_rejected_before_any_run(
        self, monkeypatch
    ):
        from app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "a2a_max_message_chars", 100)
        with pytest.raises(task_svc.TaskRejectedError) as exc:
            task_svc.validate_submission([{"kind": "text", "text": "x" * 101}])
        # CONTENT_TYPE_NOT_SUPPORTED, like every other pre-run refusal here, so
        # the partner is told the shape is wrong and is charged for nothing.
        assert exc.value.reason == task_svc.REJECT_CONTENT_TYPE

    def test_the_cap_counts_the_whole_flattened_message_not_one_part(
        self, monkeypatch
    ):
        from app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "a2a_max_message_chars", 100)
        parts = [{"kind": "text", "text": "y" * 60} for _ in range(2)]
        with pytest.raises(task_svc.TaskRejectedError):
            task_svc.validate_submission(parts)

    def test_a_message_at_the_limit_is_accepted(self, monkeypatch):
        from app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "a2a_max_message_chars", 100)
        assert task_svc.validate_submission(
            [{"kind": "text", "text": "z" * 100}]
        ) == "z" * 100


class TestTerminalWriteSurvivesADatabaseBlip:
    """The terminal write is the one write on this path whose loss is
    partner-visible: the answer exists and has been charged for, so dropping it
    leaves the reaper to report "abandoned" a lease later.
    """

    async def test_terminal_write_is_retried_on_a_fresh_session(self, monkeypatch):
        from app.core.database import async_session as real_session  # noqa: F401

        calls = {"fresh": 0}

        class _Result:
            rowcount = 1

        class _FreshDB:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, _stmt):
                return _Result()

            async def commit(self):
                calls["fresh"] += 1

        class _BrokenDB:
            is_active = True

            async def flush(self):
                raise SQLAlchemyError("connection reset")

        monkeypatch.setattr("app.core.database.async_session", lambda: _FreshDB())
        monkeypatch.setattr(task_svc, "_TERMINAL_WRITE_BACKOFF_SECONDS", 0)

        task = _Task()
        task.lease_heartbeat_at = None
        task.reaped = False
        await task_svc._finalise_run(
            _BrokenDB(), task, uuid.uuid4(), "completed", "the answer", None
        )
        # The primary attempt died; the retry landed the write elsewhere.
        assert calls["fresh"] == 1

    async def test_giving_up_leaves_it_to_the_reaper_rather_than_raising(
        self, monkeypatch
    ):
        class _AlwaysBroken:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def execute(self, _stmt):
                raise SQLAlchemyError("still down")

            async def commit(self):
                raise SQLAlchemyError("still down")

        class _BrokenDB:
            is_active = True

            async def flush(self):
                raise SQLAlchemyError("connection reset")

        monkeypatch.setattr(
            "app.core.database.async_session", lambda: _AlwaysBroken()
        )
        monkeypatch.setattr(task_svc, "_TERMINAL_WRITE_BACKOFF_SECONDS", 0)

        task = _Task()
        task.lease_heartbeat_at = None
        task.reaped = False
        # Never raises out of the executor's ``finally`` — the run is already
        # over, and the reaper is the documented backstop.
        await task_svc._finalise_run(
            _BrokenDB(), task, uuid.uuid4(), "completed", "lost", None
        )


class TestBlockingWaitLeavesTheCallersObjectsUsable:
    """The blocking wait must end its poll transaction with COMMIT, not ROLLBACK.

    The send handler has ``agent`` and ``exposure`` loaded in the same session
    and reads them AFTER the wait, to render the response. ``rollback`` expires
    every instance in a session, and plain attribute access on an expired object
    in an async session raises instead of lazy-loading — so ending the poll with
    a rollback turned every blocking send (the v1.0 default, i.e. every
    ADK/Vertex call) into a 500 on ``agent.output_format``. ``commit`` on a
    session configured ``expire_on_commit=False`` releases the connection and
    expires nothing.
    """

    async def test_wait_commits_between_polls_and_never_rolls_back(self, monkeypatch):
        from app.core.config import settings as app_settings

        calls: list[str] = []

        class _DB:
            async def commit(self):
                calls.append("commit")

            async def rollback(self):
                calls.append("rollback")

            async def refresh(self, _obj):
                calls.append("refresh")

        states = [proto.A2AState.WORKING, proto.A2AState.COMPLETED]

        async def fake_state(_db, _task):
            return states.pop(0)

        monkeypatch.setattr(task_svc, "resolve_state", fake_state)
        monkeypatch.setattr(app_settings, "a2a_blocking_poll_interval_seconds", 0)

        await task_svc.wait_until_terminal(_DB(), _Task())

        assert "rollback" not in calls, (
            "the blocking wait rolled back, which expires the caller's agent "
            "and exposure and makes the render raise"
        )
        # One non-terminal poll ends the transaction and reloads nothing: the
        # next poll's ``resolve_state`` reads the task again with its run,
        # which is how a cancel committed elsewhere is seen.
        assert calls == ["commit"]


class TestLifecycleRegressions:
    """The three ways a task could leave a terminal state or ignore a cancel.

    All were reachable, and all produced a partner-visible lie: FAILED then
    COMPLETED, CANCELED then COMPLETED, or a follow-up answered with a
    different task id.
    """

    async def test_finalise_never_overwrites_another_writers_terminal_state(self):
        """The reaper (or a cancel) wins. The executor's own write is
        conditional on the run still being `running`, so a stale in-session
        object cannot resurrect a finished run."""
        seen = {}

        class _Result:
            rowcount = 0  # the guarded UPDATE matched nothing

        class _DB:
            is_active = True  # a healthy session skips the aborted-tx rollback

            async def execute(self, stmt):
                seen["sql"] = str(stmt)
                return _Result()

            async def commit(self):
                pass

            async def flush(self):
                # The task row is written and flushed before the run UPDATE, so
                # both this path and the reaper take the two locks in the same
                # order.
                pass

        task = type(
            "T", (), {"id": uuid.uuid4(), "lease_heartbeat_at": None, "reaped": False}
        )()
        await task_svc._finalise_run(
            _DB(), task, uuid.uuid4(), "completed", "result", None
        )
        # The guard is in the statement itself, not a read-then-write race.
        assert "status" in seen["sql"].lower()

    def test_finaliser_guards_on_running(self):
        import inspect

        src = inspect.getsource(task_svc._finalise_run)
        assert 'AssignmentRun.status == "running"' in src

    def test_supervisor_interval_is_well_inside_the_lease(self):
        """A run must heartbeat several times before the reaper could consider
        it abandoned, or a long run reaps itself mid-flight."""
        from app.core.config import settings

        assert task_svc._supervise_interval() * 2 <= settings.a2a_run_lease_seconds

    def test_executor_passes_a_cancel_event(self):
        """tasks/cancel writes cancel_requested_at; without this the run never
        learns, finishes, and the final write clobbers the cancellation."""
        import inspect

        src = inspect.getsource(task_svc.execute_task_run)
        assert "cancel_event=cancel_event" in src
        assert "_supervise_run" in src

    def test_supervisor_polls_the_cross_replica_cancel_flag(self):
        import inspect

        src = inspect.getsource(task_svc._supervise_run)
        assert "cancel_requested_at" in src
        assert "lease_heartbeat_at" in src

    def test_the_cancel_is_polled_faster_than_the_lease_is_refreshed(self):
        """The delay between a cancel and the run noticing is what a user
        watching sits through; the lease cadence is a different question and
        must not set it."""
        from app.core.config import settings

        assert task_svc._supervise_interval() >= settings.a2a_cancel_poll_seconds
        assert settings.a2a_cancel_poll_seconds <= 30

    def test_the_heartbeat_keeps_its_own_cadence(self):
        """Polling often must not mean writing often: the lease is refreshed
        on its own interval, not on every cancel check."""
        import inspect

        src = inspect.getsource(task_svc._supervise_run)
        beat = src.index("now - last_beat >= beat_every")
        # The write itself, not the docstring naming the column.
        assert beat < src.index("values(lease_heartbeat_at")

    def test_the_heartbeat_is_measured_in_wall_time(self):
        """A tick sum drifts under load, and a run of failed ticks would
        advance it while no heartbeat landed — the reaper then takes a run
        whose supervisor is alive."""
        import inspect

        src = inspect.getsource(task_svc._supervise_run)
        assert "last_beat = datetime.now(UTC)" in src
        # Moved only after the write, so a failed tick retries at the next
        # poll rather than a lease later.
        assert src.index("await db.commit()") < src.index("last_beat = now")

    async def test_duplicate_during_a_redis_outage_returns_the_existing_task(
        self, monkeypatch
    ):
        """_claim_message_id returns True for both callers when Redis is down
        and defers to the unique index — which must not surface as a 500.

        The outage is simulated rather than left to the environment: on a
        machine whose Redis still held this claim from an earlier run, the
        refused claim found the task before any insert, and the unique index
        was never reached."""
        from sqlalchemy.exc import IntegrityError

        outages: list[int] = []

        async def _down():
            outages.append(1)
            raise ConnectionError("redis down")

        monkeypatch.setattr("app.core.redis.redis_client", _down)

        existing = type("T", (), {"id": uuid.uuid4(), "rejected_reason": None})()
        calls = {"n": 0}

        class _Result:
            def __init__(self, v):
                self._v = v

            def scalar_one_or_none(self):
                return self._v

        class _DB:
            async def execute(self, _s):
                calls["n"] += 1
                # first lookup: nothing; after the IntegrityError: the winner
                return _Result(existing if calls["n"] > 1 else None)

            async def flush(self):
                # create_task flushes the AssignmentRun first; only the second
                # flush (the A2ATask insert) can hit the unique index.
                calls["flush"] = calls.get("flush", 0) + 1
                if calls["flush"] > 1:
                    raise IntegrityError("dup", None, Exception())

            async def rollback(self):
                pass

            async def refresh(self, _o):
                # The real session must reload the rollback-expired
                # exposure/agent; the fake's objects never expire.
                pass

            def add(self, _o):
                pass

        class _Agent:
            id = uuid.uuid4()
            user_id = uuid.uuid4()
            output_format = "markdown"

        class _Exposure:
            id = uuid.uuid4()
            run_as_user_id = uuid.uuid4()

        task, created = await task_svc.create_task(
            _DB(),
            exposure=_Exposure(),
            agent=_Agent(),
            client_id="c1",
            message_id="m-1",
            parts=[{"text": "hi"}],
        )
        assert created is False
        assert task is existing
        assert outages == [1], "the simulated outage was never reached"
        # The task insert hit the index: the run's flush, then the task's.
        assert calls["flush"] == 2


class _CapturingDB:
    """Records the bound values of every statement issued against it."""

    is_active = True

    def __init__(self):
        self.seen: dict = {}

    async def execute(self, stmt):
        self.seen.update(stmt.compile().params)
        return type("R", (), {"rowcount": 1})()

    async def commit(self):
        pass

    async def flush(self):
        pass


async def _finalised_values(conversation):
    """What ``_finalise_run`` writes to the run, given *conversation* as trace."""
    db = _CapturingDB()
    task = type(
        "T", (), {"id": uuid.uuid4(), "lease_heartbeat_at": None, "reaped": False}
    )()
    await task_svc._finalise_run(
        db, task, uuid.uuid4(), "completed", "result", None, conversation=conversation
    )
    return db.seen


class TestAnExternalTaskRecordsWhatItWasAsked:
    """The run-detail panel rebuilds the rendered prompt from the first
    user-role message in ``AssignmentRun.messages``, so an external task with
    none reads as having been sent nothing — and it is the one kind of run with
    no conversation to inspect instead.
    """

    def test_the_prompt_is_recorded_at_the_claim(self):
        """Not at the finalize. The prompt is the run's input, known before the
        run does anything, and a run still in flight is when someone most wants
        to read it."""
        import inspect

        src = inspect.getsource(task_svc.execute_task_run)
        claim = src.index('status="running"')
        assert 'messages=[{"role": "user", "content": message}]' in src[claim : claim + 400]
        assert "messages_out=conversation" in src

    async def test_the_finalize_persists_the_conversation(self):
        msg = type("M", (), {"role": "assistant", "content": "hi", "tool_calls": None,
                             "tool_call_id": None, "name": None})()
        values = await _finalised_values([msg])
        assert values["messages"] == [{"role": "assistant", "content": "hi"}]

    async def test_an_unserialisable_trace_costs_no_terminal_write(self):
        values = await _finalised_values([object()])
        assert values["status"] == "completed"
        assert "messages" not in values


class TestACancelReachesEverythingTheRunHandedOff:
    """Three kinds of child, and a run reaches all three — a transfer child run, a
    local sub-agent, another deployment over A2A. The first is a run, so the
    other two arms miss it; the last is reached by nothing else, because a run
    parked on a delegation has no live frame and its teardown never executes.

    The order is the load-bearing part: both other arms are scoped BY RUN ID,
    so a child's own sub-agents and delegations stay out of reach until the
    tree is known.
    """

    def _arms(self, monkeypatch, *, children=()):
        from app.services import orchestration_engine, sub_agent

        seen: dict[str, list] = {"children": [], "sub_agents": [], "reasons": []}

        async def _children(_db, run_ids):
            seen["children"].append(list(run_ids))
            return list(children)

        async def _sub_agents(_db, run_ids, **kwargs):
            seen["sub_agents"].append(list(run_ids))
            seen["reasons"].append(kwargs.get("reason"))
            return 0

        monkeypatch.setattr(
            orchestration_engine, "cancel_child_runs", _children
        )
        monkeypatch.setattr(
            sub_agent, "mark_sub_agent_tasks_cancelled_for_runs", _sub_agents
        )
        return seen

    async def test_the_local_arms_are_reached(self, monkeypatch):
        seen = self._arms(monkeypatch)
        run = _Run("running")
        told: list = []
        assert (
            await task_svc.request_cancel(
                _DB([run, None]), _Task(run_id=run.id), cancelled_out=told
            )
            is True
        )
        assert seen["children"] == [[run.id]]
        assert seen["sub_agents"] == [[run.id]]
        assert told == [run.id], "the caller was not told whom to stop"
        # The background tasks say the partner stopped them: read as "its run
        # was deleted", an operator went looking for a deletion that never was.
        assert seen["reasons"] == [task_svc.PARTNER_CANCEL_TASK_REASON]

    async def test_a_child_s_own_sub_agents_and_partners_are_reached(
        self, monkeypatch
    ):
        """The defect this class exists for: the transfer child was cancelled
        as a row, and the sub-agents and partner tasks IT had started were
        never asked to stop."""
        child, grandchild = uuid.uuid4(), uuid.uuid4()
        seen = self._arms(monkeypatch, children=[child, grandchild])
        run = _Run("running")
        told: list = []
        await task_svc.request_cancel(
            _DB([run, None]), _Task(run_id=run.id), cancelled_out=told
        )

        assert seen["sub_agents"] == [[run.id, child, grandchild]]
        assert told == [run.id, child, grandchild]

    async def test_the_delegated_tasks_are_locked_before_any_child_run_is_written(
        self, monkeypatch
    ):
        """The executor starting a background task holds its task row and
        then marks its run running. A cascade that wrote the child runs first
        and reached the task rows last deadlocked with it, and the partner's
        ``tasks/cancel`` answered a bare 500."""
        from app.services import orchestration_engine, sub_agent

        child = uuid.uuid4()
        order: list[tuple[str, list]] = []

        async def _find(_db, run_ids):
            order.append(("find", list(run_ids)))
            return [child]

        async def _lock(_db, run_ids):
            order.append(("lock sub_agent_tasks", list(run_ids)))

        async def _children(_db, run_ids):
            order.append(("write child runs", list(run_ids)))
            return [child]

        async def _sub_agents(_db, run_ids, **_kwargs):
            order.append(("write sub_agent_tasks", list(run_ids)))
            return 0

        monkeypatch.setattr(orchestration_engine, "find_child_runs", _find)
        monkeypatch.setattr(sub_agent, "lock_sub_agent_tasks_for_runs", _lock)
        monkeypatch.setattr(orchestration_engine, "cancel_child_runs", _children)
        monkeypatch.setattr(
            sub_agent, "mark_sub_agent_tasks_cancelled_for_runs", _sub_agents
        )
        root = uuid.uuid4()
        assert await task_svc.cascade_cancel(_DB(), [root]) == [root, child]
        steps = [step for step, _ in order]
        assert steps.index("lock sub_agent_tasks") < steps.index("write child runs")
        # The whole tree, so a child's own background tasks are held too.
        assert ("lock sub_agent_tasks", [root, child]) in order

    async def test_no_partner_is_told_inside_the_transaction(self, monkeypatch):
        """A cancel sent to a partner cannot be recalled, and this caller has
        not committed yet: if its commit fails, the partner has stopped work
        this side goes on believing is running. The cascade therefore reports
        whom to tell and sends nothing itself."""
        from app.a2a.client import delegation as delegation_mod

        spawned: list = []
        monkeypatch.setattr(
            delegation_mod,
            "spawn_run_cancel",
            lambda ids: spawned.append(list(ids)),
        )
        self._arms(monkeypatch)
        run = _Run("running")
        await task_svc.request_cancel(_DB([run, None]), _Task(run_id=run.id))
        assert spawned == [], "a partner was told before the caller committed"

    async def test_the_route_tells_the_partners_after_its_commit(self):
        """The other half of the same contract, asserted as the ordering claim
        it is: the spawn has to sit below the commit, not above it."""
        import inspect

        from app.api.v1 import a2a_server

        src = inspect.getsource(a2a_server.a2a_jsonrpc)
        cancel_at = src.index("if op == \"cancel\":")
        block = src[cancel_at : cancel_at + 1400]
        assert block.index("await db.commit()") < block.index(
            "spawn_run_cancel(cancelled_runs)"
        )

    async def test_the_reaper_tells_them_after_its_commit_too(self):
        """The twin site. Its loop owns the commit, so it owns the spawn."""
        import inspect

        from app import main as main_mod

        src = inspect.getsource(main_mod._a2a_reaper_loop)
        assert src.index("await db.commit()") < src.index(
            "spawn_run_cancel(cancelled_runs)"
        )

    async def test_a_task_with_no_run_handed_nothing_off(self, monkeypatch):
        """``run_id`` is the only correlation to any of the three."""
        seen = self._arms(monkeypatch)
        told: list = []
        await task_svc.request_cancel(
            _DB([None, None]), _Task(run_id=None), cancelled_out=told
        )
        assert seen == {"children": [], "sub_agents": [], "reasons": []}
        assert told == []

    async def test_the_partner_cancel_is_off_the_response_path(self, monkeypatch):
        """A cancel reaching N partners is N network calls, each with its own
        timeout. Holding the cancelling partner's own response for the sum of
        them makes a cancel look broken to the one caller entitled to know it
        worked.

        Asserted by making the work block: the call has to come back with it
        still unfinished, and the work has to run anyway.
        """
        import asyncio
        import contextlib

        from app.a2a.client import delegation as delegation_mod

        started = asyncio.Event()
        finish = asyncio.Event()

        @contextlib.asynccontextmanager
        async def _session():
            class _Db:
                async def commit(self):
                    pass

            yield _Db()

        async def _slow(_db, **_kw):
            started.set()
            await finish.wait()
            return delegation_mod.CancelOutcome()

        monkeypatch.setattr("app.core.database.async_session", _session)
        monkeypatch.setattr(delegation_mod, "cancel_outstanding_for", _slow)

        delegation_mod.spawn_run_cancel([uuid.uuid4()])
        assert not started.is_set(), "the caller waited for the partner call"

        await asyncio.sleep(0)
        assert started.is_set(), "the cancel never ran at all"
        finish.set()
        await asyncio.sleep(0)

    async def test_the_reaper_reaches_them_too(self):
        """The twin site, same sentence: the lifetime bound only holds if the
        work holding the task open actually stops."""
        import inspect

        src = inspect.getsource(task_svc.reap_stale_tasks)
        assert "db, over_age_run_ids, reason=LIFETIME_CANCEL_TASK_REASON" in src
        # Guarded, and suppressed: the reap is a backstop, so one pathological
        # tree must not cost every other task the sweep already drove terminal.
        assert "if over_age_run_ids:" in src
        assert src.index("if over_age_run_ids:") < src.index("cascade_cancel(")
