"""What a quota number means.

Two kinds of number, one reading each:

* a per-user / per-group / platform-default LIMIT: ``None`` is unlimited and
  ``0`` is a hard stop - including where it meets the Filigran license pool, which
  used to read the ``0`` as "no value" and replace it with the shared pool, so
  a user an admin had blocked kept running;
* a platform CEILING (``platform_limit``, a license period): ``None`` is
  uncapped. The license certificate writes an uncapped period as ``0``; it is
  reported as ``None`` everywhere it leaves the quota service, and a stored
  ``platform_limit`` of ``0`` reads the same way.

The LIMIT rules apply to values saved from now on (stamped ``rules_version``,
see :func:`_saved`). A value stored before keeps the reading it always had -
the legacy tests below pin that, and that re-saving it applies the new rules.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import quota as quota_module
from app.services.quota import (
    RULES_VERSION,
    RULES_VERSION_KEY,
    QuotaExceededError,
    ceiling_or_none,
    check_agentic_quota,
    normalize_override_for_write,
    normalize_quota_config,
    resolve_full_quota_info,
    resolve_quota_config,
)

_LICENSE_POOL = {"default": 30, "period": "daily", "global": True}


def _saved(type_cfg: dict) -> dict:
    """A type config as every save writes it from now on."""
    return {**type_cfg, RULES_VERSION_KEY: RULES_VERSION}


def _db_returning(user_cfg) -> MagicMock:
    """DB fake whose ``execute(...).scalar_one_or_none()`` is the user's config."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=user_cfg)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _empty_redis() -> MagicMock:
    """No cached config and every counter at zero."""
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    return redis


def _xtm_one_with_license_pool(monkeypatch, *, groups=None, global_cfg=None):
    monkeypatch.setattr(quota_module.settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(
        quota_module,
        "_load_user_groups_with_quotas",
        AsyncMock(return_value=list(groups or [])),
    )
    monkeypatch.setattr(
        quota_module, "_load_global_quota_config", AsyncMock(return_value=global_cfg)
    )
    monkeypatch.setattr(
        quota_module,
        "_resolve_xtm_agentic_config",
        AsyncMock(return_value=dict(_LICENSE_POOL)),
    )


# -- A limit of 0 is a hard stop, under a license pool too ------------------


@pytest.mark.asyncio
async def test_a_zero_user_limit_survives_a_license_ceiling(monkeypatch):
    _xtm_one_with_license_pool(monkeypatch)
    db = _db_returning({"agentic": _saved({"default": 0, "period": "monthly"})})

    resolved = await resolve_quota_config(db, _empty_redis(), uuid.uuid4(), "agentic")

    assert resolved == _saved({"default": 0, "period": "monthly"})
    assert not resolved.get("global")


@pytest.mark.asyncio
async def test_a_zero_user_limit_is_refused_on_the_first_call(monkeypatch):
    """The gate, end to end: the pool has room, the user's own 0 refuses."""
    _xtm_one_with_license_pool(monkeypatch)
    monkeypatch.setattr(
        quota_module, "get_xtm_agentic_ceilings", AsyncMock(return_value={"daily": 30})
    )
    redis = _empty_redis()
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=redis))
    db = _db_returning({"agentic": _saved({"default": 0, "period": "monthly"})})

    with pytest.raises(QuotaExceededError):
        await check_agentic_quota(db, uuid.uuid4())


@pytest.mark.asyncio
async def test_a_zero_group_limit_survives_a_license_ceiling(monkeypatch):
    _xtm_one_with_license_pool(
        monkeypatch, groups=[{"agentic": _saved({"default": 0, "period": "monthly"})}]
    )
    db = _db_returning(None)

    resolved = await resolve_quota_config(db, _empty_redis(), uuid.uuid4(), "agentic")

    assert resolved == _saved({"default": 0, "period": "monthly"})


# -- A 0 stored before the rules update keeps the license pool ---------------


@pytest.mark.parametrize("tier", ["user", "group", "platform default"])
@pytest.mark.asyncio
async def test_a_legacy_zero_is_still_replaced_by_the_license_pool(monkeypatch, tier):
    """As on main: users who can work before the upgrade still can after it."""
    legacy = {"agentic": {"default": 0, "period": "monthly"}}
    _xtm_one_with_license_pool(
        monkeypatch,
        groups=[legacy] if tier == "group" else None,
        global_cfg=legacy if tier == "platform default" else None,
    )
    db = _db_returning(legacy if tier == "user" else None)

    resolved = await resolve_quota_config(db, _empty_redis(), uuid.uuid4(), "agentic")

    assert resolved == _LICENSE_POOL


@pytest.mark.asyncio
async def test_a_legacy_zero_user_is_not_refused_while_the_pool_has_room(monkeypatch):
    _xtm_one_with_license_pool(monkeypatch)
    monkeypatch.setattr(
        quota_module, "get_xtm_agentic_ceilings", AsyncMock(return_value={"daily": 30})
    )
    monkeypatch.setattr(
        quota_module, "redis_client", AsyncMock(return_value=_empty_redis())
    )
    db = _db_returning({"agentic": {"default": 0, "period": "monthly"}})

    await check_agentic_quota(db, uuid.uuid4())


def test_a_legacy_rule_still_in_use_is_logged_once_per_process(monkeypatch, caplog):
    monkeypatch.setattr(quota_module, "_legacy_rules_warned", set())

    with caplog.at_level("WARNING", logger=quota_module.logger.name):
        for _ in range(3):
            assert quota_module._license_ceiling_is_tighter({"default": 0}, 30)
        assert not quota_module._license_ceiling_is_tighter(_saved({"default": 0}), 30)

    warnings = [r for r in caplog.records if "replaced by the license pool" in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_a_legacy_zero_re_saved_is_a_hard_stop(monkeypatch):
    """Saving the same value stamps it, and the current rule applies."""
    _xtm_one_with_license_pool(monkeypatch)
    re_saved = normalize_override_for_write({"agentic": {"default": 0, "period": "monthly"}})
    db = _db_returning(re_saved)

    resolved = await resolve_quota_config(db, _empty_redis(), uuid.uuid4(), "agentic")

    assert resolved == _saved({"default": 0, "period": "monthly"})


@pytest.mark.asyncio
async def test_a_legacy_platform_default_zero_re_saved_is_a_hard_stop(monkeypatch):
    re_saved = normalize_quota_config(
        {"agentic": {"default": 0, "period": "monthly"}}, for_write=True
    )
    _xtm_one_with_license_pool(monkeypatch, global_cfg=re_saved)

    resolved = await resolve_quota_config(
        _db_returning(None), _empty_redis(), uuid.uuid4(), "agentic"
    )

    assert resolved == _saved({"default": 0, "period": "monthly"})


@pytest.mark.asyncio
async def test_an_unlimited_user_limit_is_still_capped_by_the_license(monkeypatch):
    _xtm_one_with_license_pool(monkeypatch)
    db = _db_returning({"agentic": {"default": None, "period": "monthly"}})

    resolved = await resolve_quota_config(db, _empty_redis(), uuid.uuid4(), "agentic")

    assert resolved == _LICENSE_POOL


@pytest.mark.asyncio
async def test_a_looser_user_limit_is_still_capped_by_the_license(monkeypatch):
    _xtm_one_with_license_pool(monkeypatch)
    db = _db_returning({"agentic": {"default": 500, "period": "monthly"}})

    resolved = await resolve_quota_config(db, _empty_redis(), uuid.uuid4(), "agentic")

    assert resolved == _LICENSE_POOL


@pytest.mark.asyncio
async def test_the_displayed_limit_keeps_a_zero_user_limit(monkeypatch):
    """The overview resolver mirrors the gate: the 0 is shown as the user's."""
    _xtm_one_with_license_pool(monkeypatch)
    db = _db_returning({"agentic": _saved({"default": 0, "period": "monthly"})})

    resolved, sources = await resolve_full_quota_info(db, MagicMock(), uuid.uuid4())

    assert resolved["agentic"] == _saved({"default": 0, "period": "monthly"})
    assert sources["agentic"] == "user"


@pytest.mark.asyncio
async def test_the_displayed_limit_of_a_legacy_zero_is_the_pool(monkeypatch):
    """The overview resolver applies the same legacy decision as the gate."""
    _xtm_one_with_license_pool(monkeypatch)
    db = _db_returning({"agentic": {"default": 0, "period": "monthly"}})

    resolved, sources = await resolve_full_quota_info(db, MagicMock(), uuid.uuid4())

    assert resolved["agentic"] == {"default": 30, "period": "daily"}
    assert sources["agentic"] == "global"


# -- The most permissive group wins: Unlimited is the most permissive -------


@pytest.mark.parametrize(
    "groups",
    [
        [{"agentic": _saved({"default": None, "period": "monthly"})}, {"agentic": _saved({"default": 50, "period": "monthly"})}],
        [{"agentic": _saved({"default": 50, "period": "monthly"})}, {"agentic": _saved({"default": None, "period": "monthly"})}],
    ],
)
@pytest.mark.asyncio
async def test_an_unlimited_group_wins_over_a_limited_group(monkeypatch, groups):
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    monkeypatch.setattr(
        quota_module, "_load_user_groups_with_quotas", AsyncMock(return_value=groups)
    )
    monkeypatch.setattr(
        quota_module, "_load_global_quota_config", AsyncMock(return_value=None)
    )
    db = _db_returning(None)

    resolved = await resolve_quota_config(db, _empty_redis(), uuid.uuid4(), "agentic")

    assert resolved["default"] is None


@pytest.mark.parametrize(
    "groups",
    [
        [{"agentic": _saved({"default": 0, "period": "monthly"})}, {"agentic": _saved({"default": None, "period": "monthly"})}],
        [{"agentic": _saved({"default": None, "period": "monthly"})}, {"agentic": _saved({"default": 0, "period": "monthly"})}],
    ],
)
@pytest.mark.asyncio
async def test_a_hard_stop_group_never_beats_an_unlimited_one(monkeypatch, groups):
    """The two used to tie at 0, so the answer depended on row order."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    monkeypatch.setattr(
        quota_module, "_load_user_groups_with_quotas", AsyncMock(return_value=groups)
    )
    monkeypatch.setattr(
        quota_module, "_load_global_quota_config", AsyncMock(return_value=None)
    )

    resolved, sources = await resolve_full_quota_info(_db_returning(None), MagicMock(), uuid.uuid4())

    assert resolved["agentic"]["default"] is None
    assert sources["agentic"] == "group"


# -- The admin monitoring view resolves limits like the gate ----------------


class _MonitoringDb:
    """Answers the two selects of ``_resolve_per_user_limits``."""

    def __init__(self, users: dict[uuid.UUID, dict | None], groups: list[tuple[uuid.UUID, dict]]):
        self._users = users
        self._groups = groups

    async def execute(self, stmt):
        from app.models.group import GroupMember

        entity = stmt.column_descriptions[0]["entity"]
        if entity is GroupMember:
            rows = [SimpleNamespace(user_id=uid, quota_config=cfg) for uid, cfg in self._groups]
        else:
            rows = [SimpleNamespace(id=uid, quota_config=cfg) for uid, cfg in self._users.items()]
        result = MagicMock()
        result.all = MagicMock(return_value=rows)
        return result


@pytest.mark.asyncio
async def test_the_monitoring_view_resolves_limits_like_the_gate():
    """The dashboard's own resolver ranked groups by
    ``default or 0``, kept only a positive group limit, and let a user's
    ``null`` override fall through - so a 0 group limit was ignored and an
    Unlimited group lost, while the gate did the opposite."""
    zero_user, zero_group_user, unlimited_group_user, unlimited_user = (uuid.uuid4() for _ in range(4))
    db = _MonitoringDb(
        users={
            zero_user: {"agentic": _saved({"default": 0})},
            unlimited_user: {"agentic": _saved({"unlimited": True, "default": None})},
            zero_group_user: None,
            unlimited_group_user: None,
        },
        groups=[
            (zero_group_user, {"agentic": _saved({"default": 0})}),
            (unlimited_group_user, {"agentic": _saved({"default": 50})}),
            (unlimited_group_user, {"agentic": _saved({"unlimited": True, "default": None})}),
        ],
    )

    resolved = await quota_module._resolve_per_user_limits(
        db,
        "agentic",
        [str(u) for u in (zero_user, zero_group_user, unlimited_group_user, unlimited_user)],
        100,
    )

    assert resolved[str(zero_user)] == 0
    assert resolved[str(zero_group_user)] == 0
    assert resolved[str(unlimited_group_user)] is None
    assert resolved[str(unlimited_user)] is None


@pytest.mark.asyncio
async def test_the_monitoring_view_ranks_legacy_groups_like_the_gate():
    """A group without a limit saved before the update yields to a sibling
    group with a number, in the dashboard as in the gate."""
    member = uuid.uuid4()
    db = _MonitoringDb(
        users={member: None},
        groups=[
            (member, {"agentic": {"default": None}}),
            (member, {"agentic": {"default": 50}}),
        ],
    )

    resolved = await quota_module._resolve_per_user_limits(db, "agentic", [str(member)], 100)

    assert resolved[str(member)] == 50


# -- A ceiling is None when uncapped ----------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), (0, None), (-1, None), (True, None), ("5", None), (5, 5), (7.0, 7)],
)
def test_ceiling_or_none(value, expected):
    assert ceiling_or_none(value) == expected


def test_a_stored_zero_platform_limit_reads_as_uncapped():
    stored = {
        "agentic": {"platform_limit": 0, "default": 0, "period": "monthly"},
        "llm_proxy": {"platform_limit": 500, "default": None, "period": "daily"},
    }

    normalized = normalize_quota_config(stored)

    assert normalized["agentic"]["platform_limit"] is None
    # The per-user default is a LIMIT: its 0 is a hard stop and is kept.
    assert normalized["agentic"]["default"] == 0
    assert normalized["llm_proxy"]["platform_limit"] == 500
    # Copy-on-write: the stored row is not mutated.
    assert stored["agentic"]["platform_limit"] == 0


def test_normalize_quota_config_passes_through_what_it_does_not_know():
    assert normalize_quota_config(None) is None
    assert normalize_quota_config({"agentic": None}) == {"agentic": None}


@pytest.mark.asyncio
async def test_a_zero_platform_limit_caps_nothing(monkeypatch):
    """Copilot mode: a legacy stored ``platform_limit`` of 0 is no cap at all."""
    monkeypatch.setattr(quota_module.settings, "platform_mode", "copilot")
    monkeypatch.setattr(
        quota_module,
        "_load_global_quota_config",
        AsyncMock(return_value={"agentic": {"platform_limit": 0, "period": "monthly"}}),
    )
    redis = MagicMock()
    redis.get = AsyncMock(return_value=b"1000")

    # Would raise QuotaExceededError if the 0 were read as a ceiling.
    await quota_module._check_platform_wide_limit(MagicMock(), redis, "agentic")

    assert await quota_module.agentic_platform_cap_enforced(MagicMock()) is False


@pytest.mark.asyncio
async def test_the_platform_quota_route_stores_an_uncapped_limit_as_null(monkeypatch):
    """Normalised on write, so the stored config has one spelling of uncapped."""
    from app.api.deps import AfterCommit
    from app.api.v1 import platform_settings_helpers
    from app.api.v1 import settings as settings_api
    from app.api.v1.schemas.settings import QuotaConfig

    ps = SimpleNamespace(quota_config=None)
    monkeypatch.setattr(
        platform_settings_helpers, "get_platform_settings", AsyncMock(return_value=ps)
    )
    monkeypatch.setattr(quota_module, "invalidate_quota_cache", AsyncMock())
    monkeypatch.setattr(settings_api, "log_audit", AsyncMock())
    db = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()

    body = QuotaConfig.model_validate(
        {"agentic": {"platform_limit": 0, "default": 0, "period": "monthly"}}
    )
    after_commit = AfterCommit()
    out = await settings_api.update_platform_quotas(
        body, request=MagicMock(), user=MagicMock(), db=db, _ai_gate=None, after_commit=after_commit
    )
    # What ``run_after_commit`` does once the route has returned.
    await after_commit.run()

    assert ps.quota_config["agentic"]["platform_limit"] is None
    assert ps.quota_config["agentic"]["default"] == 0
    assert ps.quota_config["agentic"][RULES_VERSION_KEY] == RULES_VERSION
    assert out["quota_config"] is ps.quota_config
