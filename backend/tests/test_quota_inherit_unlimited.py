"""User / group quota overrides: empty inherits, Unlimited is explicit.

The editors promised "leave empty to inherit" while an emptied field was stored
as ``{"default": None}`` and resolved as Unlimited. For every value saved from
now on (stamped ``rules_version``, see :func:`_saved`):

* an override type holding no limit is dropped on write and ignored on read -
  the user inherits from the group or the platform;
* Unlimited is the explicit ``{"unlimited": True, "default": None}`` - still
  ``None`` on the wire, still the most permissive group.

A value stored before keeps exactly the reading it always had: any non-empty
type is a value at its tier (Unlimited when it holds no limit, whatever keys it
has), and a group without a limit yields to a sibling group with a number.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.deps import AfterCommit
from app.services import quota as quota_module
from app.services.quota import (
    RULES_VERSION,
    RULES_VERSION_KEY,
    normalize_override_for_write,
    normalize_quota_config,
    read_override_config,
    read_override_type_config,
    resolve_full_quota_info,
    resolve_quota_config,
)

_UNLIMITED = {"unlimited": True, "default": None, "period": "monthly"}


def _saved(type_cfg: dict) -> dict:
    """A type config as every save writes it from now on."""
    return {**type_cfg, RULES_VERSION_KEY: RULES_VERSION}


def _result(*, scalar=None, rows=None, count=None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=scalar)
    result.all = MagicMock(return_value=list(rows or []))
    result.scalar = MagicMock(return_value=count)
    return result


def _db(*results: MagicMock) -> MagicMock:
    """DB fake answering each ``execute`` with the next result, in order."""
    db = MagicMock()
    db.execute = AsyncMock(side_effect=list(results))
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    return db


def _empty_redis() -> MagicMock:
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.eval = AsyncMock(return_value=1)
    redis.delete = AsyncMock(return_value=1)
    return redis


@pytest.fixture
def copilot_mode(monkeypatch):
    """No license ceiling in the way, and no platform default to fall back to."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    monkeypatch.setattr(quota_module, "_load_global_quota_config", AsyncMock(return_value=None))


async def _resolve(user_cfg, group_cfgs):
    """Resolve ``agentic`` through the real user AND group reads.

    ``resolve_quota_config`` reads the configuration a second time to confirm
    what it cached, so each read is answered by the entity it selects.
    """
    user_read = _result(scalar=user_cfg)
    group_read = _result(rows=[(cfg,) for cfg in group_cfgs])

    async def execute(stmt, *args, **kwargs):
        entity = stmt.column_descriptions[0]["entity"]
        return user_read if entity is quota_module.User else group_read

    db = _db()
    db.execute = AsyncMock(side_effect=execute)
    return await resolve_quota_config(db, _empty_redis(), uuid.uuid4(), "agentic")


# -- Write: an emptied type is dropped, Unlimited is explicit ---------------


@pytest.mark.parametrize(
    "inherits",
    [
        {"default": None, "per_model": None, "unlimited": False},
        {"period": "monthly", "unlimited": False},
        {},
    ],
)
def test_an_empty_or_explicitly_not_unlimited_type_is_not_stored(inherits):
    assert normalize_override_for_write({"agentic": inherits}) is None
    assert normalize_override_for_write({"agentic": inherits, "llm_proxy": {"default": 5}}) == {
        "llm_proxy": _saved({"default": 5})
    }


@pytest.mark.parametrize(
    "limit_less",
    [
        {"default": None, "period": "daily"},
        {"period": "monthly"},
        {"default": None, "platform_limit": None, "per_model": {}},
    ],
)
def test_a_limit_less_type_without_the_flag_is_stored_as_unlimited(limit_less):
    """What API clients and older editors send for Unlimited keeps that meaning."""
    assert normalize_override_for_write({"agentic": limit_less}) == {
        "agentic": _saved({**limit_less, "unlimited": True, "default": None})
    }


def test_unlimited_is_stored_explicitly_and_drops_a_stray_number():
    stored = normalize_override_for_write({"agentic": {"unlimited": True, "default": 40, "period": "daily"}})

    assert stored == {"agentic": _saved({"unlimited": True, "default": None, "period": "daily"})}


@pytest.mark.parametrize("flag", ["false", "true", 1, "yes"])
def test_only_the_boolean_true_lifts_a_limit(flag):
    """The body is a raw dict; a malformed flag keeps the limit."""
    stored = normalize_override_for_write({"agentic": {"default": 5, "unlimited": flag}})

    assert stored == {"agentic": _saved({"default": 5})}
    assert read_override_type_config(_saved({"default": 5, "unlimited": flag})) == _saved(
        {"default": 5, "unlimited": flag}
    )


def test_unlimited_drops_per_model_caps():
    """Per-model caps kept under Unlimited were still enforced."""
    stored = normalize_override_for_write(
        {"llm_proxy": {"unlimited": True, "default": None, "per_model": {"gpt-5": 10}}}
    )

    assert stored == {"llm_proxy": _saved({"unlimited": True, "default": None})}


def test_a_limit_is_stored_as_is_without_a_stray_flag():
    stored = normalize_override_for_write(
        {
            "agentic": {"default": 0, "period": "daily", "unlimited": False},
            "llm_proxy": {"default": None, "per_model": {"gpt-5": 10}},
        }
    )

    assert stored == {
        "agentic": _saved({"default": 0, "period": "daily"}),
        "llm_proxy": _saved({"default": None, "per_model": {"gpt-5": 10}}),
    }


def test_write_normalisation_ignores_junk():
    assert normalize_override_for_write(None) is None
    assert normalize_override_for_write({}) is None
    assert normalize_override_for_write({"agentic": None, "llm_proxy": 3}) is None


# -- Read: absent / emptied inherit, marker and legacy are Unlimited --------


def test_read_semantics_type_by_type():
    assert read_override_type_config(None) is None
    assert read_override_type_config({}) is None
    assert read_override_type_config(_saved({"period": "daily"})) is None
    assert read_override_type_config(_saved(_UNLIMITED)) == _saved(_UNLIMITED)
    assert read_override_type_config(_saved({"default": 0})) == _saved({"default": 0})


def test_a_legacy_value_reads_as_it_always_did():
    legacy = {"default": None, "period": "monthly"}
    assert read_override_type_config(legacy) == _UNLIMITED
    assert "unlimited" not in legacy  # copy-on-write
    # No limit and no ``default`` key: still a value, Unlimited at its tier.
    assert read_override_type_config({"period": "daily"}) == {
        "period": "daily",
        "unlimited": True,
        "default": None,
    }
    assert read_override_type_config({"default": 0}) == {"default": 0}


def test_read_override_config_drops_what_inherits():
    assert read_override_config({"agentic": _saved({"period": "daily"})}) is None
    assert read_override_config(
        {"agentic": {"default": None}, "llm_proxy": _saved({"period": "daily"})}
    ) == {"agentic": {"unlimited": True, "default": None}}


@pytest.mark.asyncio
async def test_an_absent_user_override_inherits_the_group(copilot_mode):
    resolved = await _resolve(None, [{"agentic": {"default": 20, "period": "daily"}}])

    assert resolved == {"default": 20, "period": "daily"}


@pytest.mark.asyncio
async def test_an_emptied_user_override_inherits_the_group(copilot_mode):
    resolved = await _resolve(
        {"agentic": _saved({"period": "monthly"})},
        [{"agentic": {"default": 20, "period": "daily"}}],
    )

    assert resolved == {"default": 20, "period": "daily"}


@pytest.mark.asyncio
async def test_a_legacy_user_type_without_a_default_is_still_unlimited(copilot_mode):
    """Written through the API before the update, it has always counted as
    Unlimited at the user's tier: it does not start inheriting the group."""
    resolved = await _resolve(
        {"agentic": {"period": "daily"}},
        [{"agentic": {"default": 20, "period": "daily"}}],
    )

    assert resolved["default"] is None
    assert resolved["period"] == "daily"


@pytest.mark.asyncio
async def test_a_legacy_user_type_without_a_default_re_saved_as_is_stays_unlimited(copilot_mode):
    """The same body sent again keeps its meaning, now stamped."""
    re_saved = normalize_override_for_write({"agentic": {"period": "daily"}})

    resolved = await _resolve(re_saved, [{"agentic": {"default": 20, "period": "daily"}}])

    assert re_saved == {"agentic": _saved({"period": "daily", "unlimited": True, "default": None})}
    assert resolved["default"] is None


@pytest.mark.asyncio
async def test_a_legacy_user_type_emptied_in_the_editor_inherits(copilot_mode):
    """The current editor omits an emptied type: the current rule applies."""
    re_saved = normalize_override_for_write({"llm_proxy": {"default": 5}})

    resolved = await _resolve(re_saved, [{"agentic": {"default": 20, "period": "daily"}}])

    assert "agentic" not in re_saved
    assert resolved == {"default": 20, "period": "daily"}


@pytest.mark.asyncio
async def test_an_unlimited_user_override_is_unlimited(copilot_mode):
    resolved = await _resolve(
        {"agentic": _saved(_UNLIMITED)},
        [{"agentic": {"default": 20, "period": "daily"}}],
    )

    assert resolved["default"] is None
    assert resolved["unlimited"] is True


@pytest.mark.asyncio
async def test_a_legacy_empty_user_field_keeps_meaning_unlimited(copilot_mode):
    """No silent change on upgrade: the row stored by the old editor."""
    resolved = await _resolve(
        {"agentic": {"default": None, "period": "monthly"}},
        [{"agentic": {"default": 20, "period": "daily"}}],
    )

    assert resolved == _UNLIMITED


@pytest.mark.asyncio
async def test_the_overview_resolver_reads_overrides_the_same_way(copilot_mode):
    db = _db(
        _result(scalar={"agentic": _saved({"period": "monthly"})}),
        _result(rows=[({"agentic": {"default": None}},)]),
    )

    resolved, sources = await resolve_full_quota_info(db, MagicMock(), uuid.uuid4())

    assert resolved["agentic"] == {"unlimited": True, "default": None}
    assert sources["agentic"] == "group"


# -- Group ranking: an emptied group is no candidate, Unlimited wins --------


@pytest.mark.parametrize("order", [1, -1])
@pytest.mark.asyncio
async def test_an_emptied_group_does_not_beat_a_limited_one(copilot_mode, order):
    groups = [
        {"agentic": _saved({"period": "monthly"})},
        {"agentic": _saved({"default": 50, "period": "monthly"})},
    ][::order]

    resolved = await _resolve(None, groups)

    assert resolved == _saved({"default": 50, "period": "monthly"})


@pytest.mark.parametrize("order", [1, -1])
@pytest.mark.asyncio
async def test_an_unlimited_group_beats_a_limited_one(copilot_mode, order):
    groups = [
        {"agentic": _saved(_UNLIMITED)},
        {"agentic": _saved({"default": 50, "period": "monthly"})},
    ][::order]

    resolved = await _resolve(None, groups)

    assert resolved == _saved(_UNLIMITED)


# A group emptied before the update, and a sibling group limited to 50.
_LEGACY_EMPTIED_GROUP = {"agentic": {"default": None, "period": "monthly"}}
_LEGACY_GROUP_OF_50 = {"agentic": {"default": 50, "period": "monthly"}}


@pytest.mark.parametrize("order", [1, -1])
@pytest.mark.asyncio
async def test_a_legacy_emptied_group_still_yields_to_a_group_of_50(copilot_mode, order):
    """As on main: the 50 an admin relies on keeps applying after the upgrade."""
    resolved = await _resolve(None, [_LEGACY_EMPTIED_GROUP, _LEGACY_GROUP_OF_50][::order])

    assert resolved == {"default": 50, "period": "monthly"}


@pytest.mark.parametrize("order", [1, -1])
@pytest.mark.asyncio
async def test_a_legacy_emptied_group_re_saved_as_unlimited_wins(copilot_mode, order):
    """Saving the group as the editor shows it (Unlimited) applies the current rule."""
    served = read_override_config(_LEGACY_EMPTIED_GROUP)
    re_saved = normalize_override_for_write(served)

    resolved = await _resolve(None, [re_saved, _LEGACY_GROUP_OF_50][::order])

    assert resolved == _saved(_UNLIMITED)


@pytest.mark.parametrize(
    ("groups", "expected"),
    [
        # Legacy no-limit ranks as 0 against a current 50.
        ([_LEGACY_EMPTIED_GROUP, {"agentic": _saved({"default": 50, "period": "monthly"})}], 50),
        # A current Unlimited ranks above a legacy 50...
        ([{"agentic": _saved(_UNLIMITED)}, _LEGACY_GROUP_OF_50], None),
        # ...and above a legacy group without a limit.
        ([_LEGACY_EMPTIED_GROUP, {"agentic": _saved(_UNLIMITED)}], None),
    ],
)
@pytest.mark.parametrize("order", [1, -1])
@pytest.mark.asyncio
async def test_mixed_legacy_and_current_groups_rank_as_each_would(copilot_mode, groups, expected, order):
    resolved = await _resolve(None, groups[::order])

    assert resolved["default"] == expected


@pytest.mark.asyncio
async def test_a_user_with_only_emptied_groups_falls_back_to_the_platform(monkeypatch):
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    monkeypatch.setattr(
        quota_module,
        "_load_global_quota_config",
        AsyncMock(return_value={"agentic": {"default": 7, "period": "daily"}}),
    )

    resolved = await _resolve(None, [{"agentic": _saved({"period": "monthly"})}])

    assert resolved == {"default": 7, "period": "daily"}


@pytest.mark.asyncio
async def test_the_monitoring_view_reads_overrides_like_the_gate():
    emptied_user, legacy_user = uuid.uuid4(), uuid.uuid4()

    class _MonitoringDb:
        async def execute(self, stmt):
            from app.models.group import GroupMember

            if stmt.column_descriptions[0]["entity"] is GroupMember:
                rows = [
                    SimpleNamespace(user_id=emptied_user, quota_config={"agentic": _saved({"period": "daily"})}),
                    SimpleNamespace(user_id=emptied_user, quota_config={"agentic": {"default": 9}}),
                ]
            else:
                rows = [
                    SimpleNamespace(id=emptied_user, quota_config={"agentic": _saved({"period": "monthly"})}),
                    SimpleNamespace(id=legacy_user, quota_config={"agentic": {"default": None}}),
                ]
            result = MagicMock()
            result.all = MagicMock(return_value=rows)
            return result

    resolved = await quota_module._resolve_per_user_limits(
        _MonitoringDb(), "agentic", [str(emptied_user), str(legacy_user)], 100
    )

    assert resolved[str(emptied_user)] == 9
    assert resolved[str(legacy_user)] is None


def test_the_platform_default_carries_no_unlimited_marker():
    """The last tier has nothing to inherit from: the marker means nothing there."""
    assert normalize_quota_config({"agentic": {"default": None, "unlimited": None, "period": "daily"}}) == {
        "agentic": {"default": None, "period": "daily"}
    }


def test_the_platform_default_is_stamped_on_write_only():
    stored = {"agentic": {"default": 0, "period": "daily"}}

    assert normalize_quota_config(stored) == stored
    assert normalize_quota_config(stored, for_write=True) == {"agentic": _saved({"default": 0, "period": "daily"})}
    assert normalize_quota_config({"agentic": None, "llm_proxy": None}, for_write=True) == {
        "agentic": None,
        "llm_proxy": None,
    }


# -- The API: PUT normalises, GET serves the explicit marker -----------------


@pytest.fixture
def api_stubs(monkeypatch):
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    monkeypatch.setattr(quota_module, "invalidate_quota_cache", AsyncMock())
    monkeypatch.setattr(quota_module, "get_xtm_agentic_ceilings", AsyncMock(return_value=None))


async def _put(route, *args, **kwargs):
    """Call a PUT route as FastAPI does: what it hands to ``run_after_commit`` runs once it has returned."""
    after_commit = AfterCommit()
    result = await route(*args, after_commit=after_commit, **kwargs)
    await after_commit.run()
    return result


@pytest.mark.asyncio
async def test_put_user_quotas_keeps_a_default_null_body_unlimited(monkeypatch, api_stubs, copilot_mode):
    """An API script or an older browser tab sets Unlimited with
    ``{"default": null}``: it reads back, and resolves, as Unlimited."""
    from app.api.v1 import users as users_api

    monkeypatch.setattr(users_api, "log_audit", AsyncMock())
    target = SimpleNamespace(id=uuid.uuid4(), email="a@b.c", full_name="A", quota_config={"x": 1})
    db = MagicMock()
    db.get = AsyncMock(return_value=target)
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    body = users_api.UpdateUserQuotaRequest(quota_config={"agentic": {"default": None, "period": "monthly"}})
    await _put(users_api.update_user_quotas, target.id, body, request=MagicMock(), admin=MagicMock(), db=db)

    assert target.quota_config == {"agentic": _saved(_UNLIMITED)}
    resolved = await _resolve(target.quota_config, [{"agentic": {"default": 20, "period": "daily"}}])
    assert resolved["default"] is None


@pytest.mark.asyncio
async def test_put_user_quotas_drops_an_explicitly_inheriting_type(monkeypatch, api_stubs):
    from app.api.v1 import users as users_api

    monkeypatch.setattr(users_api, "log_audit", AsyncMock())
    target = SimpleNamespace(id=uuid.uuid4(), email="a@b.c", full_name="A", quota_config={"x": 1})
    db = MagicMock()
    db.get = AsyncMock(return_value=target)
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    body = users_api.UpdateUserQuotaRequest(quota_config={"agentic": {"default": None, "unlimited": False}})
    out = await _put(users_api.update_user_quotas, target.id, body, request=MagicMock(), admin=MagicMock(), db=db)

    assert target.quota_config is None
    assert out == {"quota_config": None}


@pytest.mark.asyncio
async def test_put_user_quotas_stores_unlimited_explicitly(monkeypatch, api_stubs):
    from app.api.v1 import users as users_api

    monkeypatch.setattr(users_api, "log_audit", AsyncMock())
    target = SimpleNamespace(id=uuid.uuid4(), email="a@b.c", full_name="A", quota_config=None)
    db = MagicMock()
    db.get = AsyncMock(return_value=target)
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    body = users_api.UpdateUserQuotaRequest(
        quota_config={
            "agentic": {"unlimited": True, "default": None, "period": "daily"},
            "llm_proxy": {"default": None, "period": "monthly"},
        }
    )
    await _put(users_api.update_user_quotas, target.id, body, request=MagicMock(), admin=MagicMock(), db=db)

    assert target.quota_config == {
        "agentic": _saved({"unlimited": True, "default": None, "period": "daily"}),
        "llm_proxy": _saved(_UNLIMITED),
    }


@pytest.mark.asyncio
async def test_get_user_quotas_serves_a_legacy_field_as_unlimited(monkeypatch):
    import app.core.redis as redis_module
    from app.api.v1 import users as users_api

    target = SimpleNamespace(
        id=uuid.uuid4(),
        quota_config={
            "agentic": {"default": None, "period": "monthly"},
            "llm_proxy": {"period": "daily"},
        },
    )
    db = MagicMock()
    db.get = AsyncMock(return_value=target)
    monkeypatch.setattr(redis_module, "redis_client", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(quota_module, "resolve_full_quota_info", AsyncMock(return_value=({}, {})))
    monkeypatch.setattr(quota_module, "get_user_quota_usage", AsyncMock(return_value=[]))

    out = await users_api.get_user_quotas(target.id, _admin=MagicMock(), db=db)

    agentic = out.user_config.agentic
    assert agentic.unlimited is True
    assert agentic.default is None
    assert agentic.rules_version is None
    # No ``default`` key: still Unlimited at the user's tier, as before.
    assert out.user_config.llm_proxy.unlimited is True
    # A user-level legacy Unlimited means the same under the current rules.
    assert out.user_config_notices == {}


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ({"agentic": {"default": 0, "period": "monthly"}}, {"agentic": quota_module.LEGACY_NOTICE_ZERO_UNDER_POOL}),
        ({"agentic": _saved({"default": 0, "period": "monthly"})}, {}),
    ],
)
@pytest.mark.asyncio
async def test_get_user_quotas_flags_a_legacy_zero_under_the_license_pool(monkeypatch, stored, expected):
    import app.core.redis as redis_module
    from app.api.v1 import users as users_api

    target = SimpleNamespace(id=uuid.uuid4(), quota_config=stored)
    db = MagicMock()
    db.get = AsyncMock(return_value=target)
    monkeypatch.setattr(redis_module, "redis_client", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(quota_module, "resolve_full_quota_info", AsyncMock(return_value=({}, {})))
    monkeypatch.setattr(quota_module, "get_user_quota_usage", AsyncMock(return_value=[]))
    monkeypatch.setattr(quota_module, "license_pool_active", AsyncMock(return_value=True))

    out = await users_api.get_user_quotas(target.id, _admin=MagicMock(), db=db)

    assert out.user_config_notices == expected


def _group(quota_config):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="G",
        description=None,
        created_by=None,
        created_at=None,
        updated_at=None,
        quota_config=quota_config,
        kind="auto",
    )


@pytest.mark.asyncio
async def test_put_group_stores_unlimited_explicitly(monkeypatch, api_stubs):
    from app.api.v1 import groups as groups_api

    monkeypatch.setattr(groups_api, "log_audit", AsyncMock())
    group = _group(None)
    db = _db(_result(scalar=group), _result(count=3))

    body = groups_api.UpdateGroupRequest(
        quota_config={
            "agentic": {"unlimited": True, "default": 12, "period": "daily"},
            "llm_proxy": {"default": None, "period": "monthly"},
        }
    )
    out = await _put(groups_api.update_group, str(group.id), body, request=MagicMock(), admin=MagicMock(), db=db)

    assert group.quota_config == {
        "agentic": _saved({"unlimited": True, "default": None, "period": "daily"}),
        "llm_proxy": _saved(_UNLIMITED),
    }
    assert out.quota_config == group.quota_config


@pytest.mark.asyncio
async def test_put_group_grants_and_removes_a_ctem_right(monkeypatch: pytest.MonkeyPatch, api_stubs: None) -> None:
    """#1748: the CTEM right and its notification switch, audited; an unknown right is a 422."""
    from fastapi import HTTPException

    from app.api.v1 import groups as groups_api

    audit = AsyncMock()
    monkeypatch.setattr(groups_api, "log_audit", audit)
    group = SimpleNamespace(
        id=uuid.uuid4(),
        name="G",
        description=None,
        created_by=None,
        created_at=None,
        updated_at=None,
        quota_config=None,
        kind="auto",
    )

    async def put(body: groups_api.UpdateGroupRequest) -> groups_api.GroupListItem:
        db = _db(_result(scalar=group), _result(count=1))
        route: Any = _put
        answer: groups_api.GroupListItem = await route(
            groups_api.update_group, str(group.id), body, request=MagicMock(), admin=MagicMock(), db=db
        )
        return answer

    out = await put(groups_api.UpdateGroupRequest(ctem_access="decide", ctem_notify=False))
    assert group.ctem_access == "decide" and group.ctem_notify is False
    assert out.ctem_access == "decide" and out.ctem_notify is False
    assert audit.await_args is not None
    assert audit.await_args.kwargs["details"]["ctem_access"] == "decide"

    out = await put(groups_api.UpdateGroupRequest(ctem_access="none"))
    assert group.ctem_access is None and out.ctem_access is None

    with pytest.raises(HTTPException) as refused:
        await put(groups_api.UpdateGroupRequest(ctem_access="admin"))
    assert refused.value.status_code == 422


@pytest.mark.asyncio
async def test_put_group_with_only_emptied_fields_inherits(monkeypatch, api_stubs):
    from app.api.v1 import groups as groups_api

    monkeypatch.setattr(groups_api, "log_audit", AsyncMock())
    group = _group({"agentic": {"default": 5}})
    db = _db(_result(scalar=group), _result(count=0))

    body = groups_api.UpdateGroupRequest(quota_config={"agentic": {"default": None, "unlimited": False}})
    await _put(groups_api.update_group, str(group.id), body, request=MagicMock(), admin=MagicMock(), db=db)

    assert group.quota_config is None


@pytest.mark.asyncio
async def test_put_group_with_an_empty_quota_config_clears_the_override(monkeypatch, api_stubs):
    """What the group tab sends once both cards are emptied (and what the route
    has always read as a clear) - also how a legacy Unlimited group is made to
    inherit from the tab."""
    from app.api.v1 import groups as groups_api

    monkeypatch.setattr(groups_api, "log_audit", AsyncMock())
    group = _group({"agentic": {"default": None, "period": "monthly"}})
    db = _db(_result(scalar=group), _result(count=2))

    body = groups_api.UpdateGroupRequest.model_validate({"quota_config": {}})
    out = await _put(groups_api.update_group, str(group.id), body, request=MagicMock(), admin=MagicMock(), db=db)

    assert group.quota_config is None
    assert out.quota_config is None
    quota_module.invalidate_quota_cache.assert_awaited_once()


@pytest.mark.parametrize("body", [{"quota_config": None}, {"description": "Night shift"}])
@pytest.mark.asyncio
async def test_put_group_with_a_null_or_omitted_quota_config_keeps_the_override(monkeypatch, api_stubs, body):
    """An API client that sends the whole group back on a rename, with
    ``quota_config: null``, keeps the group's quotas - as it always did."""
    from app.api.v1 import groups as groups_api

    monkeypatch.setattr(groups_api, "log_audit", AsyncMock())
    stored = {"agentic": {"default": 5, "period": "daily"}}
    group = _group(dict(stored))
    db = _db(_result(scalar=group), _result(count=2))

    update = groups_api.UpdateGroupRequest.model_validate(body)
    out = await _put(groups_api.update_group, str(group.id), update, request=MagicMock(), admin=MagicMock(), db=db)

    assert group.quota_config == stored
    assert out.quota_config == stored
    quota_module.invalidate_quota_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_group_returns_its_override_read_like_the_resolver():
    """The detail route never returned ``quota_config``, so the group quota tab
    always opened empty and a save overwrote the other quota type."""
    from app.api.v1 import groups as groups_api

    group = _group({"agentic": {"default": None, "period": "monthly"}})
    db = _db(_result(scalar=group), _result(count=2))

    out = await groups_api.get_group(str(group.id), admin=MagicMock(), db=db)

    assert out.quota_config == {"agentic": _UNLIMITED}
    # Legacy: it still yields to a sibling group with a number, which the
    # current rule would change on the next save - the tab says so.
    assert out.quota_notices == {"agentic": quota_module.LEGACY_NOTICE_GROUP_NO_LIMIT}


@pytest.mark.asyncio
async def test_get_group_has_no_notice_for_a_value_saved_now():
    from app.api.v1 import groups as groups_api

    group = _group({"agentic": _saved(_UNLIMITED)})
    db = _db(_result(scalar=group), _result(count=2))

    out = await groups_api.get_group(str(group.id), admin=MagicMock(), db=db)

    assert out.quota_notices == {}
