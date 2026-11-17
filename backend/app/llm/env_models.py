# SPDX-License-Identifier: Apache-2.0

"""Model catalogs of env-seeded LLM providers.

Single source of truth for how ``LLM_PROVIDER_{PROVIDER}_MODELS`` is read and
applied. Shared by the boot seed (``main._seed_llm_providers_from_env``) and
the settings API (``update_llm_provider`` / the provider serializer) so the
two can never disagree on what "env-locked" means for a provider's models.

Two regimes, selected by ``LLM_PROVIDER_MODELS_EDITABLE``:

* **Pinned** (default) — the env list *is* the catalog: it overwrites
  ``available_models`` / ``default_model`` on every boot and the Settings UI
  hides the model editor (historical behavior).
* **Editable** — the env list is a *baseline*: seeded on first boot, then
  delta-merged on every restart by :func:`merge_env_models` so a fleet
  operator can still roll out or retire models across every install while
  every other admin edit sticks. The model editor is unlocked; the API key
  stays env-managed either way.

Pure stdlib on purpose (no DB, no settings import at module load) so both
the seed and the API can import it cheaply and the merge is unit-testable.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

ENV_PREFIX = "LLM_PROVIDER_"
MODELS_SUFFIX = "_MODELS"


def models_env_var(provider: str) -> str:
    """Name of the env var carrying *provider*'s model list."""
    return f"{ENV_PREFIX}{provider.upper()}{MODELS_SUFFIX}"


def parse_models_csv(raw: str | None) -> list[str]:
    """Comma-separated env value -> ordered, de-duplicated, stripped list.

    Blank entries are dropped, so ``"a, ,b"`` -> ``["a", "b"]`` and a value of
    only separators / whitespace parses to ``[]`` (treated as unset by
    :func:`env_models_for`).
    """
    if not raw:
        return []
    seen: set[str] = set()
    models: list[str] = []
    for part in raw.split(","):
        model = part.strip()
        if model and model not in seen:
            seen.add(model)
            models.append(model)
    return models


def env_models_for(
    provider: str, environ: Mapping[str, str] | None = None
) -> list[str] | None:
    """The env-declared catalog for *provider*, or ``None`` when unset.

    ``None`` (unset / blank) means the seed must not touch the stored
    catalog at all — the historical "``*_MODELS`` optional" contract.
    """
    env = os.environ if environ is None else environ
    return parse_models_csv(env.get(models_env_var(provider), "")) or None


def merge_env_models(
    current: list[str] | None,
    previous_env: list[str] | None,
    env: list[str],
) -> list[str]:
    """Delta-merge the env baseline *env* into the admin-curated *current* list.

    * models newly present in *env* (vs. *previous_env*) are appended;
    * models dropped from *env* (vs. *previous_env*) are removed;
    * everything else in *current* is preserved — both the models an admin
      enabled on top of the baseline and the baseline models they disabled.

    *previous_env* is the snapshot applied at the last boot
    (``LLMProviderConfig.env_models``). ``None`` — a row that predates the
    snapshot, or a baseline introduced on an already-curated provider — is
    treated as "nothing was env-applied before", so the whole baseline
    counts as newly added and nothing is removed. On the upgrade path this
    yields exactly the pinned list (the stored catalog *was* the env list),
    and on an already-curated provider it is purely additive rather than
    silently discarding the admin's selection.

    Order: *current* order first (minus removals), then additions in env
    order, so the UI does not reshuffle on every boot.
    """
    prev = set(previous_env or [])
    env_set = set(env)
    removed = prev - env_set
    kept = [m for m in (current or []) if m not in removed]
    kept_set = set(kept)
    added = [m for m in env if m not in prev and m not in kept_set]
    return kept + added


def resolve_default_model(
    current_default: str | None, available: list[str]
) -> str | None:
    """Keep *current_default* while it is still offered, else the first model."""
    if current_default and current_default in available:
        return current_default
    return available[0] if available else None


def models_env_locked(config: Any, app_settings: Any | None = None) -> bool:
    """True when *config*'s model catalog is owned by the environment.

    Locked = seeded from env AND the operator has not opted into admin
    curation, with either ``LLM_PROVIDER_MODELS_EDITABLE=true`` (catalogs
    only) or ``BYOLLM_ACTIVATED=true`` (the whole LLM configuration) — the
    two are resolved into ``settings.provider_models_admin_curated``.
    Deliberately keyed on those operator-controlled booleans rather than on
    whether
    ``LLM_PROVIDER_{P}_MODELS`` happens to be set: an env-seeded provider
    has always been presented as read-only, and the Env badge must stay
    predictable across every provider on the install.

    The built-in TEI embedding row (``BUILTIN_EMBEDDING_SERVER_*``) is the
    one exception that stays locked even in editable mode: its own seed
    pins ``available_models`` to the single served model on every boot, so
    offering an editor there would be a phantom edit.

    ``MODELS_ENV_PROVIDING_ONLY`` is not consulted here — it blocks every
    provider mutation upstream (``ensure_llm_provider_management_allowed``)
    and the UI hides every control under it; this predicate only refines
    what happens once that gate is passed.
    """
    if not getattr(config, "is_from_env", False):
        return False
    if app_settings is None:
        from app.core.config import settings as app_settings
    if (
        getattr(config, "provider", None) == "tei"
        and app_settings.builtin_embedding_server_base_url.strip()
        and app_settings.builtin_embedding_server_model.strip()
    ):
        return True
    return not app_settings.provider_models_admin_curated
