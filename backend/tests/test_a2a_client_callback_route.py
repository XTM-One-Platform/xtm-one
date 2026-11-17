"""The one route a partner may call as part of the outbound direction.

`009` ruled a partner-facing surface out; spec 011 supersedes that for User
Story 4, which is why every guard here is one the inbound surface already
applies and why the ordering is asserted rather than assumed: HTTPS, then the
pre-auth rate limit, then a constant-time comparison, and only then is anything
read.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI

from app.a2a.client import callbacks as callback_svc
from app.api.v1 import a2a_callbacks
from tests.a2a_server_stubs import partner_client


@pytest.fixture
def client(monkeypatch):
    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(a2a_callbacks, "_enforce_preauth_rate_limit", _noop)

    # A2A is offered exactly while chat is; every case here is about what
    # happens when it IS.
    async def _enabled(_db):
        return True

    monkeypatch.setattr("app.api.v1.platform.is_chat_enabled", _enabled)

    # Acting is detached, so nothing here may start a real one: it would open
    # its own session against no database, from a task nothing awaits.
    monkeypatch.setattr(callback_svc, "spawn_partner_finished", lambda *_a: None)
    app = FastAPI()
    app.include_router(a2a_callbacks.router)
    return partner_client(app)


def _post(c, delegation_id="d-1", token="a-minted-secret"):  # noqa: S107 - a test literal, not a credential
    return c.post(
        f"/a2a/callbacks/{delegation_id}",
        json={"taskId": "t-1", "contextId": "c-1", "state": "completed"},
        headers={"Authorization": f"Bearer {token}"},
    )


class TestEveryRefusalLooksTheSame:
    def test_no_credential_is_not_found(self, client):
        r = client.post("/a2a/callbacks/d-1", json={})
        assert r.status_code == 404

    def test_an_unknown_delegation_is_not_found(self, client, monkeypatch):
        async def _none(*_a, **_k):
            return None

        monkeypatch.setattr(callback_svc, "addressed_delegation", _none)
        monkeypatch.setattr(a2a_callbacks, "async_session", _FakeSession)
        assert _post(client).status_code == 404

    def test_a_wrong_secret_is_not_found(self, client, monkeypatch):
        """Identical to an unknown delegation, so a partner cannot learn which
        delegations exist by asking."""
        async def _row(*_a, **_k):
            return _delegation()

        monkeypatch.setattr(callback_svc, "addressed_delegation", _row)
        monkeypatch.setattr(callback_svc, "presented_secret_matches", lambda *_a: False)
        monkeypatch.setattr(a2a_callbacks, "async_session", _FakeSession)
        assert _post(client).status_code == 404


class TestNothingIsReadBeforeAuthentication:
    def test_a_failed_authentication_acts_on_nothing(self, client, monkeypatch):
        acted: list = []

        async def _row(*_a, **_k):
            return _delegation()

        def _spawned(*_a, **_k):
            acted.append(1)

        monkeypatch.setattr(callback_svc, "addressed_delegation", _row)
        monkeypatch.setattr(callback_svc, "presented_secret_matches", lambda *_a: False)
        monkeypatch.setattr(callback_svc, "spawn_partner_finished", _spawned)
        monkeypatch.setattr(a2a_callbacks, "async_session", _FakeSession)
        _post(client)
        assert acted == []

    def test_the_route_authenticates_before_it_acts(self):
        import inspect

        src = inspect.getsource(a2a_callbacks.receive_callback)
        assert src.index("presented_secret_matches") < src.index(
            "spawn_partner_finished"
        )
        assert src.index("_enforce_preauth_rate_limit") < src.index(
            "addressed_delegation"
        )


class TestAnAcceptedCallback:
    def test_it_hands_the_work_off_and_answers(self, client, monkeypatch):
        spawned: list = []
        row = _delegation()

        async def _row(*_a, **_k):
            return row

        monkeypatch.setattr(callback_svc, "addressed_delegation", _row)
        monkeypatch.setattr(callback_svc, "presented_secret_matches", lambda *_a: True)
        monkeypatch.setattr(
            callback_svc, "spawn_partner_finished", lambda i: spawned.append(i)
        )
        monkeypatch.setattr(a2a_callbacks, "async_session", _FakeSession)
        r = _post(client)
        assert r.status_code == 200
        assert spawned == [row.id]

    def test_the_answer_does_not_wait_for_the_collect(self):
        """Acting is an ordinary collect, bounded by the delegation ceiling and
        not by anything shorter. Awaited here it would hold the partner's own
        response — and the request's database connection — for the whole of it,
        which is the inverse of what a callback is for."""
        import inspect

        src = inspect.getsource(a2a_callbacks.receive_callback)
        assert "await callback_svc.on_partner_finished" not in src
        assert "spawn_partner_finished(row.id)" in src


class TestActingOnItDetached:
    async def test_it_opens_its_own_session(self):
        """The request's session is gone by the time it runs."""
        import inspect

        src = inspect.getsource(callback_svc.act_on_callback)
        assert "async_session()" in src

    async def test_it_re_reads_the_row_it_was_named(self, monkeypatch):
        """The window between answering the partner and acting is real, and
        ``addressed_delegation`` is what refuses one the floor concluded in
        it — carrying the row over would act on a conclusion twice."""
        src = __import__("inspect").getsource(callback_svc.act_on_callback)
        assert "addressed_delegation(" in src
        assert "if row is None" in src

    async def test_a_failure_acting_is_swallowed(self, monkeypatch):
        """The partner has its 200 and there is no caller left to raise at;
        the polling floor concludes the delegation either way."""
        async def _boom(*_a, **_k):
            raise RuntimeError("resume failed")

        monkeypatch.setattr(callback_svc, "on_partner_finished", _boom)
        # No database here, so the session open raises before the stub does —
        # either way the contract under test is that nothing escapes.
        await callback_svc.act_on_callback(uuid.uuid4(), mechanism="called_back")


class _FakeSession:
    """The route opens its own session rather than taking the request's, so the
    double is a context manager and not a dependency generator."""

    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_exc):
        return False


def _delegation():
    """A stand-in with the fields the route touches — an id among them, which
    the failure path logs."""
    from types import SimpleNamespace

    return SimpleNamespace(id=uuid.uuid4(), partner_task_id="t-1")


class TestTheBodyIsNotTheAnswer:
    def test_the_state_routes_and_does_not_decide(self):
        """A body on an unauthenticated route is a reason to look, never the
        answer: the result comes from reading the partner's task."""
        import inspect

        src = inspect.getsource(callback_svc.on_partner_finished)
        assert "delegate(" in src

    def test_a_uuid_delegation_id_is_what_the_url_carries(self):
        url = callback_svc.callback_url(uuid.UUID(int=1))
        assert url.endswith("/a2a/callbacks/00000000-0000-0000-0000-000000000001")

    def test_the_callback_base_wins_over_the_browser_address(self, monkeypatch):
        """The address a partner reaches is not always the one a browser does."""
        monkeypatch.setattr(
            callback_svc.app_settings, "base_url", "https://copilot.example"
        )
        monkeypatch.setattr(
            callback_svc.app_settings,
            "a2a_client_callback_base_url",
            "https://partners.example/",
        )
        assert callback_svc.callback_url(uuid.UUID(int=1)).startswith(
            "https://partners.example/a2a/callbacks/"
        )

    def test_without_one_the_configured_address_is_used(self, monkeypatch):
        monkeypatch.setattr(
            callback_svc.app_settings, "base_url", "https://copilot.example"
        )
        monkeypatch.setattr(
            callback_svc.app_settings, "a2a_client_callback_base_url", ""
        )
        assert callback_svc.callback_url(uuid.UUID(int=1)).startswith(
            "https://copilot.example/a2a/callbacks/"
        )


class TestTheTokenIsReadWhereOurOwnSenderPutsIt:
    """Found live, twice over: every callback this platform sent itself was
    refused because the receiver read only the ``Authorization`` header while
    the sender writes the token into the BODY.

    ``push.py`` sets a header only when the partner registered a scheme and
    credentials. We register a token, so against ourselves it is always the
    body — and the docstring said the receiver read both while the code read
    one.
    """

    def test_a_token_in_the_body_authenticates(self, client, monkeypatch):
        seen: list = []

        async def _row(*_a, **_k):
            return _delegation()

        def _matches(_row, presented):
            seen.append(presented)
            return True

        async def _acted(_db, _row, **_k):
            return None

        monkeypatch.setattr(callback_svc, "addressed_delegation", _row)
        monkeypatch.setattr(callback_svc, "presented_secret_matches", _matches)
        monkeypatch.setattr(callback_svc, "on_partner_finished", _acted)
        monkeypatch.setattr(a2a_callbacks, "async_session", _FakeSession)
        r = client.post(
            "/a2a/callbacks/d-1",
            json={"taskId": "t-1", "state": "completed", "token": "from-the-body"},
        )
        assert r.status_code == 200
        assert seen == ["from-the-body"]

    def test_a_header_still_wins_where_a_partner_uses_one(self, client, monkeypatch):
        """A partner that registered a scheme sends a header, and that path
        must keep working."""
        seen: list = []

        async def _row(*_a, **_k):
            return _delegation()

        def _matches(_row, presented):
            seen.append(presented)
            return True

        async def _acted(_db, _row, **_k):
            return None

        monkeypatch.setattr(callback_svc, "addressed_delegation", _row)
        monkeypatch.setattr(callback_svc, "presented_secret_matches", _matches)
        monkeypatch.setattr(callback_svc, "on_partner_finished", _acted)
        monkeypatch.setattr(a2a_callbacks, "async_session", _FakeSession)
        client.post(
            "/a2a/callbacks/d-1",
            json={"taskId": "t-1", "token": "from-the-body"},
            headers={"Authorization": "Bearer from-the-header"},
        )
        assert seen == ["from-the-header"]

    def test_neither_is_still_a_refusal(self, client):
        r = client.post("/a2a/callbacks/d-1", json={"taskId": "t-1"})
        assert r.status_code == 404

    def test_an_unparseable_body_is_a_refusal_not_a_crash(self, client):
        r = client.post(
            "/a2a/callbacks/d-1",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 404


class TestACraftedSecretIsTheUsualRefusal:
    """The presented secret reaches a constant-time comparison, which
    raised ``TypeError`` on a non-ASCII ``str`` - a 500 where every other
    refusal is the same 404. The REAL comparison runs here, against a row
    carrying a minted secret."""

    @pytest.fixture
    def delegation(self, monkeypatch):
        from types import SimpleNamespace

        row = SimpleNamespace(
            id=uuid.uuid4(),
            partner_task_id="t-1",
            callback_secret=callback_svc.store_secret(callback_svc.mint_secret()),
        )

        async def _row(*_a, **_k):
            return row

        monkeypatch.setattr(callback_svc, "addressed_delegation", _row)
        monkeypatch.setattr(a2a_callbacks, "async_session", _FakeSession)
        return row

    def test_a_non_ascii_bearer_is_not_found(self, client, delegation):
        r = client.post(
            "/a2a/callbacks/d-1",
            json={"taskId": "t-1", "state": "completed"},
            headers={"Authorization": b"Bearer caf\xc3\xa9"},
        )
        assert r.status_code == 404

    def test_a_lone_surrogate_in_the_body_token_is_not_found(self, client, delegation):
        # JSON's ``\ud800`` escape decodes to a lone surrogate, which
        # neither strict UTF-8 nor ``surrogateescape`` can encode.
        r = client.post(
            "/a2a/callbacks/d-1",
            content=b'{"taskId": "t-1", "token": "\\ud800"}',
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 404
