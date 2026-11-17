"""Unit tests for effective HITL whitelist resolution.

Membership is computed as ``(shipped_default - removals) | additions`` rather
than stored. These tests pin the layering rules that choice exists to guarantee:

* FR-001e — an administrator's removal survives a release that re-adds the tool
  to the shipped default. This is the dangerous direction: getting it wrong
  silently re-exempts a tool somebody deliberately gated.
* FR-001f — a newly shipped default entry takes effect with no admin action.

The database is faked rather than hit, so these stay fast unit tests; the
against-Postgres behaviour is covered by the migration/API integration tests.
"""

import pytest

from app.services import hitl_whitelist
from app.services.hitl_whitelist import (
    WhitelistUnavailableError,
    resolve_whitelist,
)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Minimal stand-in returning canned override and registration rows.

    Two tables are read while resolving membership, and the fake has to tell
    them apart: answering the registration query with override rows made the
    platform layer fail its own unpacking and come back empty, so tests passed
    through an error path while appearing to exercise the happy one.
    """

    def __init__(self, rows=(), raises=None, registrations=()):
        self._rows = list(rows)
        self._raises = raises
        self._registrations = list(registrations)

    async def execute(self, stmt):
        if self._raises is not None:
            raise self._raises
        if "platform_registrations" in str(stmt):
            return _FakeResult(self._registrations)
        return _FakeResult(self._rows)


#: One registration row as ``platform_approval_by_provider`` selects it:
#: identifier, declared capability, policy, provenance.
_OPENCTI_REGISTERED = ("opencti", True, "require", "default")


@pytest.fixture
def shipped(monkeypatch):
    """Pin a small, known shipped default so tests don't track the real list."""
    default = frozenset({"get_agent", "list_agents", "search_memory"})
    monkeypatch.setattr(hitl_whitelist, "SHIPPED_DEFAULT_WHITELIST", default)
    return default


@pytest.mark.asyncio
async def test_no_overrides_returns_the_shipped_default(shipped):
    assert await resolve_whitelist(_FakeSession()) == shipped


@pytest.mark.asyncio
async def test_admin_addition_is_included(shipped):
    db = _FakeSession([("delete_entity", "add")])
    result = await resolve_whitelist(db)
    assert "delete_entity" in result
    assert shipped <= result


@pytest.mark.asyncio
async def test_admin_removal_is_excluded(shipped):
    db = _FakeSession([("get_agent", "remove")])
    result = await resolve_whitelist(db)
    assert "get_agent" not in result
    assert "list_agents" in result


@pytest.mark.asyncio
async def test_additions_and_removals_combine(shipped):
    db = _FakeSession([("get_agent", "remove"), ("delete_entity", "add")])
    result = await resolve_whitelist(db)
    assert result == frozenset({"list_agents", "search_memory", "delete_entity"})


@pytest.mark.asyncio
async def test_removal_survives_a_release_that_re_adds_the_tool(monkeypatch):
    """FR-001e — the rule that protects a deliberate re-gating decision.

    Simulates an upgrade: the administrator removed ``get_agent``, then a later
    release ships a default that contains it. The removal must still win, or the
    upgrade silently un-gates a tool somebody chose to gate.
    """
    db = _FakeSession([("get_agent", "remove")])

    monkeypatch.setattr(
        hitl_whitelist, "SHIPPED_DEFAULT_WHITELIST", frozenset({"list_agents"})
    )
    before = await resolve_whitelist(db)
    assert "get_agent" not in before

    # New release re-adds it to the shipped default.
    monkeypatch.setattr(
        hitl_whitelist,
        "SHIPPED_DEFAULT_WHITELIST",
        frozenset({"list_agents", "get_agent"}),
    )
    after = await resolve_whitelist(db)
    assert "get_agent" not in after, "upgrade silently re-exempted a re-gated tool"


@pytest.mark.asyncio
async def test_new_default_entry_applies_without_admin_action(monkeypatch):
    """FR-001f — a shipped read-only tool must not gate until someone notices."""
    db = _FakeSession([("delete_entity", "add")])

    monkeypatch.setattr(
        hitl_whitelist, "SHIPPED_DEFAULT_WHITELIST", frozenset({"get_agent"})
    )
    assert "list_runs" not in await resolve_whitelist(db)

    monkeypatch.setattr(
        hitl_whitelist,
        "SHIPPED_DEFAULT_WHITELIST",
        frozenset({"get_agent", "list_runs"}),
    )
    after = await resolve_whitelist(db)
    assert "list_runs" in after
    assert "delete_entity" in after, "admin addition lost across the upgrade"


@pytest.mark.asyncio
async def test_read_failure_raises_rather_than_returning_empty(shipped):
    """An empty set would be indistinguishable from a successful empty overlay.

    Callers must be able to tell "nothing is whitelisted" from "we could not
    find out", because only the second one means gate everything.
    """
    db = _FakeSession(raises=RuntimeError("connection reset"))
    with pytest.raises(WhitelistUnavailableError):
        await resolve_whitelist(db)


@pytest.mark.asyncio
async def test_a_sanitised_alias_is_not_vouched_for(shipped, monkeypatch):
    """Run history records the name the MODEL saw, not the registered one.

    A custom tool called "Address search" is offered to the provider as
    "Address_search" and lands in ``actions_taken`` under that spelling, while
    the gate keys off the registered name. Vouching for the alias sends an
    administrator to type the one spelling guaranteed to match nothing — the
    failure ``recognised`` exists to catch, inverted, and silent either way.

    Calls ``known_tool_names`` rather than re-deriving the set arithmetic. The
    earlier version reimplemented it, and so stayed green through a regression
    that folded run history into the "registered" side of the subtraction and
    vouched for the alias after all.
    """

    class _AliasSession:
        """Custom tool "Address search"; history has its sanitised alias."""

        async def execute(self, stmt):
            text = str(stmt)
            if "custom_tools" in text:
                return _FakeResult([("Address search",)])
            if "assignment_runs" in text:
                return _FakeResult(
                    [([{"tool": "Address_search"}],), ([{"tool": "search_knowledge"}, "junk"],)]
                )
            return _FakeResult([])

    monkeypatch.setattr(
        hitl_whitelist, "read_tools_for", lambda providers: frozenset()
    )
    known = await hitl_whitelist.known_tool_names(_AliasSession())

    assert "Address search" in known, "the registered name must stay vouched for"
    assert "Address_search" not in known, (
        "the sanitised alias is vouched for again — an administrator typing it "
        "would be told it is recognised while the gate can never match it"
    )
    assert "search_knowledge" in known, "a genuine snake_case name must survive"


def test_a_snake_case_tool_keeps_its_entry():
    """An alias is dropped only when the name it derives from is known, so a
    genuinely snake_case tool seen only in run history survives."""
    from app.tools.base import sanitize_tool_name

    names = {"search_knowledge", "get_entity"}
    aliases = {s for n in names if (s := sanitize_tool_name(n)) != n}

    assert aliases == set()
    assert names - aliases == names


def test_a_real_name_colliding_with_an_alias_is_kept():
    """"address_search" can be a registered tool in its own right while
    "address search" also exists. Subtracting every sanitised form would delete
    the real one, and the administrator would be told their correct whitelist
    entry has no effect — advisory, but wrong in the direction that erodes
    trust in the screen."""
    from app.tools.base import sanitize_tool_name

    registered = {"address search", "address_search"}
    aliases = {
        s for n in registered if (s := sanitize_tool_name(n)) != n
    } - registered

    assert aliases == set()
    assert registered - aliases == registered


# --- Restoring a shipped default -------------------------------------------


def test_restoring_clears_the_override_rather_than_pinning_an_addition():
    """Undoing a removal is not the same act as adding a tool.

    Re-adding via an ``add`` override would exempt the tool today and go on
    exempting it after a release that drops it from the shipped default — the
    overlay outliving the decision it recorded. Clearing returns the tool to
    tracking the default, which is what "restore" means.

    (The path that was actually broken sent DELETE here: the tool was already
    gated, so the endpoint rewrote the same ``remove`` override and reported
    success while nothing changed.)
    """
    import inspect

    from app.api.v1 import hitl_whitelist

    source = inspect.getsource(hitl_whitelist.add_to_whitelist)
    assert "ACTION_REMOVE" in source and "clear_override" in source, (
        "adding a shipped-default tool that carries a removal must clear it"
    )


# ── The derived platform-read layer ──────────────────────────────
#
# The default has a second half: the read-classified tools of the platforms
# this deployment has registered. It layers exactly like the curated half, and
# these pin that — most of all in the dangerous direction, where an operator
# who re-gated a platform read must not find it exempt again after an upgrade.


@pytest.fixture
def platform_reads(monkeypatch):
    """Pin a small, known derived set instead of the real 107 names."""
    derived = frozenset({"search_opencti", "list_opencti_entities"})

    def _fake_read_tools_for(providers):
        return derived if "opencti" in providers else frozenset()

    monkeypatch.setattr(hitl_whitelist, "read_tools_for", _fake_read_tools_for)
    return derived


@pytest.mark.asyncio
async def test_registered_platform_reads_are_exempt(shipped, platform_reads):
    db = _FakeSession(registrations=[_OPENCTI_REGISTERED])
    assert await resolve_whitelist(db) == shipped | platform_reads


@pytest.mark.asyncio
async def test_unregistered_platform_contributes_nothing(shipped, platform_reads):
    """No registration, no derived entries — the tools do not exist here."""
    assert await resolve_whitelist(_FakeSession()) == shipped


@pytest.mark.asyncio
async def test_removing_a_platform_read_survives_an_upgrade(shipped, platform_reads):
    """FR-001e, applied to the derived half.

    The subtraction is applied to the whole default, so a durable ``remove``
    outlives the release that keeps classifying the tool as a read. Getting
    this wrong silently re-exempts a tool somebody deliberately gated, and the
    operator has no way to tell.
    """
    db = _FakeSession(
        rows=[("search_opencti", "remove")], registrations=[_OPENCTI_REGISTERED]
    )
    result = await resolve_whitelist(db)
    assert "search_opencti" not in result
    assert "list_opencti_entities" in result


@pytest.fixture
def registrations_unreadable(monkeypatch):
    """Make the registration read fail, the way a database hiccup would."""

    async def _boom(_db):
        raise RuntimeError("registrations unavailable")

    monkeypatch.setattr(hitl_whitelist, "platform_approval_by_provider", _boom)


@pytest.mark.asyncio
async def test_unreadable_registrations_gate_the_platform_reads(
    shipped, platform_reads, registrations_unreadable
):
    """The gate fails closed: not knowing which platforms are connected exempts none.

    Distinct from the overlay failing, which raises out of `resolve_whitelist`.
    Here the curated default is still trustworthy, so the answer is "everything
    we ship, nothing we inferred" rather than an error.
    """
    db = _FakeSession(registrations=[_OPENCTI_REGISTERED])
    assert await resolve_whitelist(db) == shipped


@pytest.mark.asyncio
async def test_unreadable_registrations_raise_for_membership_questions(
    shipped, platform_reads, registrations_unreadable
):
    """...but a membership *question* must not get a confident wrong answer.

    This is the split that matters. Returning an empty set to everyone made the
    administrator path silently fail open: the tool looked absent from the
    default, so "require approval" concluded there was nothing to do, wrote
    nothing, and reported success.
    """
    db = _FakeSession(registrations=[_OPENCTI_REGISTERED])
    with pytest.raises(hitl_whitelist.PlatformReadsUnavailableError):
        await hitl_whitelist.default_membership(db)
    with pytest.raises(hitl_whitelist.PlatformReadsUnavailableError):
        await hitl_whitelist.platform_read_default(db)


@pytest.mark.asyncio
async def test_known_names_degrade_rather_than_raise(
    shipped, platform_reads, registrations_unreadable
):
    """Name suggestions are advisory, so they lose entries instead of the screen."""
    db = _FakeSession(registrations=[_OPENCTI_REGISTERED])
    names = await hitl_whitelist.known_tool_names(db)
    assert shipped <= names
    assert not (platform_reads & names)


@pytest.mark.asyncio
async def test_default_membership_reports_both_halves(shipped, platform_reads):
    db = _FakeSession(registrations=[_OPENCTI_REGISTERED])
    assert await hitl_whitelist.default_membership(db) == shipped | platform_reads


# --- Degrading must not poison the session it degrades in ------------------


class _RollbackRecordingSession(_FakeSession):
    """Fails every query and records whether the caller rolled back."""

    def __init__(self, raises):
        super().__init__(raises=raises)
        self.rolled_back = False

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_an_unreadable_toggle_leaves_the_session_usable():
    """``resolve_exempt_sources`` fails closed and returns "exempt nothing" —
    correct, but it ran on the per-turn gate-build path without rolling back.

    Postgres aborts the whole transaction on a failed statement, so the caller's
    next query fails too: the gate would go from "gate everything" to "the turn
    dies on its next statement". Exactly the defect already fixed one function
    above in ``platform_read_default``, which records what it cost.
    """
    from app.services.hitl_whitelist import resolve_exempt_sources

    db = _RollbackRecordingSession(RuntimeError("connection reset"))

    assert await resolve_exempt_sources(db) == frozenset()
    assert db.rolled_back, (
        "the transaction was already dead; without a rollback the caller "
        "cannot run its own queries either"
    )


@pytest.mark.asyncio
async def test_advisory_name_lookups_also_leave_the_session_usable():
    """``known_tool_names`` is advisory, but the session it borrows is not:
    ``add_to_whitelist`` reads the overlay straight after asking it."""
    from app.services.hitl_whitelist import known_tool_names

    db = _RollbackRecordingSession(RuntimeError("connection reset"))

    await known_tool_names(db)
    assert db.rolled_back
