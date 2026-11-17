"""Which signature an email-sending integration's tools add: the resolution order.

``email_signature_settings.plan_signatures`` resolves, per sending integration:
on / off from the agent's binding, else the integration's setting, else on;
the signature integration from the binding, else the integration, else the
single one bound alongside, else the single one the acting person may use,
else none with a hint that says how to set one. An existing deployment - a
signature integration bound, nothing configured - signs exactly as before.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import email_signature_settings as settings
from app.services import email_signatures
from app.services.email_signature_settings import (
    compact_signatures,
    merge_signatures,
    override_label,
    parse_integration_signatures,
    plan_signatures,
    pruned_signatures,
)
from app.services.email_signatures import OFF_BY_BINDING, OFF_BY_INTEGRATION, SignaturePlan

SENDER = "fake_mail"
SOURCE = "fake_signatures"


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    """One sending provider and one signature provider, nothing else."""

    async def _reader(_credential, _address):
        return None

    monkeypatch.setattr(email_signatures, "_READERS", {SOURCE: _reader})
    monkeypatch.setattr(email_signatures, "_SENDERS", {SENDER})


def _row(provider: str, name: str, **columns) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), provider=provider, name=name, user_id=uuid.uuid4(), **columns)


def _mailbox(name: str = "Mailbox", **columns) -> SimpleNamespace:
    columns.setdefault("signature_enabled", True)
    columns.setdefault("signature_integration_id", None)
    return _row(SENDER, name, **columns)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Db:
    """Answers the name lookup of chosen signature integrations with every known one."""

    def __init__(self, *known: SimpleNamespace) -> None:
        self.known = known

    async def execute(self, _statement):
        return _Rows([(row.id, row.name, row.provider) for row in self.known])


@pytest.fixture
def visible(monkeypatch):
    """The signature integrations the acting person may use, by user id (any user when keyed ``*``)."""
    seen: dict[str, list] = {}

    async def _visible(_db, user_id):
        rows = seen.get(str(user_id or ""), seen.get("*", []))
        return [(str(r.id), r.name, r.provider) for r in rows]

    monkeypatch.setattr(settings, "_visible_signature_integrations", _visible)
    return seen


def _rows(*rows) -> dict[str, SimpleNamespace]:
    return {str(row.id): row for row in rows}


async def test_an_existing_deployment_signs_with_the_signature_integration_bound_alongside(visible):
    mailbox, letsign = _mailbox(), _row(SOURCE, "Signatures")
    plans = await plan_signatures(_Db(letsign), _rows(mailbox, letsign))
    assert plans == {
        str(mailbox.id): SignaturePlan(integration_id=str(letsign.id), integration_name="Signatures")
    }
    assert plans[str(mailbox.id)].signs and plans[str(mailbox.id)].signature_fn() is not None


async def test_the_integration_setting_off_signs_nothing():
    mailbox, letsign = _mailbox(signature_enabled=False), _row(SOURCE, "Signatures")
    plan = (await plan_signatures(_Db(letsign), _rows(mailbox, letsign)))[str(mailbox.id)]
    assert plan == SignaturePlan(enabled=False, off_by=OFF_BY_INTEGRATION)
    assert plan.signature_fn() is None
    assert "integration's settings" in plan.off_reason()


async def test_a_binding_override_beats_the_integration_setting_both_ways():
    letsign = _row(SOURCE, "Signatures")
    on, off = _mailbox("On"), _mailbox("Off", signature_enabled=False)
    rows = _rows(on, off, letsign)
    overrides = {
        str(on.id): {"signature_mode": "off"},
        str(off.id): {"signature_mode": "on"},
    }
    plans = await plan_signatures(_Db(letsign), rows, overrides=overrides)
    assert plans[str(on.id)] == SignaturePlan(enabled=False, off_by=OFF_BY_BINDING)
    assert plans[str(off.id)].signs and plans[str(off.id)].integration_id == str(letsign.id)
    # Another agent bound to the same integrations, without overrides, inherits.
    inherited = await plan_signatures(_Db(letsign), rows)
    assert inherited[str(on.id)].signs and not inherited[str(off.id)].enabled
    assert "agent's" in plans[str(on.id)].off_reason()


async def test_the_signature_integration_comes_from_the_binding_then_the_integration_then_the_one_bound(visible):
    bound, chosen, overriding = _row(SOURCE, "Bound"), _row(SOURCE, "Chosen"), _row(SOURCE, "Override")
    mailbox = _mailbox(signature_integration_id=chosen.id)
    db = _Db(bound, chosen, overriding)
    rows = _rows(mailbox, bound)
    assert (await plan_signatures(db, rows))[str(mailbox.id)].integration_id == str(chosen.id)
    overrides = {str(mailbox.id): {"signature_mode": "inherit", "signature_integration_id": str(overriding.id)}}
    plan = (await plan_signatures(db, rows, overrides=overrides))[str(mailbox.id)]
    assert plan == SignaturePlan(integration_id=str(overriding.id), integration_name="Override")
    mailbox.signature_integration_id = None
    assert (await plan_signatures(db, rows))[str(mailbox.id)].integration_id == str(bound.id)


async def test_two_bound_mailboxes_may_resolve_differently():
    letsign = _row(SOURCE, "Signatures")
    first, second = _mailbox("First"), _mailbox("Second")
    plans = await plan_signatures(
        _Db(letsign), _rows(first, second, letsign), overrides={str(second.id): "off"}
    )
    assert plans[str(first.id)].signs and not plans[str(second.id)].enabled


async def test_without_one_bound_the_single_one_the_acting_person_may_use_is_picked(visible):
    letsign = _row(SOURCE, "Company signatures")
    mailbox = _mailbox()
    acting = uuid.uuid4()
    visible[str(acting)] = [letsign]
    plan = (await plan_signatures(_Db(letsign), _rows(mailbox), acting_user_id=acting))[str(mailbox.id)]
    assert plan == SignaturePlan(integration_id=str(letsign.id), integration_name="Company signatures")
    # Nobody known to act: the integration's owner is the person.
    visible.clear()
    visible[str(mailbox.user_id)] = [letsign]
    assert (await plan_signatures(_Db(letsign), _rows(mailbox)))[str(mailbox.id)].signs


async def test_with_no_signature_integration_it_signs_nothing_and_says_how_to_set_one(visible, caplog):
    mailbox = _mailbox()
    with caplog.at_level("INFO", logger=settings.logger.name):
        plan = (await plan_signatures(_Db(), _rows(mailbox)))[str(mailbox.id)]
    assert plan.enabled and not plan.signs and plan.signature_fn() is None
    assert "no signature integration is available" in plan.missing
    assert "Email signature setting" in plan.missing
    assert any("no signature integration is available" in record.getMessage() for record in caplog.records)


async def test_several_candidates_and_no_choice_pick_none(visible):
    first, second = _row(SOURCE, "Alpha"), _row(SOURCE, "Beta")
    mailbox = _mailbox()
    bound = (await plan_signatures(_Db(first, second), _rows(mailbox, first, second)))[str(mailbox.id)]
    assert not bound.signs and "Alpha, Beta" in bound.missing
    visible["*"] = [first, second]
    seen = (await plan_signatures(_Db(first, second), _rows(mailbox)))[str(mailbox.id)]
    assert not seen.signs and "several signature integrations" in seen.missing


async def test_a_chosen_signature_integration_that_is_gone_is_said(visible):
    mailbox = _mailbox(signature_integration_id=uuid.uuid4())
    plan = (await plan_signatures(_Db(), _rows(mailbox, _row(SOURCE, "Bound"))))[str(mailbox.id)]
    assert not plan.signs and "no longer" in plan.missing


async def test_only_email_sending_integrations_get_a_plan():
    assert await plan_signatures(_Db(), _rows(_row("chat", "Chat"), _row(SOURCE, "Signatures"))) == {}


# -- overrides as written ---------------------------------------------------


def test_overrides_parse_from_objects_json_and_modes():
    iid, sid = uuid.uuid4(), uuid.uuid4()
    value, error, provided = parse_integration_signatures(
        {str(iid).upper(): {"signature_mode": "ON", "signature_integration_id": str(sid)}}
    )
    assert (value, error, provided) == (
        {str(iid): {"signature_mode": "on", "signature_integration_id": str(sid)}},
        None,
        True,
    )
    assert parse_integration_signatures(f'{{"{iid}": "off"}}')[0] == {str(iid): {"signature_mode": "off"}}
    assert parse_integration_signatures("") == (None, None, False)
    assert parse_integration_signatures("clear") == (None, None, True)
    _value, error, _ = parse_integration_signatures({str(iid): {"signature_mode": "sometimes"}})
    assert error and str(iid) in error
    assert parse_integration_signatures({"not-an-id": "off"})[1]


def test_a_merge_replaces_per_binding_and_an_inherit_entry_removes_one():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    current = {a: {"signature_mode": "off"}, b: {"signature_mode": "on"}}
    assert merge_signatures(current, {a: {"signature_mode": "inherit"}}) == {b: {"signature_mode": "on"}}
    assert merge_signatures(current, {a: {"signature_mode": "inherit"}, b: {"signature_mode": "inherit"}}) is None
    assert compact_signatures({a: {"signature_mode": "inherit"}}) is None


def test_an_unbound_integration_keeps_no_override():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    kept = pruned_signatures({a: {"signature_mode": "off"}, b: {"signature_mode": "on"}}, {"fake_mail": [a]})
    assert kept == {a: {"signature_mode": "off"}}
    assert pruned_signatures({b: {"signature_mode": "on"}}, {"fake_mail": [a]}) is None


@pytest.mark.parametrize(
    "module_name",
    [
        "app.services.agent_chat",
        "app.services.assignment_engine",
        "app.services.evaluation_engine",
        "app.services.scheduled_work_item_runner",
        "app.api.v1.public_chat",
    ],
)
def test_every_agent_run_hands_its_signature_overrides_to_the_tool_builder(module_name):
    """Tripwire: an agent's tools built without its overrides would sign as its integrations alone say."""
    import importlib
    import inspect

    source = inspect.getsource(importlib.import_module(module_name))
    starts = [i for i in range(len(source)) if source.startswith("await build_integration_tools(", i)]
    assert starts
    for start in starts:
        window = source[max(0, start - 800) : start + 1600]
        assert "SIGNATURE_CONTEXT_KEY" in window, f"{module_name} call at {start}"


def test_get_agent_shows_an_override_only_when_there_is_one():
    sid = str(uuid.uuid4())
    assert override_label({"signature_mode": "off"}) == "signature: off"
    assert override_label({"signature_mode": "inherit", "signature_integration_id": sid}) == f"signature: inherit via {sid}"
    assert override_label({"signature_mode": "inherit"}) == override_label(None) == ""
