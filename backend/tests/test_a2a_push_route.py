"""The push-notification methods as a partner actually reaches them.

``test_a2a_push`` covers the service functions directly, which is where the
rules live — and is exactly why it did not notice the route calling ``get_task``
with its arguments in the wrong order. Every one of the four methods addresses a
task, so every one of them goes through that call: this file drives the route.
"""

import uuid

import pytest
from fastapi import FastAPI

from app.api.v1 import a2a_server
from app.core.database import get_db
from tests.a2a_chat_gate import stub_chat_gate
from tests.a2a_server_stubs import partner_client

TASK_ID = uuid.uuid4()
CONTEXT_ID = uuid.uuid4()


class _Agent:
    id = uuid.uuid4()
    name = "Exposed"
    description = ""
    enabled = True
    output_format = "text"
    builtin_tool_ids: list = []
    is_company_managed = False
    user_id = uuid.uuid4()


class _Exposure:
    id = uuid.uuid4()
    agent_id = _Agent.id
    card_token = "tok"
    revoked_at = None
    run_as_user_id = uuid.uuid4()


class _Task:
    id = TASK_ID
    context_id = CONTEXT_ID
    exposure_id = _Exposure.id
    client_id = "partner-1"
    run_id = None
    rejected_reason = None
    cancel_requested_at = None
    responding_agent_id = None


@pytest.fixture
def client(monkeypatch):
    stub_chat_gate(monkeypatch)

    import app.a2a.exposure as exposure_mod

    async def _get_exposure(_db, _token):
        return _Exposure()

    async def _noop(*_a, **_k):
        return None

    async def _agent(*_a, **_k):
        return _Agent()

    async def _auth(*_a, **_k):
        return "partner-1", None, "oauth_a2a"

    monkeypatch.setattr(exposure_mod, "get_exposure_by_token", _get_exposure)
    monkeypatch.setattr(a2a_server, "_enforce_preauth_rate_limit", _noop)
    monkeypatch.setattr(a2a_server, "_enforce_a2a_rate_limit", _noop)
    monkeypatch.setattr(a2a_server, "_authenticate", _auth)
    monkeypatch.setattr(a2a_server, "_resolve_agent", _agent, raising=False)

    # The task the methods address, found only for its own client.
    async def _get_task(_db, task_id, client_id):
        if str(task_id) == str(TASK_ID) and client_id == "partner-1":
            return _Task()
        return None

    # Imported inside the handler, so the patch lands on the source module —
    # the same reason the refusals suite patches ``a2a.exposure``.
    import app.a2a.tasks as task_svc

    monkeypatch.setattr(task_svc, "get_task", _get_task)

    class _Db:
        async def execute(self, _s):
            class _R:
                rowcount = 1

                def scalar_one_or_none(self):
                    return _Agent()

                def scalars(self):
                    return self

                def all(self):
                    return []

            return _R()

        async def commit(self):
            pass

        async def flush(self):
            pass

        async def rollback(self):
            pass

        def add(self, _o):
            pass

        async def delete(self, _o):
            pass

    app = FastAPI()
    app.include_router(a2a_server.router)
    app.dependency_overrides[get_db] = _Db
    return partner_client(app)


def _rpc(c, method, params):
    return c.post(
        "/a2a/tok/jsonrpc",
        json={"jsonrpc": "2.0", "id": "r1", "method": method, "params": params},
        headers={"Authorization": "Bearer whatever"},
    )


class TestEveryMethodReachesItsTask:
    """Each of the four addresses a task, so each goes through the lookup that
    was wrong. A 500 here is the shape that bug had."""

    def test_registering_a_callback_succeeds(self, client, monkeypatch):
        async def _register(_db, **kw):
            return type(
                "C",
                (),
                {
                    "id": uuid.uuid4(),
                    "task_id": TASK_ID,
                    "url": kw["url"],
                    "auth_scheme": None,
                },
            )()

        import app.a2a.push as push_svc

        async def _allow(url, **_k):
            return url

        monkeypatch.setattr(push_svc, "assert_allowed_async", _allow)
        monkeypatch.setattr(push_svc, "register", _register)
        r = _rpc(
            client,
            "tasks/pushNotificationConfig/set",
            {
                "taskId": str(TASK_ID),
                "pushNotificationConfig": {"url": "https://partner.example/hook"},
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["result"]["pushNotificationConfig"]["url"] == (
            "https://partner.example/hook"
        )

    def test_listing_callbacks_succeeds(self, client, monkeypatch):
        import app.a2a.push as push_svc

        async def _list(_db, **_kw):
            return []

        monkeypatch.setattr(push_svc, "list_for_task", _list)
        r = _rpc(
            client, "tasks/pushNotificationConfig/list", {"taskId": str(TASK_ID)}
        )
        assert r.status_code == 200, r.text
        assert r.json()["result"] == []

    def test_a_refused_address_is_a_structured_error_not_a_500(
        self, client, monkeypatch
    ):
        import app.a2a.push as push_svc
        from app.a2a.client.destinations import DestinationRefusedError

        async def _refuse(_url, **_k):
            # Refused where the address is actually checked — before the task
            # is written, so a bad one cannot leave a task nothing will run.
            raise DestinationRefusedError("resolves inward", reason="private_address")

        monkeypatch.setattr(push_svc, "assert_allowed_async", _refuse)
        r = _rpc(
            client,
            "tasks/pushNotificationConfig/set",
            {
                "taskId": str(TASK_ID),
                "pushNotificationConfig": {"url": "http://169.254.169.254/"},
            },
        )
        assert r.status_code == 200, r.text
        assert "resolves inward" in r.json()["error"]["message"]


class TestTasksThisClientDoesNotOwn:
    def test_another_clients_task_is_not_found(self, client):
        r = _rpc(
            client,
            "tasks/pushNotificationConfig/list",
            {"taskId": str(uuid.uuid4())},
        )
        assert r.status_code == 200, r.text
        assert r.json()["error"]["code"] == -32001

    def test_a_malformed_task_id_is_not_found_rather_than_a_500(self, client):
        """A partner-supplied string reaches the id parse; an exception there is
        a 500 where the uniform not-found belongs."""
        r = _rpc(client, "tasks/pushNotificationConfig/list", {"taskId": "nope"})
        assert r.status_code == 200, r.text
        assert r.json()["error"]["code"] == -32001

    def test_a_missing_task_id_is_not_found(self, client):
        r = _rpc(client, "tasks/pushNotificationConfig/list", {})
        assert r.status_code == 200, r.text
        assert r.json()["error"]["code"] == -32001


class TestPartnerSuppliedShapesReachAProtocolError:
    def test_a_non_object_config_is_invalid_params_not_a_500(self, client):
        """``or {}`` rescues a falsy value and nothing else, so a string or a
        list reached ``.get`` and raised where a protocol error belongs.
        ``params`` and ``message`` are both isinstance-checked; this read was
        the one that was not."""
        for shape in ("a-string", ["a", "list"], 7):
            r = _rpc(
                client,
                "tasks/pushNotificationConfig/set",
                {"pushNotificationConfig": shape},
            )
            assert r.status_code == 200, r.text
            assert r.json()["error"]["code"] == -32602, shape
            assert "must be an object" in r.json()["error"]["message"]


class TestTheTaskIdIsSpelledAsTheRestOfTheSurfaceSpellsIt:
    """All four push methods extend TaskIdParams, so `id` is the TASK id here
    exactly as it is for `tasks/get` and `tasks/cancel`. Reading it as a config
    id made `list` answer not-found for a partner that spelled it that way, put
    `delete` out of reach — its config id arrives under its own key, so `id`
    was the task id it then lost — and had `get` look a task id up among
    configurations.
    """

    def test_list_accepts_the_id_spelling(self, client, monkeypatch):
        import app.a2a.push as push_svc

        async def _list(_db, **_kw):
            return []

        monkeypatch.setattr(push_svc, "list_for_task", _list)
        r = _rpc(client, "tasks/pushNotificationConfig/list", {"id": str(TASK_ID)})
        assert r.status_code == 200, r.text
        assert r.json()["result"] == []

    def test_delete_reaches_its_config(self, client, monkeypatch):
        import app.a2a.push as push_svc

        deleted: list = []

        async def _get(_db, *, config_id, client_id):
            return type("C", (), {"id": config_id})()

        async def _delete(_db, *, config):
            deleted.append(config.id)

        monkeypatch.setattr(push_svc, "get", _get)
        monkeypatch.setattr(push_svc, "delete", _delete)
        r = _rpc(
            client,
            "tasks/pushNotificationConfig/delete",
            {"id": str(TASK_ID), "pushNotificationConfigId": "cfg-1"},
        )
        assert r.status_code == 200, r.text
        assert deleted == ["cfg-1"], "the config id must not be shadowed"

    def test_get_without_a_config_id_answers_the_tasks_own(self, client, monkeypatch):
        import app.a2a.push as push_svc

        async def _list(_db, **_kw):
            return [
                type("C", (), {
                    "id": "cfg-1",
                    "task_id": TASK_ID,
                    "url": "https://partner.example/hook",
                    "auth_scheme": None,
                })()
            ]

        monkeypatch.setattr(push_svc, "list_for_task", _list)
        r = _rpc(client, "tasks/pushNotificationConfig/get", {"id": str(TASK_ID)})
        assert r.status_code == 200, r.text
        assert r.json()["result"]["pushNotificationConfig"]["url"] == (
            "https://partner.example/hook"
        )
