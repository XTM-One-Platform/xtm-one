# SPDX-License-Identifier: Apache-2.0

"""The platform registration token - the one place it is read and compared.

``PLATFORM_REGISTRATION_TOKEN`` is the shared secret an OpenCTI / OpenAEV /
OpenCRQ platform presents as ``Authorization: Bearer <token>`` to register
with XTM One (``POST /api/v1/platform/register``, which is also its heartbeat:
a platform re-registers every few minutes) and to unregister
(``DELETE /api/v1/platform/register/{platform_identifier}``). It is the only
credential those two routes check, and the registration answer can carry the
Filigran license certificate.

Every read of the setting goes through this module - pinned by
``tests/test_registration_token.py`` - so there is exactly one comparison, and
it is constant-time and fail-closed wherever the token is verified.

The configured value is in one of three states (:func:`registration_token_state`):

* ``unset`` - nothing configured: registration is disabled, no bearer
  authenticates, the empty one included.
* ``published_default`` - the fallback ``docker-compose.yml`` ships. It is
  public, so it authenticates nothing. It is ACCEPTED only to keep an
  existing registration alive: the platforms of an existing deployment are
  configured with it, and refusing their heartbeats would silently cut them
  off. It never registers a new platform, moves a registered one to another
  URL or unregisters one (:data:`PUBLISHED_DEFAULT_REFUSED_DETAIL`): a
  registered platform's URL is a trusted issuer of the cross-platform JWTs
  that sign XTM One users in, so whoever chose it could sign in as anyone.
  Nor does it rename one: the title is what the agents bound to the platform
  read in their system prompt. Nor does it change what a platform declares
  (its intents and business vertical), or clear, downgrade or swap its
  license: only a renewal the validator accepts - newer, same customer and
  instance - replaces it (``platform._registered_license``). It is reported as
  well: a startup warning (:func:`log_registration_token_status`) and a
  critical finding in the admin security audit.
* ``configured`` - a value the operator chose.
"""

from __future__ import annotations

import hmac
import logging
from typing import Literal

from app.core.config import settings

logger = logging.getLogger(__name__)

# The ``docker-compose.yml`` fallback (and, in older releases, the ``.env.sample``
# value). Must equal the compose fallback, or the warning and the audit finding
# stop recognising the value deployments actually run with - pinned by a test.
PUBLISHED_DEFAULT_REGISTRATION_TOKEN = "xtm-default-registration-token"  # noqa: S105 - published sample value, not a secret

REGISTRATION_TOKEN_GENERATE_COMMAND = 'python -c "import secrets; print(secrets.token_urlsafe(32))"'  # noqa: S105 - a command, not a secret

REGISTRATION_DISABLED_DETAIL = (
    "Platform registration is disabled: PLATFORM_REGISTRATION_TOKEN is not set on this XTM One "
    "instance. Set it to a random secret (for example the output of "
    f"{REGISTRATION_TOKEN_GENERATE_COMMAND}) and configure the same value on each OpenCTI, "
    "OpenAEV or OpenCRQ platform that registers with it."
)

PUBLISHED_DEFAULT_REFUSED_DETAIL = (
    "PLATFORM_REGISTRATION_TOKEN is the published default value on this XTM One instance: it "
    "only keeps an already registered platform connected, and cannot register a new platform, "
    "change the URL of a registered one or unregister one. Set it to a random secret (for example "
    f"the output of {REGISTRATION_TOKEN_GENERATE_COMMAND}) here and on each OpenCTI, OpenAEV or "
    "OpenCRQ platform that registers with it."
)

RegistrationTokenState = Literal["unset", "published_default", "configured"]


def configured_registration_token() -> str:
    """The configured token without surrounding whitespace, ``""`` when unset.

    Trimmed because a secret mounted from a file often ends with a newline,
    which no platform can send back in a header; a whitespace-only value is
    therefore unset.
    """
    return (settings.platform_registration_token or "").strip()


def registration_token_state() -> RegistrationTokenState:
    """Which of the three states the configured token is in (see the module docstring)."""
    token = configured_registration_token()
    if not token:
        return "unset"
    if token == PUBLISHED_DEFAULT_REGISTRATION_TOKEN:
        return "published_default"
    return "configured"


def registration_token_matches(presented: str | None) -> bool:
    """Whether *presented* is the configured registration token.

    Fail closed: with no configured token nothing matches, and an empty
    presented token never matches. The comparison is ``hmac.compare_digest``
    over UTF-8 bytes - constant-time, and a header value Starlette decoded as
    latin-1 into non-ASCII characters is a clean mismatch rather than the
    ``TypeError`` ``compare_digest`` raises on non-ASCII ``str``.
    """
    expected = configured_registration_token()
    candidate = (presented or "").strip()
    if not expected or not candidate:
        return False
    return hmac.compare_digest(
        candidate.encode("utf-8", "surrogateescape"),
        expected.encode("utf-8", "surrogateescape"),
    )


def log_registration_token_status() -> None:
    """Say at startup when the token disables registration or protects nothing."""
    state = registration_token_state()
    if state == "published_default":
        logger.warning(
            "PLATFORM_REGISTRATION_TOKEN is set to the published default value from "
            "docker-compose.yml. It is public, so it authenticates nobody: it only keeps the "
            "OpenCTI, OpenAEV or OpenCRQ platforms already registered with this instance "
            "connected, it cannot register a new platform, change a registered platform's URL "
            "or unregister one, and the Filigran license certificate is withheld from registration "
            "answers while it is in use. Generate a random token (%s), set it as "
            "PLATFORM_REGISTRATION_TOKEN here and on every connected platform, then restart them.",
            REGISTRATION_TOKEN_GENERATE_COMMAND,
        )
    elif state == "unset":
        logger.info(
            "PLATFORM_REGISTRATION_TOKEN is not set: platform registration is disabled and "
            "no OpenCTI, OpenAEV or OpenCRQ platform can connect. Set it to a random secret "
            "(%s) and use the same value on each platform to connect them.",
            REGISTRATION_TOKEN_GENERATE_COMMAND,
        )
