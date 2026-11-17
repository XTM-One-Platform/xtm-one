"""What the Security Audit reports about vault providers and references.

Gathered here, worded in ``security_audit._check_secret_vaults``. Nothing a
vault holds is read: the providers are described from their declaration (the
environment) or their row (the UI), the stored references by their provider
name (one that does not parse by its field alone), the failures by their
reason code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.vault import REASON_DESCRIPTIONS
from app.services.vault.base import plain_http_off_loopback
from app.services.vault.config import SOURCE_DATABASE
from app.services.vault.failures import failure_matches, recent_failures
from app.services.vault.resolver import get_resolver
from app.services.vault.usage import KIND_LABELS, scan_reference_uses

__all__ = ["KIND_LABELS", "VaultAuditFacts", "gather_vault_facts"]

#: The auth methods of a database provider that store a long-lived cloud credential instead of a workload identity.
STATIC_KEY_METHODS = {
    "aws": "access_key",
    "gcp": "service_account_key",
    "azure": "client_secret",
}


@dataclass
class VaultAuditFacts:
    providers: dict[str, dict[str, str]] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)
    #: Declared providers whose address (HashiCorp, Azure, an AWS endpoint) is plain HTTP, off loopback.
    plain_http: list[str] = field(default_factory=list)
    #: Usable providers any user may write a reference to, at any path (``WRITERS=all`` without ``PATHS``,
    #: or a database provider granted to everyone with no path).
    open_writers: list[str] = field(default_factory=list)
    #: ``label -> providers`` of every secret field holding a reference.
    references: dict[str, list[str]] = field(default_factory=dict)
    #: Stored references to a provider nothing declares.
    unknown_provider: list[str] = field(default_factory=list)
    #: ``label: field`` of every secret field holding a value that starts with ``vault://`` but is not a valid reference.
    invalid_references: list[str] = field(default_factory=list)
    #: ``label: field (provider: reason)`` of the recent failures, ``None`` when Redis did not answer.
    failures: list[str] | None = None
    #: The failure record was full, so an older failure may be missing from :attr:`failures`.
    failures_truncated: bool = False
    #: Database providers storing a long-lived cloud key (``name: method``) instead of a workload identity.
    static_keys: list[str] = field(default_factory=list)
    #: Database providers whose last connection test failed (``name: reason``).
    failing: list[str] = field(default_factory=list)
    #: Usable database providers no secret field references.
    unused: list[str] = field(default_factory=list)
    #: Database providers an environment declaration of the same name hides.
    shadowed: list[str] = field(default_factory=list)


async def _database_rows(db: AsyncSession) -> list[Any]:
    from sqlalchemy import select

    from app.models.secret_vault_provider import SecretVaultProvider

    return list((await db.execute(select(SecretVaultProvider).order_by(SecretVaultProvider.name))).scalars().all())


async def gather_vault_facts(db: AsyncSession) -> VaultAuditFacts:
    resolver = get_resolver()
    facts = VaultAuditFacts(
        providers={name: provider.describe() for name, provider in sorted(resolver.providers.items())},
        refused=dict(sorted(resolver.errors.items())),
    )
    facts.plain_http = [
        name for name, details in facts.providers.items() if plain_http_off_loopback(details.get("address", ""))
    ]
    facts.open_writers = [
        name for name in sorted(resolver.providers) if resolver.policy(name).open_to_everyone_everywhere
    ]
    declared = resolver.known_names()

    scan = await scan_reference_uses(db)
    for use in scan.uses:
        facts.references[f"{use.label}: {use.field}"] = list(use.providers)
        facts.unknown_provider.extend(
            f"{use.label}: {use.field} -> {provider}" for provider in use.providers if provider not in declared
        )
    facts.invalid_references = [
        f"{scan.labels[(kind, entity_id)]}: {column_field}" for kind, entity_id, _name, column_field in scan.malformed
    ]
    used = set(scan.by_provider())

    rows = await _database_rows(db) if resolver.database or resolver.shadowed else []
    for row in rows:
        if row.name in resolver.shadowed:
            facts.shadowed.append(row.name)
            continue
        method = str((row.settings or {}).get("auth_method") or "")
        if STATIC_KEY_METHODS.get(row.type) == method:
            facts.static_keys.append(f"{row.name}: {method}")
        if row.enabled and row.last_check_ok is False:
            facts.failing.append(
                f"{row.name}: {REASON_DESCRIPTIONS.get(row.last_check_reason or '', row.last_check_reason or 'failed')}"
            )
        if row.enabled and row.name not in used and resolver.sources.get(row.name) == SOURCE_DATABASE:
            facts.unused.append(row.name)

    history = await recent_failures()
    if history is not None:
        facts.failures = [
            f"{scan.labels[(entry['kind'], entry['id'])]}: {entry['field']} "
            f"({entry['provider'] or 'unknown provider'}: {REASON_DESCRIPTIONS.get(entry['reason'], entry['reason'])})"
            for entry in history.entries
            if (entry["kind"], entry["id"]) in scan.labels
            and failure_matches(entry, scan.fingerprints.get((entry["kind"], entry["id"]), {}))
        ]
        facts.failures_truncated = history.truncated
    return facts
