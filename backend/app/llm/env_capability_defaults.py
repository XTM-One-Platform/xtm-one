# SPDX-License-Identifier: Apache-2.0

"""The video, speech, music and decision models an operator sets from the environment.

``DEFAULT_<ROLE>_PROVIDER`` names a provider type (``gemini``, ``openai``,
``elevenlabs``, ...) and ``DEFAULT_<ROLE>_MODEL`` the model it serves; video,
speech and music take the provider's built-in model when the model is left
empty (``router.VIDEO_DEFAULTS`` / ``SPEECH_DEFAULTS`` / ``MUSIC_DEFAULTS``), and
``DEFAULT_SPEECH_VOICE`` names the voice. Like ``DEFAULT_IMAGE_*``, the boot seed
(``main._seed_llm_providers_from_env``) writes them to ``PlatformSettings`` on
every boot, the environment winning, or once under ``BYOLLM_ACTIVATED``.

A value the platform cannot serve is never stored: a provider type whose class
does not implement the capability or that has no model to pin (no ``*_MODEL``
and no built-in one), one with no configured provider, and a model or voice
named without its provider are logged at WARNING and the stored choice is kept.

A role's Settings > AI and models > Models section is read-only (:func:`env_locked_fields`,
read by ``settings._env_managed_ga_fields``) while its provider type can serve
it, whatever is configured: the first two refusals unlock it, since no
configuration would make the seed apply them. A provider type that serves the
role but is not configured keeps it locked: the environment owns the section,
and configuring that provider (its ``LLM_PROVIDER_*_API_KEY``, or Settings) lets
the next boot apply it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.config import settings
from app.core.feature_flags import FeatureFlag, is_feature_enabled

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.llm_config import LLMProviderConfig
    from app.models.platform_settings import PlatformSettings

logger = logging.getLogger(__name__)


def _implements(method: str) -> Callable[[str], bool]:
    """Whether a provider type's class overrides ``BaseLLMProvider.<method>``."""

    def serves(provider_type: str) -> bool:
        from app.llm.base import BaseLLMProvider
        from app.llm.router import PROVIDER_MAP

        cls = PROVIDER_MAP.get(provider_type)
        return cls is not None and getattr(cls, method) is not getattr(BaseLLMProvider, method)

    return serves


def _built_in(table: str) -> Callable[[str], str | None]:
    """The model a provider type starts with, read from the router's ``table``."""

    def model(provider_type: str) -> str | None:
        from app.llm import router

        return getattr(router, table).get(provider_type)

    return model


def _serves_decisions(provider_type: str) -> bool:
    from app.services.flow_classifier import decision_provider_types

    return provider_type in decision_provider_types()


@dataclass(frozen=True)
class CapabilityRole:
    """One platform-wide model choice the environment can pin."""

    #: Stable id, stored by the model catalog announcements.
    key: str
    #: What the provider must serve, as the logs say it.
    capability: str
    #: What the logs say is kept when the environment cannot be applied.
    label: str
    #: ``DEFAULT_VIDEO`` -> ``DEFAULT_VIDEO_PROVIDER`` / ``DEFAULT_VIDEO_MODEL``.
    env_prefix: str
    model_attr: str
    provider_id_attr: str
    serves: Callable[[str], bool]
    built_in: Callable[[str], str | None]
    #: The provider type column, when the role stores one.
    provider_attr: str | None = None
    voice_attr: str | None = None
    #: The feature the role's model serves alone: while its flag is off the
    #: users cannot use the model, so it is never announced to them.
    feature_flag: FeatureFlag | None = None

    @property
    def provider_env(self) -> str:
        return f"{self.env_prefix}_PROVIDER"

    @property
    def model_env(self) -> str:
        return f"{self.env_prefix}_MODEL"

    @property
    def voice_env(self) -> str | None:
        return f"{self.env_prefix}_VOICE" if self.voice_attr else None

    @property
    def fields(self) -> frozenset[str]:
        """The ``PlatformSettings`` fields the role writes, saved together by Settings."""
        return frozenset(
            attr
            for attr in (self.provider_attr, self.provider_id_attr, self.model_attr, self.voice_attr)
            if attr
        )


ROLES: tuple[CapabilityRole, ...] = (
    CapabilityRole(
        key="video",
        capability="video generation",
        label="the video model",
        env_prefix="DEFAULT_VIDEO",
        provider_attr="video_provider",
        provider_id_attr="video_provider_id",
        model_attr="video_model",
        serves=_implements("generate_video"),
        built_in=_built_in("VIDEO_DEFAULTS"),
        feature_flag="VIDEO_STUDIO",
    ),
    CapabilityRole(
        key="speech",
        capability="speech generation",
        label="the speech model",
        env_prefix="DEFAULT_SPEECH",
        provider_attr="speech_provider",
        provider_id_attr="speech_provider_id",
        model_attr="speech_model",
        voice_attr="speech_voice",
        serves=_implements("synthesize_speech"),
        built_in=_built_in("SPEECH_DEFAULTS"),
    ),
    CapabilityRole(
        key="music",
        capability="music generation",
        label="the music model",
        env_prefix="DEFAULT_MUSIC",
        provider_attr="music_provider",
        provider_id_attr="music_provider_id",
        model_attr="music_model",
        serves=_implements("generate_music"),
        built_in=_built_in("MUSIC_DEFAULTS"),
        feature_flag="VIDEO_STUDIO",
    ),
    CapabilityRole(
        key="decision",
        capability="decision models",
        label="the decision model",
        env_prefix="DEFAULT_DECISION",
        provider_id_attr="decision_provider_id",
        model_attr="decision_model",
        serves=_serves_decisions,
        # Decision models are named with their provider: no provider class
        # declares one to start with.
        built_in=lambda _provider_type: None,
    ),
)


def _env(name: str | None) -> str:
    return str(getattr(settings, name.lower(), "") or "").strip() if name else ""


def _provider_type(role: CapabilityRole) -> str:
    return _env(role.provider_env).lower()


def _named(role: CapabilityRole) -> bool:
    return bool(_provider_type(role) or _env(role.model_env) or _env(role.voice_env))


def env_sets_any() -> bool:
    """Whether the environment names anything for one of the roles."""
    return any(_named(role) for role in ROLES)


def _pinned_model(role: CapabilityRole, provider_type: str) -> str | None:
    """The model the environment pins *role* to: its ``*_MODEL``, else the provider type's built-in one."""
    return _env(role.model_env) or role.built_in(provider_type) or None


def _type_refusal(role: CapabilityRole, provider_type: str) -> str | None:
    """Why no configuration of *provider_type* could serve *role*, or ``None``."""
    if not role.serves(provider_type):
        return f"{role.provider_env}={provider_type} is not a provider type that serves {role.capability}"
    if not _pinned_model(role, provider_type):
        return (
            f"{role.provider_env}={provider_type} has no built-in model for {role.capability}: "
            f"set {role.model_env}"
        )
    return None


def _pins(role: CapabilityRole) -> bool:
    """Whether the environment names a provider type able to serve *role*."""
    provider_type = _provider_type(role)
    return bool(provider_type) and _type_refusal(role, provider_type) is None


def env_locked_fields() -> set[str]:
    """The fields the boot seed re-applies on every boot, hence read-only in Settings.

    A role is locked while its provider type can serve it. A model or voice
    named alone, and a provider type the seed refuses whatever is configured,
    lock nothing: the boot stores none of them.
    """
    locked: set[str] = set()
    for role in ROLES:
        if _pins(role):
            locked |= role.fields
    return locked


def stores_a_choice(ps: PlatformSettings) -> bool:
    """Whether *ps* holds a model chosen for one of the roles."""
    return any(getattr(ps, role.model_attr, None) for role in ROLES)


@dataclass(frozen=True)
class PinnedChoice:
    """A capability model the environment pins, as ``PlatformSettings`` holds it."""

    key: str
    model: str
    provider_id: object | None
    #: The provider type column, for a role that stores one.
    provider: str | None


def pinned_choices(ps: PlatformSettings) -> list[PinnedChoice]:
    """The image model and every role the environment pins now, as *ps* stores them.

    Image is seeded by ``main`` (its compound base URL predates this module)
    but pinned the same way, by ``DEFAULT_IMAGE_MODEL``. A role counts only
    while *ps* holds what the environment pins: one the seed could not apply
    (no configured provider of that type) still holds the stored choice, which
    the environment did not make. Nothing is pinned under ``BYOLLM_ACTIVATED``:
    the environment only furnished the install once.
    """
    if settings.byollm_activated:
        return []
    pinned: list[PinnedChoice] = []
    if settings.default_image_model.strip() and ps.image_model:
        pinned.append(PinnedChoice("image", ps.image_model, ps.image_provider_id, ps.image_provider))
    for role in ROLES:
        if not _pins(role):
            continue
        provider_type = _provider_type(role)
        model = getattr(ps, role.model_attr, None)
        provider = getattr(ps, role.provider_attr, None) if role.provider_attr else None
        if model != _pinned_model(role, provider_type) or (role.provider_attr and provider != provider_type):
            continue
        pinned.append(PinnedChoice(role.key, model, getattr(ps, role.provider_id_attr, None), provider))
    return pinned


def capability_offered(key: str) -> bool:
    """Whether the users can use capability *key*: the feature its role serves, when it names one, is on.

    The video and music models serve the Video Studio alone (``VIDEO_STUDIO``);
    image, speech and decision serve the whole platform.
    """
    role = next((role for role in ROLES if role.key == key), None)
    return role is None or role.feature_flag is None or is_feature_enabled(role.feature_flag)


async def seed_env_capability_defaults(
    db: AsyncSession,
    ps: PlatformSettings,
    *,
    may_seed: Callable[[str], bool],
) -> None:
    """Write the roles the environment sets onto *ps*; the caller commits.

    *may_seed* is the boot seed's BYOLLM gate, asked with the role's provider
    variable before a role is written.
    """
    roles = [role for role in ROLES if _named(role)]
    if not roles:
        return

    from sqlalchemy import select

    from app.models.llm_config import LLMProviderConfig

    configs = list((await db.execute(select(LLMProviderConfig))).scalars().all())
    for role in roles:
        provider_type = _provider_type(role)
        if not provider_type:
            named = ", ".join(f"{env}={_env(env)}" for env in (role.model_env, role.voice_env) if _env(env))
            logger.warning(
                "%s is set without %s: %s is left unchanged. Name the provider type that serves it.",
                named,
                role.provider_env,
                role.label,
            )
            continue
        if not may_seed(role.provider_env):
            continue
        target = _target(role, provider_type, configs)
        if target is None:
            continue
        config, model = target
        if role.provider_attr:
            setattr(ps, role.provider_attr, provider_type)
        setattr(ps, role.provider_id_attr, config.id)
        setattr(ps, role.model_attr, model)
        if role.voice_attr:
            setattr(ps, role.voice_attr, _env(role.voice_env) or None)


def _target(
    role: CapabilityRole, provider_type: str, configs: list[LLMProviderConfig]
) -> tuple[LLMProviderConfig, str] | None:
    """The config and model *role* is pinned to, or ``None`` (logged) when it cannot be served."""
    from app.llm.router import choose_provider_config

    if refusal := _type_refusal(role, provider_type):
        logger.warning("%s. %s is left unchanged.", refusal, role.label.capitalize())
        return None
    model = _pinned_model(role, provider_type) or ""
    # The env-seeded config of that type first: the environment that names the
    # provider is the one that holds its key.
    env_seeded = [cfg for cfg in configs if cfg.provider == provider_type and cfg.is_from_env]
    config = choose_provider_config(env_seeded or configs, provider_type, model)
    if config is None:
        logger.warning(
            "%s=%s names no configured provider (set LLM_PROVIDER_%s_API_KEY or add it in Settings > AI "
            "and models > Models): %s is left unchanged.",
            role.provider_env,
            provider_type,
            provider_type.upper(),
            role.label,
        )
        return None
    return config, model
