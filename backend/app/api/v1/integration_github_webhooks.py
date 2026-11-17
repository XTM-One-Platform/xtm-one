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

"""GitHub webhook management — create, list, and delete repository webhooks.

Allows users to programmatically manage GitHub repository webhooks from the
integration detail page and assignment trigger configuration.  Managed webhook
references are persisted in the integration's ``extra_config_enc`` under the
``managed_webhooks`` key so the UI can display them without polling the GitHub
API every time.

Endpoints
---------
  GET  /integrations/{id}/github-webhooks          – list managed webhooks
  POST /integrations/{id}/github-webhooks          – create a repo webhook
  DELETE /integrations/{id}/github-webhooks/{hook_id} – remove a repo webhook
"""

import json
import logging
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.helpers import get_user_group_ids, get_user_integration
from app.core.config import settings
from app.core.database import get_db
from app.core.egress import egress_client
from app.core.security import decrypt_json, decrypt_value, encrypt_value
from app.integrations.providers.github import GITHUB_API
from app.models.user import User
from app.services.audit import log_audit
from app.services.integration_credentials import require_oauth_credentials_usable
from app.services.relays import integration_egress_route
from app.services.vault import reveal_field, reveal_json_field

logger = logging.getLogger(__name__)

github_webhook_router = APIRouter()

_MANAGED_KEY = "managed_webhooks"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateGitHubWebhookRequest(BaseModel):
    repo: str  # "owner/name"
    events: list[str] = ["push"]
    target: str = "integration"  # "integration" | "assignment"
    assignment_id: str | None = None


class ManagedWebhook(BaseModel):
    hook_id: int
    repo: str
    events: list[str]
    target: str
    assignment_id: str | None = None
    webhook_url: str
    created_at: str


class ManagedWebhookList(BaseModel):
    webhooks: list[ManagedWebhook]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_extra_config(integration) -> dict:
    """The stored extra_config as written (a vault reference kept), for the managed-hooks merge; {} on failure."""
    cfg = decrypt_json(integration.extra_config_enc)
    return cfg if isinstance(cfg, dict) else {}


async def _webhook_secret(integration) -> str:
    """The ``webhook_secret`` GitHub signs deliveries with, a vault reference resolved (#6080)."""
    cfg = await reveal_json_field(integration, "extra_config_enc") if integration.extra_config_enc else None
    return str((cfg if isinstance(cfg, dict) else {}).get("webhook_secret") or "")


def _save_extra_config(integration, cfg: dict) -> None:
    """Encrypt and persist extra_config back to the integration."""
    integration.extra_config_enc = encrypt_value(json.dumps(cfg))


async def _resolve_token(integration, user: User) -> str:
    # The token acts on GitHub as its owner: a personal OAuth one only for
    # them, unless they shared it.
    require_oauth_credentials_usable(integration, user.id, is_admin=user.is_admin)
    if integration.access_token_enc:
        return decrypt_value(integration.access_token_enc)
    token = await reveal_field(integration, "api_key_enc")
    if token:
        return token
    raise HTTPException(
        status_code=400,
        detail="GitHub integration has no credentials — connect it first",
    )


def _github_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    return str(body.get("message", "")) if isinstance(body, dict) else ""


_APP_FIX_STEPS = (
    "How to fix:\n"
    "1. GitHub → Settings → Developer settings → GitHub Apps → your app → Permissions & events: "
    "set Repository permissions → Webhooks to 'Read and write', then Save changes.\n"
    "2. GitHub → Settings → Applications → Installed GitHub Apps → Configure (use the organization's "
    "settings for an organization repository): accept the pending permission request and check that "
    "'{repo}' is in Repository access.\n"
    "3. Make sure the GitHub account that authorized this integration is an administrator of '{repo}'.\n"
    "4. Re-authorize this integration, then retry."
)


def _forbidden_detail(resp: httpx.Response, repo: str, token: str) -> str:
    """Turn a GitHub 403 on webhook creation into the one fix that applies."""
    message = _github_message(resp)
    lowered = message.lower()
    said = f'\n\nGitHub said: "{message}"' if message else ""

    if resp.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in lowered:
        return f"GitHub API rate limit reached. Wait a few minutes, then retry.{said}"

    sso = resp.headers.get("x-github-sso", "")
    if sso or "saml" in lowered:
        sso_url = sso.split("url=", 1)[1].strip() if "url=" in sso else ""
        where = f" at {sso_url}" if sso_url else " (GitHub → Settings → Applications)"
        return (
            f"The organization that owns '{repo}' enforces SAML single sign-on. "
            f"Authorize this integration's credential for SSO{where}, then retry.{said}"
        )

    if "ip allow list" in lowered:
        return (
            f"The organization that owns '{repo}' restricts API access with an IP allow list that does "
            "not include this server. An organization owner must add this server's outbound IP, or allow "
            f"installed GitHub Apps in the IP allow list settings.{said}"
        )

    if "oauth app access restrictions" in lowered:
        return (
            f"The organization that owns '{repo}' restricts third-party OAuth apps. An organization owner "
            "must approve this app in Organization settings → Third-party access → OAuth app policy, "
            f"or use a GitHub App instead.{said}"
        )

    if "must have admin rights" in lowered:
        return (
            f"The GitHub account that authorized this integration is not an administrator of '{repo}', "
            "and creating a webhook requires admin access. Ask a repository admin to grant it, or "
            f"re-authorize the integration with an admin account.{said}"
        )

    if "personal access token" in lowered or token.startswith("github_pat_"):
        return (
            f"The fine-grained personal access token cannot manage webhooks on '{repo}'. Edit it in "
            "GitHub → Settings → Developer settings → Personal access tokens: include the repository in "
            f"Repository access and set Repository permissions → Webhooks to 'Read and write'.{said}"
        )

    if token.startswith(("ghp_", "gho_")):
        return (
            f"The token is not allowed to create webhooks on '{repo}'. It needs the 'admin:repo_hook' "
            "scope, and its GitHub account must be an administrator of the repository. Update the token "
            f"scopes (or re-authorize the integration), then retry.{said}"
        )

    return f"The GitHub App cannot create webhooks on '{repo}'.\n\n" + _APP_FIX_STEPS.format(repo=repo) + said


def _webhook_url(integration_id: str, assignment_id: str | None) -> str:
    base = settings.base_url.rstrip("/")
    if assignment_id:
        return f"{base}/api/hooks/integrations/{integration_id}/assignments/{assignment_id}"
    return f"{base}/api/hooks/integrations/{integration_id}"


# ---------------------------------------------------------------------------
# List managed webhooks
# ---------------------------------------------------------------------------


@github_webhook_router.get(
    "/{integration_id}/github-webhooks",
    response_model=ManagedWebhookList,
)
async def list_github_webhooks(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the list of GitHub repo webhooks managed by this integration."""
    integration = await get_user_integration(db, user.id, integration_id, is_admin=user.is_admin)
    if integration.provider != "github":
        raise HTTPException(status_code=400, detail="Not a GitHub integration")

    cfg = _get_extra_config(integration)
    hooks = cfg.get(_MANAGED_KEY, [])
    return ManagedWebhookList(webhooks=[ManagedWebhook(**h) for h in hooks])


# ---------------------------------------------------------------------------
# Create a webhook on a GitHub repository
# ---------------------------------------------------------------------------


@github_webhook_router.post(
    "/{integration_id}/github-webhooks",
    response_model=ManagedWebhook,
    status_code=status.HTTP_201_CREATED,
)
async def create_github_webhook(
    integration_id: str,
    body: CreateGitHubWebhookRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a GitHub repository webhook pointing to this integration (or a
    specific assignment).

    Requires the GitHub App to have *Webhooks: Read & Write* permission
    on the target repository.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(db, user.id, integration_id, user_group_ids, is_admin=user.is_admin)
    if integration.provider != "github":
        raise HTTPException(status_code=400, detail="Not a GitHub integration")

    if body.target == "assignment" and not body.assignment_id:
        raise HTTPException(
            status_code=400,
            detail="assignment_id is required when target is 'assignment'",
        )

    # Validate repo format
    if "/" not in body.repo or body.repo.count("/") != 1:
        raise HTTPException(
            status_code=400,
            detail="repo must be in 'owner/name' format",
        )

    token = await _resolve_token(integration, user)
    url = _webhook_url(str(integration.id), body.assignment_id)

    # The stored config as written (merged back below) and the webhook secret it names, resolved.
    cfg = _get_extra_config(integration)
    secret = await _webhook_secret(integration)

    # Create webhook via GitHub API
    gh_url = f"{GITHUB_API}/repos/{body.repo}/hooks"
    gh_payload: dict = {
        "name": "web",
        "active": True,
        "events": body.events,
        "config": {
            "url": url,
            "content_type": "json",
            "insecure_ssl": "0",
        },
    }
    if secret:
        gh_payload["config"]["secret"] = secret

    route = await integration_egress_route(db, integration)
    async with egress_client(timeout=20, route=route) as client:
        resp = await client.post(
            gh_url,
            json=gh_payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        )

    if resp.status_code in (403, 404):
        logger.warning(
            "GitHub webhook create returned %s for repo=%s: %s",
            resp.status_code,
            body.repo,
            _github_message(resp),
        )
    if resp.status_code == 404:
        # GitHub answers 404, not 403, when the credential cannot see the repository at all.
        raise HTTPException(
            status_code=404,
            detail=f"Repository '{body.repo}' was not found or is not visible to this integration. "
            "Check the owner/name spelling, that the GitHub App is installed on it (Installed GitHub "
            "Apps → Configure → Repository access), and that the authorizing account can access it.",
        )
    if resp.status_code == 403:
        raise HTTPException(
            status_code=422,
            detail=_forbidden_detail(resp, body.repo, token),
        )
    if resp.status_code == 422:
        detail = resp.json().get("message", resp.text[:300])
        errors = resp.json().get("errors", [])
        if any("already exists" in str(e).lower() for e in errors):
            raise HTTPException(
                status_code=409,
                detail=f"A webhook already exists for this URL on '{body.repo}'.",
            )
        raise HTTPException(
            status_code=422,
            detail=f"GitHub rejected the webhook: {detail}",
        )
    if resp.status_code == 401:
        raise HTTPException(
            status_code=422,
            detail="GitHub rejected the integration's credential (expired or revoked). "
            "Re-authorize this integration, then retry.",
        )
    if resp.status_code >= 500:
        raise HTTPException(
            status_code=502,
            detail=f"GitHub API error ({resp.status_code}): {resp.text[:300]}",
        )
    # Not 502: the frontend treats a 502 as "backend down" and drops the message.
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=422,
            detail=f"GitHub API error ({resp.status_code}): {_github_message(resp) or resp.text[:300]}",
        )

    hook_data = resp.json()
    hook_id = hook_data.get("id")

    managed = ManagedWebhook(
        hook_id=hook_id,
        repo=body.repo,
        events=body.events,
        target=body.target,
        assignment_id=body.assignment_id,
        webhook_url=url,
        created_at=datetime.now(UTC).isoformat(),
    )

    # Persist to extra_config
    hooks_list = cfg.get(_MANAGED_KEY, [])
    hooks_list.append(managed.model_dump())
    cfg[_MANAGED_KEY] = hooks_list
    _save_extra_config(integration, cfg)
    await db.flush()

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="integration",
        entity_id=integration.id,
        entity_name=integration.name,
        summary=f"Created GitHub webhook on {body.repo} → {body.target}",
        request=request,
        details={"repo": body.repo, "events": body.events, "target": body.target},
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return managed


# ---------------------------------------------------------------------------
# Delete a managed webhook
# ---------------------------------------------------------------------------


@github_webhook_router.delete(
    "/{integration_id}/github-webhooks/{hook_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_github_webhook(
    integration_id: str,
    hook_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a managed GitHub repository webhook."""
    user_group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(db, user.id, integration_id, user_group_ids, is_admin=user.is_admin)
    if integration.provider != "github":
        raise HTTPException(status_code=400, detail="Not a GitHub integration")

    cfg = _get_extra_config(integration)
    hooks_list: list[dict] = cfg.get(_MANAGED_KEY, [])

    # Find the webhook in the managed list
    hook_entry = next((h for h in hooks_list if h.get("hook_id") == hook_id), None)
    if not hook_entry:
        raise HTTPException(status_code=404, detail="Webhook not found in managed list")

    repo = hook_entry.get("repo", "")
    token = await _resolve_token(integration, user)

    # Delete from GitHub (best-effort — the hook may already be gone)
    gh_url = f"{GITHUB_API}/repos/{repo}/hooks/{hook_id}"
    route = await integration_egress_route(db, integration)
    async with egress_client(timeout=15, route=route) as client:
        resp = await client.delete(
            gh_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        )
    if resp.status_code not in (204, 404):
        logger.warning(
            "GitHub webhook delete returned %s for repo=%s hook=%s: %s",
            resp.status_code,
            repo,
            hook_id,
            resp.text[:200],
        )

    # Remove from persisted list
    cfg[_MANAGED_KEY] = [h for h in hooks_list if h.get("hook_id") != hook_id]
    _save_extra_config(integration, cfg)
    await db.flush()

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="integration",
        entity_id=integration.id,
        entity_name=integration.name,
        summary=f"Deleted GitHub webhook on {repo}",
        request=request,
        details={"repo": repo, "hook_id": hook_id},
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
