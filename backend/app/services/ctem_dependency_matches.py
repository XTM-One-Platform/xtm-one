# SPDX-License-Identifier: Apache-2.0

"""Repository dependencies matched to OpenCTI vulnerabilities (#1331, slice 4).

The GitHub and GitLab asset sources store each repository's dependencies as
its software (SBOM or dependency list, with PURL when the source gives one).
After a sync is saved, the components of each repository are looked up in
OpenCTI, PURL first: a Software observable whose ``x_opencti_purl`` names the
same package at the same version is the component. Each "has" relation from
that Software to a Vulnerability is one path 3 signal (``code_flaw``, lens
``inside``, subject ``code``) on the repository asset, through the one CTEM
intake, as a Dependabot alert (slice 2) or a GitLab finding (slice 3) is. The
fixed version is the smallest Software version OpenCTI says remediates the
vulnerability above the installed one, never guessed; with it the case is titled
"Upgrade <library> to <fixed version> in <repository>".

The read uses the sync owner's own OpenCTI credential, after the sync's commit,
in a session of its own, so a slow or absent OpenCTI never holds the sync.
A component with no PURL, or an unknown version, is never matched. Rules only,
no model call; nothing is written to OpenCTI, GitHub or GitLab.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

PROVIDER = "opencti"
TRIGGER_PATH = "code_flaw"
LENS = "inside"
SUBJECT_KIND = "code"
#: Most components looked up in OpenCTI per sync (the rest wait for the next one).
MAX_COMPONENTS = 2000
#: Most vulnerability relations read per sync.
MAX_RELATIONS = 2000
_PAGE = 200

_OCTI_SOFTWARE_BY_PURL = """
query CtemDependencySoftware($filters: FilterGroup, $first: Int) {
  stixCyberObservables(types: ["Software"], filters: $filters, first: $first) {
    edges { node { id ... on Software { name version x_opencti_purl } } }
  }
}
"""

# The Software OpenCTI says remediates each vulnerability, with its PURL so a fixed
# version is only ever taken from the same package (the CTEM-11 query of
# ctem_cases_source selects no PURL).
_OCTI_SOFTWARE_REMEDIATES_PURL = """
query CtemDependencySoftwareRemediates($to: [String], $first: Int) {
  stixCoreRelationships(toId: $to, relationship_type: ["remediates"], fromTypes: ["Software"], first: $first) {
    edges { node {
      from { ... on Software { id name version x_opencti_purl } }
      to { ... on Vulnerability { id } }
    } }
  }
}
"""


@dataclass(frozen=True)
class Component:
    """One dependency of one repository, as the asset source stored it."""

    repository_id: str
    repository: str
    identity_key: str
    product: str
    version: str
    purl: str


# ── Matching (pure) ───────────────────────────────────────────────────


def purl_parts(purl: str | None) -> tuple[str, str]:
    """``(base, version)`` of a PURL: ``pkg:type/namespace/name`` lower-cased, and its version.

    Qualifiers and subpath are left out; a value that is not a PURL answers ``("", "")``.
    """
    text = (purl or "").strip()
    if not text.lower().startswith("pkg:"):
        return "", ""
    text = text.split("#", 1)[0].split("?", 1)[0]
    base, _, version = text.rpartition("@") if "@" in text[4:] else (text, "", "")
    if not base.lower().startswith("pkg:") or "/" in version or base.endswith("/"):
        base, version = text, ""  # an unencoded scope (pkg:npm/@babel/core) is not a version
    return unquote(base).lower().rstrip("/"), unquote(version).strip()


def components(
    software: Mapping[str, Iterable[Mapping[str, Any]]], repositories: Mapping[str, Mapping[str, str]]
) -> list[Component]:
    """The matchable components of each repository: a PURL and a known version (pure)."""
    out: dict[tuple[str, str, str], Component] = {}
    for repo_id, entries in software.items():
        repo = repositories.get(repo_id) or {}
        for entry in entries:
            base, purl_version = purl_parts(str(entry.get("purl") or ""))
            version = str(entry.get("version") or "").strip() or purl_version
            if not base or not version or version.lower() == "unknown":
                continue
            out.setdefault(
                (repo_id, base, version.lower()),
                Component(
                    repository_id=repo_id,
                    repository=str(repo.get("name") or repo_id),
                    identity_key=str(repo.get("identity_key") or ""),
                    product=str(entry.get("product") or base.rsplit("/", 1)[-1]),
                    version=version,
                    purl=f"{base}@{version}",
                ),
            )
            if len(out) >= MAX_COMPONENTS:
                return list(out.values())
    return list(out.values())


def node_key(node: Mapping[str, Any]) -> tuple[str, str]:
    """``(purl base, version)`` of an OpenCTI Software: the PURL's version, else the observable's."""
    base, version = purl_parts(str(node.get("x_opencti_purl") or ""))
    return base, (version or str(node.get("version") or "").strip()).lower()


def match_software(found: Iterable[Mapping[str, Any]], wanted: Iterable[Component]) -> dict[str, list[Component]]:
    """OpenCTI Software id -> the components it stands for (same PURL base, same version) (pure)."""
    by_key: dict[tuple[str, str], list[Component]] = {}
    for comp in wanted:
        base, _ = purl_parts(comp.purl)
        by_key.setdefault((base, comp.version.lower()), []).append(comp)
    out: dict[str, list[Component]] = {}
    for node in found:
        node_id = str(node.get("id") or "")
        hits = by_key.get(node_key(node))
        if node_id and hits:
            out[node_id] = hits
    return out


def signal_fields(comp: Component, vuln: Mapping[str, Any], fixed_version: str | None) -> dict[str, Any] | None:
    """The signal of one vulnerable component (pure); ``None`` without the vulnerability's id."""
    vuln_id = str(vuln.get("id") or "").strip()
    if not vuln_id:
        return None
    name = str(vuln.get("name") or "").strip()
    evidence: dict[str, Any] = {
        "event": "opencti_dependency_vulnerability",
        "affected_package": comp.product,
        "installed_version": comp.version,
        "purl": comp.purl,
        "repository": comp.repository,
        "vulnerability": name,
        "opencti_vulnerability_id": vuln_id,
    }
    if name.upper().startswith("CVE-"):
        evidence["cve_id"] = name.upper()
    for field_name, key in (("cvss", "x_opencti_cvss_base_score"), ("epss", "x_opencti_epss_score")):
        if vuln.get(key) is not None:
            evidence[field_name] = vuln.get(key)
    if vuln.get("x_opencti_cisa_kev"):
        evidence["kev"] = True
    if fixed_version:
        evidence["fixed_version"] = fixed_version[:100]
    assets = [comp.repository, comp.repository_id, comp.identity_key]
    evidence["assets"] = list(dict.fromkeys(a for a in assets if a))
    base, _ = purl_parts(comp.purl)
    external_id = f"{comp.identity_key or comp.repository_id}|{base}|{vuln_id}"[:300]
    return {"external_id": external_id, "ref": comp.product, "label": comp.product, "evidence": evidence}


def fixes_by_vulnerability(relations: Iterable[Mapping[str, Any]]) -> dict[str, list[tuple[str, str]]]:
    """Vulnerability id -> ``(package key, version)`` of each Software OpenCTI says remediates it (pure).

    The package key is the PURL base, else ``name:<lower-cased name>``; a Software with
    neither names no package and is left out, so its version can never be taken as a fix.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for rel in relations:
        vuln_id = str((rel.get("to") or {}).get("id") or "")
        source = rel.get("from") or {}
        base, version = purl_parts(str(source.get("x_opencti_purl") or ""))
        version = version or str(source.get("version") or "").strip()
        name = str(source.get("name") or "").strip().lower()
        key = base or (f"name:{name}" if name else "")
        if vuln_id and version and key:
            out.setdefault(vuln_id, []).append((key, version))
    return out


def fixed_version_for(comp: Component, fixes: Iterable[tuple[str, str]]) -> str | None:
    """The smallest remediating version above the installed one, of the same package only.

    Same package: the same PURL base, or, for a Software OpenCTI holds without a PURL,
    the same name as the component's package.
    """
    from app.services import ctem_fixed_version as fixed_versions

    base, _ = purl_parts(comp.purl)
    names = {f"name:{base.rsplit('/', 1)[-1]}", f"name:{comp.product.strip().lower()}"}
    versions = [v for key, v in fixes if key == base or key in names]
    return fixed_versions.smallest_fix_above(versions, comp.version)


# ── Reading and recording (I/O) ───────────────────────────────────────


async def read_matches(
    client: Any, base_url: str, headers: dict[str, str], wanted: list[Component]
) -> list[tuple[Component, dict[str, Any], str | None]] | None:
    """The ``(component, vulnerability, fixed version)`` OpenCTI knows; ``None`` when it could not answer."""
    from app.integrations.providers.opencti._common import _graphql
    from app.services.ctem_cases_source import _OCTI_STACK_VULNS
    from app.services.ctem_metrics_collector import _fg

    purls = sorted({c.purl for c in wanted} | {purl_parts(c.purl)[0] for c in wanted})
    found: list[dict[str, Any]] = []
    for start in range(0, len(purls), _PAGE):
        chunk = purls[start : start + _PAGE]
        data, err = await _graphql(
            client,
            base_url,
            headers,
            _OCTI_SOFTWARE_BY_PURL,
            {"filters": _fg([{"key": ["x_opencti_purl"], "values": chunk}]), "first": _PAGE * 4},
        )
        if err:
            logger.info("CTEM dependency matching: OpenCTI software unavailable: %s", err)
            return None
        found.extend(
            (e or {}).get("node") or {} for e in ((data or {}).get("stixCyberObservables") or {}).get("edges") or []
        )
    matched = match_software(found, wanted)
    if not matched:
        return []
    relations: list[dict[str, Any]] = []
    ids = sorted(matched)
    for start in range(0, len(ids), _PAGE):
        after: str | None = None
        while len(relations) < MAX_RELATIONS:
            data, err = await _graphql(
                client,
                base_url,
                headers,
                _OCTI_STACK_VULNS,
                {"from": ids[start : start + _PAGE], "first": _PAGE, "after": after},
            )
            if err:
                logger.info("CTEM dependency matching: OpenCTI vulnerabilities unavailable: %s", err)
                return None
            conn = (data or {}).get("stixCoreRelationships") or {}
            relations.extend((e or {}).get("node") or {} for e in conn.get("edges") or [])
            info = conn.get("pageInfo") or {}
            after = info.get("endCursor")
            if not info.get("hasNextPage") or not after:
                break
    vuln_ids = sorted({str((r.get("to") or {}).get("id")) for r in relations if (r.get("to") or {}).get("id")})
    fixes: dict[str, list[tuple[str, str]]] = {}
    for start in range(0, len(vuln_ids), _PAGE):
        data, err = await _graphql(
            client,
            base_url,
            headers,
            _OCTI_SOFTWARE_REMEDIATES_PURL,
            {"to": vuln_ids[start : start + _PAGE], "first": _PAGE * 2},
        )
        if err:  # no remediating Software is a known state: the fixed version stays unknown
            logger.info("CTEM dependency matching: OpenCTI remediating software unavailable: %s", err)
            continue
        for vid, pairs in fixes_by_vulnerability(
            (e or {}).get("node") or {} for e in ((data or {}).get("stixCoreRelationships") or {}).get("edges") or []
        ).items():
            fixes.setdefault(vid, []).extend(pairs)
    out: list[tuple[Component, dict[str, Any], str | None]] = []
    for rel in relations:
        software_id = str((rel.get("from") or {}).get("id") or "")
        vuln = rel.get("to") or {}
        for comp in matched.get(software_id, []):
            out.append((comp, vuln, fixed_version_for(comp, fixes.get(str(vuln.get("id") or ""), []))))
    return out


async def record_matches(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: str,
    software: Mapping[str, list[dict[str, Any]]],
    repositories: Mapping[str, Mapping[str, str]],
) -> dict[str, int]:
    """Look the repositories' components up in OpenCTI and record each vulnerable one; never raises.

    The caller commits; the case events are raised after that commit.
    """
    from app.core.config import settings

    counts = {"components": 0, "recorded": 0}
    if settings.is_copilot_like or not software:
        return counts
    wanted = components(software, repositories)
    counts["components"] = len(wanted)
    if not wanted:
        return counts
    try:
        import httpx

        from app.services import ctem_case_loop, ctem_scope_gate_store, ctem_signals
        from app.services.ctem_code_findings import _publish_after_commit
        from app.services.ctem_metrics_collector import _user_creds

        creds = await _user_creds(db, user_id, "opencti")
        if creds is None:
            return counts
        base_url, headers = creds
        async with httpx.AsyncClient(timeout=30) as client:
            matches = await read_matches(client, base_url, headers, wanted)
        if not matches:
            return counts
        pending = ctem_case_loop.PendingEvents()
        for comp, vuln, fixed in matches:
            fields = signal_fields(comp, vuln, fixed)
            if fields is None:
                continue
            async with db.begin_nested():
                signal, created = await ctem_signals.record_signal(
                    db,
                    organization_id=organization_id,
                    signal=ctem_signals.SignalInput(
                        trigger_path=TRIGGER_PATH,
                        lens=LENS,
                        source_provider=PROVIDER,
                        source_external_id=fields["external_id"],
                        subject_kind=SUBJECT_KIND,
                        subject_ref=fields["ref"],
                        subject_label=fields["label"],
                        evidence=fields["evidence"],
                    ),
                    recorded_via="listener",
                    recorded_by=ctem_signals.SYSTEM_ACTOR,
                )
            found = await ctem_scope_gate_store.evaluate_after_record(db, signal, created=created)
            pending.items.extend(found.items)
            pending.verticals |= found.verticals
            pending.frames.extend(found.frames)
            counts["recorded"] += int(created)
        _publish_after_commit(db, pending)
    except Exception:
        logger.warning("CTEM dependency matching not recorded", exc_info=True)
    return counts


def schedule_after_commit(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: str,
    software: Mapping[str, list[dict[str, Any]]],
    repositories: Mapping[str, Mapping[str, str]],
) -> None:
    """Run :func:`record_matches` in a session of its own once the caller's commit lands."""
    from sqlalchemy import event

    if not software or not user_id:
        return
    soft = {k: list(v) for k, v in software.items()}
    repos = {k: dict(v) for k, v in repositories.items()}

    async def _run() -> None:
        from app.core.database import async_session

        try:
            async with async_session() as session:
                await record_matches(
                    session, organization_id=organization_id, user_id=user_id, software=soft, repositories=repos
                )
                await session.commit()
        except Exception:
            logger.warning("CTEM dependency matching failed", exc_info=True)

    def _after_commit(_session: Any) -> None:
        try:
            asyncio.get_running_loop().create_task(_run())
        except RuntimeError:  # no loop: the next sync matches them
            logger.debug("CTEM dependency matching left to the next sync")

    event.listen(db.sync_session, "after_commit", _after_commit, once=True)
