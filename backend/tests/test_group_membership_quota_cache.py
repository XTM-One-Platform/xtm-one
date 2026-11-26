"""A group membership change drops the member's cached quota resolution.

``resolve_quota_config`` caches a user's whole resolution - their own override,
their groups' quotas, the platform's - under ``quota:config:{user_id}`` for five
minutes. Every path that adds or removes a membership therefore drops the
affected users' entries once its change has COMMITTED, through
``quota.invalidate_membership_quota_cache``: the group routes and an account
deletion (pinned in ``test_account_writes_commit_before_response.py``), and SAML
group sync on sign-in with its merge of a UUID-named group (here). A
deactivation keeps the memberships, so it drops nothing.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import types
import uuid
from types import SimpleNamespace

import pytest

import app.services.quota as quota_module
from app.api import auth_sso
from app.api.auth_sso import _rename_uuid_groups, _sync_user_groups, saml_acs
from app.core.config import settings
from app.models.group import Group, GroupMember
from app.models.user import User
from tests import app_sources

EMAIL = "dana@example.com"
SUBJECT = "idp|dana"
FRONTEND = "https://app.example.com"
#: Held before any test patches it, for the test that needs the real one.
_REAL_DROP = quota_module.invalidate_membership_quota_cache


class _CacheRedis:
    """The quota config cache: ``get`` / ``set`` / ``delete`` and the resolver's
    compare-and-set, deletes recorded."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.deletes: list[tuple[str, ...]] = []

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value
        return True

    async def eval(self, script, numkeys, *args):
        assert script == quota_module._SET_IF_GENERATION_LUA
        cache_key, generation_key, payload, _ttl, generation = args
        if (self.data.get(generation_key) or "") != generation:
            return 0
        self.data[cache_key] = payload
        return 1

    async def delete(self, *keys):
        self.deletes.append(keys)
        return sum(self.data.pop(key, None) is not None for key in keys)

    def cached(self) -> list[str]:
        """The cached resolutions, without the generation beside them."""
        return [key for key in self.data if key.startswith(quota_module._CONFIG_CACHE_PREFIX)]


def _use_redis(monkeypatch, redis) -> list[int]:
    """Route the quota module's ``redis_client`` to *redis*; count the calls."""
    calls: list[int] = []

    async def _client():
        calls.append(1)
        return redis

    monkeypatch.setattr(quota_module, "redis_client", _client)
    return calls


class _NoQuotaRows:
    """A session holding no override, no group quota and no platform quota."""

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: None, all=lambda: [])


# -- the helper ------------------------------------------------------------------


async def test_the_drop_removes_the_resolution_the_resolver_cached(monkeypatch):
    async def _no_license(_db):
        return None

    monkeypatch.setattr(quota_module, "_resolve_xtm_agentic_config", _no_license)
    redis = _CacheRedis()
    _use_redis(monkeypatch, redis)
    member, other = uuid.uuid4(), uuid.uuid4()
    for user_id in (member, other):
        await quota_module.resolve_quota_config(_NoQuotaRows(), redis, user_id, "agentic")
    assert len(redis.cached()) == 2

    # Spelled any way a caller holds the id: the key is the one the resolver
    # wrote, never a second spelling of it.
    await quota_module.invalidate_membership_quota_cache([str(member).upper()])

    assert redis.cached() == [quota_module._config_cache_key(other)]


async def test_the_drop_is_one_delete_for_the_affected_users(monkeypatch):
    redis = _CacheRedis()
    _use_redis(monkeypatch, redis)
    first, second = uuid.uuid4(), uuid.uuid4()

    await quota_module.invalidate_membership_quota_cache(
        [first, str(first), str(first).upper(), second, None, "not-a-user-id"]
    )

    assert redis.deletes == [
        tuple(sorted(quota_module._config_cache_key(uid) for uid in (first, second)))
    ]


async def test_the_drop_batches_a_large_membership(monkeypatch):
    redis = _CacheRedis()
    _use_redis(monkeypatch, redis)
    batch = quota_module._CONFIG_CACHE_DELETE_BATCH

    await quota_module.invalidate_membership_quota_cache(
        uuid.uuid4() for _ in range(2 * batch + 1)
    )

    assert [len(keys) for keys in redis.deletes] == [batch, batch, 1]


@pytest.mark.parametrize("user_ids", [[], [None, "not-a-user-id"]])
async def test_nothing_to_drop_never_reaches_redis(monkeypatch, user_ids):
    calls = _use_redis(monkeypatch, _CacheRedis())

    await quota_module.invalidate_membership_quota_cache(user_ids)

    assert calls == []


async def test_the_drop_never_fails_a_committed_change(monkeypatch):
    async def _unreachable():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(quota_module, "redis_client", _unreachable)
    await quota_module.invalidate_membership_quota_cache([uuid.uuid4()])

    class _Failing(_CacheRedis):
        async def delete(self, *keys):
            raise TimeoutError("redis timed out")

    _use_redis(monkeypatch, _Failing())
    # It runs after the membership change committed (a sign-in included): an
    # outage leaves the old resolution cached until its TTL, nothing more.
    await quota_module.invalidate_membership_quota_cache([uuid.uuid4()])


def _writes_memberships(tree: ast.AST) -> bool:
    """Creates a ``GroupMember`` row, or bulk-deletes the table."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "GroupMember":
            return True
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name in {"delete", "sa_delete", "sql_delete"} and any(
            isinstance(arg, ast.Name) and arg.id == "GroupMember" for arg in node.args
        ):
            return True
    return False


def test_every_module_writing_memberships_drops_the_quota_cache():
    """A new membership source (an OIDC group mapping, SCIM, an import) drops
    the affected users' resolutions like the ones below, or fails here."""
    app_root = pathlib.Path(quota_module.__file__).resolve().parents[1]
    writers: dict[str, bool] = {}
    for path in app_sources.python_files(app_root):
        source = app_sources.source(path)
        if _writes_memberships(app_sources.tree(path)):
            writers[path.relative_to(app_root).as_posix()] = (
                "invalidate_membership_quota_cache" in source
            )

    # The detection must keep seeing the writers it was written for.
    assert {"api/v1/groups.py", "api/auth_sso.py"} <= set(writers), sorted(writers)
    missing = sorted(name for name, drops in writers.items() if not drops)
    assert not missing, (
        "these modules add or remove group memberships without dropping the "
        "affected users' cached quota resolution - call "
        f"quota.invalidate_membership_quota_cache once the change commits: {missing}"
    )


# -- SAML group sync -------------------------------------------------------------


class _Rows:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def all(self):
        return list(self._value or [])

    def scalar_one_or_none(self):
        return self._value


class _SyncDb:
    """The user's memberships and the asserted groups, by the table queried;
    no agent is shared with a group the user leaves."""

    def __init__(self, memberships, groups):
        self.memberships = memberships
        self.groups = groups
        self.added: list = []
        self.deleted: list = []

    async def execute(self, stmt):
        rendered = str(stmt)
        if "FROM agents" in rendered:
            return _Rows([])
        if "FROM group_members" in rendered:
            return _Rows(self.memberships)
        if "groups.auto_join IS true" in rendered:
            # The ids of the auto-join groups among the user's SAML memberships.
            return _Rows([g.id for g in self.groups if g.auto_join])
        return _Rows(self.groups)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()


def _membership(group: Group, user, source: str) -> GroupMember:
    return GroupMember(id=uuid.uuid4(), group_id=group.id, user_id=user.id, source=source)


@pytest.fixture
def dana():
    return SimpleNamespace(id=uuid.uuid4(), email=EMAIL)


async def test_sync_reports_an_added_membership(dana):
    analysts = Group(id=uuid.uuid4(), name="Analysts")
    db = _SyncDb(memberships=[], groups=[analysts])

    assert await _sync_user_groups(db, dana, ["Analysts"], auto_create=False) is True
    assert [(m.group_id, m.user_id, m.source) for m in db.added] == [
        (analysts.id, dana.id, "saml")
    ]


async def test_sync_reports_a_membership_in_a_group_it_created(dana):
    db = _SyncDb(memberships=[], groups=[])

    assert await _sync_user_groups(db, dana, ["New Team"], auto_create=True) is True
    assert [type(obj) for obj in db.added] == [Group, GroupMember]


async def test_sync_reports_a_removed_membership(dana):
    analysts, stale = Group(id=uuid.uuid4(), name="Analysts"), Group(id=uuid.uuid4(), name="Old")
    kept, gone = _membership(analysts, dana, "saml"), _membership(stale, dana, "saml")
    db = _SyncDb(memberships=[kept, gone], groups=[analysts])

    assert await _sync_user_groups(db, dana, ["Analysts"], auto_create=False) is True
    assert db.deleted == [gone]


@pytest.mark.parametrize("source", ["saml", "manual"])
async def test_sync_that_keeps_the_memberships_reports_nothing(dana, source):
    """Already a member: nothing changes, and a re-tag to "saml" is no change
    to what the user belongs to."""
    analysts = Group(id=uuid.uuid4(), name="Analysts")
    membership = _membership(analysts, dana, source)
    db = _SyncDb(memberships=[membership], groups=[analysts])

    assert await _sync_user_groups(db, dana, ["Analysts"], auto_create=False) is False
    assert (db.added, db.deleted, membership.source) == ([], [], "saml")


@pytest.mark.parametrize(("source", "changed"), [("saml", True), ("manual", False)])
async def test_an_assertion_with_no_group_reports_what_it_removed(dana, source, changed):
    analysts = Group(id=uuid.uuid4(), name="Analysts")
    membership = _membership(analysts, dana, source)
    db = _SyncDb(memberships=[membership], groups=[])

    assert await _sync_user_groups(db, dana, [], auto_create=False) is changed
    # Only SAML-sourced memberships are ever removed.
    assert db.deleted == ([membership] if changed else [])


@pytest.mark.parametrize("names", [[], ["Analysts"]])
async def test_a_saml_membership_of_an_auto_join_group_is_never_removed_as_stale(
    dana: SimpleNamespace, names: list[str]
) -> None:
    """Auto join turned on after the mapping wrote the row (#1901): the row
    stays "saml" and the mapping keeps it whatever the assertion names."""
    everyone = Group(id=uuid.uuid4(), name="Everyone", auto_join=True)
    analysts = Group(id=uuid.uuid4(), name="Analysts")
    membership = _membership(everyone, dana, "saml")
    db = _SyncDb(memberships=[membership], groups=[everyone, analysts])

    await _sync_user_groups(db, dana, names, auto_create=False)
    assert db.deleted == []
    assert membership.source == "saml"


class _ScriptedDb:
    """Answers each query in turn, as ``_rename_uuid_groups`` asks them."""

    def __init__(self, *answers):
        self._answers = list(answers)
        self.added: list = []
        self.deleted: list = []

    async def execute(self, _stmt):
        return _Rows(self._answers.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        return None


async def test_a_merged_uuid_group_records_the_members_it_moved(monkeypatch):
    pruned: list[uuid.UUID] = []

    async def _prune(_db, group_id):
        # Its grants go with it (``helpers.prune_group_grants``, against
        # PostgreSQL in ``test_shared_agent_owner_fields_postgres.py``).
        pruned.append(group_id)
        return 0

    monkeypatch.setattr(auth_sso, "prune_group_grants", _prune)
    entra_id = str(uuid.uuid4())
    uuid_group = Group(id=uuid.uuid4(), name=entra_id)
    target = Group(id=uuid.uuid4(), name="Security Team")
    newcomer = SimpleNamespace(id=uuid.uuid4())
    already = SimpleNamespace(id=uuid.uuid4())
    members = [_membership(uuid_group, newcomer, "saml"), _membership(uuid_group, already, "manual")]
    db = _ScriptedDb(
        [uuid_group],  # the groups still named by their Entra id
        [target],  # the display names that already exist: a merge
        target,  # the group merged into
        members,  # the UUID group's members
        None,  # the newcomer is not in the target yet
        SimpleNamespace(id=uuid.uuid4()),  # the other one already is
        [],  # the LLM providers whose exclusions it carries: none
    )
    moved: set[uuid.UUID] = set()

    renamed = await _rename_uuid_groups(
        db, {entra_id: "Security Team"}, moved_members=moved
    )

    assert renamed == 1
    assert moved == {newcomer.id, already.id}
    assert [(m.group_id, m.user_id) for m in db.added] == [(target.id, newcomer.id)]
    assert db.deleted == [*members, uuid_group]
    assert pruned == [uuid_group.id]


async def test_a_renamed_uuid_group_moves_nobody():
    entra_id = str(uuid.uuid4())
    uuid_group = Group(id=uuid.uuid4(), name=entra_id)
    db = _ScriptedDb([uuid_group], [])
    moved: set[uuid.UUID] = set()

    assert await _rename_uuid_groups(db, {entra_id: "Security Team"}, moved_members=moved) == 1
    assert uuid_group.name == "Security Team"
    assert moved == set()


# -- the SAML Assertion Consumer Service -----------------------------------------


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def all(self):
        # No group has auto join on in this fake (#1901).
        return []


class _AcsDb:
    """The account lookup of ``_get_or_create_sso_user``, commits recorded."""

    def __init__(self, account: User, calls: list):
        self._account = account
        self._calls = calls
        self.added: list = []

    async def execute(self, stmt):
        return _Result(self._account if "users.sso_subject =" in str(stmt) else None)

    async def scalar(self, _stmt):
        return 1

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, _obj):
        return None

    async def flush(self):
        return None

    async def commit(self):
        self._calls.append("commit")

    async def rollback(self):
        return None


class _Request:
    def __init__(self):
        self.query_params: dict = {}
        self.headers: dict = {}
        self.client = SimpleNamespace(host="203.0.113.7")

    async def form(self):
        return {"SAMLResponse": "opaque"}


@pytest.fixture
def acs(monkeypatch):
    """Real ACS, stubbed python3-saml and group mapping steps, drops recorded."""
    import app.core.sso_helpers as sso_helpers

    monkeypatch.setattr(settings, "frontend_url", FRONTEND)
    monkeypatch.setattr(settings, "xtm_hub_admin_group", "")
    monkeypatch.setattr(settings, "xtm_hub_platform_id", "")
    stack = SimpleNamespace(
        calls=[],
        synced=False,
        resolved={},
        moved=set(),
        account=User(
            id=uuid.uuid4(),
            email=EMAIL,
            full_name="Dana",
            role="user",
            is_active=True,
            sso_provider="saml",
            sso_subject=SUBJECT,
        ),
    )
    cfg = SimpleNamespace(
        saml_enabled=True,
        saml_auto_admin_new_users=False,
        saml_auto_create_users=True,
        saml_groups_mapping_enabled=True,
        saml_groups_auto_create=True,
        allowed_domain_list=[],
    )
    attrs = {
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress": [EMAIL],
        "groups": ["Security Team"],
    }

    class _Auth:
        def __init__(self, req, saml_settings):
            pass

        def process_response(self):
            pass

        def get_errors(self):
            return []

        def get_last_error_reason(self):
            return None

        def is_authenticated(self):
            return True

        def get_attributes(self):
            return attrs

        def get_nameid(self):
            return SUBJECT

    fake_module = types.ModuleType("onelogin.saml2.auth")
    fake_module.OneLogin_Saml2_Auth = _Auth
    monkeypatch.setitem(sys.modules, "onelogin", types.ModuleType("onelogin"))
    monkeypatch.setitem(sys.modules, "onelogin.saml2", types.ModuleType("onelogin.saml2"))
    monkeypatch.setitem(sys.modules, "onelogin.saml2.auth", fake_module)

    async def _cfg(_db):
        return cfg

    async def _saml_settings(_cfg):
        return {}

    async def _resolve(_groups, _cfg, _db):
        return stack.resolved

    async def _rename(_db, resolved, *, moved_members=None):
        moved_members.update(stack.moved)
        return len(resolved)

    async def _sync(_db, _user, _groups, auto_create):
        return stack.synced

    async def _drop(user_ids):
        stack.calls.append(("drop", sorted(user_ids)))

    monkeypatch.setattr(sso_helpers, "get_sso_config", _cfg)
    monkeypatch.setattr(auth_sso, "_build_saml_settings_from_cfg", _saml_settings)
    monkeypatch.setattr(auth_sso, "_resolve_group_ids_via_graph", _resolve)
    monkeypatch.setattr(auth_sso, "_rename_uuid_groups", _rename)
    monkeypatch.setattr(auth_sso, "_sync_user_groups", _sync)
    monkeypatch.setattr(quota_module, "invalidate_membership_quota_cache", _drop)
    return stack


async def _sign_in(acs) -> str:
    response = await saml_acs(_Request(), db=_AcsDb(acs.account, acs.calls))
    assert response.status_code == 303
    location = response.headers["location"]
    assert "#token=" in location
    return location


async def test_a_saml_sync_that_changed_a_membership_drops_it_after_the_commit(acs):
    acs.synced = True

    await _sign_in(acs)

    # Dropped before the commit, a quota check in between would re-cache the
    # groups the user just left (or miss the ones just joined) for the TTL.
    assert acs.calls == ["commit", ("drop", [acs.account.id])]


async def test_a_saml_sign_in_that_changed_nothing_drops_nothing(acs):
    await _sign_in(acs)

    assert acs.calls == ["commit"]


async def test_a_merged_uuid_group_drops_the_members_it_moved(acs):
    moved = {uuid.uuid4(), uuid.uuid4()}
    acs.resolved = {str(uuid.uuid4()): "Security Team"}
    acs.moved = moved

    await _sign_in(acs)

    assert acs.calls == ["commit", ("drop", sorted(moved))]


async def test_a_redis_outage_never_fails_the_sign_in(acs, monkeypatch):
    async def _unreachable():
        raise ConnectionError("redis is down")

    # The real helper this time, against an unreachable Redis.
    monkeypatch.setattr(quota_module, "invalidate_membership_quota_cache", _REAL_DROP)
    monkeypatch.setattr(quota_module, "redis_client", _unreachable)
    acs.synced = True

    await _sign_in(acs)

    assert acs.calls == ["commit"]
