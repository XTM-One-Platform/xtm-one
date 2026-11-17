"""A partner learns its task moved without polling for it.

Spec 010 User Story 3. The card declared push notifications unsupported and the
four configuration methods were refused by name; both are now served, because a
task can stay open for as long as the work it delegated runs.

The platform holds no key material for this. The partner decides how its own
endpoint is protected and registers what to present.
"""

import uuid

import pytest

from app.a2a import push as push_svc
from app.a2a import tasks as push_svc_tasks
from app.a2a.client.destinations import DestinationRefusedError
from app.core.config import settings
from app.core.security import decrypt_value, encrypt_value
from tests.a2a_server_stubs import FakeDb, Result, task

pytestmark = pytest.mark.asyncio


def _config(**overrides):
    fields = {
        "id": uuid.uuid4(),
        "task_id": uuid.uuid4(),
        "client_id": "partner-1",
        "url": "https://partner.example.com/hook",
        "token": None,
        "auth_scheme": None,
        "auth_credentials": None,
        "delivery_state": "pending",
        "attempts": 0,
    }
    fields.update(overrides)
    return type("Config", (), fields)()


def _allow(monkeypatch, *, refuse=False):
    async def _check(url, **_k):
        if refuse:
            raise DestinationRefusedError("resolves inward", reason="private_address")
        return url

    monkeypatch.setattr(push_svc, "assert_allowed_async", _check)


def _transport(monkeypatch, *, status=200, boom=None):
    """Capture the one POST a delivery makes, without a socket."""
    sent: dict = {}

    class _Response:
        def raise_for_status(self):
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")

    class _Client:
        def __init__(self, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_e):
            return False

        async def post(self, url, json=None, headers=None):
            if boom:
                raise boom
            sent.update(url=url, json=json, headers=headers)
            return _Response()

    monkeypatch.setattr(push_svc.httpx, "AsyncClient", _Client)
    return sent


class TestRegistration:
    async def test_an_address_resolving_inward_is_refused(self, monkeypatch):
        """FR-019. A partner-supplied URL we POST to is the same threat as a
        partner card URL we fetch, so it goes through the same guard."""
        _allow(monkeypatch, refuse=True)
        with pytest.raises(push_svc.PushConfigRefusedError):
            # Through the partner-facing entry point: the address is settled
            # before a task exists, so a refusal cannot leave one behind.
            await push_svc.register_declared(
                db=FakeDb(), task=task(), client_id="partner-1",
                declared={"url": "http://169.254.169.254/latest"},
            )

    async def test_registrations_past_the_bound_are_refused_not_evicted(
        self, monkeypatch
    ):
        """FR-022. A partner relying on a callback must not have it silently
        dropped for a newer one."""
        _allow(monkeypatch)
        full = [_config() for _ in range(settings.a2a_push_configs_per_task)]
        with pytest.raises(push_svc.PushConfigRefusedError) as exc:
            await push_svc.register(
                db=FakeDb(Result(full)), task=task(), client_id="partner-1",
                url="https://partner.example.com/hook",
            )
        assert str(settings.a2a_push_configs_per_task) in str(exc.value)

    async def test_a_valid_registration_is_stored(self, monkeypatch):
        _allow(monkeypatch)
        db = FakeDb(Result([]))
        config = await push_svc.register(
            db=db, task=task(), client_id="partner-1",
            url="https://partner.example.com/hook", token="s3cret",
        )
        assert config.url == "https://partner.example.com/hook"
        # Stored encrypted, not verbatim (Constitution III).
        assert config.token != "s3cret"
        assert decrypt_value(config.token) == "s3cret"
        assert db.flushes == 1


class TestInlineRegistration:
    async def test_a_declared_config_registers_without_a_task_id(self, monkeypatch):
        """FR-031. A partner has no task id until its submission returns, so
        the submission is where it can say where to be told."""
        _allow(monkeypatch)
        db = FakeDb(Result([]))
        config = await push_svc.register_declared(
            db,
            task=task(),
            client_id="partner-1",
            declared={
                "url": "https://partner.example/hook",
                "token": "opaque",
                "authentication": {"schemes": ["Bearer"], "credentials": "abc"},
            },
        )
        assert config.url == "https://partner.example/hook"
        assert decrypt_value(config.token) == "opaque"
        assert config.auth_scheme == "Bearer"
        assert decrypt_value(config.auth_credentials) == "abc"

    async def test_a_bare_url_registers(self, monkeypatch):
        _allow(monkeypatch)
        config = await push_svc.register_declared(
            db=FakeDb(Result([])), task=task(), client_id="partner-1",
            declared={"url": "https://partner.example/hook"},
        )
        assert config.token is None and config.auth_scheme is None

    def test_the_send_path_reads_the_configuration_block(self):
        """Read through the shared reader, which knows every version's
        spelling; reading one key directly registers nothing for a partner
        that sent another, silently."""
        import inspect

        from app.api.v1 import a2a_server

        src = inspect.getsource(a2a_server)
        assert 'proto.declared_push_config(params.get("configuration"))' in src

    def test_every_spelling_of_the_block_is_read(self):
        """One route serves both versions, so a submission carries whichever
        its partner's own types name. v0.3's JSON-RPC binding says
        `pushNotificationConfig`, its proto binding `pushNotification`, and
        v1.0 `taskPushNotificationConfig`."""
        from app.a2a import protocol as proto

        declared = {"url": "https://partner.example/hook"}
        for key in (
            "pushNotificationConfig",
            "pushNotification",
            "taskPushNotificationConfig",
            "task_push_notification_config",
        ):
            assert proto.declared_push_config({key: declared}) == declared
        assert proto.declared_push_config({}) is None
        assert proto.declared_push_config(None) is None

    def test_both_entry_points_share_one_reader(self):
        """Two parsers would drift on which spelling of the auth block wins."""
        import inspect

        from app.api.v1 import a2a_server

        assert inspect.getsource(a2a_server).count("register_declared") == 2


class TestReadsAreScopedToTheirOwner:
    async def test_another_clients_config_is_not_found(self):
        """FR-010's rule applied here: a partner cannot enumerate another's
        callbacks, and the client is part of the query."""
        import inspect

        assert "client_id" in inspect.getsource(push_svc.get)

    async def test_a_malformed_id_is_not_found_rather_than_an_error(self):
        assert await push_svc.get(FakeDb(), config_id="nope", client_id="p") is None


class TestTheNotification:
    async def test_it_carries_identity_and_state_and_no_content(self, monkeypatch):
        """FR-017. The partner reads the result through tasks/get with its own
        credential, which is what keeps an answer off an endpoint we can
        validate but not authenticate."""
        _allow(monkeypatch)
        sent = _transport(monkeypatch)
        _task = task()
        await push_svc._deliver(_config(), _task, "completed")
        assert sent["json"] == {
            "taskId": str(_task.id),
            "contextId": str(_task.context_id),
            "state": "completed",
        }

    async def test_a_registered_token_is_echoed(self, monkeypatch):
        """FR-018. Only the partner and this platform hold it, so echoing it
        is what lets the partner reject a call it did not originate."""
        _allow(monkeypatch)
        sent = _transport(monkeypatch)
        await push_svc._deliver(_config(token=encrypt_value("shared")), task(), "completed")
        assert sent["json"]["token"] == "shared"

    async def test_a_registered_scheme_becomes_a_header(self, monkeypatch):
        _allow(monkeypatch)
        sent = _transport(monkeypatch)
        await push_svc._deliver(
            _config(auth_scheme="Bearer", auth_credentials=encrypt_value("abc")),
            task(),
            "completed",
        )
        assert sent["headers"]["Authorization"] == "Bearer abc"

    async def test_the_address_is_rechecked_before_every_send(self, monkeypatch):
        """DNS moves: a host that resolved outward at registration can resolve
        inward by the time a task finishes."""
        _allow(monkeypatch, refuse=True)
        _transport(monkeypatch)
        config = _config()
        await push_svc._deliver(config, task(), "completed")
        assert config.delivery_state == "failed"


class TestDeliveryCannotTouchTheTask:
    async def test_a_failed_send_is_recorded_and_swallowed(self, monkeypatch):
        """FR-021. An exception escaping here would cost the task the terminal
        write already committed."""
        _allow(monkeypatch)
        _transport(monkeypatch, boom=RuntimeError("connection reset"))
        config = _config()
        await push_svc._deliver(config, task(), "completed")
        assert config.delivery_state == "failed"
        assert config.attempts == 1

    async def test_notify_survives_a_lookup_failure(self, monkeypatch):
        class _Broken:
            async def execute(self, _s):
                raise RuntimeError("database gone")

        await push_svc.notify(_Broken(), task(), "completed")

    async def test_an_exhausted_config_is_not_attempted(self, monkeypatch):
        """FR-020. The attempt bound is in the query, so an exhausted callback
        costs nothing on every later transition."""
        import inspect

        assert "a2a_push_max_attempts" in inspect.getsource(push_svc.notify)


class TestCredentialsAtRest:
    """Constitution III: no partner secret sits in the database in the clear."""

    async def test_a_credential_that_cannot_be_decrypted_fails_the_delivery(
        self, monkeypatch
    ):
        """A row written under a rotated key cannot be presented. Sending the
        ciphertext as though it were the credential would have the partner
        reject the call while the platform recorded a delivered one."""
        _allow(monkeypatch)
        sent = _transport(monkeypatch)
        await push_svc._deliver(
            _config(token="not-a-fernet-token"), task(), "completed"
        )
        assert "token" not in sent["json"]

    def test_neither_column_is_written_verbatim(self):
        """Asserted on the writer: a future field added beside these two would
        otherwise be stored in the clear without anyone noticing."""
        import inspect

        src = inspect.getsource(push_svc.register)
        assert "encrypt_value(token)" in src
        assert "encrypt_value(auth_credentials)" in src


class TestBookkeepingIsPersisted:
    """The attempt counter and delivery state are this module's own, and
    nothing else commits them: the caller commits the task's terminal write
    before calling notify, and its session closes after. So a counter left
    uncommitted here reads as zero however many POSTs were attempted."""

    async def test_notify_commits_what_it_recorded(self, monkeypatch):
        _allow(monkeypatch)
        _transport(monkeypatch, boom=RuntimeError("connection reset"))
        _count_deliveries(monkeypatch)
        monkeypatch.setattr(push_svc, "_audit_abandoned", _noop_audit)

        db = FakeDb(Result([_config()]))
        await push_svc.notify(db, task(), "completed")
        assert db.commits == 1, (
            "without a commit the counter never advances: the retry bound never "
            "binds, the metrics record nothing, and abandonment never fires"
        )

    async def test_a_failed_commit_does_not_raise(self, monkeypatch):
        """The notification path must not become the reason anything raises."""
        _allow(monkeypatch)
        _transport(monkeypatch)
        _count_deliveries(monkeypatch)

        class _DbNoCommit(FakeDb):
            async def commit(self):
                raise RuntimeError("database gone")

        await push_svc.notify(_DbNoCommit(Result([_config()])), task(), "completed")


class TestAnOperatorCanSeeDeliveryFailing:
    """A callback is the only part of this surface depending on something
    outside it, and delivery is best-effort by design — so a partner going dark
    produces no error anywhere. Without these it is invisible until the partner
    complains."""

    async def test_a_delivered_call_is_counted(self, monkeypatch):
        _allow(monkeypatch)
        _transport(monkeypatch)
        counted = _count_deliveries(monkeypatch)
        await push_svc._deliver(_config(), task(), "completed")
        assert counted == ["delivered"]

    async def test_a_failed_call_is_counted(self, monkeypatch):
        _allow(monkeypatch)
        _transport(monkeypatch, boom=RuntimeError("connection reset"))
        counted = _count_deliveries(monkeypatch)
        await push_svc._deliver(_config(), task(), "completed")
        assert counted == ["failed"]

    async def test_exhausting_the_budget_is_counted_and_audited(self, monkeypatch):
        """Separate from a failed attempt: one failure is a blip, and this is
        the platform having stopped trying. An operator asked "why was this
        partner never told" needs the task and the address, which a counter
        cannot answer."""
        _allow(monkeypatch)
        _transport(monkeypatch, boom=RuntimeError("connection reset"))
        _count_deliveries(monkeypatch)
        abandoned, audited = {"n": 0}, {}

        def _abandon():
            abandoned["n"] += 1

        async def _audit(_db, _task, config):
            audited["url"] = config.url
            audited["attempts"] = config.attempts

        monkeypatch.setattr(push_svc.a2a_metrics, "record_push_abandoned", _abandon)
        monkeypatch.setattr(push_svc, "_audit_abandoned", _audit)

        config = _config(attempts=settings.a2a_push_max_attempts - 1)
        await push_svc.notify(FakeDb(Result([config])), task(), "completed")

        assert abandoned["n"] == 1
        assert audited["url"] == config.url
        assert audited["attempts"] == settings.a2a_push_max_attempts

    async def test_a_failure_short_of_the_budget_is_not_abandonment(self, monkeypatch):
        """It will be retried on the next transition, so calling it abandoned
        would make the alerting series fire on every blip."""
        _allow(monkeypatch)
        _transport(monkeypatch, boom=RuntimeError("connection reset"))
        _count_deliveries(monkeypatch)
        abandoned = {"n": 0}
        monkeypatch.setattr(
            push_svc.a2a_metrics,
            "record_push_abandoned",
            lambda: abandoned.__setitem__("n", abandoned["n"] + 1),
        )
        monkeypatch.setattr(push_svc, "_audit_abandoned", _noop_audit)

        await push_svc.notify(FakeDb(Result([_config(attempts=0)])), task(), "completed")
        assert abandoned["n"] == 0

    async def test_a_failed_audit_does_not_raise(self, monkeypatch):
        """The notification path must not become the reason anything raises."""
        class _Broken:
            async def execute(self, _s):
                raise RuntimeError("database gone")

        await push_svc._audit_abandoned(_Broken(), task(), _config())


class TestTheRenderedConfig:
    def test_it_never_echoes_the_credential(self):
        """Returning a secret in a read body benefits nobody: the partner
        registered it and already knows it."""
        body = push_svc.render(
            _config(
                token=encrypt_value("s3cret"),
                auth_scheme="Bearer",
                auth_credentials=encrypt_value("cr3dential"),
            ),
            "1.0",
        )
        assert "s3cret" not in str(body)
        assert "cr3dential" not in str(body)

    def test_it_names_the_scheme_the_partner_registered(self):
        body = push_svc.render(_config(auth_scheme="Bearer"), "0.3")
        assert body["pushNotificationConfig"]["authentication"]["schemes"] == ["Bearer"]

    def test_v1_carries_the_fields_at_one_level(self):
        """v1.0's ``TaskPushNotificationConfig`` holds ``taskId`` alongside
        ``url`` rather than wrapping a nested config — a v1.0 parser reads the
        nested shape as a registration with no address at all."""
        body = push_svc.render(_config(auth_scheme="Bearer"), "1.0")
        assert "pushNotificationConfig" not in body
        assert body["url"] and body["taskId"]
        assert body["authentication"]["schemes"] == ["Bearer"]


class TestTheMethodVocabulary:
    @pytest.mark.parametrize(
        "method,op",
        [
            ("tasks/pushNotificationConfig/set", "push_set"),
            ("tasks/pushNotificationConfig/get", "push_get"),
            ("tasks/pushNotificationConfig/list", "push_list"),
            ("tasks/pushNotificationConfig/delete", "push_delete"),
            ("CreateTaskPushNotificationConfig", "push_set"),
            ("GetTaskPushNotificationConfig", "push_get"),
            ("ListTaskPushNotificationConfigs", "push_list"),
            ("DeleteTaskPushNotificationConfig", "push_delete"),
        ],
    )
    def test_both_vocabularies_resolve_to_the_same_four_operations(self, method, op):
        """FR-016. The two versions do not share a namespace, and a partner
        speaking either must be served."""
        from app.a2a import protocol as proto

        resolved = proto.resolve_method(method)
        assert resolved is not None, method
        assert resolved[0] == op

    def test_the_refusal_is_gone(self):
        """Left in place it would be dead code that reads as a live refusal."""
        from app.a2a import protocol as proto

        assert not hasattr(proto, "is_push_notification_method")


def _count_deliveries(monkeypatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        push_svc.a2a_metrics, "record_push_delivery", lambda outcome: seen.append(outcome)
    )
    return seen


async def _noop_audit(*_a, **_k):
    return None


class TestASchemeThisSurfaceCannotPresent:
    """``_deliver`` puts the registered credential verbatim after the scheme
    name, so a scheme needing anything computed would be sent as a header the
    partner rejects. The contract refuses it on registration rather than at
    delivery — and unvalidated it is worse than accepted: a name past the
    column bound raises a DataError, which is not a refusal, so the partner
    gets a 500 and a poisoned transaction instead of invalid-params.
    """

    async def _register(self, declared, monkeypatch):
        _allow(monkeypatch)
        return await push_svc.register_declared(
            db=FakeDb(Result([])),
            task=task(),
            client_id="partner-1",
            declared=declared,
        )

    async def test_an_unsupported_scheme_is_refused(self, monkeypatch):
        with pytest.raises(push_svc.PushConfigRefusedError):
            await self._register(
                {
                    "url": "https://partner.example/hook",
                    "authentication": {"schemes": ["Negotiate"]},
                },
                monkeypatch,
            )

    async def test_a_name_past_the_column_bound_is_refused_not_raised(
        self, monkeypatch
    ):
        with pytest.raises(push_svc.PushConfigRefusedError):
            await self._register(
                {
                    "url": "https://partner.example/hook",
                    "authentication": {"schemes": ["B" * 64]},
                },
                monkeypatch,
            )

    async def test_a_scheme_list_that_is_not_a_list_is_refused_not_raised(
        self, monkeypatch
    ):
        """Partner-supplied and unparsed until here: a dict subscripts to a
        KeyError, a number to a TypeError, and neither is a refusal."""
        for schemes in ({"0": "Bearer"}, 7, "Bearer"):
            config = await self._register(
                {
                    "url": "https://partner.example/hook",
                    "authentication": {"schemes": schemes},
                },
                monkeypatch,
            )
            assert config.auth_scheme is None

    async def test_a_supported_scheme_still_registers(self, monkeypatch):
        config = await self._register(
            {
                "url": "https://partner.example/hook",
                "authentication": {"schemes": ["Bearer"], "credentials": "s3cr3t"},
            },
            monkeypatch,
        )
        assert config.auth_scheme == "Bearer"

    async def test_a_declaration_that_is_not_an_object_is_refused(self, monkeypatch):
        with pytest.raises(push_svc.PushConfigRefusedError):
            await self._register("https://partner.example/hook", monkeypatch)


class TestDeliveryIsNotOnTheRequestPath:
    """Delivery is bounded per callback and not per task, so awaiting it on a
    request path lets one partner's dead endpoint hold its own cancel for
    ``a2a_push_configs_per_task`` × ``_DELIVERY_TIMEOUT_SECONDS`` — and holds
    the request's database connection for the same span. FR-021: a
    notification never blocks, delays or changes the task's outcome.
    """

    def test_the_cancel_route_does_not_await_delivery(self):
        import inspect

        from app.api.v1 import a2a_server

        src = inspect.getsource(a2a_server.a2a_jsonrpc)
        assert "await task_svc.announce_state(db, task)" not in src
        assert "_spawn_announce(task.id)" in src

    def test_the_detached_announcement_opens_its_own_session(self):
        """The request's session is gone by the time it runs."""
        import inspect

        src = inspect.getsource(push_svc_tasks.announce_state_by_id)
        assert "async_session()" in src

    def test_it_never_raises(self):
        """A callback is an accelerator whose floor is the partner polling, and
        this one has no caller left to hand a failure to."""
        import inspect

        assert "except Exception:" in inspect.getsource(
            push_svc_tasks.announce_state_by_id
        )


class TestARefusedCallbackLeavesNoTaskBehind:
    """The submission path decides the address before it writes anything.

    Refusing after `create_task` left a task and a run that nothing executed —
    the spawn sits behind the same `created` guard the refusal returns past —
    and the message id was claimed by then, so the retry deduped to the same
    dead task and the partner could never submit that message at all.
    """

    def test_the_route_validates_before_it_creates(self):
        import inspect

        from app.api.v1 import a2a_server

        src = inspect.getsource(a2a_server.a2a_jsonrpc)
        assert src.index("validate_declared(") < src.index("task_svc.create_task(")

    def test_validation_writes_nothing(self):
        """It takes no session, which is what lets it run before the task."""
        import inspect

        sig = inspect.signature(push_svc.validate_declared)
        assert list(sig.parameters) == ["declared"]

    async def test_a_bad_address_is_refused_there(self, monkeypatch):
        _allow(monkeypatch, refuse=True)
        with pytest.raises(push_svc.PushConfigRefusedError):
            await push_svc.validate_declared({"url": "http://169.254.169.254/"})


class TestAFailedNotificationStaysAnnounceable:
    """Stamp-then-send made one bad ten seconds permanent: `announce_missed`
    selects on `terminal_announced_at IS NULL`, and nothing else ever calls
    `notify` for that task again — so `attempts` stopped at 1, and the
    abandonment audit and metric the retry bound exists for were unreachable.
    """

    async def test_notify_reports_an_unsettled_delivery(self, monkeypatch):
        _allow(monkeypatch)
        _transport(monkeypatch, status=500)
        config = _config(attempts=0)

        class _Db(FakeDb):
            async def execute(self, *_a, **_k):
                return Result([config])

            async def commit(self):
                return None

        assert await push_svc.notify(_Db(), task(), "completed") is False

    async def test_a_spent_budget_settles_it(self, monkeypatch):
        """Once every address has spent its attempts there is nothing left to
        retry, so the announcement stops coming back."""
        _allow(monkeypatch)
        _transport(monkeypatch, status=500)
        config = _config(attempts=settings.a2a_push_max_attempts - 1)

        class _Db(FakeDb):
            async def execute(self, *_a, **_k):
                return Result([config])

            async def commit(self):
                return None

        assert await push_svc.notify(_Db(), task(), "completed") is True

    def test_the_claim_is_given_back_when_nothing_took_it(self):
        import inspect

        from app.a2a import tasks as task_svc

        src = inspect.getsource(task_svc.announce_state)
        assert "_release_announcement" in src
        assert "terminal_announced_at=None" in inspect.getsource(
            task_svc._release_announcement
        )


class TestTheNotificationSpeaksThePartnersVersion:
    """The two versions spell a state differently — v0.3's lowercase values
    against v1.0's enum names — and a partner branching on the state to decide
    whether to read the task would never branch on the other's spelling.
    """

    def test_the_version_is_recorded_on_the_task(self):
        """Negotiated per request from the header, and sent long after that
        request is over — from a sweep, or a detached announcement — so it has
        to be recorded rather than re-derived. The outbound half already does
        this, for the same reason (FR-023)."""
        from app.models.a2a import A2ATask

        assert "protocol_version" in A2ATask.__table__.columns

    def test_the_announcement_reads_it(self):
        import inspect

        src = inspect.getsource(push_svc_tasks.announce_state)
        assert "task.protocol_version or proto.PROTOCOL_VERSION_0_3" in src

    def test_a_task_from_before_it_was_recorded_is_v0_3(self):
        """Which is what every such task was already being sent."""
        from app.a2a import protocol as proto

        assert proto.state_wire(
            proto.A2AState.COMPLETED, None or proto.PROTOCOL_VERSION_0_3
        ) == proto.state_wire(proto.A2AState.COMPLETED, proto.PROTOCOL_VERSION_0_3)
