# SPDX-License-Identifier: Apache-2.0

"""Platform feature flags — env-var driven, the env var read once at process start.

Design summary:

- Flags gate unfinished/experimental code paths, platform-wide (no
  per-user/per-org scoping — an accepted simplification).
- Configured via the ``ENABLED_FEATURES`` env var (comma-separated flag
  ids), parsed once into ``settings.enabled_feature_flags``. Requires a
  restart to change — there is no database table and no admin UI toggle
  by design, so regular admins are never shown a half-finished feature.
- Unset/empty ``ENABLED_FEATURES`` means NO flags are enabled (fail
  closed). Missing config must never silently turn on experimental
  behaviour.
- ``FEATURE_FLAG_ALL`` ("*") enables every known flag at once (dev/staging
  convenience).
- ``KNOWN_FEATURE_FLAGS`` is the single source of truth for valid flag
  ids. ``Settings`` validates ``ENABLED_FEATURES`` against this registry
  at startup: an unknown flag id (typo, or a flag that only exists on
  another branch) is logged and ignored rather than crashing the process,
  so switching branches with a stale ``.env`` never bricks boot. Unknown
  ids stay fail-closed — they can never enable behaviour.
- A flag in ``FLAG_CONDITIONS`` also needs a condition of the deployment
  (``EVALUATION``: a mode where AI runs, every mode but ``ai_disabled``;
  ``AGENTIC_CODING``: the copilot or dev mode), checked on every read
  rather than at startup, though the mode does not change within a process.
"""

# Special value that enables every known flag at once.
FEATURE_FLAG_ALL = "*"

# Single source of truth for known flag ids. Add new flags here only, with a
# short comment: owner + condition for removal (feature flags are meant to
# be temporary — remove the flag and this entry once the feature ships).
#
from collections.abc import Callable
from typing import Literal

# owner: platform-core — FLOW_BUILDER gates the visual flow builder while it is
#   not announced externally: the flow library tab, the builder canvas, their ⌘K
#   commands, the /api/v1/flows OpenAPI operations and two docs pages. NOT the
#   whole of ``/flows`` (its Agent-to-agent map, with the Schedule view, and
#   its Fleets tab do not depend on the builder; only the Flows tab is hidden)
#   and NOT the engine: the REST surface stays mounted (the agent-creation
#   wizard reads the node catalog) and existing flows keep running. See
#   dev-docs/flow-builder-feature-flag.md. Remove once the feature is announced.
# owner: platform-core — DEEP_INVESTIGATION gates Deep Investigation
#   while it is not production-ready: the /api/v1/investigations
#   API, the chat composer's investigation mode, the ``deep_investigation``
#   built-in tool, the seeded Deep Investigation Agent (and the CTEM
#   Assistant's handover to it) and every UI entry point. Off, an agent that
#   carries the tool keeps it in its configuration but is never handed it.
#   Remove once the feature is ready to ship.
# owner: platform-core — AGENT_WIZARD_ASSIST gates the agent wizard's assistant,
#   an experiment: a prompt bar under /agents/new/wizard whose answer is played
#   onto the wizard step by step (POST /api/v1/ai/agent-wizard/assist and the
#   bar). Remove once the drive-UI epic replaces it or it ships.
# owner: video-studio — VIDEO_STUDIO gates the Video Studio: a render
#   needs a worker sized for Chromium and ffmpeg, so a deployment turns
#   it on when its worker is. Off: the /api/v1/video-studio API, public video
#   links, the ``video_studio`` built-in tool, the seeded Video Production skill
#   and video templates, the Settings video / music models and every UI entry
#   point. Stored videos are kept. See dev-docs/video-studio-feature-flag.md.
#   Remove if the Video Studio no longer needs a worker of its own size.
# owner: platform-core — DASHBOARD_STUDIO gates custom dashboards as a whole:
#   the dashboards pages and API, the ``custom_dashboards`` built-in tool, the
#   studio, live OpenCTI / OpenAEV / OpenCRQ widgets, widget packs, publishing
#   to OpenCTI / OpenAEV and published links. Off: nothing of it is offered,
#   its routes answer 404, the tool is never built, a home dashboard is not
#   served; what is stored is kept, so turning it back on restores it. Remove
#   once custom dashboards are announced.
# owner: evaluations — EVALUATION gates evaluation, on in every mode where AI
#   runs (xtm_one, copilot, dev; never ai_disabled) with the flag set
#   (``services/evaluation/availability.py``, dev-docs/mode-split.md S3): its
#   API, pages, scheduled and automatic runs, persona rewrites, agent tools and
#   seeded benchmark. Stored data is kept. The engine has a gate of its own
#   (``engine_accepts``): a Platform Health launch runs whatever the mode (#8257). See
#   dev-docs/evaluation-feature-flag.md.
#   Remove once evaluation ships on every deployment where AI runs.
# owner: platform-core - AGENTIC_CODING gates the agentic coding labs: one
#   Kubernetes pod per lab, created and reaped by the platform
#   (``services/sandbox_workspace/labs.py``), behind the four sandbox service
#   tools. Filigran's own use in copilot or dev mode only, never in XTM One mode,
#   and off until ``AGENTIC_LABS_NAMESPACE`` names the namespace it may use. See
#   dev-docs/reference/agentic-labs.md. Remove if labs ship to every deployment.
# owner: platform-core - MOBILE_COMPANION gates the mobile companion while it
#   is not announced: the phone app shell, its Today / Inbox / Ask / Share
#   pages, linking a phone and its push notifications, the companion PIN lock
#   and the ``phone`` built-in tool. Off: the /api/v1/mobile-devices,
#   /api/v1/mobile and /api/v1/app-lock routes answer 404 and are not
#   documented, nothing is pushed to a phone, the PIN lock never refuses a
#   request, and a phone gets the web app. Stored phones and PINs are kept.
#   See dev-docs/mobile-companion-feature-flag.md. Remove once it is announced.
FeatureFlag = Literal[
    "AGENTIC_CODING",
    "AGENT_WIZARD_ASSIST",
    "DASHBOARD_STUDIO",
    "DEEP_INVESTIGATION",
    "EVALUATION",
    "FLOW_BUILDER",
    "MOBILE_COMPANION",
    "VIDEO_STUDIO",
]

# Runtime-checkable mirror of ``FeatureFlag`` (a ``Literal`` isn't introspectable
# at runtime in a convenient way for validation, so this set is maintained by
# hand alongside it — keep both in sync when adding/removing a flag).
KNOWN_FEATURE_FLAGS: frozenset[str] = frozenset(
    {
        "AGENTIC_CODING",
        "AGENT_WIZARD_ASSIST",
        "DASHBOARD_STUDIO",
        "DEEP_INVESTIGATION",
        "EVALUATION",
        "FLOW_BUILDER",
        "MOBILE_COMPANION",
        "VIDEO_STUDIO",
    }
)


def _copilot_like() -> bool:
    from app.core.config import settings

    return settings.is_copilot_like


def _ai_mode() -> bool:
    """A mode where AI runs: every mode but ``ai_disabled`` (``config.AI_MODES``)."""
    from app.core.config import AI_MODES, settings

    return settings.platform_mode in AI_MODES


#: A flag that also needs a condition of the deployment, checked on every read:
#: while it fails, the flag is off whatever ``ENABLED_FEATURES`` says, ``*``
#: included. ``EVALUATION``: a generic AI capability, on in both products
#: (XTM One and Copilot, dev-docs/mode-split.md S3), never in ``ai_disabled``.
#: ``AGENTIC_CODING``: the labs are Filigran's own tooling, never an XTM One feature.
FLAG_CONDITIONS: dict[str, Callable[[], bool]] = {"EVALUATION": _ai_mode, "AGENTIC_CODING": _copilot_like}


def _holds(flag: str) -> bool:
    condition = FLAG_CONDITIONS.get(flag)
    return condition is None or condition()


def is_feature_enabled(flag: FeatureFlag) -> bool:
    """Return True if ``flag`` is enabled for this platform instance.

    Backed by ``settings.enabled_feature_flags`` (parsed from the
    ``ENABLED_FEATURES`` env var, unknown ids dropped). The ``"*"``
    wildcard only enables ids present in ``KNOWN_FEATURE_FLAGS`` —
    consistent with ``resolve_enabled_feature_flags()`` — so an unknown
    id can never evaluate as enabled. A flag with a ``FLAG_CONDITIONS``
    entry is also off while its condition fails. Imports ``app.core.config``
    lazily because ``config.py`` imports this module's registry to validate
    the env var at startup — a top-level import here would be circular.
    """
    from app.core.config import settings

    enabled = settings.enabled_feature_flags
    listed = flag in KNOWN_FEATURE_FLAGS if FEATURE_FLAG_ALL in enabled else flag in enabled
    return listed and _holds(flag)


def feature_disabled_detail(flag: FeatureFlag) -> str:
    """The refusal a disabled flag answers with."""
    return f"Feature '{flag}' is disabled on this platform."


def require_feature(flag: FeatureFlag):
    """FastAPI dependency that 403s when ``flag`` is not enabled.

    Usage::

        @router.get("/some-route", dependencies=[Depends(require_feature("MY_FLAG"))])
    """
    from fastapi import HTTPException, status

    def _dependency() -> None:
        if not is_feature_enabled(flag):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=feature_disabled_detail(flag),
            )

    return _dependency


def require_feature_or_404(flag: FeatureFlag):
    """FastAPI dependency that answers 404 when ``flag`` is not enabled.

    For a surface that must not be known to exist while it is off: the answer
    is the one an unknown route gets, naming no flag.
    """
    from fastapi import HTTPException, status

    def _dependency() -> None:
        if not is_feature_enabled(flag):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    return _dependency


def resolve_enabled_feature_flags() -> list[str]:
    """Return the concrete, sorted list of flag ids in effect for this instance.

    Expands ``FEATURE_FLAG_ALL`` ("*") into every id in ``KNOWN_FEATURE_FLAGS``,
    and leaves out a flag whose ``FLAG_CONDITIONS`` entry fails, so callers
    (e.g. the ``GET /auth/config`` response consumed by the frontend, the
    startup logs) never need to special-case the wildcard or a condition.
    """
    return [flag for flag in sorted(KNOWN_FEATURE_FLAGS) if is_feature_enabled(flag)]
