# SPDX-License-Identifier: Apache-2.0

"""Whether LLM providers may be managed from the UI and the API.

``MODELS_ENV_PROVIDING_ONLY`` makes the environment the only source of LLM
providers: every provider mutation - the Settings routes and an integration
feeding a provider (``openrouter_llm_provider.adopt_provider``) - asks
:func:`ensure_llm_provider_management_allowed` first.

``BYOLLM_ACTIVATED=true`` says the opposite (the admin owns the providers) and
wins over it, through ``settings.provider_management_env_only``.
"""

from __future__ import annotations

from fastapi import HTTPException


def ensure_llm_provider_management_allowed() -> None:
    """Block UI/API provider management (add / edit / rename / delete) when env-only mode is enabled.

    Despite the legacy single ``MODELS_ENV_PROVIDING_ONLY`` flag wording,
    the guard covers every provider-mutation surface — POST (add), PUT
    (edit), the PUT-based rename path, and DELETE — because env-only
    mode means "providers are the env vars; nothing the UI does can
    stick across a restart". The four verbs in the summary line are
    kept in lockstep with the 403 detail message below, the
    ``.env.sample`` block, the ``models_env_providing_only`` docstring
    in ``core/config.py``, and the amber Settings banner in
    ``Settings.tsx`` so a reader landing here from any one surface sees
    consistent wording (the previous "create / update / delete"
    summary dropped the rename verb that the helper actually guards).

    Gated on ``provider_management_env_only``, not on the raw flag:
    ``BYOLLM_ACTIVATED=true`` hands provider management back to the admin
    even on a deployment that still sets ``MODELS_ENV_PROVIDING_ONLY``.
    """
    from app.core.config import settings as app_settings

    if app_settings.provider_management_env_only:
        raise HTTPException(
            status_code=403,
            detail=(
                "LLM provider management (add, edit, rename, delete) is disabled "
                "because MODELS_ENV_PROVIDING_ONLY=true. Configure providers via "
                "LLM_PROVIDER_* environment variables and restart the backend."
            ),
        )
