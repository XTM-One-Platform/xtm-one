"""Unit tests for the vertical-aware, two-tier CTEM analyst override resolution
in ``app.api.v1.ctem_metrics``.

Covers the pure override-map helpers (nested vs legacy-flat reads, legacy
migration, the apply patch, and the request-id filter) and the async resolvers'
priority order (per-user override -> platform default -> seeded analyst),
including a disabled/missing override falling through to the next tier.

No DB / Redis / network: ``_load_enabled_agent``, ``_seeded_stage_agent``,
``_briefing_agent`` and ``get_platform_settings`` are monkeypatched, so the
tests exercise only the resolution logic this PR introduces.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import ctem_metrics
from app.services.ctem_verticals import CTEM_VERTICALS

# ── Pure helpers: reading an override id from a stage / briefing map ──────────


def test_vertical_stage_override_id_nested_wins_and_is_per_vertical():
    m = {"cyber": {"scoping": "A"}, "fimi": {"scoping": "B"}}
    assert ctem_metrics._vertical_stage_override_id(m, "cyber", "scoping") == "A"
    assert ctem_metrics._vertical_stage_override_id(m, "fimi", "scoping") == "B"
    # A vertical bucket exists but the stage is absent -> no override.
    assert ctem_metrics._vertical_stage_override_id(m, "cyber", "discovery") is None
    # A vertical with no bucket at all -> no override (no cross-vertical bleed).
    assert ctem_metrics._vertical_stage_override_id(m, "fraud", "scoping") is None


def test_vertical_stage_override_id_legacy_flat_applies_to_any_vertical():
    # The legacy flat {stage: id} shape historically applied to every vertical,
    # so it is read as a per-vertical fallback when there is no nested bucket.
    flat = {"scoping": "  L  "}
    for vertical in CTEM_VERTICALS:
        assert ctem_metrics._vertical_stage_override_id(flat, vertical, "scoping") == "L"
    assert ctem_metrics._vertical_stage_override_id(flat, "cyber", "discovery") is None


def test_vertical_stage_override_id_handles_blank_and_non_dict():
    assert ctem_metrics._vertical_stage_override_id(None, "cyber", "scoping") is None
    assert ctem_metrics._vertical_stage_override_id("nope", "cyber", "scoping") is None
    assert (
        ctem_metrics._vertical_stage_override_id(
            {"cyber": {"scoping": "   "}}, "cyber", "scoping"
        )
        is None
    )


def test_vertical_briefing_override_id():
    m = {"cyber": "A", "fimi": "  B  "}
    assert ctem_metrics._vertical_briefing_override_id(m, "cyber") == "A"
    assert ctem_metrics._vertical_briefing_override_id(m, "fimi") == "B"
    assert ctem_metrics._vertical_briefing_override_id(m, "fraud") is None
    assert ctem_metrics._vertical_briefing_override_id(None, "cyber") is None


# ── Legacy flat -> nested migration ──────────────────────────────────────────


def test_migrate_flat_stage_map_expands_legacy_to_all_verticals():
    out = ctem_metrics._migrate_flat_stage_map({"scoping": "A"})
    assert set(out) == set(CTEM_VERTICALS)
    for vertical in CTEM_VERTICALS:
        assert out[vertical]["scoping"] == "A"


def test_migrate_flat_stage_map_preserves_nested_and_copies():
    nested = {"cyber": {"scoping": "A"}}
    out = ctem_metrics._migrate_flat_stage_map(nested)
    assert out == {"cyber": {"scoping": "A"}}
    # A deep-ish copy is returned, so mutating the result never touches the input.
    out["cyber"]["scoping"] = "Z"
    assert nested["cyber"]["scoping"] == "A"


def test_migrate_flat_stage_map_merges_flat_under_nested():
    # Flat is the base for every vertical; a nested per-vertical entry overrides it.
    m = {"scoping": "FLAT", "cyber": {"scoping": "CYBER", "discovery": "D"}}
    out = ctem_metrics._migrate_flat_stage_map(m)
    assert out["cyber"]["scoping"] == "CYBER"
    assert out["cyber"]["discovery"] == "D"
    assert out["fimi"]["scoping"] == "FLAT"
    assert out["fraud"]["scoping"] == "FLAT"


def test_migrate_flat_stage_map_empty_inputs():
    assert ctem_metrics._migrate_flat_stage_map(None) == {}
    assert ctem_metrics._migrate_flat_stage_map({}) == {}
    assert ctem_metrics._migrate_flat_stage_map("nope") == {}


# ── Applying an override patch for one vertical ───────────────────────────────


def test_apply_overrides_set_then_reset_stage():
    stage_map, brief_map = ctem_metrics._apply_overrides(None, None, "cyber", {"scoping": "A"})
    assert stage_map == {"cyber": {"scoping": "A"}}
    assert brief_map is None
    # Resetting the only entry collapses the map back to None.
    stage_map, brief_map = ctem_metrics._apply_overrides(
        stage_map, brief_map, "cyber", {"scoping": None}
    )
    assert stage_map is None
    assert brief_map is None


def test_apply_overrides_briefing_key():
    stage_map, brief_map = ctem_metrics._apply_overrides(None, None, "fimi", {"briefing": "B"})
    assert brief_map == {"fimi": "B"}
    assert stage_map is None
    # Empty string resets the briefing slot for that vertical.
    stage_map, brief_map = ctem_metrics._apply_overrides(
        stage_map, brief_map, "fimi", {"briefing": ""}
    )
    assert brief_map is None


def test_apply_overrides_ignores_unknown_keys():
    stage_map, brief_map = ctem_metrics._apply_overrides(None, None, "cyber", {"bogus": "X"})
    assert stage_map is None
    assert brief_map is None


def test_apply_overrides_reset_after_legacy_migration_keeps_other_verticals():
    # A legacy flat override applied to ALL verticals. Resetting cyber's scoping
    # must drop only cyber and keep the (now-migrated) fimi/fraud entries instead
    # of silently falling back to the legacy entry.
    stage_map, _ = ctem_metrics._apply_overrides(
        {"scoping": "A"}, None, "cyber", {"scoping": None}
    )
    assert "cyber" not in (stage_map or {})
    assert stage_map["fimi"]["scoping"] == "A"
    assert stage_map["fraud"]["scoping"] == "A"


def test_apply_overrides_tolerates_non_dict_brief_map():
    # A malformed / non-dict briefing override must not raise (mirrors the
    # stage-map guard); it is treated as empty and the patch still applies.
    stage_map, brief_map = ctem_metrics._apply_overrides(
        None, "garbage", "cyber", {"briefing": "B"}
    )
    assert stage_map is None
    assert brief_map == {"cyber": "B"}


# ── Request-id filter (mirrors _apply_overrides' known-key set) ───────────────


def test_requested_override_ids_only_known_keys():
    ids = ctem_metrics._requested_override_ids(
        {"scoping": " A ", "briefing": "B", "bogus": "C", "discovery": None}
    )
    # "bogus" is dropped (apply ignores it) and a null value contributes nothing.
    assert ids == {"A", "B"}


# ── Async resolvers: per-user override -> platform default -> seeded ──────────


def _patch_agent_registry(monkeypatch, registry: dict[str, object]):
    """Patch ``_load_enabled_agent`` to resolve only the ids in *registry*
    (simulating "exists and is enabled"); any other id resolves to None, exactly
    as a missing or disabled agent would."""

    async def fake_load_enabled(db, agent_id):
        if not agent_id:
            return None
        return registry.get(str(agent_id).strip())

    monkeypatch.setattr(ctem_metrics, "_load_enabled_agent", fake_load_enabled)


async def test_resolve_stage_agent_priority_order(monkeypatch):
    user_agent = SimpleNamespace(id="user-agent", enabled=True)
    platform_agent = SimpleNamespace(id="platform-agent", enabled=True)
    seeded_agent = SimpleNamespace(id="seeded-agent", enabled=True)

    _patch_agent_registry(
        monkeypatch, {"user-agent": user_agent, "platform-agent": platform_agent}
    )

    async def fake_seeded(db, stage):
        return seeded_agent

    async def fake_ps(db):
        return SimpleNamespace(
            ctem_stage_agent_ids={"cyber": {"scoping": "platform-agent"}},
            ctem_briefing_agent_ids=None,
        )

    monkeypatch.setattr(ctem_metrics, "_seeded_stage_agent", fake_seeded)
    monkeypatch.setattr(ctem_metrics, "get_platform_settings", fake_ps)

    # 1. A per-user override wins over the platform default and the seeded analyst.
    user = SimpleNamespace(ctem_stage_agent_ids={"cyber": {"scoping": "user-agent"}})
    assert await ctem_metrics.resolve_stage_agent(None, "scoping", "cyber", user) is user_agent

    # 2. No user override -> the platform default is used.
    user_none = SimpleNamespace(ctem_stage_agent_ids=None)
    assert (
        await ctem_metrics.resolve_stage_agent(None, "scoping", "cyber", user_none)
        is platform_agent
    )

    # 3. Neither user nor platform override for this stage -> the seeded analyst.
    assert (
        await ctem_metrics.resolve_stage_agent(None, "discovery", "cyber", user_none)
        is seeded_agent
    )

    # 4. A user override pointing at a disabled/missing agent falls through to the
    #    platform default (and would fall to seeded if that were missing too).
    user_disabled = SimpleNamespace(ctem_stage_agent_ids={"cyber": {"scoping": "ghost"}})
    assert (
        await ctem_metrics.resolve_stage_agent(None, "scoping", "cyber", user_disabled)
        is platform_agent
    )

    # 5. No user passed at all -> platform default, then seeded.
    assert await ctem_metrics.resolve_stage_agent(None, "scoping", "cyber", None) is platform_agent


async def test_resolve_stage_agent_legacy_flat_user_override(monkeypatch):
    user_agent = SimpleNamespace(id="user-agent", enabled=True)
    seeded_agent = SimpleNamespace(id="seeded-agent", enabled=True)
    _patch_agent_registry(monkeypatch, {"user-agent": user_agent})

    async def fake_seeded(db, stage):
        return seeded_agent

    async def fake_ps(db):
        return SimpleNamespace(ctem_stage_agent_ids=None, ctem_briefing_agent_ids=None)

    monkeypatch.setattr(ctem_metrics, "_seeded_stage_agent", fake_seeded)
    monkeypatch.setattr(ctem_metrics, "get_platform_settings", fake_ps)

    # A legacy flat user map (no vertical bucket) resolves for ANY vertical.
    user_flat = SimpleNamespace(ctem_stage_agent_ids={"scoping": "user-agent"})
    assert await ctem_metrics.resolve_stage_agent(None, "scoping", "fimi", user_flat) is user_agent


async def test_resolve_stage_agent_unknown_vertical_defaults_to_cyber(monkeypatch):
    platform_agent = SimpleNamespace(id="platform-agent", enabled=True)
    seeded_agent = SimpleNamespace(id="seeded-agent", enabled=True)
    _patch_agent_registry(monkeypatch, {"platform-agent": platform_agent})

    async def fake_seeded(db, stage):
        return seeded_agent

    async def fake_ps(db):
        return SimpleNamespace(
            ctem_stage_agent_ids={"cyber": {"scoping": "platform-agent"}},
            ctem_briefing_agent_ids=None,
        )

    monkeypatch.setattr(ctem_metrics, "_seeded_stage_agent", fake_seeded)
    monkeypatch.setattr(ctem_metrics, "get_platform_settings", fake_ps)

    # An unknown vertical is normalized to cyber, so the cyber platform default wins.
    assert (
        await ctem_metrics.resolve_stage_agent(None, "scoping", "not-a-vertical", None)
        is platform_agent
    )


async def test_resolve_briefing_agent_priority_order(monkeypatch):
    user_agent = SimpleNamespace(id="ub", enabled=True)
    platform_agent = SimpleNamespace(id="pb", enabled=True)
    seeded_brief = SimpleNamespace(id="sb", enabled=True)
    _patch_agent_registry(monkeypatch, {"ub": user_agent, "pb": platform_agent})

    async def fake_briefing(db):
        return seeded_brief

    async def fake_ps(db):
        return SimpleNamespace(
            ctem_stage_agent_ids=None, ctem_briefing_agent_ids={"cyber": "pb"}
        )

    monkeypatch.setattr(ctem_metrics, "_briefing_agent", fake_briefing)
    monkeypatch.setattr(ctem_metrics, "get_platform_settings", fake_ps)

    # 1. Per-user briefing override wins.
    user = SimpleNamespace(ctem_briefing_agent_ids={"cyber": "ub"})
    assert await ctem_metrics.resolve_briefing_agent(None, "cyber", user) is user_agent

    # 2. No user override -> platform default.
    user_none = SimpleNamespace(ctem_briefing_agent_ids=None)
    assert await ctem_metrics.resolve_briefing_agent(None, "cyber", user_none) is platform_agent

    # 3. A vertical with no platform briefing entry -> seeded briefing analyst.
    assert await ctem_metrics.resolve_briefing_agent(None, "fimi", user_none) is seeded_brief

    # 4. No user passed -> platform default, then seeded.
    assert await ctem_metrics.resolve_briefing_agent(None, "cyber", None) is platform_agent


# ── Agentic-AI gate on the override write path ───────────────────────────────


async def test_require_ctem_agentic_ai_blocks_when_disabled(monkeypatch):
    # Override writes (fork/assign a custom analyst) are an agentic capability,
    # so they must be gated on Enterprise Edition server-side too.
    import app.api.v1.platform as platform_mod
    import app.services.ai_state as ai_state_mod
    from app.api.deps import ENTERPRISE_EDITION_REQUIRED_DETAIL

    async def fake_disabled(db):
        return False

    async def ai_on(db):
        return True

    # AI on, licence lacking: the licence refusal, not the AI one.
    monkeypatch.setattr(ai_state_mod, "is_ai_enabled", ai_on)
    monkeypatch.setattr(platform_mod, "is_agentic_ai_licensed", fake_disabled)
    with pytest.raises(HTTPException) as exc:
        await ctem_metrics._require_ctem_agentic_ai(None)
    assert exc.value.status_code == 403
    assert exc.value.detail == ENTERPRISE_EDITION_REQUIRED_DETAIL


async def test_require_ctem_agentic_ai_refuses_with_the_contract_code_when_ai_is_off(
    monkeypatch,
):
    """AI off is a different refusal from a licence gap (FR-009).

    Collapsing the two sent an administrator to buy a licence they already had,
    and gave clients prose instead of ``detail.code`` to branch on.
    """
    import app.services.ai_state as ai_state_mod

    async def ai_off(db):
        return False

    monkeypatch.setattr(ai_state_mod, "is_ai_enabled", ai_off)
    with pytest.raises(HTTPException) as exc:
        await ctem_metrics._require_ctem_agentic_ai(None)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "ai_disabled"


async def test_require_ctem_agentic_ai_allows_when_enabled(monkeypatch):
    import app.api.v1.platform as platform_mod
    import app.services.ai_state as ai_state_mod

    async def fake_enabled(db):
        return True

    monkeypatch.setattr(ai_state_mod, "is_ai_enabled", fake_enabled)
    monkeypatch.setattr(platform_mod, "is_agentic_ai_licensed", fake_enabled)
    # Must not raise.
    assert await ctem_metrics._require_ctem_agentic_ai(None) is None
