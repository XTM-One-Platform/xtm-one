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

"""The actor an A2A audit entry is attributed to.

``log_audit`` takes a ``User``, but external work has no platform user making
the request: the caller is a partner holding an OAuth client credential. Every
A2A audit entry is therefore anchored on the exposure's **run-as user** — the
identity the work actually executes as, and a NOT NULL column — while the
partner is named in the actor's email slot so an operator can tell which
registration acted.

This exists as one helper rather than an anonymous class built at each call
site so that the shape ``log_audit`` relies on is stated once. ``log_audit``
reads exactly ``id`` and ``email``; a dataclass means a future attribute access
fails loudly here instead of at one of four inline definitions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuditActor:
    """Stand-in for the ``User`` an audit entry is attributed to."""

    id: uuid.UUID
    email: str


def audit_actor(run_as_user_id: uuid.UUID, client_id: str | None = None) -> AuditActor:
    """The audit actor for external work run under *run_as_user_id*.

    ``client_id`` names the partner registration that acted. Omit it for a
    request that failed before any credential was identified — the entry is
    still anchored on the run-as user, because the exposure is what was
    addressed.
    """
    return AuditActor(
        id=run_as_user_id,
        email=f"a2a:{client_id}" if client_id else "a2a:anonymous",
    )
