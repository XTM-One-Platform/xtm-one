# SPDX-License-Identifier: Apache-2.0

"""The outside lens, first version (CTEM-26, #1110): the attack surface pass.

An opt-in, scheduled pass reads the outside view of the organization's owned
domains and address ranges from every connected attack-surface provider the
profile declares (``READERS``: Shodan, Google Threat Intelligence ASM), merges
them into one observation per public host
(``models/ctem_surface_observation.py``) and diffs it, in code, against the
previous pass and against the profile. Each difference is a signal of the
outside lens (``services/ctem_signals.record_signal``, recorded by ``system``):

- path 5, new exposure: a declared asset that is not internet-facing answers,
  or a declared asset answers on a port it did not answer on before;
- path 6, stale or orphaned asset: a name that resolves to nothing, a name of
  an owned domain whose address is outside the owned ranges, or a host unseen
  for ``attack_surface.stale_after_days``;
- path 7, attack-surface drift: a host that is neither declared nor seen by an
  earlier pass (the reconcile leaves it ``to_validate``).

The first pass sets the baseline: it raises paths 5 and 6 (they compare with
the profile) and no path 7. The model writes nothing here: the spine (scope
gate, cases) decides what each signal becomes. No gate. Design:
``dev-docs/ctem-attack-surface.md``.
"""

from __future__ import annotations

import ipaddress
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_surface_observation import CtemSurfaceObservation

logger = logging.getLogger(__name__)

#: The providers the pass can read today, by integration provider name (``READERS`` builds them).
SUPPORTED_PROVIDERS: tuple[str, ...] = ("shodan", "gti")
#: Providers XTM One connects that cannot fill an observation, with the reason (``dev-docs/ctem-attack-surface.md``).
NOT_SUPPORTED: dict[str, str] = {
    "securityscorecard": "the API answers scores and findings per issue type, not a host inventory with its open ports",
    "socradar": "the ASM digital asset list carries names, source and monitoring state, no address and no open ports",
    "domaintools": "DNS and WHOIS history only: no open ports, and its view is the registrar's, not the reachable surface",
    "rapid7": "InsightVM assets are the inside scan of managed assets, not the outside view",
}
#: The source label of a pass signal.
SOURCE = "attack_surface"


@dataclass(frozen=True)
class Port:
    port: int
    transport: str = "tcp"
    product: str = ""

    @property
    def key(self) -> str:
        return f"{self.port}/{self.transport}"


@dataclass
class Observation:
    """What the provider saw of one public host in this pass."""

    host: str
    ip: str = ""
    ports: list[Port] = field(default_factory=list)
    cert_names: list[str] = field(default_factory=list)
    dns_target: str = ""
    #: False for a name the provider lists with no record behind it.
    resolves: bool = True
    #: The providers that saw the host in this pass (set by ``merge_observations``).
    providers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Baseline:
    """What an earlier pass kept of one host (``signalled``: the signal keys still holding)."""

    host: str
    ports: tuple[str, ...]
    last_seen_at: datetime
    signalled: tuple[str, ...] = ()


@dataclass(frozen=True)
class Finding:
    """One difference the pass records as a signal."""

    trigger_path: str
    host: str
    reason: str
    detail: str
    evidence: dict[str, Any]

    @property
    def dedupe_key(self) -> str:
        return f"{self.trigger_path}:{SOURCE}:{self.host}:{self.detail}"[:200]


class SurfaceReader(Protocol):
    """A provider's outside view of owned domains and ranges."""

    provider: str

    async def observe(self, domains: list[str], ranges: list[str]) -> list[Observation]: ...


# ── the diff, pure ──────────────────────────────────────────────────────


def host_key(value: object) -> str:
    from app.services.ctem_scope_gate import stable_key

    return stable_key(value)


def _in_ranges(ip: str, ranges: Iterable[str]) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for block in ranges:
        try:
            if address in ipaddress.ip_network(block, strict=False):
                return True
        except ValueError:
            continue
    return False


def _owned_domain(host: str, domains: Iterable[str]) -> str:
    for owned in domains:
        if host == owned or host.endswith(f".{owned}"):
            return owned
    return ""


def _declared(profile: Any) -> dict[str, Any]:
    """The profile's assets by every key a host can match (name, external id)."""
    out: dict[str, Any] = {}
    for asset in getattr(profile, "assets_exposure", []) or []:
        for key in (host_key(asset.name), host_key(asset.external_id)):
            if key:
                out.setdefault(key, asset)
    return out


def diff(
    profile: Any,
    observations: list[Observation],
    baseline: dict[str, Baseline],
    *,
    now: datetime,
    provider: str,
) -> list[Finding]:
    """The differences between this pass, the previous one and the profile (no I/O)."""
    surface = getattr(profile, "attack_surface", None)
    stale_days = int(getattr(surface, "stale_after_days", 14) or 14)
    domains = list(getattr(profile, "owned_domains", []) or [])
    ranges = list(getattr(profile, "address_ranges", []) or [])
    declared = _declared(profile)
    first_pass = not baseline
    found: list[Finding] = []
    seen: set[str] = set()

    def _evidence(obs: Observation, **extra: Any) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source": SOURCE,
            "provider": provider,
            "hostnames": [obs.host],
            "ports": [p.key for p in obs.ports],
        }
        if obs.ip:
            data["ips"] = [obs.ip]
        if obs.dns_target:
            data["dns_target"] = obs.dns_target
        if obs.cert_names:
            data["cert_names"] = obs.cert_names[:20]
        if len(obs.providers) > 1:
            data["providers"] = obs.providers
        data.update(extra)
        return data

    for obs in observations:
        host = host_key(obs.host)
        if not host or host in seen:
            continue
        seen.add(host)
        asset = declared.get(host) or (declared.get(host_key(obs.ip)) if obs.ip else None)
        before = baseline.get(host)
        owned = _owned_domain(host, domains)
        # path 6: a name that resolves to nothing, or an owned name pointing outside the owned ranges.
        if not obs.resolves or (not obs.ip and not obs.dns_target and not obs.ports):
            found.append(
                Finding(
                    "stale_asset",
                    host,
                    "the name resolves to nothing",
                    "dangling",
                    _evidence(obs, baseline="resolves", observed="no record"),
                )
            )
            continue
        if owned and ranges and obs.ip and not _in_ranges(obs.ip, ranges):
            found.append(
                Finding(
                    "stale_asset",
                    host,
                    f"a name of {owned} points to {obs.ip}, outside the owned address ranges",
                    f"unowned:{obs.ip}",
                    _evidence(obs, owned_domain=owned, address_ranges=ranges[:20]),
                )
            )
        if asset is not None:
            # path 5: a declared asset that should not answer, or a new port on a declared asset.
            if asset.internet_facing == "no" and obs.ports:
                found.append(
                    Finding(
                        "new_exposure",
                        host,
                        f"{asset.name} is declared not internet-facing and answers",
                        "answers",
                        _evidence(obs, asset=asset.name, declared_internet_facing="no"),
                    )
                )
            if before is not None:
                for port in obs.ports:
                    if port.key not in before.ports:
                        found.append(
                            Finding(
                                "new_exposure",
                                host,
                                f"{asset.name} answers on {port.key}"
                                + (f" ({port.product})" if port.product else "")
                                + ", not seen by the previous pass",
                                f"port:{port.key}",
                                _evidence(obs, asset=asset.name, new_port=port.key, previous_ports=list(before.ports)),
                            )
                        )
            continue
        # path 7: anything new against the baseline (never on the first pass, which sets it).
        if before is None and not first_pass:
            found.append(
                Finding(
                    "surface_drift",
                    host,
                    "a host neither declared nor seen by an earlier pass",
                    "new_host",
                    _evidence(obs, baseline="absent"),
                )
            )
        elif before is not None:
            for port in obs.ports:
                if port.key not in before.ports:
                    found.append(
                        Finding(
                            "surface_drift",
                            host,
                            f"an undeclared host answers on {port.key}, not seen by the previous pass",
                            f"port:{port.key}",
                            _evidence(obs, new_port=port.key, previous_ports=list(before.ports)),
                        )
                    )

    # path 6: a host an earlier pass saw that no longer appears for N days.
    limit = now - timedelta(days=stale_days)
    for host, before in baseline.items():
        if host in seen or before.last_seen_at > limit:
            continue
        days = max(stale_days, (now - before.last_seen_at).days)
        found.append(
            Finding(
                "stale_asset",
                host,
                f"unseen for {days} days",
                "unseen",
                {
                    "source": SOURCE,
                    "provider": provider,
                    "hostnames": [host],
                    "last_seen_at": before.last_seen_at.isoformat(),
                    "stale_after_days": stale_days,
                },
            )
        )
    held = {host: set(before.signalled) for host, before in baseline.items()}
    return [f for f in found if f.dedupe_key not in held.get(f.host, set())]


# ── the pass ────────────────────────────────────────────────────────────


def _still_holding(signalled: list[str], obs: Observation) -> list[str]:
    """The signal keys of a host that still describe it: a closed port, a name that resolves
    again or a host seen again may raise its signal again the next time it happens."""
    open_ports = {f"port:{p.key}" for p in obs.ports}
    marker = f":{SOURCE}:{host_key(obs.host)}:"
    kept: list[str] = []
    for key in signalled:
        detail = key.split(marker, 1)[1] if marker in key else ""
        if detail.startswith("port:") and detail not in open_ports:
            continue
        if detail == "unseen":
            continue
        if detail == "dangling" and obs.resolves and (obs.ip or obs.dns_target or obs.ports):
            continue
        if detail.startswith("unowned:") and detail != f"unowned:{obs.ip}":
            continue
        kept.append(key)
    return kept


async def _baseline(db: AsyncSession, organization_id: uuid.UUID) -> dict[str, CtemSurfaceObservation]:
    rows = (
        (
            await db.execute(
                select(CtemSurfaceObservation).where(CtemSurfaceObservation.organization_id == organization_id)
            )
        )
        .scalars()
        .all()
    )
    return {row.host: row for row in rows}


def _as_baseline(row: CtemSurfaceObservation) -> Baseline:
    ports = tuple(f"{p.get('port')}/{p.get('transport') or 'tcp'}" for p in (row.ports or []) if isinstance(p, dict))
    seen = row.last_seen_at if row.last_seen_at.tzinfo else row.last_seen_at.replace(tzinfo=UTC)
    return Baseline(host=row.host, ports=ports, last_seen_at=seen, signalled=tuple(row.signalled or []))


async def run_pass(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    profile: Any,
    reader: SurfaceReader,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One pass: observe, diff, record the signals and store the new baseline. The caller commits.

    Answers the counts and the case events to raise after the commit
    (``ctem_case_loop.publish_pending``) under ``pending``.
    """
    from app.services import ctem_case_loop, ctem_scope_gate_store, ctem_signals

    at = now or datetime.now(UTC)
    domains = list(getattr(profile, "owned_domains", []) or [])
    ranges = list(getattr(profile, "address_ranges", []) or [])
    observations = await reader.observe(domains, ranges)
    rows = await _baseline(db, organization_id)
    findings = diff(
        profile, observations, {h: _as_baseline(r) for h, r in rows.items()}, now=at, provider=reader.provider
    )

    for obs in observations:
        host = host_key(obs.host)
        if not host:
            continue
        row = rows.get(host)
        ports = [{"port": p.port, "transport": p.transport, "product": p.product} for p in obs.ports]
        if row is None:
            row = CtemSurfaceObservation(organization_id=organization_id, host=host, first_seen_at=at, signalled=[])
            db.add(row)
            rows[host] = row
        row.ip = obs.ip[:64]
        row.ports = ports
        row.cert_names = obs.cert_names[:50]
        row.dns_target = obs.dns_target[:300]
        row.source_provider = reader.provider
        row.last_seen_at = at
        row.signalled = _still_holding(row.signalled or [], obs)
    await db.flush()

    pending = ctem_case_loop.PendingEvents()
    counts: dict[str, int] = {"observed": len(observations), "recorded": 0}
    for finding in findings:
        signal, created = await ctem_signals.record_signal(
            db,
            organization_id=organization_id,
            signal=ctem_signals.SignalInput(
                trigger_path=finding.trigger_path,
                source_provider=reader.provider,
                subject_kind="asset",
                subject_ref=finding.host,
                lens="outside",
                subject_label=f"{finding.host}: {finding.reason}",
                observed_at=at,
                evidence={**finding.evidence, "reason": finding.reason},
                dedupe_key=finding.dedupe_key,
            ),
            recorded_via="listener",
            recorded_by=ctem_signals.SYSTEM_ACTOR,
            now=at,
        )
        events = await ctem_scope_gate_store.evaluate_after_record(db, signal, created=created)
        if events:
            pending.items.extend(events.items)
            pending.verticals |= events.verticals
            pending.frames.extend(events.frames)
        row = rows.get(finding.host)
        if row is not None:
            row.signalled = list(dict.fromkeys([*(row.signalled or []), finding.dedupe_key]))
        counts["recorded"] += 1
        counts[finding.trigger_path] = counts.get(finding.trigger_path, 0) + 1
    return {"counts": counts, "pending": pending}


# ── the provider reader: Shodan ─────────────────────────────────────────

#: One page per query: a pass costs at most one query credit per domain and per range.
_SHODAN_URL = "https://api.shodan.io"


class ShodanSurfaceReader:
    """The outside view from Shodan: DNS records of each owned domain, banners of each range and domain."""

    provider = "shodan"

    def __init__(self, api_key: str):
        self._key = api_key

    async def _get(self, path: str, action: str, params: dict[str, Any] | None = None) -> Any:
        from app.core.egress import egress_client
        from app.integrations.http import DEFAULT_TIMEOUT, safe_request

        query = {**(params or {}), "key": self._key}
        async with egress_client(timeout=DEFAULT_TIMEOUT) as client:
            resp, err = await safe_request(client, "get", f"{_SHODAN_URL}{path}", "Shodan", action, params=query)
        if err or resp is None:
            raise SurfaceReadError(err or f"Shodan did not answer to {action}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SurfaceReadError(f"Shodan answered a non-JSON body to {action}") from exc

    async def observe(self, domains: list[str], ranges: list[str]) -> list[Observation]:
        from urllib.parse import quote

        hosts: dict[str, Observation] = {}

        def _host(name: str) -> Observation:
            key = host_key(name)
            if key not in hosts:
                hosts[key] = Observation(host=key)
            return hosts[key]

        for domain in domains:
            data = await self._get(f"/dns/domain/{quote(domain, safe='')}", f"read the DNS records of {domain}") or {}
            records = data.get("data") or []
            named: set[str] = set()
            for record in records:
                sub = str(record.get("subdomain") or "").strip()
                name = f"{sub}.{domain}" if sub else domain
                kind = str(record.get("type") or "").upper()
                value = str(record.get("value") or "").strip().rstrip(".").lower()
                # A record of any type (MX, TXT, NS included) backs the name: only a
                # listed name that no record backs resolves to nothing (path 6).
                named.add(host_key(name))
                if kind in {"A", "AAAA", "CNAME"}:
                    obs = _host(name)
                    if value:
                        obs.dns_target = obs.dns_target or value
                        if kind in {"A", "AAAA"}:
                            obs.ip = obs.ip or value
                    else:
                        obs.resolves = False
            for sub in data.get("subdomains") or []:
                name = f"{sub}.{domain}"
                if host_key(name) not in named:
                    _host(name).resolves = False
            self._banners(await self._search(f"hostname:{domain}"), _host)
        for block in ranges:
            self._banners(await self._search(f"net:{block}"), _host)
        return list(hosts.values())

    async def _search(self, query: str) -> list[dict[str, Any]]:
        data = await self._get("/shodan/host/search", f"search {query}", {"query": query, "minify": "false"}) or {}
        return [m for m in data.get("matches") or [] if isinstance(m, dict)]

    @staticmethod
    def _banners(matches: list[dict[str, Any]], host: Any) -> None:
        for match in matches:
            ip = str(match.get("ip_str") or "")
            names = [str(h) for h in match.get("hostnames") or []] or ([ip] if ip else [])
            cert = ((match.get("ssl") or {}).get("cert") or {}) if isinstance(match.get("ssl"), dict) else {}
            cert_names = [str(cert.get("subject", {}).get("CN") or "")] if isinstance(cert.get("subject"), dict) else []
            try:
                port = Port(
                    int(str(match.get("port"))), str(match.get("transport") or "tcp"), str(match.get("product") or "")
                )
            except (TypeError, ValueError):
                continue
            for name in names:
                obs = host(name)
                obs.ip = obs.ip or ip
                obs.resolves = True
                if port.key not in {p.key for p in obs.ports}:
                    obs.ports.append(port)
                obs.cert_names = list(dict.fromkeys([*obs.cert_names, *[c for c in cert_names if c]]))


# ── the provider reader: Google Threat Intelligence ASM ────────────────

#: Pages read per project and projects read per pass: a bounded cost per pass.
_GTI_MAX_PAGES = 10
_GTI_MAX_PROJECTS = 5
_GTI_IP_ENTITY = "Intrigue::Entity::IpAddress"


def _gti_ports(summary: dict[str, Any]) -> list[Port]:
    """The open ports of an ASM IP entity (``summary.ports.tcp`` / ``udp``: a list, or null)."""
    raw_ports = summary.get("ports")
    ports: dict[str, Any] = raw_ports if isinstance(raw_ports, dict) else {}
    out: list[Port] = []
    for transport in ("tcp", "udp"):
        for item in ports.get(transport) or []:
            raw = item.get("port") if isinstance(item, dict) else item
            try:
                port = Port(int(str(raw)), transport)
            except (TypeError, ValueError):
                continue
            if port.key not in {p.key for p in out}:
                out.append(port)
    return out


class GtiAsmSurfaceReader:
    """The outside view from Google Threat Intelligence ASM: the in-scope IP entities of the
    account's ASM projects, each with its open ports and the names that resolve to it.

    ASM discovers the surface from its own seeds; the reader keeps what falls under the
    profile's owned domains or address ranges. ASM lists no unresolved name, so path 6
    "resolves to nothing" comes from DNS-reading providers only.
    """

    provider = "gti"

    def __init__(self, api_key: str):
        from app.integrations.providers.gti._common import build_gti_headers

        self._headers = build_gti_headers(api_key)

    async def _get(self, path: str, action: str, params: dict[str, Any] | None = None, project: str = "") -> Any:
        from app.core.egress import egress_client
        from app.integrations.http import DEFAULT_TIMEOUT, safe_request
        from app.integrations.providers.gti._common import GTI_ASM_API

        headers = {**self._headers, "PROJECT-ID": project} if project else self._headers
        async with egress_client(timeout=DEFAULT_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{GTI_ASM_API}{path}",
                "Google Threat Intelligence",
                action,
                headers=headers,
                params=params,
            )
        if err or resp is None:
            raise SurfaceReadError(err or f"Google Threat Intelligence did not answer to {action}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SurfaceReadError(f"Google Threat Intelligence answered a non-JSON body to {action}") from exc

    async def observe(self, domains: list[str], ranges: list[str]) -> list[Observation]:
        from urllib.parse import quote

        from app.integrations.providers.gti.asm import _next_token, _result_list

        projects = _result_list(await self._get("/projects", "list the ASM projects"))
        hosts: dict[str, Observation] = {}
        path = f"/search/entities/{quote(f'type:{_GTI_IP_ENTITY}', safe=':')}"
        for project in projects[:_GTI_MAX_PROJECTS]:
            project_id = str(project.get("id") or "").strip()
            if not project_id.isdigit():
                continue
            token = ""
            for _ in range(_GTI_MAX_PAGES):
                params: dict[str, Any] = {"page_size": 100}
                if token:
                    params["page_token"] = token
                payload = await self._get(
                    path, f"search the IP entities of ASM project {project_id}", params, project_id
                )
                for entity in _result_list(payload):
                    self._entity(entity, domains, ranges, hosts)
                token = _next_token(payload) or ""
                if not token:
                    break
        return list(hosts.values())

    @staticmethod
    def _entity(entity: dict[str, Any], domains: list[str], ranges: list[str], hosts: dict[str, Observation]) -> None:
        raw_summary = entity.get("summary")
        summary: dict[str, Any] = raw_summary if isinstance(raw_summary, dict) else {}
        if summary.get("scoped") is False:
            return
        ip = str(entity.get("name") or "").strip()
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return
        names = [host_key(n) for n in summary.get("resolutions") or [] if _owned_domain(host_key(n), domains)]
        if not names:
            if not _in_ranges(ip, ranges):
                return
            names = [ip]
        ports = _gti_ports(summary)
        for name in names:
            obs = hosts.setdefault(name, Observation(host=name))
            obs.ip = obs.ip or ip
            for port in ports:
                if port.key not in {p.key for p in obs.ports}:
                    obs.ports.append(port)


class SurfaceReadError(RuntimeError):
    """The provider could not be read; the pass records nothing and keeps the baseline."""


#: The reader of each supported provider, built from the integration's credential.
READERS: dict[str, Any] = {"shodan": ShodanSurfaceReader, "gti": GtiAsmSurfaceReader}


def merge_observations(per_reader: list[tuple[str, list[Observation]]]) -> list[Observation]:
    """One observation per host across providers: ports and certificate names joined, the
    first address and DNS target kept, resolving when any provider saw it resolve."""
    merged: dict[str, Observation] = {}
    for provider, observations in per_reader:
        for obs in observations:
            host = host_key(obs.host)
            if not host:
                continue
            into = merged.get(host)
            if into is None:
                into = merged[host] = Observation(host=host, resolves=obs.resolves)
            elif obs.resolves:
                into.resolves = True
            into.ip = into.ip or obs.ip
            into.dns_target = into.dns_target or obs.dns_target
            for port in obs.ports:
                if port.key not in {p.key for p in into.ports}:
                    into.ports.append(port)
            into.cert_names = list(dict.fromkeys([*into.cert_names, *obs.cert_names]))
            if provider not in into.providers:
                into.providers.append(provider)
    return list(merged.values())


class MergedSurfaceReader:
    """Several providers read as one: every reader must answer, else the pass records nothing
    (a partial view would age the hosts only the failed provider sees into path 6)."""

    def __init__(self, readers: list[SurfaceReader]):
        self.readers = readers
        self.provider = "+".join(r.provider for r in readers)[:40]

    async def observe(self, domains: list[str], ranges: list[str]) -> list[Observation]:
        per_reader = [(r.provider, await r.observe(domains, ranges)) for r in self.readers]
        return merge_observations(per_reader)


def declared_integrations(surface: Any) -> list[str]:
    """The integration ids of the pass, each once: ``integration`` then ``integrations``."""
    ids = [getattr(surface, "integration", "") or "", *(getattr(surface, "integrations", None) or [])]
    return list(dict.fromkeys(i.strip() for i in ids if i and i.strip()))


async def _reader_of(db: AsyncSession, integration_id: str) -> SurfaceReader | str:
    from app.models.integration import Integration
    from app.services.integration_tools import _get_token

    try:
        integration = await db.get(Integration, uuid.UUID(integration_id))
    except ValueError:
        return f"the attack surface provider {integration_id!r} is not an integration id"
    if integration is None:
        return f"the attack surface integration {integration_id} no longer exists"
    build = READERS.get(integration.provider)
    if build is None:
        why = NOT_SUPPORTED.get(integration.provider)
        return f"{integration.provider} has no attack surface pass" + (f": {why}" if why else " yet")
    owner = str(integration.user_id) if integration.user_id else None
    token = await _get_token(db, integration_id, integration.provider, user_id=owner, integ=integration)
    if not token:
        return f"no credential could be resolved for the {integration.provider} attack surface integration"
    reader: SurfaceReader = build(str(token).strip())
    return reader


async def reader_for(db: AsyncSession, profile: Any) -> SurfaceReader | str:
    """The reader of the declared provider integrations, or the reason there is none.

    The credential is each integration owner's, as the CTEM ticketing sync does:
    the integration was declared by someone holding the profile edit right, and
    the token only reads the outside view of the owned domains and ranges. A
    declared integration that cannot be read is skipped with its reason logged;
    the pass runs on the others.
    """
    ids = declared_integrations(getattr(profile, "attack_surface", None))
    if not ids:
        return "no attack surface provider is declared in the profile"
    readers: list[SurfaceReader] = []
    reasons: list[str] = []
    for integration_id in ids:
        reader = await _reader_of(db, integration_id)
        if isinstance(reader, str):
            reasons.append(reader)
        else:
            readers.append(reader)
    if not readers:
        return "; ".join(reasons)
    for reason in reasons:
        logger.warning("CTEM attack surface pass: an integration is skipped: %s", reason)
    return readers[0] if len(readers) == 1 else MergedSurfaceReader(readers)


# ── the schedule ────────────────────────────────────────────────────────


_CADENCE_SLOT_KEY = "ctem:attack-surface:pass"


async def pass_due(cadence_hours: int) -> bool:
    """Take the pass slot for this cadence window; True when this tick runs it (or Redis is unavailable)."""
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        return bool(await client.set(_CADENCE_SLOT_KEY, "1", nx=True, ex=max(1, cadence_hours) * 3600))
    except Exception:  # no Redis: the pass runs; it is idempotent (signals dedupe on their key)
        return True


async def release_pass_slot() -> None:
    """Give the slot back after a pass that did not complete, so the next tick retries it."""
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        await client.delete(_CADENCE_SLOT_KEY)
    except Exception:  # no Redis: there is no slot to give back
        logger.debug("CTEM attack surface pass slot not released: Redis unavailable")


async def scheduled_pass(db: AsyncSession) -> dict[str, Any] | None:
    """The tick's pass on the primary organization when its profile turned it on; ``None`` when off."""
    from app.services import ctem_case_loop
    from app.services import organization_context as org_context
    from app.services.organizations import get_primary

    primary = await get_primary(db)
    if primary is None:
        return None
    row = await org_context.current_version(db, primary.id)
    profile = org_context.parse_profile(row.profile if row else {})
    surface = profile.attack_surface
    if not surface.enabled or not (profile.owned_domains or profile.address_ranges):
        return None
    if not await pass_due(surface.cadence_hours):
        return None
    completed = False
    try:
        reader = await reader_for(db, profile)
        if isinstance(reader, str):
            logger.info("CTEM attack surface pass skipped: %s", reader)
            return {"skipped": reader}
        try:
            result = await run_pass(db, organization_id=primary.id, profile=profile, reader=reader)
        except SurfaceReadError as exc:
            await db.rollback()
            logger.warning("CTEM attack surface pass: provider read failed: %s", exc)
            return {"skipped": str(exc)}
        await db.commit()
        completed = True
    finally:
        # The slot is kept only by a pass that committed: a skipped or failed
        # pass (no credential yet, a provider 5xx or rate limit) retries next tick.
        if not completed:
            await release_pass_slot()
    await ctem_case_loop.publish_pending(result["pending"])
    counts: dict[str, Any] = result["counts"]
    return counts
