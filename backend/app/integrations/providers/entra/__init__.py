"""Microsoft Entra ID integration provider package.

Comprehensive identity & access management (IAM) for a Microsoft Entra ID
(formerly Azure Active Directory) tenant via the Microsoft Graph API. Designed
as a best-in-class building block for an agentic IAM / identity-security team:
joiner-mover-leaver lifecycle, access governance, privileged-role audits, app
& credential hygiene, and identity-threat investigation + response.

The provider is split by domain:

* ``_common``             - dual-auth Graph client, formatters, constants.
* ``users``               - user lifecycle, offboarding (disable / revoke
                            sessions / reset password), manager, memberships,
                            licenses.
* ``groups``              - security / Microsoft 365 groups, members, owners.
* ``roles``               - directory roles & unified RBAC (privileged-access
                            governance).
* ``applications``        - app registrations, service principals, credential
                            expiry hygiene, OAuth2 consent grants.
* ``devices``             - registered / joined directory device objects.
* ``logs``                - sign-in & directory audit logs (investigation).
* ``identity_protection`` - risky users, risk detections, confirm / dismiss
                            (Entra ID P2).
* ``directory``           - organization, domains, license SKUs, conditional
                            access policies, recycle bin, and tenant-wide
                            password protection (Smart Lockout threshold /
                            duration + banned-password policy).

Authentication (resolved by ``integration_tools._get_token``):

* **Delegated OAuth** (default) - ``token`` is a Microsoft Graph access token;
  the integration's refresh token is redeemed on 401 via ``refresh_fn``.
* **App-only client credentials** - ``token`` is the compound
  ``tenant_id||client_id||client_secret`` and a Graph token is minted via the
  client-credentials grant. Recommended for unattended IAM automation.

A single Graph audience is used either way, so :class:`EntraClient` picks the
mode purely from the token shape (a compound token contains ``||``).
"""

from __future__ import annotations

from typing import Any

from app.integrations.base import BaseIntegration, OAuthDefaults
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import app_credentials
from ._common import GRAPH_SCOPE, EntraClient, RefreshFn, get_app_token
from .applications import register as register_applications
from .devices import register as register_devices
from .directory import register as register_directory
from .groups import register as register_groups
from .identity_protection import register as register_identity_protection
from .logs import register as register_logs
from .roles import register as register_roles
from .users import register as register_users

__all__ = [
    "GRAPH_SCOPE",
    "EntraClient",
    "EntraIntegration",
    "build_tools",
    "get_app_token",
]

# Delegated OAuth scopes (Microsoft Graph). App-only deployments grant the
# equivalent *application* permissions and use the API-key auth mode instead.
_SCOPES = [
    "openid",
    "profile",
    "email",
    "offline_access",
    "User.Read.All",
    "User.ReadWrite.All",
    "User.EnableDisableAccount.All",
    "User.RevokeSessions.All",
    "UserAuthenticationMethod.ReadWrite.All",
    "Group.ReadWrite.All",
    "GroupMember.ReadWrite.All",
    "Directory.Read.All",
    "RoleManagement.ReadWrite.Directory",
    "Application.Read.All",
    "Device.Read.All",
    "AuditLog.Read.All",
    "IdentityRiskyUser.ReadWrite.All",
    "IdentityRiskEvent.Read.All",
    "Policy.Read.All",
    "Organization.Read.All",
]

# The app-only credential mints client secrets on app registrations for the
# administrator tool create_integration_app_secret (app.integrations.app_secrets).
app_credentials.register()


class EntraIntegration(BaseIntegration):
    provider_name = "entra"
    display_name = "Microsoft Entra ID"
    icon = "Users"
    description = (
        "Connect Microsoft Entra ID (Azure AD) for identity & access "
        "management: user lifecycle, groups, RBAC roles, app/credential "
        "hygiene, sign-in/audit logs, and Identity Protection."
    )
    auth_types = ["oauth", "api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults:
        return OAuthDefaults(
            authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",  # noqa: S106
            scopes=_SCOPES,
        )

    async def validate_connection(self, token: str) -> bool:
        client = EntraClient(token)
        # /organization works in both delegated and app-only modes and only
        # needs the always-present Organization/Directory read.
        _, err = await client.get_json("/organization", "validate connection")
        return err is None

    async def get_user_info(self, token: str) -> dict:
        client = EntraClient(token)
        if client.app_only:
            data, _ = await client.get_json("/organization", "get tenant info")
            orgs = (data or {}).get("value", []) or []
            return orgs[0] if orgs else {}
        data, _ = await client.get_json("/me", "get user info")
        return data or {}


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    *,
    refresh_fn: RefreshFn | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build the Microsoft Entra ID tool set.

    ``token`` is either a Microsoft Graph access token (delegated OAuth) or the
    compound ``tenant_id||client_id||client_secret`` (app-only client
    credentials); :class:`EntraClient` handles both transparently.
    """
    client = EntraClient(token, refresh_fn)

    tools: list[ToolDefinition] = []
    tools += register_users(client, executor)
    tools += register_groups(client, executor)
    tools += register_roles(client, executor)
    tools += register_applications(client, executor)
    tools += register_devices(client, executor)
    tools += register_logs(client, executor)
    tools += register_identity_protection(client, executor)
    tools += register_directory(client, executor)
    return tools
