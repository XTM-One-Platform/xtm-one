"""Whitelist administration and self-service approvals.

Two endpoints that together decide whether a wrong default is correctable:

* ``/hitl-whitelist`` — the escape hatch. Without it the shipped default is the
  whole list and a deployment that disagrees with our judgement (``code_interpreter``
  being the obvious candidate) would need a code change.
* ``/tool-approvals`` — revocation. "Yes, always" is one click; without a way
  back it would be a one-way door, which is worse than not offering the button.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.v1.hitl_whitelist as whitelist_api
import app.api.v1.tool_approvals as approvals_api
from app.services.hitl_whitelist_default import SHIPPED_DEFAULT_WHITELIST


class _FakeDB:
    def __init__(self):
        self.committed = False
        self.deleted = []
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True

    async def scalar(self, _stmt):
        # No platform_settings row: the endpoint reads the kill switch
        # defensively and treats a missing one as approvals-on.
        return None


_ADMIN = SimpleNamespace(id=uuid.uuid4(), email="a@example.com", role="admin")
_REQUEST = SimpleNamespace(client=None, headers={})


@pytest.fixture(autouse=True)
def _stub_overrides(monkeypatch):
    """No overrides unless a test says otherwise.

    Both endpoints read the current overlay now — adding a shipped-default tool
    that carries a removal restores it rather than pinning an addition — so a
    test that only cares about the add path still needs the read to answer.
    Tests with their own expectations re-stub this and win.
    """

    async def _overrides(_db):
        return {}

    monkeypatch.setattr(whitelist_api, "load_overrides", _overrides)


@pytest.fixture(autouse=True)
def _stub_platform_layer(monkeypatch):
    """A deployment with no registered platform, unless a test says otherwise.

    Needed since the derived layer stopped swallowing its own read failures:
    `_FakeDB` has no `execute`, which used to be absorbed into "no platform
    reads" and now surfaces as `PlatformReadsUnavailableError` → 503. That is
    the point of the change, but it means a test about the curated half has to
    say which world it is in. Tests that want reads, or want the layer
    unreadable, re-stub these and win.
    """

    async def _no_reads(_db):
        return frozenset()

    async def _default(_db):
        return SHIPPED_DEFAULT_WHITELIST

    monkeypatch.setattr(whitelist_api, "platform_read_default", _no_reads)
    monkeypatch.setattr(whitelist_api, "default_membership", _default)


@pytest.fixture(autouse=True)
def _stub_audit(monkeypatch):
    """Audit is exercised by its own suite; record calls instead of writing."""
    calls = []

    async def _log(_db, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(whitelist_api, "log_audit", _log)
    monkeypatch.setattr(approvals_api, "log_audit", _log)
    return calls


# --- listing ------------------------------------------------------------


async def test_listing_attributes_each_entry_to_its_layer(monkeypatch):
    async def _overrides(_db):
        return {"delete_entity": "add", "get_agent": "remove"}

    monkeypatch.setattr(whitelist_api, "load_overrides", _overrides)

    payload = await whitelist_api.list_whitelist(user=_ADMIN, db=_FakeDB())

    sources = {e["tool_key"]: e["source"] for e in payload["entries"]}
    assert sources["delete_entity"] == "admin_added"
    assert sources.get("list_agents") == "shipped_default"
    assert "get_agent" not in sources, "a removed default is still listed as active"
    assert payload["removed_from_default"] == ["get_agent"]


async def test_listing_surfaces_removals_separately(monkeypatch):
    """Otherwise "we ship this whitelisted, why does it prompt?" is unanswerable."""

    async def _overrides(_db):
        return {"get_agent": "remove"}

    monkeypatch.setattr(whitelist_api, "load_overrides", _overrides)

    payload = await whitelist_api.list_whitelist(user=_ADMIN, db=_FakeDB())

    assert payload["removed_from_default"] == ["get_agent"]
    assert payload["shipped_default_version"] >= 1


# --- adding -------------------------------------------------------------


async def test_adding_a_destructive_tool_is_permitted(monkeypatch, _stub_audit):
    """The API must not refuse — the safeguards are the warning and the audit."""
    recorded = {}

    async def _set(_db, *, tool_key, action, user_id, reason=None):
        recorded.update(tool_key=tool_key, action=action)
        return SimpleNamespace()

    monkeypatch.setattr(whitelist_api, "set_override", _set)
    db = _FakeDB()

    result = await whitelist_api.add_to_whitelist(
        body=whitelist_api.WhitelistAddRequest(tool_key="delete_agent", reason="trusted"),
        request=_REQUEST,
        user=_ADMIN,
        db=db,
    )

    assert result["tool_key"] == "delete_agent"
    assert recorded == {"tool_key": "delete_agent", "action": "add"}
    assert db.committed
    assert _stub_audit[0]["entity_type"] == "hitl_whitelist"


async def test_adding_an_empty_tool_key_is_rejected():
    with pytest.raises(HTTPException) as exc:
        await whitelist_api.add_to_whitelist(
            body=whitelist_api.WhitelistAddRequest(tool_key="   "),
            request=_REQUEST,
            user=_ADMIN,
            db=_FakeDB(),
        )
    assert exc.value.status_code == 400


# --- removing -----------------------------------------------------------


async def test_removing_a_shipped_default_records_a_durable_override(
    monkeypatch, _stub_audit
):
    """The rule that makes a re-gating decision survive an upgrade."""
    shipped = next(iter(SHIPPED_DEFAULT_WHITELIST))
    recorded = {}

    async def _overrides(_db):
        return {}

    async def _set(_db, *, tool_key, action, user_id, reason=None):
        recorded.update(tool_key=tool_key, action=action)
        return SimpleNamespace()

    monkeypatch.setattr(whitelist_api, "load_overrides", _overrides)
    monkeypatch.setattr(whitelist_api, "set_override", _set)

    await whitelist_api.remove_from_whitelist(
        tool_key=shipped, request=_REQUEST, user=_ADMIN, db=_FakeDB()
    )

    assert recorded["action"] == "remove"
    assert _stub_audit[0]["details"]["outcome"] == "removed_from_shipped_default"


async def test_removing_an_admin_addition_clears_it(monkeypatch, _stub_audit):
    """No point recording "remove" for something only we had added."""
    cleared = []

    async def _overrides(_db):
        return {"delete_agent": "add"}

    async def _clear(_db, tool_key):
        cleared.append(tool_key)
        return True

    monkeypatch.setattr(whitelist_api, "load_overrides", _overrides)
    monkeypatch.setattr(whitelist_api, "clear_override", _clear)

    await whitelist_api.remove_from_whitelist(
        tool_key="delete_agent", request=_REQUEST, user=_ADMIN, db=_FakeDB()
    )

    assert cleared == ["delete_agent"]
    assert _stub_audit[0]["details"]["outcome"] == "cleared_admin_addition"


async def test_removing_an_already_gated_tool_is_not_an_error(monkeypatch, _stub_audit):
    """The caller's intent — "this must require approval" — is already satisfied."""

    async def _overrides(_db):
        return {}

    monkeypatch.setattr(whitelist_api, "load_overrides", _overrides)

    await whitelist_api.remove_from_whitelist(
        tool_key="never_whitelisted_tool", request=_REQUEST, user=_ADMIN, db=_FakeDB()
    )

    assert _stub_audit[0]["details"]["outcome"] == "already_gated"


# --- self-service approvals --------------------------------------------


async def test_listing_returns_only_the_callers_preferences(monkeypatch):
    user = SimpleNamespace(id=uuid.uuid4(), approve_all_tools=False)
    seen = {}

    async def _list(_db, user_id):
        seen["user_id"] = user_id
        return [
            SimpleNamespace(
                id=uuid.uuid4(),
                tool_key="delete_entity",
                tool_source="builtin:test",
                granted_at=None,
                granted_by_admin_id=None,
            )
        ]

    async def _invalidated(_db, _user_id, limit=10):
        return []

    monkeypatch.setattr(approvals_api, "list_preferences", _list)
    monkeypatch.setattr(approvals_api, "list_recently_invalidated", _invalidated)

    payload = await approvals_api.list_my_tool_approvals(user=user, db=_FakeDB())

    assert seen["user_id"] == user.id
    assert payload["preferences"][0]["tool_key"] == "delete_entity"


async def test_revoking_someone_elses_preference_is_404(monkeypatch):
    """404, not 403 — a 403 would confirm another user's preference exists."""

    async def _revoke(_db, *, preference_id, user_id, reason=None):
        return None

    monkeypatch.setattr(approvals_api, "revoke_exception", _revoke)

    with pytest.raises(HTTPException) as exc:
        await approvals_api.revoke_my_tool_approval(
            preference_id=str(uuid.uuid4()),
            request=_REQUEST,
            user=SimpleNamespace(id=uuid.uuid4()),
            db=_FakeDB(),
        )

    assert exc.value.status_code == 404


async def test_malformed_preference_id_is_404_not_500():
    with pytest.raises(HTTPException) as exc:
        await approvals_api.revoke_my_tool_approval(
            preference_id="not-a-uuid",
            request=_REQUEST,
            user=SimpleNamespace(id=uuid.uuid4()),
            db=_FakeDB(),
        )

    assert exc.value.status_code == 404


async def test_revocation_is_audited_with_the_impersonating_admin(
    monkeypatch, _stub_audit
):
    admin_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), impersonated_by=admin_id)

    async def _revoke(_db, *, preference_id, user_id, reason=None):
        return SimpleNamespace(id=preference_id, tool_key="delete_entity")

    monkeypatch.setattr(approvals_api, "revoke_exception", _revoke)
    db = _FakeDB()

    await approvals_api.revoke_my_tool_approval(
        preference_id=str(uuid.uuid4()), request=_REQUEST, user=user, db=db
    )

    assert _stub_audit[0]["details"]["impersonated_by"] == admin_id
    assert db.committed


# --- unrecognised entries (typo guard) ----------------------------------


def test_suggest_similar_catches_a_reordered_name():
    """Reuses the executor's own token-key recovery, so it catches the same
    class of mistake the dispatcher already forgives at runtime."""
    from app.services.hitl_whitelist import suggest_similar

    assert suggest_similar("agent_delete", {"delete_agent"}) == "delete_agent"


def test_suggest_similar_catches_a_transposition():
    from app.services.hitl_whitelist import suggest_similar

    assert suggest_similar("delete_agnet", {"delete_agent"}) == "delete_agent"


def test_suggest_similar_returns_none_when_nothing_is_close():
    from app.services.hitl_whitelist import suggest_similar

    assert suggest_similar("totally_made_up", {"delete_agent"}) is None


def test_suggest_similar_never_suggests_the_input_itself():
    from app.services.hitl_whitelist import suggest_similar

    assert suggest_similar("delete_agent", {"delete_agent"}) is None


async def test_adding_an_unrecognised_tool_is_accepted_but_flagged(
    monkeypatch, _stub_audit
):
    """Refusing would break whitelisting ahead of connecting an integration.

    So it is accepted — but silently accepting a typo is what made the entry
    invisible before: it never matches, the tool keeps prompting, and nothing
    explains why.
    """

    async def _known(_db):
        return {"delete_agent"}

    async def _set(_db, **kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(whitelist_api, "known_tool_names", _known)
    monkeypatch.setattr(whitelist_api, "set_override", _set)

    result = await whitelist_api.add_to_whitelist(
        body=whitelist_api.WhitelistAddRequest(tool_key="delete_agnet"),
        request=_REQUEST,
        user=_ADMIN,
        db=_FakeDB(),
    )

    assert result["recognised"] is False
    assert result["suggestion"] == "delete_agent"
    assert _stub_audit[0]["details"]["recognised"] is False


async def test_adding_a_recognised_tool_is_not_flagged(monkeypatch, _stub_audit):
    async def _known(_db):
        return {"delete_agent"}

    async def _set(_db, **kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(whitelist_api, "known_tool_names", _known)
    monkeypatch.setattr(whitelist_api, "set_override", _set)

    result = await whitelist_api.add_to_whitelist(
        body=whitelist_api.WhitelistAddRequest(tool_key="delete_agent"),
        request=_REQUEST,
        user=_ADMIN,
        db=_FakeDB(),
    )

    assert result["recognised"] is True
    assert result["suggestion"] is None


async def test_listing_flags_unrecognised_admin_additions(monkeypatch):
    async def _overrides(_db):
        return {"delete_agnet": "add", "delete_agent": "add"}

    async def _known(_db):
        return {"delete_agent"}

    monkeypatch.setattr(whitelist_api, "load_overrides", _overrides)
    monkeypatch.setattr(whitelist_api, "known_tool_names", _known)

    payload = await whitelist_api.list_whitelist(user=_ADMIN, db=_FakeDB())
    added = {e["tool_key"]: e for e in payload["entries"] if e["source"] == "admin_added"}

    assert added["delete_agnet"]["recognised"] is False
    assert added["delete_agnet"]["suggestion"] == "delete_agent"
    assert added["delete_agent"]["recognised"] is True


async def test_shipped_entries_are_always_recognised(monkeypatch):
    """They come from the product, so questioning them would be noise."""

    async def _overrides(_db):
        return {}

    async def _known(_db):
        return set()

    monkeypatch.setattr(whitelist_api, "load_overrides", _overrides)
    monkeypatch.setattr(whitelist_api, "known_tool_names", _known)

    payload = await whitelist_api.list_whitelist(user=_ADMIN, db=_FakeDB())
    assert all(e["recognised"] for e in payload["entries"])


# --- the derived platform-read layer ----------------------------
#
# Half the default is now derived from the tool classifier rather than curated,
# and the endpoints have to treat both halves alike: attribute an entry to the
# layer it came from, record a durable removal for either, and — because the
# platform half arrives 107 names at a time — offer a way to reject the whole
# lot that is not 107 clicks.


@pytest.fixture
def _stub_platform_reads(monkeypatch):
    """Two OpenCTI reads and one OpenAEV read, instead of the real 107."""
    reads = frozenset({"search_opencti", "list_opencti_entities", "get_openaev_asset"})
    providers = {
        "search_opencti": "opencti",
        "list_opencti_entities": "opencti",
        "get_openaev_asset": "openaev",
    }

    async def _platform_read_default(_db):
        return reads

    async def _default_membership(_db):
        return SHIPPED_DEFAULT_WHITELIST | reads

    async def _known(_db):
        return set(SHIPPED_DEFAULT_WHITELIST) | reads

    monkeypatch.setattr(
        whitelist_api, "platform_read_default", _platform_read_default
    )
    monkeypatch.setattr(whitelist_api, "default_membership", _default_membership)
    monkeypatch.setattr(whitelist_api, "known_tool_names", _known)
    monkeypatch.setattr(whitelist_api, "provider_of", providers.get)
    return reads


async def test_listing_attributes_platform_reads_to_their_platform(
    monkeypatch, _stub_platform_reads
):
    """"Why is this exempt?" has a different answer here — a classifier, not us."""
    payload = await whitelist_api.list_whitelist(user=_ADMIN, db=_FakeDB())

    entries = {e["tool_key"]: e for e in payload["entries"]}
    assert entries["search_opencti"]["source"] == "platform_read"
    assert entries["search_opencti"]["provider"] == "opencti"
    assert entries["search_opencti"]["recognised"] is True
    assert payload["platform_reads"]["opencti"]["exempt"] == [
        "list_opencti_entities",
        "search_opencti",
    ]
    assert payload["platform_reads"]["openaev"]["exempt"] == ["get_openaev_asset"]


async def test_listing_reports_re_gated_platform_reads_per_platform(
    monkeypatch, _stub_platform_reads
):
    """The state in which the operator most needs the button that undoes it."""

    async def _overrides(_db):
        return {"search_opencti": "remove"}

    monkeypatch.setattr(whitelist_api, "load_overrides", _overrides)

    payload = await whitelist_api.list_whitelist(user=_ADMIN, db=_FakeDB())

    listed = {e["tool_key"] for e in payload["entries"]}
    assert "search_opencti" not in listed
    assert payload["platform_reads"]["opencti"] == {
        "exempt": ["list_opencti_entities"],
        "regated": ["search_opencti"],
    }


async def test_removing_a_platform_read_records_a_durable_override(
    monkeypatch, _stub_audit, _stub_platform_reads
):
    """The bug this closes: without it the removal was a silent no-op.

    Membership was tested against the shipped list alone, so re-gating a
    derived read fell through to "already gated", wrote nothing, and the tool
    went on running unattended while the screen showed it as re-gated.
    """
    recorded = {}

    async def _set(_db, *, tool_key, action, user_id, reason=None):
        recorded.update(tool_key=tool_key, action=action)
        return SimpleNamespace()

    monkeypatch.setattr(whitelist_api, "set_override", _set)

    await whitelist_api.remove_from_whitelist(
        tool_key="search_opencti", request=_REQUEST, user=_ADMIN, db=_FakeDB()
    )

    assert recorded == {"tool_key": "search_opencti", "action": "remove"}
    assert _stub_audit[0]["details"]["outcome"] == "removed_from_platform_read"


async def test_re_exempting_a_platform_read_clears_rather_than_pins(
    monkeypatch, _stub_audit, _stub_platform_reads
):
    """Undoing a removal must not freeze the tool exempt.

    Pinning an ``add`` would keep it running unattended after a release that
    reclassifies it as a write — the overlay outliving the decision it
    recorded.
    """
    cleared = []

    async def _overrides(_db):
        return {"search_opencti": "remove"}

    async def _clear(_db, tool_key):
        cleared.append(tool_key)
        return True

    monkeypatch.setattr(whitelist_api, "load_overrides", _overrides)
    monkeypatch.setattr(whitelist_api, "clear_override", _clear)

    result = await whitelist_api.add_to_whitelist(
        body=whitelist_api.WhitelistAddRequest(tool_key="search_opencti"),
        request=_REQUEST,
        user=_ADMIN,
        db=_FakeDB(),
    )

    assert cleared == ["search_opencti"]
    assert result["source"] == "platform_read"
    assert _stub_audit[0]["details"]["outcome"] == "restored_platform_read"


async def test_bulk_regate_covers_one_platform_only(
    monkeypatch, _stub_audit, _stub_platform_reads
):
    """Scoped per platform: rejecting OpenCTI reads says nothing about OpenAEV."""
    recorded = []

    async def _set(_db, *, tool_key, action, user_id, reason=None):
        recorded.append((tool_key, action))
        return SimpleNamespace()

    monkeypatch.setattr(whitelist_api, "set_override", _set)

    result = await whitelist_api.require_approval_for_platform_reads(
        provider="opencti", request=_REQUEST, user=_ADMIN, db=_FakeDB()
    )

    assert result.changed == 2
    assert result.tool_keys == ["list_opencti_entities", "search_opencti"]
    assert {action for _, action in recorded} == {"remove"}
    assert "get_openaev_asset" not in dict(recorded)
    assert _stub_audit[0]["details"]["outcome"] == "removed_from_platform_read_bulk"


async def test_bulk_regate_skips_tools_already_re_gated(
    monkeypatch, _stub_audit, _stub_platform_reads
):
    """Idempotent, and honest about it: the count reflects what changed."""
    recorded = []

    async def _overrides(_db):
        return {"search_opencti": "remove"}

    async def _set(_db, *, tool_key, action, user_id, reason=None):
        recorded.append(tool_key)
        return SimpleNamespace()

    monkeypatch.setattr(whitelist_api, "load_overrides", _overrides)
    monkeypatch.setattr(whitelist_api, "set_override", _set)

    result = await whitelist_api.require_approval_for_platform_reads(
        provider="opencti", request=_REQUEST, user=_ADMIN, db=_FakeDB()
    )

    assert recorded == ["list_opencti_entities"]
    assert result.changed == 1


async def test_bulk_restore_clears_the_removals(
    monkeypatch, _stub_audit, _stub_platform_reads
):
    """Without this, a bulk re-gate is a one-way door 107 clicks wide."""
    cleared = []

    async def _overrides(_db):
        return {"search_opencti": "remove", "list_opencti_entities": "remove"}

    async def _clear(_db, tool_key):
        cleared.append(tool_key)
        return True

    monkeypatch.setattr(whitelist_api, "load_overrides", _overrides)
    monkeypatch.setattr(whitelist_api, "clear_override", _clear)

    result = await whitelist_api.restore_platform_reads(
        provider="opencti", request=_REQUEST, user=_ADMIN, db=_FakeDB()
    )

    assert sorted(cleared) == ["list_opencti_entities", "search_opencti"]
    assert result.changed == 2
    assert _stub_audit[0]["details"]["outcome"] == "restored_platform_read_bulk"


@pytest.mark.parametrize(
    "endpoint",
    [
        whitelist_api.require_approval_for_platform_reads,
        whitelist_api.restore_platform_reads,
    ],
)
async def test_bulk_actions_reject_a_provider_that_is_not_a_platform(
    endpoint, _stub_platform_reads
):
    """``slack`` has read tools too; they are not governed by this control."""
    with pytest.raises(HTTPException) as exc:
        await endpoint(
            provider="slack", request=_REQUEST, user=_ADMIN, db=_FakeDB()
        )
    assert exc.value.status_code == 404


# --- refusing rather than reporting a success that did not happen ---
#
# The defect these pin shipped briefly and was invisible: with the registration
# read failing, a derived platform read looked absent from the default, so
# "require approval for this tool" took the already-gated branch, wrote nothing
# and answered 204. The operator was told the tool now asks while it went on
# running unattended as soon as the database recovered.


@pytest.fixture
def _platform_reads_unavailable(monkeypatch):
    """The derived layer cannot be determined right now."""
    from app.services.hitl_whitelist import PlatformReadsUnavailableError

    async def _boom(_db):
        raise PlatformReadsUnavailableError("registrations unavailable")

    async def _known(_db):
        return set(SHIPPED_DEFAULT_WHITELIST)

    monkeypatch.setattr(whitelist_api, "default_membership", _boom)
    monkeypatch.setattr(whitelist_api, "platform_read_default", _boom)
    monkeypatch.setattr(whitelist_api, "known_tool_names", _known)


async def test_removing_refuses_when_the_derived_layer_is_unknown(
    monkeypatch, _stub_audit, _platform_reads_unavailable
):
    """503, not a 204 that changed nothing."""
    wrote = []

    async def _set(_db, **kwargs):
        wrote.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(whitelist_api, "set_override", _set)

    with pytest.raises(HTTPException) as exc:
        await whitelist_api.remove_from_whitelist(
            tool_key="search_opencti", request=_REQUEST, user=_ADMIN, db=_FakeDB()
        )
    assert exc.value.status_code == 503
    assert not wrote, "nothing may be written against a membership set we cannot read"
    assert not _stub_audit, "and nothing may be recorded as though it had been"


async def test_adding_refuses_when_the_derived_layer_is_unknown(
    _stub_audit, _platform_reads_unavailable
):
    """Same on the way in: restore-or-pin cannot be decided without the default."""
    with pytest.raises(HTTPException) as exc:
        await whitelist_api.add_to_whitelist(
            body=whitelist_api.WhitelistAddRequest(tool_key="search_opencti"),
            request=_REQUEST,
            user=_ADMIN,
            db=_FakeDB(),
        )
    assert exc.value.status_code == 503
    assert not _stub_audit


@pytest.mark.parametrize(
    "endpoint",
    [
        whitelist_api.require_approval_for_platform_reads,
        whitelist_api.restore_platform_reads,
    ],
)
async def test_bulk_actions_refuse_when_the_derived_layer_is_unknown(
    endpoint, _stub_audit, _platform_reads_unavailable
):
    """A bulk action reporting `changed: 0` would read as "already done"."""
    with pytest.raises(HTTPException) as exc:
        await endpoint(
            provider="opencti", request=_REQUEST, user=_ADMIN, db=_FakeDB()
        )
    assert exc.value.status_code == 503
    assert not _stub_audit


async def test_listing_degrades_instead_of_refusing(_platform_reads_unavailable):
    """A read-only screen shows what it knows, and says the rest is unknown.

    Refusing here would blank the whole page over the derived half, and
    reporting an empty `platform_reads` without the flag would read as "this
    deployment has none" — which the operator would take as "they were
    re-gated".
    """
    payload = await whitelist_api.list_whitelist(user=_ADMIN, db=_FakeDB())

    assert payload["platform_reads_available"] is False
    assert payload["platform_reads"] == {}
    assert {e["source"] for e in payload["entries"]} <= {
        "shipped_default",
        "admin_added",
    }


async def test_listing_reports_the_derived_layer_as_available_normally(
    _stub_platform_reads,
):
    payload = await whitelist_api.list_whitelist(user=_ADMIN, db=_FakeDB())
    assert payload["platform_reads_available"] is True


# --- regressions found in review ----------------------------------------


async def test_regated_platform_reads_are_not_listed_as_shipped_defaults(
    monkeypatch, _stub_platform_reads
):
    """``removed_from_default`` carries the curated half only.

    The screen renders that list under "Shipped exempt, but re-gated here" with
    a "Restore the shipped default" control, so folding the derived half in put
    platform tools under a heading that misnames where they came from — dozens
    of them after one bulk re-gate. They are reported per platform instead.
    """

    async def _overrides(_db):
        return {"search_opencti": "remove", "get_agent": "remove"}

    monkeypatch.setattr(whitelist_api, "load_overrides", _overrides)

    payload = await whitelist_api.list_whitelist(user=_ADMIN, db=_FakeDB())

    assert payload["removed_from_default"] == ["get_agent"]
    assert payload["platform_reads"]["opencti"]["regated"] == ["search_opencti"]


async def test_adding_an_already_exempt_tool_does_not_pin_it(
    monkeypatch, _stub_audit, _stub_platform_reads
):
    """Pinning an ``add`` on something already exempt outlives the reason for it.

    A frozen ``add`` keeps the tool running unattended after a release that
    drops it from the default or reclassifies it as a write — the argument the
    restore branch already makes — and lists the tool under two layers at once.
    """
    wrote = []

    async def _set(_db, **kwargs):
        wrote.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(whitelist_api, "set_override", _set)

    result = await whitelist_api.add_to_whitelist(
        body=whitelist_api.WhitelistAddRequest(tool_key="search_opencti"),
        request=_REQUEST,
        user=_ADMIN,
        db=_FakeDB(),
    )

    assert not wrote, "nothing may be pinned for a tool the default already exempts"
    assert result["changed"] is False
    assert result["source"] == "platform_read"
    assert _stub_audit[0]["details"]["outcome"] == "already_exempt"


async def test_adding_an_already_exempt_shipped_tool_reports_that_layer(
    monkeypatch, _stub_audit, _stub_platform_reads
):
    """Same for the curated half — and it must not claim an admin added it."""
    shipped = next(iter(SHIPPED_DEFAULT_WHITELIST))

    result = await whitelist_api.add_to_whitelist(
        body=whitelist_api.WhitelistAddRequest(tool_key=shipped),
        request=_REQUEST,
        user=_ADMIN,
        db=_FakeDB(),
    )

    assert result["source"] == "shipped_default"
    assert result["changed"] is False
