"""Which secret fields reference which vault provider.

One scan of every column of ``REFERENCE_COLUMNS``, for the Security Audit
(``audit.gather_vault_facts``), the provider list's "where used" drill-down,
and the guards that refuse deleting a provider in use or renaming one a
reference names. It decrypts each stored secret to read the references it
holds and keeps only the provider, the entity and the field: never a value,
never a path.

A reference is stored encrypted like any secret, so no query can count the
uses of one provider: every scan decrypts every stored secret. The guards, the
drill-down and the audit take a fresh scan (:func:`scan_reference_uses`); the
use counts of the provider list and of an edit's answer read the last scan of
this process while it is :data:`SCAN_CACHE_SECONDS` old at most
(:func:`recent_reference_uses`).
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.vault import ENTITY_KINDS, REFERENCE_COLUMNS, reference_fingerprint, scan_stored_references

#: How each kind of entity is named in a finding or a drill-down.
KIND_LABELS = {
    "integration": "integration",
    "mcp_server": "MCP server",
    "llm_provider": "LLM provider",
    "variable": "variable",
    "channel_bot": "channel bot",
    "sso": "single sign-on",
    "langfuse": "Langfuse",
    "browser_profile": "browser profile",
    "oauth_default": "OAuth default",
}

#: The name a singleton settings row is shown by, having none of its own.
_SINGLETON_NAMES = {"sso_config": "OpenID Connect", "langfuse_config": "Langfuse"}

#: How long a scan answers what an administrator's page shows: a use count a few seconds late, never a guard.
SCAN_CACHE_SECONDS = 10.0

#: The last scan of this process and when it stops answering (``time.monotonic()``).
_last_scan: tuple[float, ReferenceScan] | None = None


@dataclass(frozen=True)
class ReferenceUse:
    """One secret field holding at least one reference: the entity, the field, the providers it names."""

    kind: str
    id: str
    name: str
    field: str
    providers: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"{KIND_LABELS.get(self.kind, self.kind)} '{self.name}' ({self.id})"


@dataclass
class ReferenceScan:
    uses: list[ReferenceUse] = field(default_factory=list)
    #: ``(kind, id, name, field)`` of each field holding a ``vault://`` value that does not parse.
    malformed: list[tuple[str, str, str, str]] = field(default_factory=list)
    #: Per entity, per field, the fingerprints of the references it holds (failures match one of them).
    fingerprints: dict[tuple[str, str], dict[str, frozenset[str]]] = field(default_factory=dict)
    #: The label of every entity holding a secret, by ``(kind, id)``.
    labels: dict[tuple[str, str], str] = field(default_factory=dict)

    def by_provider(self) -> dict[str, list[ReferenceUse]]:
        found: dict[str, list[ReferenceUse]] = defaultdict(list)
        for use in self.uses:
            for provider in use.providers:
                found[provider].append(use)
        return dict(found)


def _models() -> dict[str, Any]:
    from app.models.browser_profile import BrowserProfile
    from app.models.channel_bot import ChannelBot
    from app.models.integration import Integration
    from app.models.langfuse_config import LangfuseConfig
    from app.models.llm_config import LLMProviderConfig
    from app.models.mcp_server import MCPServer
    from app.models.oauth_default import OAuthDefault
    from app.models.sso_config import SSOConfig
    from app.models.variable import Variable

    models = (
        Integration,
        MCPServer,
        LLMProviderConfig,
        Variable,
        ChannelBot,
        SSOConfig,
        LangfuseConfig,
        BrowserProfile,
        OAuthDefault,
    )
    return {model.__tablename__: model for model in models if model.__tablename__ in REFERENCE_COLUMNS}


def _name(table: str, row: Any) -> str:
    named = getattr(row, "display_name", None) or getattr(row, "name", None) or getattr(row, "provider", "")
    return str(named or _SINGLETON_NAMES.get(table, ""))


async def scan_reference_uses(db: AsyncSession) -> ReferenceScan:
    """Every stored reference, by entity and field, read now; the decryption runs off the event loop.

    The scan is kept for :func:`recent_reference_uses`.
    """
    global _last_scan
    scan = await _scan_reference_uses(db)
    _last_scan = (time.monotonic() + SCAN_CACHE_SECONDS, scan)
    return scan


async def recent_reference_uses(db: AsyncSession) -> ReferenceScan:
    """The last scan of this process while it is :data:`SCAN_CACHE_SECONDS` old at most, otherwise a new one.

    For what a page shows only: a guard refusing to delete or rename a
    provider in use reads :func:`scan_reference_uses`.
    """
    cached = _last_scan
    if cached is not None and time.monotonic() < cached[0]:
        return cached[1]
    return await scan_reference_uses(db)


def clear_reference_scan_cache() -> None:
    """Forget the last scan: the next :func:`recent_reference_uses` reads the database again."""
    global _last_scan
    _last_scan = None


async def _scan_reference_uses(db: AsyncSession) -> ReferenceScan:
    rows: list[tuple[str, Any]] = []
    scan = ReferenceScan()
    for table, model in _models().items():
        columns = REFERENCE_COLUMNS[table]
        selected = [
            getattr(model, name)
            for name in ("id", "name", "display_name", "provider", *columns)
            if hasattr(model, name)
        ]
        result = await db.execute(
            select(*selected).where(or_(*(getattr(model, column).is_not(None) for column in columns)))
        )
        rows.extend((table, row) for row in result.all())

    def _scan() -> None:
        for table, row in rows:
            kind, entity_id, name = ENTITY_KINDS[table], str(row.id), _name(table, row)
            stored = scan_stored_references(row, REFERENCE_COLUMNS[table])
            if not (stored.by_field or stored.malformed):
                continue
            key = (kind, entity_id)
            scan.labels[key] = f"{KIND_LABELS.get(kind, kind)} '{name}' ({entity_id})"
            scan.fingerprints[key] = {
                column_field: frozenset(reference_fingerprint(ref) for ref in refs)
                for column_field, refs in stored.by_field.items()
            }
            for column_field, refs in sorted(stored.by_field.items()):
                scan.uses.append(
                    ReferenceUse(
                        kind=kind,
                        id=entity_id,
                        name=name,
                        field=column_field,
                        providers=tuple(sorted({ref.provider for ref in refs})),
                    )
                )
            scan.malformed.extend((kind, entity_id, name, column_field) for column_field in stored.malformed)

    await asyncio.to_thread(_scan)
    return scan


async def provider_uses(db: AsyncSession, name: str) -> list[ReferenceUse]:
    """The secret fields referencing provider *name*, read now (the delete and rename guards)."""
    return (await scan_reference_uses(db)).by_provider().get(name, [])
