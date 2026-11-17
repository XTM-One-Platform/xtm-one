"""The one rule every run read names a parent by; the queries are in
``test_run_parent_visibility_postgres.py``."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.api.v1.helpers import parent_is_shown, without_hidden_parent

_PARENT = uuid.uuid4()
_PAYLOAD = {"trigger_type": "delegation", "parent_run_id": str(_PARENT), "parent_agent_id": "a", "task": "t"}


def test_a_parent_is_named_to_an_admin_or_when_the_caller_may_open_it():
    run = SimpleNamespace(parent_run_id=_PARENT)
    assert parent_is_shown(run, {}, is_admin=True)
    assert parent_is_shown(run, {_PARENT: uuid.uuid4()}, is_admin=False)
    assert not parent_is_shown(run, {}, is_admin=False)


def test_a_run_whose_parent_row_is_gone_keeps_what_its_payload_says():
    # ``parent_run_id`` is ``ON DELETE SET NULL``: nothing is hidden from the
    # run's own readers once the parent is deleted.
    assert parent_is_shown(SimpleNamespace(parent_run_id=None), {}, is_admin=False)


def test_only_the_parent_keys_leave_the_payload():
    assert without_hidden_parent(_PAYLOAD, True) is _PAYLOAD
    assert without_hidden_parent(_PAYLOAD, False) == {"trigger_type": "delegation", "task": "t"}
    assert _PAYLOAD["parent_run_id"] == str(_PARENT)  # the stored payload is not mutated
    other = {"raw_body": "x"}
    assert without_hidden_parent(other, False) is other
    assert without_hidden_parent(None, False) is None
