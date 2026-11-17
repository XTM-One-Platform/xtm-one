# SPDX-License-Identifier: Apache-2.0

"""Integration element listing – proxy to provider APIs for selectable items.

Provides endpoints for listing selectable elements from provider APIs
(e.g. Teams channels, Slack channels, GitHub repos) that users can bind
to assignments.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.graph_pagination import paginate_graph_collection
from app.api.v1.helpers import get_user_integration
from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.core.database import get_db
from app.core.egress import egress_client, egress_route
from app.core.security import decrypt_value
from app.core.utils import format_exception_message
from app.integrations.http import (
    EmptyBearerTokenError,
    bearer_auth_headers,
    normalize_bearer_token,
)
from app.integrations.providers.github import GITHUB_API
from app.integrations.providers.microsoft import GRAPH_API
from app.models.user import User
from app.services.integration_user_credentials import usable_credential_holder
from app.services.relays import integration_egress_route
from app.services.vault import reveal_field, reveal_json_field

logger = logging.getLogger(__name__)

element_router = APIRouter()

# An integration whose credential is blank, expired or incomplete is refused
# with 403 and a re-authorize message. Never 401: the frontend's shared error
# handler reads a 401 as the user's own session ending (clears the token and
# redirects to /login), so a provider credential problem signed the user out
# of the trigger editor (#11106). The pickers render a 403 inline.
INTEGRATION_CREDENTIAL_UNUSABLE_STATUS = 403

# Standard HubSpot CRM object types that emit webhook events. Each ``id`` matches
# the ``subscriptionType`` prefix HubSpot sends (e.g. ``contact.creation`` ->
# ``contact``), which is exactly what the webhook matcher compares against.
HUBSPOT_STANDARD_OBJECT_TYPES: list[tuple[str, str]] = [
    ("contact", "Contact"),
    ("company", "Company"),
    ("deal", "Deal"),
    ("ticket", "Ticket"),
    ("product", "Product"),
    ("line_item", "Line item"),
    ("quote", "Quote"),
]


@element_router.get("/{integration_id}/elements")
async def list_integration_elements(
    integration_id: str,
    element_type: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List selectable elements for an integration (e.g. Teams channels, Slack channels, GitHub repos).

    This proxies to the provider's API using the integration's stored credentials
    and returns a flat list of elements the user can bind to a assignment.
    """
    row = await get_user_integration(
        db, user.id, integration_id, is_admin=user.is_admin
    )
    # The caller's credential: the integration's, or their own on one whose
    # people each connect their own account.
    integration = await usable_credential_holder(db, row, user.id, is_admin=user.is_admin)

    # Validate element_type is supported for this provider
    catalog = PROVIDER_CATALOG.get(integration.provider, {})
    supported_types = [et["type"] for et in catalog.get("element_types", [])]
    if element_type not in supported_types:
        raise HTTPException(
            status_code=400,
            detail=f"Element type '{element_type}' is not supported for provider '{integration.provider}'. "
            f"Supported: {supported_types}",
        )

    # Resolve the credential, failing fast with a clear refusal when it is
    # blank so an empty ``Bearer `` header can never be built downstream (httpx
    # rejects it with an opaque LocalProtocolError → HTTP 500). The refusal is
    # a 403, never a 401: the app reads a 401 as ITS session ending and signs
    # the user out, while this is the integration's credential (#11106).
    token: str | None = None
    if integration.access_token_enc:
        token = decrypt_value(integration.access_token_enc)
    elif integration.api_key_enc:
        token = await reveal_field(integration, "api_key_enc") or ""
    try:
        token = normalize_bearer_token(token)
    except EmptyBearerTokenError as exc:
        raise HTTPException(status_code=INTEGRATION_CREDENTIAL_UNUSABLE_STATUS, detail=str(exc)) from exc

    # Jira stores the plain OAuth access token in ``access_token_enc`` and the
    # Atlassian site identifier (``cloud_id``) separately in ``extra_config``.
    # ``_list_provider_elements`` expects the compound ``cloud_id||access_token``
    # form, so assemble it here — mirroring the runtime resolution in
    # ``services/integration_tools.py``. Without this the access token was
    # parsed as the ``cloud_id`` and the bearer token came out empty, raising
    # ``Illegal header value b'Bearer '`` (HTTP 500) when listing projects.
    if integration.provider == "jira":
        cloud_id = ""
        if integration.extra_config_enc:
            try:
                extra = await reveal_json_field(integration, "extra_config_enc") or {}
                cloud_id = (extra.get("cloud_id") or "").strip()
            except Exception as exc:
                # Treat any decrypt/parse failure as a missing cloud_id.
                logger.warning(
                    "Failed to read cloud_id from extra_config for Jira integration "
                    "'%s': %s",
                    integration.name,
                    format_exception_message(exc),
                )
                cloud_id = ""
        if not cloud_id:
            raise HTTPException(
                status_code=INTEGRATION_CREDENTIAL_UNUSABLE_STATUS,
                detail=("Jira site (cloud_id) is missing. Please re-authorize the Jira integration."),
            )
        token = f"{cloud_id}||{token}"

    with egress_route(await integration_egress_route(db, row)):
        elements = await _list_provider_elements(integration.provider, element_type, token)
    return {"elements": elements, "element_type": element_type}


async def _list_provider_elements(
    provider: str,
    element_type: str,
    token: str,
) -> list[dict]:
    """Fetch selectable elements from a provider's API."""

    # Non-Jira providers authenticate with a single Bearer token, so build the
    # header here through the guarded helper (defense-in-depth: a blank token
    # fails fast, never producing an illegal ``"Bearer "`` value). Jira is
    # skipped: its ``token`` is a compound ``cloud_id||access_token`` and its
    # branch parses that and builds its own header from the access token, so a
    # generic Bearer header would be both semantically invalid and unused.
    headers: dict[str, str] = {}
    if provider != "jira":
        try:
            headers = bearer_auth_headers(token)
        except EmptyBearerTokenError as exc:
            raise HTTPException(status_code=INTEGRATION_CREDENTIAL_UNUSABLE_STATUS, detail=str(exc)) from exc

    if provider == "microsoft" and element_type == "teams_channel":
        elements: list[dict] = []
        async with egress_client(timeout=30) as client:
            teams = await paginate_graph_collection(
                client,
                initial_url=f"{GRAPH_API}/me/joinedTeams",
                headers=headers,
                select_fields="id,displayName",
                top=None,
                max_pages=50,
                logger=logger,
                error_prefix=(
                    "Failed to list joined Teams. "
                    "Ensure Team.ReadBasic.All scope is granted — the user may need to re-authorize."
                ),
                truncate_prefix="Teams channel listing",
            )
            if not teams:
                return []

            for team in teams:
                team_id = team.get("id", "")
                team_name = team.get("displayName", "Untitled")
                channels = await paginate_graph_collection(
                    client,
                    initial_url=f"{GRAPH_API}/teams/{team_id}/channels",
                    headers=headers,
                    select_fields="id,displayName,membershipType",
                    # The /teams/{id}/channels endpoint rejects $top with
                    # HTTP 400 — omit it. The helper also
                    # self-heals if $top ever leaks back in.
                    top=None,
                    max_pages=50,
                    logger=logger,
                    error_prefix=(
                        f"Failed to list channels for team {team_name}. "
                        "Ensure Channel.ReadBasic.All scope is granted."
                    ),
                    truncate_prefix=f"Teams channel listing for team {team_name}",
                )
                for ch in channels:
                    elements.append(
                        {
                            "id": f"{team_id}:{ch.get('id', '')}",
                            "name": ch.get("displayName", "Untitled"),
                            "group": team_name,
                            "metadata": {
                                "team_id": team_id,
                                "team_name": team_name,
                                "channel_id": ch.get("id", ""),
                                "membership_type": ch.get("membershipType", "standard"),
                            },
                        }
                    )
        return elements

    if provider == "slack" and element_type == "slack_channel":
        elements = []
        cursor: str | None = None
        channel_types = "public_channel,private_channel"
        async with egress_client(timeout=30) as client:
            while True:
                params: dict[str, str] = {
                    "types": channel_types,
                    "exclude_archived": "true",
                    "limit": "200",
                }
                if cursor:
                    params["cursor"] = cursor
                resp = await client.get(
                    "https://slack.com/api/conversations.list",
                    headers=headers,
                    params=params,
                )
                if resp.status_code >= 400:
                    logger.warning(
                        "Slack conversations.list returned HTTP %s: %s",
                        resp.status_code,
                        resp.text[:300],
                    )
                    return elements  # return whatever we collected so far
                data = resp.json()
                if not data.get("ok"):
                    # Fallback: if groups:read scope is missing, retry with public channels only
                    if (
                        data.get("error") == "missing_scope"
                        and "private_channel" in channel_types
                    ):
                        logger.info(
                            "Slack missing groups:read scope, falling back to public channels only",
                        )
                        channel_types = "public_channel"
                        cursor = None
                        elements.clear()
                        continue
                    logger.warning(
                        "Slack conversations.list returned ok=false: error=%s, needed=%s",
                        data.get("error", "unknown"),
                        data.get("needed", ""),
                    )
                    return elements
                for ch in data.get("channels", []):
                    elements.append(
                        {
                            "id": ch.get("id", ""),
                            "name": ch.get("name", ""),
                            "group": "private" if ch.get("is_private") else "public",
                            "metadata": {
                                "channel_id": ch.get("id", ""),
                                "is_private": ch.get("is_private", False),
                                "num_members": ch.get("num_members", 0),
                            },
                        }
                    )
                # Handle cursor-based pagination
                next_cursor = data.get("response_metadata", {}).get("next_cursor", "")
                if not next_cursor:
                    break
                cursor = next_cursor
        return elements

    if provider == "jira" and element_type == "jira_project":
        from app.integrations.http import parse_compound_token

        cloud_id, jira_token = parse_compound_token(token, 2)
        # Normalize both halves once and use the stripped values consistently
        # below. Checking emptiness on a stripped value while still building the
        # URL / ``Authorization`` header from the raw value would let a token
        # with leading/trailing whitespace or control chars pass the guard and
        # then produce an illegal header (httpx LocalProtocolError → HTTP 500).
        cloud_id = (cloud_id or "").strip()
        jira_token = (jira_token or "").strip()
        if not cloud_id or not jira_token:
            # Never build an empty ``Bearer `` header (httpx raises
            # LocalProtocolError → HTTP 500). A missing cloud_id or access token
            # means the integration needs re-authorizing; degrade gracefully to
            # an empty list so the scope picker renders instead of crashing.
            logger.warning(
                "Jira integration missing cloud_id or access token — cannot list projects"
            )
            return []
        base_url = f"https://api.atlassian.com/ex/jira/{cloud_id}"
        # Build through the shared guard too (jira_token is already non-blank
        # past the check above) so this path carries no hand-rolled Bearer
        # header - keeping the guard inseparable from header construction
        # everywhere, which is the whole point of this change.
        jira_headers = bearer_auth_headers(
            jira_token, extra={"Accept": "application/json"}
        )
        elements = []
        start_at = 0
        async with egress_client(timeout=20) as client:
            while True:
                resp = await client.get(
                    f"{base_url}/rest/api/3/project/search",
                    headers=jira_headers,
                    params={
                        "startAt": str(start_at),
                        "maxResults": "50",
                        "orderBy": "name",
                    },
                )
                if resp.status_code >= 400:
                    detail = ""
                    try:
                        detail = resp.json().get("message", resp.text[:300])
                    except Exception:
                        detail = resp.text[:300]
                    logger.warning(
                        "Jira project/search returned HTTP %s: %s",
                        resp.status_code,
                        detail,
                    )
                    return elements
                data = resp.json()
                for proj in data.get("values", []):
                    category = proj.get("projectCategory", {})
                    elements.append(
                        {
                            "id": proj.get("key", ""),
                            "name": f"{proj.get('key', '')} — {proj.get('name', '')}",
                            "group": category.get("name", ""),
                            "metadata": {
                                "project_id": proj.get("id", ""),
                                "key": proj.get("key", ""),
                                "name": proj.get("name", ""),
                                "project_type": proj.get("projectTypeKey", ""),
                                "style": proj.get("style", ""),
                            },
                        }
                    )
                if data.get("isLast", True):
                    break
                start_at += len(data.get("values", []))
        return elements

    if provider == "github" and element_type == "organization":
        # List GitHub organizations the authenticated user belongs to,
        # plus the user's own account (for personal repos).
        elements = []
        gh_headers = {**headers, "Accept": "application/vnd.github+json"}
        async with egress_client(timeout=20) as client:
            # List user's installations (GitHub App orgs)
            resp = await client.get(
                f"{GITHUB_API}/user/installations",
                headers=gh_headers,
                params={"per_page": "100"},
            )
            if resp.is_success:
                for inst in resp.json().get("installations", []):
                    acct = inst.get("account", {})
                    login = acct.get("login", "")
                    acct_type = acct.get("type", "User")
                    if login:
                        elements.append(
                            {
                                "id": login,
                                "name": login,
                                "group": acct_type,
                                "metadata": {
                                    "login": login,
                                    "type": acct_type,
                                    "installation_id": str(inst.get("id", "")),
                                    "avatar_url": acct.get("avatar_url", ""),
                                },
                            }
                        )
            else:
                # Fallback: list orgs via /user/orgs for non-GitHub-App tokens
                orgs_resp = await client.get(
                    f"{GITHUB_API}/user/orgs",
                    headers=gh_headers,
                    params={"per_page": "100"},
                )
                if orgs_resp.is_success:
                    for org in orgs_resp.json():
                        elements.append(
                            {
                                "id": org.get("login", ""),
                                "name": org.get("login", ""),
                                "group": "Organization",
                                "metadata": {
                                    "login": org.get("login", ""),
                                    "type": "Organization",
                                    "avatar_url": org.get("avatar_url", ""),
                                },
                            }
                        )
                # Also add the user's own account
                user_resp = await client.get(
                    f"{GITHUB_API}/user",
                    headers=gh_headers,
                )
                if user_resp.is_success:
                    user_data = user_resp.json()
                    elements.append(
                        {
                            "id": user_data.get("login", ""),
                            "name": user_data.get("login", ""),
                            "group": "User",
                            "metadata": {
                                "login": user_data.get("login", ""),
                                "type": "User",
                                "avatar_url": user_data.get("avatar_url", ""),
                            },
                        }
                    )
        return elements

    if provider == "github" and element_type == "repository":
        # List PRIVATE repos only — public repos are always accessible and
        # can be added manually by typing the owner/name in the UI.
        elements = []
        gh_headers = {**headers, "Accept": "application/vnd.github+json"}
        page = 1
        async with egress_client(timeout=30) as client:
            while True:
                resp = await client.get(
                    f"{GITHUB_API}/user/repos",
                    headers=gh_headers,
                    params={
                        "per_page": "100",
                        "sort": "updated",
                        "visibility": "private",
                        "page": str(page),
                    },
                )
                if resp.status_code >= 400:
                    break
                repos = resp.json()
                if not repos:
                    break
                for repo in repos:
                    owner = repo.get("owner", {}).get("login", "")
                    elements.append(
                        {
                            "id": repo.get("full_name", ""),
                            "name": repo.get("name", ""),
                            "group": owner,
                            "metadata": {
                                "full_name": repo.get("full_name", ""),
                                "owner": owner,
                                "private": True,
                                "default_branch": repo.get("default_branch", "main"),
                                "language": repo.get("language") or "",
                            },
                        }
                    )
                if len(repos) < 100:
                    break
                page += 1
        return elements

    if provider == "hubspot" and element_type == "crm_object_type":
        # Standard CRM objects are a fixed set; custom objects are fetched
        # best-effort from the CRM schema API (requires the
        # ``crm.schemas.custom.read`` scope — degrade gracefully to the
        # standard list when the call is unavailable or unauthorized).
        elements = [
            {
                "id": obj_id,
                "name": label,
                "group": "Standard objects",
                "metadata": {"object_type": obj_id},
            }
            for obj_id, label in HUBSPOT_STANDARD_OBJECT_TYPES
        ]
        seen = {obj_id for obj_id, _ in HUBSPOT_STANDARD_OBJECT_TYPES}
        try:
            async with egress_client(timeout=20) as client:
                resp = await client.get(
                    "https://api.hubapi.com/crm/v3/schemas",
                    headers=headers,
                )
                if resp.is_success:
                    for schema in resp.json().get("results", []):
                        # HubSpot custom-object webhooks use the ``objectTypeId``
                        # (e.g. ``2-12345``) as the ``subscriptionType`` prefix,
                        # so key the element on it to match the webhook payload.
                        type_id = (schema.get("objectTypeId") or "").strip()
                        labels = schema.get("labels") or {}
                        name = labels.get("singular") or schema.get("name") or type_id
                        if type_id and type_id not in seen:
                            seen.add(type_id)
                            elements.append(
                                {
                                    "id": type_id,
                                    "name": name,
                                    "group": "Custom objects",
                                    "metadata": {
                                        "object_type": type_id,
                                        "fully_qualified_name": schema.get(
                                            "fullyQualifiedName", ""
                                        ),
                                    },
                                }
                            )
                else:
                    logger.info(
                        "HubSpot crm/v3/schemas returned HTTP %s; "
                        "returning standard object types only",
                        resp.status_code,
                    )
        except Exception as exc:
            logger.warning("Failed to list HubSpot custom object schemas: %s", format_exception_message(exc))
        return elements

    return []
