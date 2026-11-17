# SPDX-License-Identifier: Apache-2.0

"""The integration-only answers of the exposure routes, in their own
shape, computed by the one exposure engine, ``app.services.resource_exposure``.

``GET /integrations/{id}/exposure``, ``GET /integrations/exposure/summary`` and
``POST /integrations/exposure/preview`` predate the routes every lent resource
has (``/exposure/...``) and stay for API clients. They answer exactly
what the generic routes answer for an integration (every path, audience and
preview item), renamed: ``credential`` for ``secret``, ``credential_usable``
for ``usable``, ``credentials`` for the preview's integration ``items``, with
what a change gains as ``reached``. The Security Audit's list of the personal
credentials other people act with (:func:`lent_personal_credentials`) reads
the same engine. Nothing here writes or refuses a change.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid
from app.models.integration import Integration
from app.models.user import User
from app.services import resource_exposure as engine

#: The engine's name of an opt-out -> this route's.
_OPT_OUTS = {engine.OPT_OUT_COMPANY_RESOURCE: "company_integration"}


def _credential(secret: dict[str, Any], integration: Any) -> dict[str, Any]:
    return {**secret, "auth_type": getattr(integration, "auth_type", None)}


def integration_answer(result: dict[str, Any], integration: Any) -> dict[str, Any]:
    """The engine's :func:`~app.services.resource_exposure.resource_exposure`
    answer for *integration*, in this route's shape."""
    paths = []
    for path in result["paths"]:
        entry = {key: value for key, value in path.items() if key != "usable"}
        entry["credential_usable"] = path["usable"]
        paths.append(entry)
    return {
        "integration_id": str(integration.id),
        "credential": _credential(result["secret"], integration),
        "direct": result["direct"],
        "paths": paths,
        "reached": result["reached"],
        "orphaned_agents": result.get("orphaned_agents", []),
        "truncated": result["truncated"],
    }


async def integration_exposure(
    db: AsyncSession,
    integration: Integration,
    *,
    viewer_id: uuid.UUID,
    viewer_is_admin: bool,
) -> dict[str, Any]:
    """Every path through which *integration*'s credential reaches people."""
    result = await engine.resource_exposure(
        db,
        engine.RESOURCE_INTEGRATION,
        integration,
        viewer_id=viewer_id,
        viewer_is_admin=viewer_is_admin,
    )
    return integration_answer(result, integration)


async def exposure_summaries(
    db: AsyncSession, integrations: Iterable[Integration]
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Per integration: how many ways it is shared, by kind (the list cards),
    and whether a lookup was cut."""
    summaries, truncated = await engine.exposure_summaries(db, engine.RESOURCE_INTEGRATION, integrations)
    return (
        {
            resource_id: {
                "integration_id": summary["id"],
                "credential_kind": summary["secret_kind"],
                "is_company": summary["is_company"],
                "account_shared": summary["account_shared"],
                "ways": summary["ways"],
                "counts": summary["counts"],
                "public_link": summary["public_link"],
            }
            for resource_id, summary in summaries.items()
        },
        truncated,
    )


#: How a personal credential reaches its integration's own owner: a token
#: another person connected, kept across an owner transfer.
LENT_TO_OWNER = "owner"

#: Who a lent credential reaches (the engine's audience).
Audience = engine.Audience


@dataclass
class LentCredential:
    """A personal credential other people act with."""

    integration_id: str
    name: str
    provider: str
    holder_name: str | None
    #: The ways it reaches them, in the order found (the engine's ``PATH_*``).
    kinds: list[str]
    #: Everyone but its holder it reaches.
    reached: Audience


async def lent_personal_credentials(db: AsyncSession) -> tuple[list[LentCredential], bool]:
    """The personal credentials someone other than their holder acts with (the Security Audit).

    Every integration that is not company-managed (a company integration's
    credential is the company's by design) and holds a credential, through
    every path the engine reports: a direct share once its account is shared,
    and every agent, assignment, flow, knowledge base, skill and General
    Assistant that carries it. Returns the lent ones and whether a reference
    lookup was cut.
    """
    kind = engine.RESOURCE_INTEGRATION
    rows = (await db.execute(select(Integration).order_by(Integration.name, Integration.id))).scalars().all()
    secrets = {row.id: engine.describe_secret(kind, row) for row in rows}
    held = [row for row in rows if engine.lends_something(secrets[row.id]) and not secrets[row.id]["is_company"]]
    if not held:
        return [], False
    references = await engine.find_references(db, kind, [row.id for row in held])
    resolver = engine.AudienceResolver(db)
    await resolver.prime_agents(
        ref.parent if ref.kind == engine.PATH_ASSIGNMENT else ref.entity
        for ref in references.all()
        if ref.kind in (engine.PATH_AGENT, engine.PATH_ASSIGNMENT)
    )
    lent: list[tuple[Integration, uuid.UUID | None, list[str], engine.Audience]] = []
    for integration in held:
        secret = secrets[integration.id]
        holder = safe_uuid(secret["holder_id"]) if secret["holder_id"] else None
        kinds: list[str] = []
        reached = engine.Audience()
        direct = (await engine.direct_audience(resolver, kind, integration, secret)).without([holder])
        if not direct.is_empty:
            # Not shared at all, the owner acts with a token someone else connected.
            kinds += [
                path
                for path, shared in (
                    (engine.PATH_GROUP, integration.shared_group_ids),
                    (engine.PATH_PEOPLE, integration.shared_user_ids),
                )
                if shared
            ] or [LENT_TO_OWNER]
            reached = reached.merge(direct)
        for ref in references.of(integration.id):
            beyond = (await engine.lent_audience(resolver, kind, integration, ref, secret)).without([holder])
            if beyond.is_empty:
                continue
            reached = reached.merge(beyond)
            if ref.kind not in kinds:
                kinds.append(ref.kind)
        if not reached.is_empty:
            lent.append((integration, holder, kinds, reached))
    holders = sorted({holder for _integration, holder, _kinds, _reached in lent if holder}, key=str)
    names = (
        {
            str(uid): name or email or ""
            for uid, name, email in (
                await db.execute(select(User.id, User.full_name, User.email).where(User.id.in_(holders)))
            ).all()
        }
        if holders
        else {}
    )
    return (
        [
            LentCredential(
                integration_id=str(integration.id),
                name=integration.name,
                provider=integration.provider,
                holder_name=names.get(str(holder)) if holder else None,
                kinds=kinds,
                reached=reached,
            )
            for integration, holder, kinds, reached in lent
        ],
        references.truncated,
    )


def generic_change(entity_type: str, change: dict[str, Any]) -> dict[str, Any]:
    """This route's preview fields as the engine's ``change``.

    ``integration_ids`` is the full list the entity will hold; an omitted
    ``withhold_connected_account`` makes no choice, as ``PUT /integrations``
    reads an omitted ``keep_oauth_credentials_on_share``.
    """
    out = {key: value for key, value in change.items() if key not in ("integration_ids", "withhold_connected_account")}
    if change.get("withhold_connected_account") is not None:
        out["keep_oauth_credentials_on_share"] = not change["withhold_connected_account"]
    ids = change.get("integration_ids")
    if ids is not None and entity_type != "integration":
        if entity_type == "knowledge_base":
            out["sync_source_integration_ids"] = list(ids)
        elif entity_type == "skill":
            out["source_integration_id"] = ids[0] if ids else None
        elif entity_type == "flow":
            # A flow holds its integrations through its nodes; a graph sent
            # with the change says it already.
            out.setdefault(
                "graph",
                {
                    "nodes": [
                        {"id": f"integration-{index}", "kind": "integration", "config": {"integration_id": iid}}
                        for index, iid in enumerate(ids)
                    ]
                },
            )
        else:
            out["integration_bindings"] = {"preview": list(ids)}
    return out


def preview_answer(result: dict[str, Any]) -> dict[str, Any]:
    """The engine's preview, in this route's shape: the integrations the change
    lends to new people, and whether a lookup was cut (the list may then be
    incomplete, so the change still asks)."""
    credentials = []
    for item in result["items"]:
        resource = item["resource"]
        reached = item["gained"]
        if resource["kind"] != engine.RESOURCE_INTEGRATION or _nobody(reached):
            continue
        credentials.append(
            {
                "integration": {
                    "id": resource["id"],
                    "name": resource["name"],
                    "provider": resource["provider"],
                    "auth_type": resource["auth_type"],
                },
                "credential": {**item["secret"], "auth_type": resource["auth_type"]},
                "via": item["via"],
                "reached": reached,
                "opt_outs": [_OPT_OUTS.get(option, option) for option in item["opt_outs"]],
            }
        )
    truncated = bool(result.get("truncated"))
    return {"credentials": credentials, "requires_confirmation": bool(credentials) or truncated, "truncated": truncated}


def _nobody(audience: dict[str, Any]) -> bool:
    return not (
        audience["everyone"]
        or audience["people_count"]
        or audience["public_link"]
        or audience["anonymous_channels"]
        or audience["mcp_key_holders"]
        or audience["a2a_partners"]
        or audience["webhook_callers"]
    )


async def preview_exposure(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    actor_is_admin: bool,
    entity_type: str,
    entity: Any,
    change: dict[str, Any],
    parent: Any = None,
) -> dict[str, Any]:
    """The stored credentials a proposed change lends to people who could not
    act with them before, with whose they are and through what. Nothing is
    written."""
    result = await engine.preview_change(
        db,
        actor_id=actor_id,
        actor_is_admin=actor_is_admin,
        entity_type=entity_type,
        entity=entity,
        change=generic_change(entity_type, change),
        parent=parent,
    )
    return preview_answer(result)
