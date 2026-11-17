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

"""Enterprise Edition licences for the agentic coding labs (Sam's decision D2, 31 Oct 2026).

At every lab start :func:`issue` asks Filigran's licensing server
(``POST <AGENTIC_LABS_LICENSING_URL>/api/ci``, a Bearer API-user token holding
the ``ci`` permission) for one short-lived ``ci`` licence per entry of
``AGENTIC_LABS_LICENCES`` (``ENV_VAR:product name``) and answers them keyed by
their variable; :mod:`labs` hands them to every container of the lab pod. Which
products exist and which variable each one reads are configuration: nothing
here names a product.

A licence that cannot be issued never stops a lab: it is left out, and
:attr:`Issued.missing` says which variable is missing and why. The token is
sent in the ``Authorization`` header only; it never reaches the pod, an
outcome, a log line or an error text. The licences are bound to no instance
(``global``): a lab's products are created with ids nobody knows in advance,
and a ``ci`` licence is short-lived by construction.
See ``dev-docs/reference/agentic-labs.md``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

#: Seconds one licensing call may take.
_TIMEOUT = 15.0
#: Far above a real certificate (a few kilobytes): a larger answer is not a licence.
_MAX_PEM_BYTES = 64 * 1024
_PEM_HEAD = "-----BEGIN CERTIFICATE-----"
#: The licensing server refuses more (src/lib/ci-license.ts, CI_MAX_VALIDITY_HOURS).
MAX_HOURS = 168
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class Issued:
    """The licences of one lab start: ``env`` (variable to PEM) and ``missing`` (variable to why)."""

    env: dict[str, str] = field(default_factory=dict)
    missing: dict[str, str] = field(default_factory=dict)
    #: The validity asked for each licence of ``env``, in hours (``0`` when none was asked).
    hours: int = 0

    def summary(self) -> str:
        """One line for the start answer: the variables set and those missing, never a PEM."""
        parts = []
        if self.env:
            parts.append("Enterprise Edition licences: " + ", ".join(sorted(self.env)))
        if self.missing:
            parts.append(
                "missing licences: " + "; ".join(f"{name} ({why})" for name, why in sorted(self.missing.items()))
            )
        return ". ".join(parts)


def configured() -> bool:
    """Whether labs get licences: the licensing URL, the token and at least one ``ENV_VAR:product`` set."""
    from app.core.config import settings

    return bool(
        (settings.agentic_labs_licensing_url or "").strip()
        and (settings.agentic_labs_licensing_token or "").strip()
        and settings.agentic_labs_licences
    )


def _hours() -> int:
    from app.core.config import settings

    return max(1, min(int(settings.agentic_labs_licence_hours or 0) or 24, MAX_HOURS))


async def issue(*, transport: httpx.AsyncBaseTransport | None = None) -> Issued:
    """One fresh ``ci`` licence per configured product; never raises, never blocks a lab."""
    from app.core.config import settings

    issued = Issued()
    if not configured():
        return issued
    url = settings.agentic_labs_licensing_url.strip().rstrip("/") + "/api/ci"
    headers = {"Authorization": f"Bearer {settings.agentic_labs_licensing_token.strip()}"}
    hours = _hours()
    issued.hours = hours
    async with httpx.AsyncClient(
        timeout=_TIMEOUT, trust_env=False, follow_redirects=False, transport=transport
    ) as client:
        for name, product in settings.agentic_labs_licences.items():
            if not _ENV_NAME.match(name):
                issued.missing[name] = "not an environment variable name"
                continue
            try:
                response = await client.post(
                    url, headers=headers, json={"product_name": product, "validity_hours": hours}
                )
            except httpx.HTTPError as exc:
                issued.missing[name] = f"the licensing server did not answer ({type(exc).__name__})"
                continue
            if response.status_code != 200:
                issued.missing[name] = f"the licensing server answered {response.status_code}"
                continue
            body = response.content
            pem = body.decode("utf-8", errors="replace").strip() if len(body) <= _MAX_PEM_BYTES else ""
            if not pem.startswith(_PEM_HEAD):
                issued.missing[name] = "the licensing server answered no certificate"
                continue
            issued.env[name] = pem + "\n"
    if issued.missing:
        logger.warning("Agentic labs: a lab starts without some licences: %s", issued.summary())
    return issued
