# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""A2A partner access tokens — the whole vocabulary in one place.

Everything that defines what an A2A partner credential *is* lives here: the
two claims that confine it, and the single function that mints it. Keeping
the pair in one module is deliberate — split across layers, the claims and
the minter can only be reasoned about together by reading both.

The authorization server owns the endpoint, client authentication and the
response envelope. It does not own what an A2A token looks like — that is this
feature's vocabulary, and it belongs next to the feature.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.core.config import settings

#: Minutes an A2A partner token stays valid. Deliberately the same figure the
#: authorization server uses for its own access tokens: a partner polling a
#: long task re-authenticates on the same cadence an MCP client does, and a
#: separate figure would be a second thing to keep in step for no benefit.
A2A_TOKEN_EXPIRE_MINUTES = 60

#: The two claims that confine a partner credential, and why each is needed.
#:
#: A partner's token carries the exposure's BOUNDARY user as ``sub``, so
#: anything authenticating on ``sub`` alone would otherwise treat it as that
#: user's own session. Each claim closes a different surface:
#:
#: - ``A2A_TOKEN_TYPE`` closes ``/mcp/*``, because the MCP resource server
#:   admits only its own token type and refuses every other kind outright;
#: - ``A2A_TOKEN_SCOPE`` closes ``/api/v1/*``, because the platform session
#:   dependency refuses any token carrying a ``scope`` claim on presence alone
#:   ("any future scoped token is closed by default").
#:
#: The direction matters: both are positive properties of the TOKEN, not
#: deny-list entries on the surfaces it must not reach. Relying on each surface
#: to exclude an enumerated list of token kinds leaves the next surface open;
#: making the token intrinsically unusable does not. Without them a partner
#: credential reaches the exposure endpoints themselves, and could expose
#: further agents and mint further credentials.
#:
#: ``A2A_TOKEN_TYPE`` must stay distinct from every other token type the
#: platform mints — ``test_a2a_oauth_client_credentials`` asserts that.
A2A_TOKEN_TYPE = "a2a_client"  # noqa: S105
A2A_TOKEN_SCOPE = "a2a:task"  # noqa: S105


def create_a2a_access_token(subject: str, *, client_id: str) -> str:
    """Mint an A2A partner token, confined to one agent's task surface.

    The two confinement claims are written HERE, together, and this is the
    only function that mints this kind of token. That co-location is the
    guarantee: both are required, the ``type`` closing ``/mcp/*`` and the
    ``scope`` closing ``/api/v1/*``, and a token carrying one without the
    other silently reopens the surface the missing claim was closing.
    """
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "iss": settings.base_url.rstrip("/"),
        "iat": now,
        "exp": now + timedelta(minutes=A2A_TOKEN_EXPIRE_MINUTES),
        "type": A2A_TOKEN_TYPE,
        "scope": A2A_TOKEN_SCOPE,
        "client_id": client_id,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
