"""Env-seeded provider model catalogs: baseline merge + lock predicate.

``app.llm.env_models`` is the single source of truth shared by the boot seed
(``main._seed_llm_providers_from_env``) and the settings API. These tests pin
the pure helpers, and source-inspect the seed (the backend suite has no
real-DB startup harness, same convention as ``test_settings_llm_providers``).
"""

from types import SimpleNamespace

from app.llm.env_models import (
    env_models_for,
    merge_env_models,
    models_env_locked,
    models_env_var,
    parse_models_csv,
    resolve_default_model,
)


def _settings(**overrides):
    """A stand-in for ``app.core.config.settings``.

    ``provider_models_admin_curated`` is computed here the way the real
    property computes it, so these cases exercise the same resolution the
    predicate reads (``LLM_PROVIDER_MODELS_EDITABLE`` or ``BYOLLM_ACTIVATED``
    — the BYOLLM side is covered in ``test_byollm_llm_ownership.py``).
    """
    base = {
        "llm_provider_models_editable": False,
        "byollm_activated": False,
        "builtin_embedding_server_base_url": "",
        "builtin_embedding_server_model": "",
    }
    base.update(overrides)
    base["provider_models_admin_curated"] = (
        base["llm_provider_models_editable"] or base["byollm_activated"]
    )
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_models_env_var_uppercases_provider():
    assert models_env_var("anthropic") == "LLM_PROVIDER_ANTHROPIC_MODELS"
    assert models_env_var("azure_openai") == "LLM_PROVIDER_AZURE_OPENAI_MODELS"


def test_parse_models_csv_strips_dedupes_and_drops_blanks():
    assert parse_models_csv(" a , b,,a , c ") == ["a", "b", "c"]
    assert parse_models_csv("") == []
    assert parse_models_csv(None) == []
    assert parse_models_csv(" , ,") == []


def test_env_models_for_reads_the_provider_var_and_treats_blank_as_unset():
    env = {
        "LLM_PROVIDER_ANTHROPIC_MODELS": "claude-sonnet-5, claude-haiku-4-5",
        "LLM_PROVIDER_OPENAI_MODELS": "  ,  ",
    }
    assert env_models_for("anthropic", env) == ["claude-sonnet-5", "claude-haiku-4-5"]
    # Blank / separator-only -> unset, so the seed leaves the catalog alone.
    assert env_models_for("openai", env) is None
    assert env_models_for("gemini", env) is None


# ---------------------------------------------------------------------------
# Delta merge
# ---------------------------------------------------------------------------


def test_merge_first_boot_yields_the_baseline():
    assert merge_env_models(None, None, ["a", "b"]) == ["a", "b"]
    assert merge_env_models([], None, ["a", "b"]) == ["a", "b"]


def test_merge_upgrade_path_matches_the_historical_pin():
    """Rows that predate the snapshot were pinned to the env list on every
    boot, so ``current == env`` and a ``None`` snapshot must reproduce the
    pin exactly (nothing appears or disappears on the first editable boot)."""
    assert merge_env_models(["a", "b"], None, ["a", "b"]) == ["a", "b"]


def test_merge_none_snapshot_is_additive_on_a_curated_catalog():
    """A baseline introduced on an already-curated provider must not discard
    the admin's selection: with no previous snapshot nothing counts as
    retired, the whole baseline counts as new."""
    assert merge_env_models(["x", "a"], None, ["a", "b"]) == ["x", "a", "b"]


def test_merge_preserves_admin_edits_when_env_is_unchanged():
    env = ["a", "b", "c"]
    # Admin disabled ``b`` and enabled ``z`` on top of the baseline.
    assert merge_env_models(["a", "c", "z"], env, env) == ["a", "c", "z"]


def test_merge_propagates_env_additions_and_retirements_only():
    previous = ["a", "b", "c"]
    current = ["a", "c", "z"]  # admin removed b, added z
    env = ["a", "c", "d"]  # operator retired b (already gone) and added d
    assert merge_env_models(current, previous, env) == ["a", "c", "z", "d"]


def test_merge_retires_a_model_the_admin_kept():
    previous = ["a", "b"]
    current = ["a", "b", "z"]
    env = ["a"]  # operator retired b
    assert merge_env_models(current, previous, env) == ["a", "z"]


def test_merge_does_not_reintroduce_an_admin_removed_baseline_model():
    previous = ["a", "b"]
    current = ["a"]  # admin disabled b
    assert merge_env_models(current, previous, ["a", "b"]) == ["a"]


def test_merge_readds_a_model_the_operator_reintroduces():
    """Removed from the env then added back: the second appearance counts as
    a new rollout and lands even if the admin had it disabled before."""
    assert merge_env_models(["a"], ["a"], ["a", "b"]) == ["a", "b"]


def test_merge_keeps_current_order_then_appends_env_additions_in_env_order():
    assert merge_env_models(["c", "a"], ["a", "c"], ["a", "c", "e", "d"]) == [
        "c",
        "a",
        "e",
        "d",
    ]


def test_resolve_default_model_keeps_a_still_available_default():
    assert resolve_default_model("b", ["a", "b"]) == "b"
    assert resolve_default_model("gone", ["a", "b"]) == "a"
    assert resolve_default_model(None, ["a"]) == "a"
    assert resolve_default_model("a", []) is None


# ---------------------------------------------------------------------------
# Lock predicate
# ---------------------------------------------------------------------------


def test_lock_only_applies_to_env_seeded_providers():
    ui_row = SimpleNamespace(is_from_env=False, provider="anthropic")
    assert models_env_locked(ui_row, _settings()) is False
    assert models_env_locked(ui_row, _settings(llm_provider_models_editable=True)) is False
    legacy_row = SimpleNamespace(is_from_env=None, provider="anthropic")
    assert models_env_locked(legacy_row, _settings()) is False


def test_lock_is_the_default_and_is_lifted_by_the_editable_flag():
    env_row = SimpleNamespace(is_from_env=True, provider="anthropic")
    assert models_env_locked(env_row, _settings()) is True
    assert models_env_locked(env_row, _settings(llm_provider_models_editable=True)) is False


def test_lock_falls_back_to_the_live_settings_object(monkeypatch):
    from app.core.config import settings as app_settings

    env_row = SimpleNamespace(is_from_env=True, provider="anthropic")
    monkeypatch.setattr(app_settings, "llm_provider_models_editable", False)
    assert models_env_locked(env_row) is True
    monkeypatch.setattr(app_settings, "llm_provider_models_editable", True)
    assert models_env_locked(env_row) is False


def test_builtin_tei_embedding_row_stays_locked_in_editable_mode():
    """``_seed_xtm_embedding_from_env`` pins the TEI row to its single served
    model on every boot, so an editor there would be a phantom edit."""
    tei = SimpleNamespace(is_from_env=True, provider="tei")
    pinned = _settings(
        llm_provider_models_editable=True,
        builtin_embedding_server_base_url="http://embed:8080",
        builtin_embedding_server_model="bge-m3",
    )
    assert models_env_locked(tei, pinned) is True
    # A TEI row that is NOT backed by the built-in seed follows the flag.
    assert models_env_locked(tei, _settings(llm_provider_models_editable=True)) is False


# ---------------------------------------------------------------------------
# Boot seed contract (source inspection)
# ---------------------------------------------------------------------------


def _seed_source() -> str:
    import inspect

    from app import main as app_main

    return inspect.getsource(app_main._seed_llm_providers_from_env)


def _seed_loop_source() -> str:
    source = _seed_source()
    start = source.find("for provider_name, api_key in providers_to_seed:")
    assert start > 0, "provider seed loop not found"
    end = source.find("_sync_compound_provider_base_url", start)
    assert end > 0
    block = source[start:end]
    return "\n".join(
        line for line in block.splitlines() if not line.lstrip().startswith("#")
    )


def test_seed_uses_the_shared_env_models_helpers():
    """The seed must read ``*_MODELS`` and merge through ``app.llm.env_models``
    (never an inline ``split(",")``), so it can't drift from the API's lock
    predicate on what the env var means."""
    code = _seed_loop_source()
    assert "env_models_for(provider_name)" in code
    assert "merge_env_models(" in code
    assert "resolve_default_model(" in code
    assert 'split(",")' not in code


def test_seed_branches_on_the_editable_flag_and_snapshots_the_baseline():
    # The resolved predicate, not the raw flag: the seed and the lock
    # predicate must answer "who owns this catalog" the same way, BYOLLM
    # included.
    assert "settings.provider_models_admin_curated" in _seed_source()
    code = _seed_loop_source()
    # Pinned regime keeps the historical overwrite ...
    assert "existing.available_models = env_models" in code
    # ... and both regimes record the list that was applied.
    assert "existing.env_models = env_models" in code
    assert "env_models=env_models" in code  # new rows


def test_seed_cleans_references_to_models_it_drops():
    """A model the seed removes from a catalog (retired from the env var, or
    reverted by the pin) must be cleaned off agents / preferences / platform
    defaults, the same way a UI shrink and the orphaned-provider prune do."""
    code = _seed_loop_source()
    assert "_cleanup_orphaned_model_references(" in code


def test_settings_flag_defaults_off():
    """Opt-in by design: an upgrade must not silently unlock model editors."""
    from app.core.config import Settings

    assert Settings.model_fields["llm_provider_models_editable"].default is False
