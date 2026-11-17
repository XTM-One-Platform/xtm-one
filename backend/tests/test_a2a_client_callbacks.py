"""Being told a partner finished, instead of asking.

Spec 011 User Story 4. This is the one part of the outbound direction that adds
a route a partner may call, which `009` ruled out — so the tests that matter
most here are the refusals, and the one that proves the floor still works when
the callback never comes.
"""

from __future__ import annotations

import uuid
from contextlib import suppress
from types import SimpleNamespace
from typing import Any

import pytest

from app.a2a.client import callbacks

pytestmark = pytest.mark.asyncio


def _row(**kw: Any) -> SimpleNamespace:
    fields = {
        "id": uuid.uuid4(),
        "integration_id": uuid.uuid4(),
        "target_id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "run_ref": None,
        "conversation_ref": None,
        "partner_task_id": "partner-task-1",
        "outcome": None,
        "callback_secret": None,
        "callback_claimed_at": None,
        "abandoned_at": None,
        "reopened_at": None,
        "submitted_at": None,
        "terminal_at": None,
    }
    fields.update(kw)
    return SimpleNamespace(**fields)


class TestWhetherAPartnerCanReachUs:
    async def test_an_operator_saying_no_settles_it(self, monkeypatch):
        """Registering tells a partner where this deployment lives, which in an
        outbound-only relationship it would otherwise never learn."""
        monkeypatch.setattr(
            callbacks.app_settings, "a2a_client_callbacks_reachable", False
        )
        assert await callbacks.deployment_is_reachable() is False

    async def test_an_operator_saying_yes_settles_it_too(self, monkeypatch):
        monkeypatch.setattr(
            callbacks.app_settings, "a2a_client_callbacks_reachable", True
        )
        monkeypatch.setattr(callbacks.app_settings, "base_url", "http://127.0.0.1:8100")
        assert await callbacks.deployment_is_reachable() is True

    async def test_a_loopback_address_is_not_reachable(self, monkeypatch):
        """Unset, the configured address decides — and one the destination
        guard would refuse to SEND to is one no partner can reach either."""
        monkeypatch.setattr(
            callbacks.app_settings, "a2a_client_callbacks_reachable", None
        )
        monkeypatch.setattr(callbacks.app_settings, "base_url", "http://127.0.0.1:8100")
        assert await callbacks.deployment_is_reachable() is False

    async def test_a_public_address_is_reachable(self, monkeypatch):
        monkeypatch.setattr(
            callbacks.app_settings, "a2a_client_callbacks_reachable", None
        )
        monkeypatch.setattr(callbacks.app_settings, "base_url", "https://copilot.example")

        async def _allowed(_url, **_kw):
            return _url

        monkeypatch.setattr(callbacks, "assert_allowed_async", _allowed)
        assert await callbacks.deployment_is_reachable() is True

    async def test_a_callback_address_names_a_reachable_one(self, monkeypatch):
        """Naming an address a partner should reach asserts that it can, so a
        deployment whose ``base_url`` reads as private needs nothing further."""
        monkeypatch.setattr(
            callbacks.app_settings, "a2a_client_callbacks_reachable", None
        )
        monkeypatch.setattr(callbacks.app_settings, "base_url", "http://127.0.0.1:8100")
        monkeypatch.setattr(
            callbacks.app_settings,
            "a2a_client_callback_base_url",
            "https://partners.example",
        )
        assert await callbacks.deployment_is_reachable() is True

    async def test_a_refusal_beats_a_callback_address(self, monkeypatch):
        """The refusal withholds this deployment's whereabouts; an address to
        disclose them at cannot overrule it."""
        monkeypatch.setattr(
            callbacks.app_settings, "a2a_client_callbacks_reachable", False
        )
        monkeypatch.setattr(
            callbacks.app_settings,
            "a2a_client_callback_base_url",
            "https://partners.example",
        )
        assert await callbacks.deployment_is_reachable() is False


class TestAuthenticatingACallback:
    def test_the_right_secret_matches(self):
        secret = callbacks.mint_secret()
        row = _row(callback_secret=callbacks.store_secret(secret))
        assert callbacks.presented_secret_matches(row, secret) is True

    def test_a_wrong_secret_does_not(self):
        row = _row(callback_secret=callbacks.store_secret(callbacks.mint_secret()))
        assert callbacks.presented_secret_matches(row, "guessed") is False

    def test_no_secret_presented_does_not(self):
        row = _row(callback_secret=callbacks.store_secret("s"))
        assert callbacks.presented_secret_matches(row, None) is False

    def test_a_delegation_with_no_secret_accepts_nothing(self):
        """No callback was registered for it, so no callback may act on it."""
        assert callbacks.presented_secret_matches(_row(), "anything") is False

    def test_an_unreadable_secret_refuses(self, monkeypatch):
        """A rotated encryption key fails closed rather than letting a callback
        through — the same choice the sending half makes."""
        def _boom(_c):
            raise ValueError("key rotated")

        monkeypatch.setattr(callbacks, "decrypt_value", _boom)
        row = _row(callback_secret="ciphertext")
        assert callbacks.presented_secret_matches(row, "anything") is False

    def test_the_comparison_is_constant_time(self):
        """Through ``timing_safe_str_eq``: ``hmac.compare_digest`` over bytes,
        because on a ``str`` it raises for a non-ASCII value."""
        import inspect

        assert "timing_safe_str_eq(" in inspect.getsource(
            callbacks.presented_secret_matches
        )

    @pytest.mark.parametrize("presented", ["caf\xe9", "caf\xc3\xa9", "\ud800", "\U0001f600"])
    def test_a_value_no_minted_secret_could_be_is_a_mismatch_not_an_error(self, presented):
        """A header arrives latin-1 decoded, the body token straight out of
        JSON - where a ``"\\ud800"`` escape is a lone surrogate."""
        row = _row(callback_secret=callbacks.store_secret(callbacks.mint_secret()))
        assert callbacks.presented_secret_matches(row, presented) is False


class TestFindingTheDelegationACallbackNames:
    async def test_a_malformed_id_finds_nothing_without_a_query(self):
        class _Db:
            async def execute(self, _s):
                raise AssertionError("should not query on an unparseable id")

        assert await callbacks.addressed_delegation(_Db(), "not-a-uuid") is None

    async def test_which_delegations_are_addressable_is_pinned_on_a_real_db(self):
        """FR-019's other half: a concluded delegation is not a thing a
        callback can act on, and answering differently would say so.

        The split is three ways, not two — abandonment cancelled nothing and
        is the one conclusion a late partner may contradict — and which rows a
        WHERE clause admits is decided by running it, not by reading it. Held
        in ``test_a2a_client_abandonment_postgres.py``; this is the pointer.
        """
        import inspect

        src = inspect.getsource(callbacks.addressed_delegation)
        assert "outcome.is_(None)" in src
        assert "abandoned_at" in src


class TestActingOnceAndOnlyOnce:
    async def test_the_claim_is_the_write_not_a_read_before_one(self):
        """A read followed by a write leaves both replicas believing they won."""
        seen: list = []

        class _Db:
            async def execute(self, stmt):
                seen.append(stmt)

                class _R:
                    rowcount = 1

                return _R()

            async def commit(self):
                return None

        assert await callbacks.claim(_Db(), _row()) is True
        # One statement, and it is the UPDATE — nothing was read first.
        assert len(seen) == 1
        assert "UPDATE" in str(seen[0]).upper()

    async def test_a_lost_claim_acts_on_nothing(self, monkeypatch):
        """Every replica can receive the same callback, and the floor runs
        underneath regardless. It records that the partner called — the
        holder of the claim collects on release — and does nothing else: no
        read of the partner's task, and no caller resumed."""
        async def _lost(_db, _row):
            return False

        monkeypatch.setattr(callbacks, "claim", _lost)

        async def _never(*_a, **_k):
            raise AssertionError("a lost claim must not act")

        monkeypatch.setattr(callbacks, "_resume_caller", _never)

        writes: list = []

        class _Db:
            async def execute(self, stmt):
                writes.append(str(stmt))
                return None

            async def commit(self):
                return None

        await callbacks.on_partner_finished(
            _Db(), _row(), mechanism="called_back"
        )
        assert len(writes) == 1, "the stamp, and nothing else"
        assert "callback_seen_at" in writes[0]


class TestTheFloorBeneathIt:
    async def test_it_only_looks_at_unconcluded_work(self):
        import inspect

        from app.a2a.client import sweep

        assert "outcome.is_(None)" in inspect.getsource(sweep.collect_overdue)

    async def test_the_floor_and_the_claim_agree_on_when_one_has_lapsed(
        self, monkeypatch
    ):
        """They must, or the floor hands a row to a caller whose claim then
        fails — and the row is skipped by both, forever. Asserted by the shared
        condition being what each one asks: two copies of this rule is how it
        drifts apart."""
        import inspect

        from app.a2a.client import sweep

        assert "unclaimed_or_stale()" in inspect.getsource(sweep.collect_overdue)
        assert "unclaimed_or_stale()" in inspect.getsource(callbacks.claim)

    async def test_the_floor_records_that_it_asked(self):
        """The row says which mechanism reported the outcome, so a collect the
        floor made must not read as a partner having called back — that field
        is the whole evidence for how a delegation actually concluded."""
        import inspect

        from app.a2a.client import sweep

        assert "mechanism=MECHANISM_ASKED" in inspect.getsource(sweep.collect_overdue)

    async def test_the_receiver_records_that_it_was_called(self):
        import inspect

        assert "mechanism=MECHANISM_CALLED_BACK" in inspect.getsource(
            callbacks.spawn_partner_finished
        )

    def test_the_mechanism_has_no_default(self):
        """Both callers state it, so a third has to decide rather than inherit
        whichever one was written first."""
        import inspect

        sig = inspect.signature(callbacks.on_partner_finished)
        assert sig.parameters["mechanism"].default is inspect.Parameter.empty

    async def test_a_lapsed_claim_can_be_taken_again(self):
        """The bug this replaced: a replica that claimed and then died left the
        delegation outstanding and invisible to the floor forever, because no
        partner calls twice about a callback it delivered successfully."""
        condition = str(callbacks.unclaimed_or_stale())
        assert "IS NULL" in condition.upper()
        assert "<" in condition

    async def test_the_lapse_is_the_ceiling_the_work_is_bounded_by(self):
        """Derived, not chosen: a claim covers one collect, and a collect
        cannot outlive the per-attempt ceiling."""
        from datetime import UTC, datetime

        elapsed = (datetime.now(UTC) - callbacks.claim_cutoff()).total_seconds()
        assert (
            abs(elapsed - callbacks.app_settings.a2a_client_delegation_ceiling_seconds)
            < 5
        )

    async def test_it_waits_out_the_floor_interval(self):
        import inspect

        from app.a2a.client import sweep

        src = inspect.getsource(sweep.collect_overdue)
        assert "a2a_client_floor_interval_seconds" in src

    async def test_it_collects_through_the_ordinary_path(self):
        """So the answer reaches whoever was waiting by the ordinary route —
        same conclusion, same resumption, same claim."""
        import inspect

        from app.a2a.client import sweep

        assert "on_partner_finished" in inspect.getsource(sweep.collect_overdue)


class TestResumingTheCaller:
    async def test_a_run_resumes_through_the_orchestration_engine(self):
        import inspect

        src = inspect.getsource(callbacks._resume_caller)
        assert "resume_assignment_run" in src

    async def test_a_conversation_resumes_through_the_existing_wake(self):
        """Not a third mechanism: two run paths genuinely resume differently,
        and both already had a way."""
        import inspect

        src = inspect.getsource(callbacks._resume_caller)
        assert "schedule_partner_wake" in src

    async def test_a_partner_that_is_not_actually_finished_resumes_nothing(
        self, monkeypatch
    ):
        """The body said done; the task says otherwise. Believe the task."""
        import inspect

        src = inspect.getsource(callbacks.on_partner_finished)
        assert "result.still_running" in src
        assert src.index("still_running") < src.index("_resume_caller")


class TestAnAnswerForARunThatHasEnded:
    """#12079: a scheduled or assignment run handed a task back as still
    running and then ended. Resuming it is refused by ``execute_assignment_run``,
    so the answer is raised as ``a2a.delegation.completed`` instead."""

    def _db(self, run: Any) -> Any:
        class _Result:
            def __init__(self, value: Any) -> None:
                self._value = value

            def scalar_one_or_none(self) -> Any:
                return self._value

        class _Db:
            def __init__(self) -> None:
                self.calls = 0

            async def execute(self, _stmt: Any) -> _Result:
                self.calls += 1
                # First the owed-cancel stamp, then the run.
                return _Result(None if self.calls == 1 else run)

        return _Db()

    def _result(self, text: str = "the answer") -> SimpleNamespace:
        return SimpleNamespace(succeeded=True, still_running=False, result_text=text, outcome="completed", detail=None)

    async def test_an_ended_run_gets_the_event_not_a_resume(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.services import assignment_engine, platform_events

        resumed: list[Any] = []
        published: list[tuple[str, dict[str, Any], str | None]] = []

        async def _resume(_db: Any, run: Any) -> None:
            resumed.append(run)

        async def _publish(name: str, data: dict[str, Any], *, event_id: str | None = None) -> str | None:
            published.append((name, data, event_id))
            return event_id

        monkeypatch.setattr(assignment_engine, "resume_assignment_run", _resume)
        monkeypatch.setattr(platform_events, "publish", _publish)
        run = SimpleNamespace(id=uuid.uuid4(), status="completed", assignment_id=uuid.uuid4())
        row: Any = _row(run_ref=run.id)

        await callbacks._resume_caller(self._db(run), row, self._result(), partner_name="Partner")

        assert resumed == []
        [(name, data, event_id)] = published
        assert name == platform_events.A2A_DELEGATION_COMPLETED
        assert event_id == f"a2a-delegation:{row.id}"
        assert data["agent_id"] == str(row.agent_id)
        assert data["personal_owner_id"] == str(row.user_id)
        assert data["partner_task_id"] == "partner-task-1"
        assert data["run_id"] == str(run.id)
        assert data["assignment_id"] == str(run.assignment_id)
        assert data["succeeded"] is True
        assert "the answer" in data["result_preview"]
        assert data["participant_agent_ids"] == [str(row.agent_id)]

    async def test_a_live_run_is_still_resumed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.services import assignment_engine, platform_events

        resumed: list[Any] = []
        published: list[Any] = []

        async def _resume(_db: Any, run: Any) -> None:
            resumed.append(run)

        async def _publish(*a: Any, **k: Any) -> None:
            published.append(a)

        monkeypatch.setattr(assignment_engine, "resume_assignment_run", _resume)
        monkeypatch.setattr(platform_events, "publish", _publish)
        run = SimpleNamespace(id=uuid.uuid4(), status="awaiting_human", assignment_id=None)
        row: Any = _row(run_ref=run.id)

        await callbacks._resume_caller(self._db(run), row, self._result(), partner_name="Partner")

        assert resumed == [run]
        assert published == []


class TestTheRowACallbackNeedsExistsBeforeTheCallbackCan:
    """Found live, and by nothing else. A partner calls the moment its task
    finishes, which is while the caller's turn is still open — so a row written
    into the caller's transaction is invisible exactly when the callback needs
    it, and the receiver answers the same not-found it gives an unknown
    delegation. The partner then burns a delivery attempt against a row it
    could never have found.
    """

    async def test_it_is_committed_in_its_own_session(self):
        """Not the caller's: that transaction commits at the end of the turn,
        and is not this function's to commit early."""
        import inspect

        from app.a2a.client import delegation

        src = inspect.getsource(delegation._publish_pending_row)
        assert "async_session" in src
        assert "own_db.commit()" in src

    async def test_it_is_written_as_soon_as_the_task_has_an_identity(self):
        """Before the partner can possibly call, and after the submission that
        gave it the URL — the task id is what the callback names."""
        import inspect

        from app.a2a.client import delegation

        src = inspect.getsource(delegation.delegate)
        published = src.index("_publish_pending_row")
        polled = src.index("_poll_to_outcome")
        assert published < polled

    async def test_a_failure_to_publish_costs_the_delegation_nothing(self):
        """The floor collects it instead — later than it should, and not
        lost."""
        import inspect

        from app.a2a.client import delegation

        src = inspect.getsource(delegation._publish_pending_row)
        assert "except Exception" in src


class TestBlockingAndACallbackAreAlternatives:
    """Also found live. Asking a partner to hold the connection AND to call
    back asks for the same answer twice, and they cancel out: a held connection
    means no polling phase, so the grace period has nothing to shorten and the
    attempt runs to the ceiling — with the callback arriving at a caller that
    has been waiting for it inline the whole time.
    """

    def _configuration(self, *, with_callback: bool):
        from app.a2a.client.delegation import _JsonRpcWire

        wire = _JsonRpcWire(
            endpoint="https://partner.example/rpc", version="0.3", headers={}
        )
        if with_callback:
            wire.push_config = {"url": "https://us.example/cb", "token": "t"}
        return wire.configuration() or {}

    def test_an_ordinary_send_still_asks_to_block(self):
        """Unchanged, and load-bearing: Vertex keeps no task store between
        requests, so a blocking send is the only way to get an answer from it."""
        assert self._configuration(with_callback=False).get("blocking") is True

    def test_a_send_that_registers_a_callback_does_not(self):
        assert "blocking" not in self._configuration(with_callback=True)

    def test_it_still_registers_the_callback(self):
        configuration = self._configuration(with_callback=True)
        assert configuration["pushNotificationConfig"]["url"] == "https://us.example/cb"


class TestTheCallbackIsRegisteredInThePartnersOwnSpelling:
    """One idea, three spellings. v0.3's JSON-RPC binding says
    ``pushNotificationConfig``, its proto binding named the field
    ``pushNotification``, and v1.0 renamed it again to
    ``taskPushNotificationConfig``.

    Getting it wrong is not a no-op: this side has already minted a secret,
    dropped ``blocking`` and cut the attempt to the grace period, so the
    calling agent is told the answer will arrive by itself and then waits for
    the floor. A v0.3 REST partner discards the unknown field silently, which
    is the worst of the three.
    """

    def _configuration(self, wire_cls, version):
        wire = wire_cls(
            endpoint="https://partner.example/rpc", version=version, headers={}
        )
        wire.push_config = {"url": "https://us.example/cb", "token": "t"}
        return wire.configuration() or {}

    def test_jsonrpc_v03_keeps_the_json_rpc_spelling(self):
        from app.a2a.client.delegation import _JsonRpcWire

        cfg = self._configuration(_JsonRpcWire, "0.3")
        assert cfg["pushNotificationConfig"]["url"] == "https://us.example/cb"

    def test_rest_v03_uses_the_proto_spelling(self):
        from app.a2a.client.delegation import _RestWire

        cfg = self._configuration(_RestWire, "0.3")
        assert "pushNotificationConfig" not in cfg
        assert cfg["pushNotification"]["url"] == "https://us.example/cb"

    def test_v10_uses_its_own_and_declines_blocking_out_loud(self):
        """v1.0 inverted the flag AND its default: blocking is what a partner
        does when told nothing, so declining it has to be said."""
        from app.a2a.client.delegation import _RestWire

        cfg = self._configuration(_RestWire, "1.0")
        assert cfg["taskPushNotificationConfig"]["url"] == "https://us.example/cb"
        assert cfg["returnImmediately"] is True


class TestADelegationWhosePartnerIsGoneConcludes:
    async def test_a_missing_target_is_concluded_not_left_outstanding(
        self, monkeypatch
    ):
        """``sync_targets`` deletes a target the partner stopped listing and
        ``target_id`` is SET NULL, so the row outlives what it was collected
        through. Left outstanding it is re-claimed and re-abandoned every
        cycle, for a caller that was told the answer would reach it."""
        import inspect

        from app.a2a.client import callbacks as callback_svc

        src = inspect.getsource(callback_svc.on_partner_finished)
        missing = src.index("integration is None or target is None")
        assert "_conclude_unreachable" in src[missing:]
        # And the caller is woken rather than left parked on it.
        assert "_resume_caller" in inspect.getsource(
            callback_svc._conclude_unreachable
        )

    def test_the_reachability_check_catches_what_it_says(self):
        """``except (DestinationRefusedError, Exception)`` reads as a narrowed
        handler and is not one."""
        import inspect

        from app.a2a.client import callbacks as callback_svc

        src = inspect.getsource(callback_svc.deployment_is_reachable)
        assert "except Exception:" in src
        assert "DestinationRefusedError" not in src


class TestACallbackThatLosesTheClaimIsNotLost:
    """The attempt that published the row holds the claim while it polls, so a
    quick partner's answer is not delivered twice. The window that leaves is
    one poll wide at the end of every grace period: a callback landing there
    loses the claim, and the partner has its 200 and will not call again. Left
    at that, work that finished in seconds reaches the user when the floor next
    looks — `a2a_client_floor_interval_seconds` of age, on a sweep an hour
    apart.
    """

    async def test_losing_the_claim_records_that_it_arrived(self, monkeypatch):
        stamped: list = []

        async def _lost(*_a, **_k):
            return False

        monkeypatch.setattr(callbacks, "claim", _lost)

        class _Db:
            async def execute(self, stmt):
                stamped.append(str(stmt))
                return None

            async def commit(self):
                return None

        row = type("Row", (), {"id": uuid.uuid4(), "partner_task_id": "t-1"})()
        await callbacks.on_partner_finished(_Db(), row, mechanism="called_back")
        assert stamped, "the fact that a partner called has to survive the loss"
        assert "callback_seen_at" in stamped[0]

    def test_and_the_release_collects_when_one_did(self):
        """On the release path, not left to the floor: the answer is already
        waiting, and the floor would not look at this row for another cycle."""
        import inspect

        from app.a2a.client import delegation

        # Read and cleared where the release is committed, and collected by
        # the caller once it is — not inside the savepoint, where the collect's
        # own session could not see it.
        release = inspect.getsource(delegation._release_pending_claim)
        assert "callback_seen_at" in release
        src = inspect.getsource(delegation.delegate)
        assert "_release_pending_claim(release_id)" in src
        assert "_spawn_collect(release_id)" in src


class TestOnlyAPartnerCallSetsThatAPartnerCalled:
    """`on_partner_finished` is reached by both mechanisms, and the floor loses
    the claim whenever another replica is already collecting. Stamping there
    writes "the partner called" on a row no partner called about — which is
    what an operator reads the column for, and what the claim-holder's release
    acts on."""

    async def _lost_claim(self, monkeypatch, mechanism):
        writes: list = []

        async def _lost(*_a, **_k):
            return False

        monkeypatch.setattr(callbacks, "claim", _lost)

        class _Db:
            async def execute(self, stmt):
                writes.append(str(stmt))
                return None

            async def commit(self):
                return None

        await callbacks.on_partner_finished(_Db(), _row(), mechanism=mechanism)
        return writes

    async def test_a_callback_records_that_it_arrived(self, monkeypatch):
        writes = await self._lost_claim(monkeypatch, "called_back")
        assert writes and "callback_seen_at" in writes[0]

    async def test_the_floor_losing_a_race_records_nothing(self, monkeypatch):
        """Nothing called. The floor asking and being beaten to it is not a
        partner's notification, however similar the two look here."""
        assert await self._lost_claim(monkeypatch, "asked") == []


class TestAnOutstandingRowIsNotImmortal:
    """A bounded floor collect hands back rather than cancelling, which is
    right — but nothing then concludes a row whose partner says nothing at all.
    The floor's predicate has a minimum age and no maximum, so such a row is
    re-collected every cycle for as long as it exists, and the caller waits on
    an answer that will never arrive.
    """

    async def _floor(self, monkeypatch, age_seconds):
        from datetime import UTC, datetime, timedelta

        from app.a2a.client import sweep

        row = type(
            "Row",
            (),
            {
                "id": uuid.uuid4(),
                "outcome": None,
                "partner_task_id": "t-1",
                "target_id": uuid.uuid4(),
                "integration_id": uuid.uuid4(),
                "run_ref": None,
                "conversation_ref": None,
                "user_id": uuid.uuid4(),
                "agent_id": uuid.uuid4(),
                "submitted_at": datetime.now(UTC) - timedelta(seconds=age_seconds),
                "reopened_at": None,
                "abandoned_at": None,
            },
        )()
        asked: list = []
        concluded: list = []

        async def _enabled(_db):
            return True

        monkeypatch.setattr("app.api.v1.platform.is_chat_enabled", _enabled)

        async def _ask(*_a, **_k):
            asked.append(1)

        async def _conclude(_db, r, **_k):
            concluded.append(r.id)

        monkeypatch.setattr(callbacks, "on_partner_finished", _ask)
        monkeypatch.setattr(callbacks, "conclude_abandoned", _conclude, raising=False)

        class _Db:
            async def execute(self, _stmt):
                class _R:
                    def scalars(self):
                        class _S:
                            def all(self):
                                return [row]

                        return _S()

                return _R()

        await sweep.collect_overdue(_Db())
        return asked, concluded, row

    async def test_a_partner_silent_past_the_bound_is_concluded(self, monkeypatch):
        from app.core.config import settings

        asked, concluded, row = await self._floor(
            monkeypatch, settings.a2a_client_delegation_abandoned_seconds + 60
        )
        assert concluded == [row.id], "a row this old is gone, not still running"
        assert asked == [], "and there is nothing left to ask"

    async def test_a_recent_one_is_still_collected(self, monkeypatch):
        asked, concluded, _row = await self._floor(monkeypatch, 60)
        assert asked and concluded == []

    async def test_a_partner_merely_slower_than_one_attempt_is_not(
        self, monkeypatch
    ):
        """Several whole attempt-ceilings of silence is a partner running a
        long job, not a partner that is gone — the hand-back and the callback
        exist for exactly that. The floor keeps asking it."""
        from app.core.config import settings

        asked, concluded, _row = await self._floor(
            monkeypatch, settings.a2a_client_delegation_ceiling_seconds * 10
        )
        assert asked and concluded == []


class TestBackgroundCollectsAreDrainedAtShutdown:
    def test_a_restart_does_not_strand_a_claim(self):
        """A collect holds the delegation's claim while it runs, and a process
        that stops mid-collect leaves it held until the claim lapses — a whole
        ceiling in which neither the floor nor a later callback can act. The
        set exists to keep the task from being collected; draining it is what
        makes a restart deterministic."""
        import inspect

        from app import main

        src = inspect.getsource(main.lifespan)
        shutdown = src[src.index("# Shutdown") :]
        assert "drain_background_a2a" in shutdown

    async def test_draining_waits_for_what_is_in_flight(self):
        import asyncio

        from app.a2a.client import delegation

        done: list = []

        async def _work():
            await asyncio.sleep(0)
            done.append(1)

        task = asyncio.create_task(_work())
        delegation._BACKGROUND_COLLECTS.add(task)
        task.add_done_callback(delegation._BACKGROUND_COLLECTS.discard)
        await delegation.drain_background_a2a()
        assert done == [1]
        assert not delegation._BACKGROUND_COLLECTS


class TestTheReleaseIsVisibleToTheCollectThatActsOnIt:
    """The assertion the string-matching version could not make.

    `act_on_callback` claims from a session of its own, and `claim` only wins
    on `callback_claimed_at IS NULL OR … < now - ceiling`. A release merely
    flushed into the caller's open transaction is invisible to it: the claim
    still reads as held, the collect does nothing, and the answer falls through
    to the floor — which is the delay the release exists to remove. So what has
    to be true is not that a release was written, but that a claim taken
    afterwards WINS.
    """

    async def test_a_claim_after_the_release_wins(self, monkeypatch):
        from datetime import UTC, datetime

        from app.a2a.client import delegation

        # One store, two "sessions": the release commits into it, and the
        # claim reads it back the way another connection would.
        stored = {"callback_claimed_at": datetime.now(UTC), "callback_seen_at": None}

        class _Own:
            async def execute(self, stmt):
                text = str(stmt)
                if text.startswith("UPDATE") and "callback_claimed_at" in text:
                    stored["callback_claimed_at"] = None

                class _R:
                    def scalar_one_or_none(self):
                        return stored["callback_seen_at"]

                return _R()

            async def commit(self):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

        monkeypatch.setattr("app.core.database.async_session", lambda: _Own())
        await delegation._release_pending_claim(uuid.uuid4())

        # What `claim` would evaluate, from a session that never saw the
        # caller's transaction.
        assert stored["callback_claimed_at"] is None, (
            "a collect claiming next has to find the claim free"
        )


class TestAStampedCallbackTakesOverIfTheClaimFreesUp:
    """`claim` commits on its own before the stamp is written, so nothing holds
    a lock across the two. The attempt can stop, read no stamp, release the
    claim and return — and the stamp lands afterwards on a row with no claim
    and nobody left to look at it. The stamp means "somebody must look at
    this"; a claim that is suddenly free means nobody else will.
    """

    async def _run(self, monkeypatch, *, second_claim_wins):
        tries: list = []
        collected: list = []

        async def _claim(_db, _row):
            tries.append(1)
            return second_claim_wins if len(tries) > 1 else False

        monkeypatch.setattr(callbacks, "claim", _claim)

        async def _acted(*_a, **_k):
            collected.append(1)
            return None

        # Stop after the claim decision: what is under test is whether the
        # callback goes on to act, not what acting does.
        monkeypatch.setattr(callbacks, "_target_of", _acted)

        class _Db:
            async def execute(self, _stmt):
                class _R:
                    def scalar_one_or_none(self):
                        return None

                return _R()

            async def commit(self):
                return None

        await callbacks.on_partner_finished(
            _Db(), _row(), mechanism="called_back"
        )
        return tries, collected

    async def test_it_claims_again_after_stamping(self, monkeypatch):
        tries, collected = await self._run(monkeypatch, second_claim_wins=True)
        assert len(tries) == 2, "the stamp is not the end of it"
        assert collected, "a claim that is free means nobody else will look"

    async def test_and_stops_if_the_claim_is_still_held(self, monkeypatch):
        tries, collected = await self._run(monkeypatch, second_claim_wins=False)
        assert len(tries) == 2
        assert collected == [], "somebody else is acting; two would be one too many"


class TestTheAbandonmentBoundIsNotOneAttemptsCeiling:
    """``claim_cutoff`` is derived because it bounds the same work it protects:
    a claim covers one collect, and one collect is bounded by the per-attempt
    ceiling. The abandonment bound is not that. The ceiling says how long ONE
    attempt waits; nothing in it says how long the delegated work may
    legitimately take — the hand-back and the callback exist precisely because
    the work outlasts the attempt. Ten ceilings is fifty minutes at defaults,
    while this platform's own inbound half lets a served task live four hours:
    a delegation ended at a fifth of what we ourselves permit is the two
    halves of one feature disagreeing.

    What it measures is how long a delegation may go without reaching a
    terminal state, which is not the same as how long the partner has been
    silent — see ``TestATalkativePartnerIsBoundedToo``.
    """

    def test_one_attempts_ceiling_does_not_move_it(self, monkeypatch):
        from app.core.config import settings

        before = callbacks.abandoned_cutoff()
        monkeypatch.setattr(
            settings,
            "a2a_client_delegation_ceiling_seconds",
            settings.a2a_client_delegation_ceiling_seconds * 4,
        )
        after = callbacks.abandoned_cutoff()
        assert abs((after - before).total_seconds()) < 5, (
            "the bound moved with a setting that measures one attempt's wait"
        )

    def test_the_claim_bound_is_still_derived_from_it(self, monkeypatch):
        """The contrast is the point: ``claim_cutoff`` genuinely bounds the
        work it protects, and stays derived."""
        from app.core.config import settings

        before = callbacks.claim_cutoff()
        monkeypatch.setattr(
            settings,
            "a2a_client_delegation_ceiling_seconds",
            settings.a2a_client_delegation_ceiling_seconds * 4,
        )
        after = callbacks.claim_cutoff()
        assert (before - after).total_seconds() > 60

    def test_its_default_outlasts_a_task_this_platform_would_serve(self):
        from app.core.config import settings

        assert (
            settings.a2a_client_delegation_abandoned_seconds
            >= settings.a2a_task_max_lifetime_seconds
        ), "we would end a delegation before our own inbound half gave up"


class TestATalkativePartnerIsBoundedToo:
    """The words said "silent" and the code never measured silence.

    A floor collect that comes back "working" stamps nothing, so a partner
    answering every cycle is bounded exactly like one that went dark. That is
    the right behaviour — a row that never concludes is the thing that has to
    end, and until it does its caller is waiting — but four places described a
    policy of measuring silence that nothing implements, and this row's
    history is what an operator reads while a partner is still billing.

    Only the RE-OPEN restarts the clock, because a revived row carries a
    ``submitted_at`` older than any cutoff and would otherwise be re-abandoned
    on the next cycle.
    """

    def test_a_collect_that_sees_work_in_progress_resets_nothing(self):
        import inspect

        from app.a2a.client import delegation as delegation_mod

        src = inspect.getsource(delegation_mod._record_outstanding)
        assert "reopened_at" not in src, (
            "an ordinary poll now restarts the bound, so a chatty partner is "
            "never ended and its caller waits forever"
        )

    def test_the_words_say_what_is_measured(self):
        """Asserted as the positive claim, not as a banned word: the new text
        says "silent" in the sentence that rules silence out, and a keyword
        ban would forbid the correction along with the error."""
        import inspect

        from app.a2a.client import callbacks as callback_svc
        from app.a2a.client import sweep

        def _flat(text):
            return " ".join(text.split()).lower()

        cutoff = _flat(inspect.getdoc(callback_svc.abandoned_cutoff))
        assert "without reaching a terminal state" in cutoff
        assert "answers every floor collect" in cutoff, (
            "the case the old wording got wrong is the one worth naming"
        )
        assert "presum" not in cutoff, (
            "presuming a partner GONE is the silence framing again"
        )
        assert "without concluding" in _flat(
            inspect.getdoc(callback_svc.conclude_abandoned)
        )
        floor = _flat(inspect.getsource(sweep.collect_overdue))
        assert "last time this side heard anything" not in floor, (
            "the one that was outright false"
        )
        assert "last started being outstanding" in floor


class TestALatePartnerMayContradictAnAbandonment:
    """Abandonment is the one conclusion a partner can legitimately disagree
    with. Every other terminal outcome was decided by something that happened
    — the partner failed, the credential was rejected, the ceiling ran out and
    the task was CANCELLED at the partner. Abandonment cancels nothing: it is
    this side giving up on silence, and the work may still be running. So a
    callback that finally arrives is the answer the caller was promised, and
    dropping it is the one outcome this feature must not produce.
    """

    async def test_the_row_is_reopened_before_it_is_collected(self, monkeypatch):
        """``_record`` concludes the row it finds still open. Collecting on a
        concluded one inserts a second row instead — the defect a delegation
        row always getting an end was meant to close — so the re-open has to
        happen first, and under the claim."""
        from datetime import UTC, datetime

        from app.a2a.client import callbacks as callback_svc
        from app.models.a2a import OUTCOME_TIMEOUT

        gave_up = datetime.now(UTC)
        row = _row(
            outcome=OUTCOME_TIMEOUT, abandoned_at=gave_up, terminal_at=gave_up
        )
        order: list = []

        async def _claim(_db, _row):
            order.append("claim")
            return True

        monkeypatch.setattr(callback_svc, "claim", _claim)

        async def _target(_db, _id):
            return SimpleNamespace(id=uuid.uuid4(), declared_name="P")

        monkeypatch.setattr(callback_svc, "_target_of", _target)

        async def _delegate(*_a, **_k):
            order.append("delegate")
            return SimpleNamespace(
                outcome="completed",
                still_running=True,  # stops before _resume_caller
            )

        monkeypatch.setattr("app.a2a.client.delegation.delegate", _delegate)

        class _Db:
            async def execute(self, stmt):
                params = {}
                with suppress(Exception):
                    params = stmt.compile().params
                if "outcome" in params and params["outcome"] is None:
                    order.append("reopen")
                return SimpleNamespace(
                    rowcount=1,
                    scalar_one_or_none=lambda: SimpleNamespace(id=uuid.uuid4()),
                )

            async def commit(self):
                return None

        await callback_svc.on_partner_finished(
            _Db(), row, mechanism="called_back"
        )
        assert "reopen" in order, "the row was collected while still concluded"
        assert order.index("claim") < order.index("reopen") < order.index(
            "delegate"
        ), f"wrong order: {order}"


class TestTheDrainDoesNotHoldShutdownOpen:
    """A collect goes through the ordinary delegation path, so it is bounded by
    the delegation ceiling — 300s at defaults — plus whatever the partner's
    connect takes, and ``lifespan`` awaits the drain. One collect against a
    slow partner would hold the process in shutdown for the whole ceiling, on
    every rolling restart. The trade the drain makes is right only while the
    wait is shorter than the stranded claim it buys: past that, letting the
    work lapse costs exactly the claim timeout that not draining would have
    cost anyway.
    """

    async def test_work_that_will_not_finish_is_left_to_lapse(self, monkeypatch):
        import asyncio

        from app.a2a.client import delegation

        monkeypatch.setattr(delegation, "_DRAIN_TIMEOUT_SECONDS", 0.05)
        never = asyncio.create_task(asyncio.sleep(30))
        delegation._BACKGROUND_COLLECTS.add(never)
        never.add_done_callback(delegation._BACKGROUND_COLLECTS.discard)
        try:
            loop = asyncio.get_running_loop()
            started = loop.time()
            await delegation.drain_background_a2a()
            assert loop.time() - started < 5, "the drain waited for it"
            assert not never.done(), "and left it running rather than killing it"
        finally:
            never.cancel()
            delegation._BACKGROUND_COLLECTS.discard(never)


class TestTheFloorArbitratesByWriting:
    """``collect_overdue`` filters on ``unclaimed_or_stale()`` in the SELECT
    and then calls ``conclude_abandoned`` directly — and a filter in a SELECT
    is not a claim. Two replicas running the floor in the same window both see
    the row unclaimed, both write, and both resume. That last part is not
    bookkeeping: the chat path's ``schedule_partner_wake`` posts a turn into
    the conversation and dedups nothing, so a second resume is a second
    "The partner never reported a terminal state" in front of a user.

    The write has to be the arbitration, and only the winner resumes.
    """

    def _db(self, rowcount):
        resumed: list = []

        class _Db:
            async def execute(self, _stmt):
                return SimpleNamespace(
                    rowcount=rowcount,
                    scalar_one_or_none=lambda: None,
                )

            async def commit(self):
                return None

        return _Db(), resumed

    async def test_the_replica_that_loses_the_write_resumes_nothing(
        self, monkeypatch
    ):
        db, resumed = self._db(0)

        async def _resume(*_a, **_k):
            resumed.append(1)

        monkeypatch.setattr(callbacks, "_resume_caller", _resume)
        await callbacks.conclude_abandoned(db, _row(), mechanism="asked")
        assert resumed == [], "both replicas resumed the same caller"

    async def test_the_one_that_wins_it_does(self, monkeypatch):
        db, resumed = self._db(1)

        async def _resume(*_a, **_k):
            resumed.append(1)

        monkeypatch.setattr(callbacks, "_resume_caller", _resume)
        await callbacks.conclude_abandoned(db, _row(), mechanism="asked")
        assert resumed == [1]

    async def test_only_an_open_row_may_be_abandoned(self):
        """The guard is in the statement, not in a read before it."""
        import inspect

        src = inspect.getsource(callbacks.conclude_abandoned)
        assert "outcome.is_(None)" in src


class TestAReopenedRowIsNotImmediatelyReabandoned:
    """``submitted_at`` is older than the abandonment cutoff for any row that
    was abandoned — by definition. So a row the callback route re-opens is
    re-abandoned on the very next floor cycle, and the collect that follows a
    callback CAN leave it open: the partner said it finished and its task says
    otherwise, which ``on_partner_finished`` explicitly returns early for.
    Each retry of the partner's delivery then re-opens it again, so a partner
    doing exactly what the protocol asks drives an abandon/re-open loop with a
    false resume on every turn.

    The clock has to restart with the row. ``submitted_at`` stays immutable —
    the history renders it as the submission time, and the floor's own "not
    asked lately" gate reads it — so the restart is its own stamp.
    """

    async def test_a_row_reopened_just_now_is_not_gone(self, monkeypatch):
        from datetime import UTC, datetime, timedelta

        from app.a2a.client import sweep
        from app.core.config import settings

        old = datetime.now(UTC) - timedelta(
            seconds=settings.a2a_client_delegation_abandoned_seconds + 3600
        )
        row = _row(
            submitted_at=old,
            reopened_at=datetime.now(UTC),
            abandoned_at=old,
        )
        asked: list = []
        concluded: list = []

        async def _enabled(_db):
            return True

        monkeypatch.setattr("app.api.v1.platform.is_chat_enabled", _enabled)

        async def _ask(*_a, **_k):
            asked.append(1)

        async def _conclude(_db, r, **_k):
            concluded.append(r.id)

        monkeypatch.setattr(callbacks, "on_partner_finished", _ask)
        monkeypatch.setattr(callbacks, "conclude_abandoned", _conclude)

        class _Db:
            async def execute(self, _stmt):
                return SimpleNamespace(
                    scalars=lambda: SimpleNamespace(all=lambda: [row])
                )

        await sweep.collect_overdue(_Db())
        assert concluded == [], (
            "a partner that called back seconds ago was declared gone"
        )
        assert asked == [1], "and the floor should simply ask it again"

    async def test_the_reopen_restarts_the_clock(self, monkeypatch):
        """The stamp is written by the re-open itself, under the claim."""
        from datetime import UTC, datetime

        from app.a2a.client import callbacks as callback_svc
        from app.models.a2a import OUTCOME_TIMEOUT

        row = _row(
            outcome=OUTCOME_TIMEOUT,
            abandoned_at=datetime.now(UTC),
            terminal_at=datetime.now(UTC),
            reopened_at=None,
        )
        stamped: list = []

        async def _claim(_db, _row):
            return True

        monkeypatch.setattr(callback_svc, "claim", _claim)

        async def _target(_db, _id):
            return SimpleNamespace(id=uuid.uuid4(), declared_name="P")

        monkeypatch.setattr(callback_svc, "_target_of", _target)

        async def _delegate(*_a, **_k):
            return SimpleNamespace(outcome="completed", still_running=True)

        monkeypatch.setattr("app.a2a.client.delegation.delegate", _delegate)

        class _Db:
            async def execute(self, stmt):
                params = {}
                with suppress(Exception):
                    params = stmt.compile().params
                if params.get("reopened_at") is not None:
                    stamped.append(params["reopened_at"])
                return SimpleNamespace(
                    rowcount=1,
                    scalar_one_or_none=lambda: SimpleNamespace(id=uuid.uuid4()),
                )

            async def commit(self):
                return None

        await callback_svc.on_partner_finished(
            _Db(), row, mechanism="called_back"
        )
        assert stamped, "re-opened with the same age it was abandoned at"

    async def test_it_will_not_reopen_something_else(self, monkeypatch):
        """The claim makes this safe today; the statement should say so itself,
        or a future caller reaching it by another route re-opens a row
        concluded ``partner_failed`` — the conflation this design rejects."""
        import inspect

        from app.a2a.client import callbacks as callback_svc

        src = inspect.getsource(callback_svc.on_partner_finished)
        reopen = src.index("outcome=None")
        window = src[max(0, reopen - 700) : reopen]
        assert "OUTCOME_TIMEOUT" in window
        assert "abandoned_at == A2ADelegation.terminal_at" in window


class TestTheReplayWindowIsBounded:
    """Re-addressability is not permanent. The secret is still checked, so an
    unbounded window is not an open door — but it turns a one-shot credential
    into an indefinitely replayable one: a secret lifted from a partner's logs
    months later would re-open the row, run a full delegation against the
    partner and resume the caller. A partner that has not answered within a
    further whole window is not going to.
    """

    def test_the_clause_bounds_how_old_an_abandonment_may_be(self):
        import inspect

        src = inspect.getsource(callbacks.addressed_delegation)
        assert "abandoned_cutoff()" in src


class TestTheReopenActsOnItsOwnGuard:
    """The guard on the statement is only half of it: nothing read whether it
    matched. `act_on_callback` re-reads the row, so the window is narrow — from
    that read to winning the claim — but it is reachable. Another replica
    claims, collects for longer than `claim_cutoff`, its claim lapses, this one
    wins the re-claim, and the other then concludes `completed`. The re-open
    matches nothing, correctly; carrying on regardless is what hurts, because
    `_record` finds no open row and inserts a SECOND one for the same exchange
    — the very defect the re-open exists to avoid — and resumes the caller
    again on top of it.
    """

    async def _drive(self, monkeypatch, rowcount):
        from datetime import UTC, datetime

        from app.a2a.client import callbacks as callback_svc
        from app.models.a2a import OUTCOME_TIMEOUT

        when = datetime.now(UTC)
        row = _row(
            outcome=OUTCOME_TIMEOUT, abandoned_at=when, terminal_at=when
        )
        collected: list = []

        async def _claim(_db, _row):
            return True

        monkeypatch.setattr(callback_svc, "claim", _claim)

        async def _target(_db, _id):
            return SimpleNamespace(id=uuid.uuid4(), declared_name="P")

        monkeypatch.setattr(callback_svc, "_target_of", _target)

        async def _delegate(*_a, **_k):
            collected.append(1)
            return SimpleNamespace(outcome="completed", still_running=True)

        monkeypatch.setattr("app.a2a.client.delegation.delegate", _delegate)

        class _Db:
            async def execute(self, stmt):
                params = {}
                with suppress(Exception):
                    params = stmt.compile().params
                reopening = "outcome" in params and params["outcome"] is None
                return SimpleNamespace(
                    rowcount=rowcount if reopening else 1,
                    scalar_one_or_none=lambda: SimpleNamespace(id=uuid.uuid4()),
                )

            async def commit(self):
                return None

        await callback_svc.on_partner_finished(
            _Db(), row, mechanism="called_back"
        )
        return collected

    async def test_a_reopen_that_matched_nothing_collects_nothing(
        self, monkeypatch
    ):
        assert await self._drive(monkeypatch, 0) == [], (
            "somebody decided in the meantime and this collected anyway"
        )

    async def test_a_reopen_that_won_carries_on(self, monkeypatch):
        assert await self._drive(monkeypatch, 1) == [1]


class TestACancelledCeilingIsNotAnAbandonment:
    """`abandoned_at` is deliberately left in place, which means a row can be
    concluded a SECOND time by something that cancelled at the partner: the
    re-opened collect runs at the full ceiling — `act_on_callback` passes none
    — and an unanswered read reaches `_cancel_at_partner` and writes `timeout`.
    The row is then `timeout` with the first give-up's stamp still on it, so
    the pair admits a callback for a task this side has since cancelled — the
    contradiction the clause exists to refuse.

    The row already distinguishes them. `conclude_abandoned` writes
    `terminal_at` and `abandoned_at` from one `now`, and it is the only writer
    of `abandoned_at`; every other conclusion writes a fresh `terminal_at` and
    leaves the stamp. So equality holds exactly when the conclusion IS the
    abandonment.
    """

    def test_the_clause_admits_only_a_row_abandonment_itself_ended(self):
        import inspect

        src = inspect.getsource(callbacks.addressed_delegation)
        assert "A2ADelegation.terminal_at" in src

    def test_abandonment_is_the_only_writer_of_the_stamp(self):
        """The invariant the clause rests on, pinned where it can break."""
        import inspect

        from app.a2a.client import delegation as delegation_mod

        writers = [
            src
            for src in (
                inspect.getsource(callbacks),
                inspect.getsource(delegation_mod),
            )
        ]
        assert sum(s.count("abandoned_at=") for s in writers) == 1, (
            "a second writer of abandoned_at breaks terminal_at equality"
        )

    def test_and_it_writes_both_from_one_now(self):
        import inspect

        src = inspect.getsource(callbacks.conclude_abandoned)
        assert "terminal_at=now" in src and "abandoned_at=now" in src


class TestTheFloorDefersToALiveCollect:
    """Making the write the arbitration settled floor against floor. It does
    not settle floor against the other actor on this row.

    Every other thing the floor does goes through ``claim``; this path does
    not, and the claim state it stands on was read once, in a batch SELECT of
    up to 25 rows — each of which is then polled to the grace period. For a
    row late in that batch, "unclaimed" was true minutes ago.

    So: the floor selects R. A partner's callback wins the claim and starts
    collecting, at the full ceiling because ``act_on_callback`` passes none.
    The floor reaches R, finds ``outcome IS NULL`` still true because the
    collect has not concluded, and abandons it — resuming the caller with "The
    partner never reported a terminal state" while the answer is in flight.
    The collect then lands: ``_outstanding_row`` requires an open row and
    finds none, so ``_record`` writes a SECOND row for the exchange and the
    caller is resumed again.

    A live claim means somebody better placed is already ending this row.
    Which rows the predicate actually spares is decided by running it, and is
    held in ``test_a2a_client_abandonment_postgres.py``; this pins that the
    condition is in the statement rather than in a SELECT taken minutes ago.
    """

    async def test_the_claim_is_part_of_the_statement_not_of_the_select(
        self, monkeypatch
    ):
        from app.a2a.client import callbacks as callback_svc

        conditions: list = []

        async def _resume(*_a, **_k):
            return None

        monkeypatch.setattr(callback_svc, "_resume_caller", _resume)

        class _Db:
            async def execute(self, stmt):
                with suppress(Exception):
                    conditions.append(str(stmt.whereclause))
                return SimpleNamespace(rowcount=1, scalar_one_or_none=lambda: None)

            async def commit(self):
                return None

        await callback_svc.conclude_abandoned(_Db(), _row(), mechanism="asked")
        assert conditions, "no statement was issued"
        assert "callback_claimed_at" in conditions[0], conditions[0]
        assert "outcome IS NULL" in conditions[0], conditions[0]
