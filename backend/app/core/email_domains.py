# SPDX-License-Identifier: Apache-2.0

"""The allowed email domains: which addresses may get a new account.

``sso_config.allowed_domains`` (Settings > Access and security > Authentication, Allowed
email domains) restricts self-registration (``auth.register``) and SSO account
creation (``auth_sso._get_or_create_sso_user``) alike; an account that already
exists keeps signing in whatever its domain. Both refusals and the Security
Audit read the list through this module, so none of them parses or matches it
differently.

Standard library only: the import-light ``app.core.security_controls`` reads
it too.
"""

from __future__ import annotations

from collections.abc import Collection


def parse_allowed_domains(raw: str | None) -> list[str]:
    """The domains of the stored comma-separated list, lower-cased; empty when every domain is allowed."""
    return [domain.strip().lower() for domain in str(raw or "").split(",") if domain.strip()]


def email_domain_allowed(email: str, allowed_domains: Collection[str]) -> bool:
    """Whether *email* may get a new account under *allowed_domains*, as :func:`parse_allowed_domains` reads them.

    The email's domain is what follows its last ``@``, compared
    case-insensitively and exactly: ``example.com`` does not admit
    ``eu.example.com``. A value with no local part or no ``@`` (a bare user
    name some identity providers assert) is at no domain. An empty list allows
    every value.
    """
    if not allowed_domains:
        return True
    local, at, domain = email.strip().lower().rpartition("@")
    return bool(local and at) and domain in allowed_domains
