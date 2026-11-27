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
the profile) and no path 7. A later pass raises a new host on path 7 only when
both its own read and the previous pass's read of the provider and scope that
saw it were complete, and the provider did not know the host before the
previous pass (#1498): a host a partial read missed joins the baseline
silently. Every committed pass is stored with what it read
(``models/ctem_surface_pass.py``). The model writes nothing here: the spine (scope
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
from app.models.ctem_surface_pass import CtemSurfacePass

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
    #: The reads that saw it, ``<provider>|<scope>`` (an owned domain, a range, ``*``).
    reads: list[str] = field(default_factory=list)
    #: The earliest time the provider's own data shows the host existed (a DNS record's
    #: ``last_seen``, a banner's ``timestamp``, an ASM entity's ``first_seen``).
    known_since: datetime | None = None


@dataclass(frozen=True)
class ReadCoverage:
    """What one provider query read in a pass: its scope, pages, records, whether it hit its bound,
    and whether it failed (``error``: the provider's answer), which leaves its scope unread."""

    provider: str
    scope: str
    kind: str
    pages: int
    records: int
    truncated: bool = False
    failed: bool = False
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "provider": self.provider,
            "scope": self.scope,
            "kind": self.kind,
            "pages": self.pages,
            "records": self.records,
            "truncated": self.truncated,
        }
        if self.failed:
            out["failed"] = True
            out["error"] = self.error[:300]
        return out


@dataclass(frozen=True)
class PreviousPass:
    """The pass the diff compares with: when it ran and the status of each of its reads."""

    at: datetime
    statuses: dict[str, str]


#: The mark the purge of every signal (#2578) leaves on a host whose new-host signal it
#: deleted: the next pass that sees the host raises it again once, without the provider-date
#: check (the provider saw it before the previous pass by definition), then drops the mark.
REARM_NEW_HOST = "rearm:new_host"


@dataclass(frozen=True)
class Baseline:
    """What an earlier pass kept of one host (``signalled``: the signal keys still holding;
    ``ip``: the address it was last seen on, so a host in scope only by its address keeps
    its unseen check)."""

    host: str
    ports: tuple[str, ...]
    last_seen_at: datetime
    signalled: tuple[str, ...] = ()
    ip: str = ""


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


def read_key(provider: str, scope: str) -> str:
    return f"{provider}|{scope}"


def unread_scopes(coverage: Iterable[ReadCoverage]) -> list[str]:
    """The scopes (owned domains, ranges) a failed query left unread in this pass."""
    return list(dict.fromkeys(c.scope for c in coverage if c.failed))


def read_statuses(coverage: Iterable[ReadCoverage]) -> dict[str, str]:
    """The status of each read of a pass: ``failed`` when a query of its scope failed,
    ``truncated`` when a query hit its page bound, ``empty`` when its queries answered
    nothing, else ``complete``."""
    grouped: dict[str, list[ReadCoverage]] = {}
    for item in coverage:
        grouped.setdefault(read_key(item.provider, item.scope), []).append(item)
    out: dict[str, str] = {}
    for key, items in grouped.items():
        if any(i.failed for i in items):
            out[key] = "failed"
        elif any(i.truncated for i in items):
            out[key] = "truncated"
        elif sum(i.records for i in items) == 0:
            out[key] = "empty"
        else:
            out[key] = "complete"
    return out


def _when(value: object) -> datetime | None:
    """A provider timestamp (ISO 8601, naive read as UTC), or ``None``."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _earliest(current: datetime | None, value: object) -> datetime | None:
    when = _when(value)
    if when is None:
        return current
    return when if current is None or when < current else current


def new_host_withheld(obs: Observation, previous: PreviousPass | None, statuses: dict[str, str]) -> str:
    """Why a host absent from the baseline is not a new host (path 7), or ``""`` when it is one.

    A host is new only when every read that saw it is complete in this pass and
    was complete in the previous one, and the provider's own data does not date
    it before the previous pass.
    """
    if previous is None:
        return "no_previous_pass"
    if obs.known_since is not None and obs.known_since <= previous.at:
        return "known_to_provider"
    if not obs.reads:
        return "read_unknown"
    for read in obs.reads:
        now_status = statuses.get(read) or "unknown"
        if now_status != "complete":
            return f"read_{now_status}"
        before_status = previous.statuses.get(read) or "missing"
        if before_status != "complete":
            return f"previous_read_{before_status}"
    return ""


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


def in_scope(host: str, ip: str, domains: Iterable[str], ranges: Iterable[str]) -> bool:
    """Whether a host a provider returned is the organization's own: a name under an owned
    domain, or an address (the host itself or its IP) inside an owned range.

    A provider answers other names too (the reverse DNS of a shared hosting IP, another
    tenant's name on the same address): those are context, never a host of the surface.
    """
    ranges = list(ranges)
    return bool(_owned_domain(host, domains) or (ip and _in_ranges(ip, ranges)) or _in_ranges(host, ranges))


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
    previous: PreviousPass | None = None,
    statuses: dict[str, str] | None = None,
    withheld: dict[str, str] | None = None,
    out_of_scope: dict[str, str] | None = None,
    unread: Iterable[str] = (),
) -> list[Finding]:
    """The differences between this pass, the previous one and the profile (no I/O).

    *previous* and *statuses* decide whether a host absent from the baseline is
    new (:func:`new_host_withheld`); a host that is not lands in *withheld*
    with the reason and joins the baseline silently.

    A host that is neither declared nor :func:`in_scope` raises nothing on any path: it
    lands in *out_of_scope* (host to IP) and is named as ``shared_ip_names`` in the
    evidence of the owned hosts on the same IP.

    *unread* names the scopes (owned domains, ranges) a failed query left unread: a
    baseline host under one of them is not aged into path 6 ``unseen`` by this pass.
    """
    surface = getattr(profile, "attack_surface", None)
    stale_days = int(getattr(surface, "stale_after_days", 14) or 14)
    domains = list(getattr(profile, "owned_domains", []) or [])
    ranges = list(getattr(profile, "address_ranges", []) or [])
    declared = _declared(profile)
    first_pass = not baseline
    found: list[Finding] = []
    seen: set[str] = set()
    foreign: dict[str, str] = {}
    for obs in observations:
        name = host_key(obs.host)
        if not name or name in declared or (obs.ip and host_key(obs.ip) in declared):
            continue
        if not in_scope(name, obs.ip, domains, ranges):
            foreign.setdefault(name, obs.ip)
    if out_of_scope is not None:
        out_of_scope.update(foreign)
    names_by_ip: dict[str, list[str]] = {}
    for name, ip in foreign.items():
        if ip:
            names_by_ip.setdefault(ip, []).append(name)

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
        if obs.ip and names_by_ip.get(obs.ip):
            data["shared_ip_names"] = names_by_ip[obs.ip][:20]
        data.update(extra)
        return data

    for obs in observations:
        host = host_key(obs.host)
        if not host or host in seen:
            continue
        seen.add(host)
        if host in foreign:
            continue
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
        # A host the purge re-armed (#2578) is raised again once, whatever the provider dates.
        rearmed = before is not None and REARM_NEW_HOST in before.signalled
        if rearmed or (before is None and not first_pass):
            why = "" if rearmed else new_host_withheld(obs, previous, statuses or {})
            if why:
                if withheld is not None:
                    withheld[host] = why
            else:
                found.append(
                    Finding(
                        "surface_drift",
                        host,
                        "a host neither declared nor seen by an earlier pass",
                        "new_host",
                        _evidence(
                            obs,
                            baseline="absent",
                            reads=list(obs.reads),
                            previous_pass_at=previous.at.isoformat() if previous else None,
                        ),
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
    unread = list(unread)
    unread_domains = [s for s in unread if "/" not in s]
    unread_ranges = [s for s in unread if "/" in s]
    for host, before in baseline.items():
        if host in seen or before.last_seen_at > limit:
            continue
        if _owned_domain(host, unread_domains) or (
            unread_ranges and (_in_ranges(before.ip, unread_ranges) or _in_ranges(host, unread_ranges))
        ):
            continue  # its scope was not read by this pass: unseen proves nothing
        if host not in declared and not in_scope(host, before.ip, domains, ranges):
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
        if key.startswith(REARM_PREFIX):  # consumed by the pass that saw the host again (#2578, #2634)
            continue
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
    return Baseline(
        host=row.host, ports=ports, last_seen_at=seen, signalled=tuple(row.signalled or []), ip=row.ip or ""
    )


async def _previous_pass(db: AsyncSession, organization_id: uuid.UUID) -> PreviousPass | None:
    """The latest committed pass of the organization, or ``None`` before the first recorded one."""
    row = (
        await db.execute(
            select(CtemSurfacePass)
            .where(CtemSurfacePass.organization_id == organization_id)
            .order_by(CtemSurfacePass.at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    at = row.at if row.at.tzinfo else row.at.replace(tzinfo=UTC)
    return PreviousPass(at=at, statuses={str(k): str(v) for k, v in (row.statuses or {}).items()})


def _kept_for_path(excluded: dict[str, set[tuple[str, str]]], path: str, providers: Iterable[str]) -> bool:
    """Whether a finding seen by *providers* is recorded on *path*: at least one of them is not excluded
    from it (#1445). A host no provider of the pass is named for keeps the old behaviour."""
    off = excluded.get(path, set())
    named = [p for p in providers if p]
    return not named or any((p, "pass") not in off for p in named)


async def run_pass(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    profile: Any,
    reader: SurfaceReader,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One pass: observe, diff, record the signals, store the new baseline and the pass summary.
    The caller commits.

    Answers the counts and the case events to raise after the commit
    (``ctem_case_loop.publish_pending``) under ``pending``.
    """
    at = now or datetime.now(UTC)
    domains = list(getattr(profile, "owned_domains", []) or [])
    ranges = list(getattr(profile, "address_ranges", []) or [])
    observations = await reader.observe(domains, ranges)
    coverage = reader_coverage(reader)
    statuses = read_statuses(coverage)
    unread = unread_scopes(coverage)
    previous = await _previous_pass(db, organization_id)
    rows = await _baseline(db, organization_id)
    withheld: dict[str, str] = {}
    out_of_scope: dict[str, str] = {}
    findings = diff(
        profile,
        observations,
        {h: _as_baseline(r) for h, r in rows.items()},
        now=at,
        provider=reader.provider,
        previous=previous,
        statuses=statuses,
        withheld=withheld,
        out_of_scope=out_of_scope,
        unread=unread,
    )

    for obs in observations:
        host = host_key(obs.host)
        if not host or host in out_of_scope:
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

    from app.services.ctem_signal_sources import parse_excluded

    seen_by = {host_key(obs.host): list(obs.providers or [reader.provider]) for obs in observations}
    counts: dict[str, int] = {"observed": len(observations), "recorded": 0}
    pending = await _record_findings(
        db,
        organization_id=organization_id,
        findings=findings,
        rows=rows,
        provider=reader.provider,
        at=at,
        excluded=parse_excluded(profile),
        seen_by=seen_by,
        counts=counts,
    )
    counts["new_host_withheld"] = len(withheld)
    counts["out_of_scope"] = len(out_of_scope)
    if unread:
        counts["unread_scopes"] = len(unread)
    db.add(
        CtemSurfacePass(
            organization_id=organization_id,
            at=at,
            provider=reader.provider[:40],
            observed=len(observations),
            coverage=[c.as_dict() for c in coverage][:200],
            statuses=statuses,
            counts=counts,
            previous_at=previous.at if previous else None,
        )
    )
    await db.flush()
    return {"counts": counts, "pending": pending, "statuses": statuses}


async def _record_findings(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    findings: list[Finding],
    rows: dict[str, CtemSurfaceObservation],
    provider: str,
    at: datetime,
    excluded: dict[str, set[tuple[str, str]]],
    seen_by: dict[str, list[str]],
    counts: dict[str, int],
) -> Any:
    """Record each finding as an outside-lens signal and hold its key on the host row; the case
    events to publish after the commit. *counts* gains ``recorded``, ``excluded`` and one count per path."""
    from app.services import ctem_case_loop, ctem_scope_gate_store, ctem_signals

    pending = ctem_case_loop.PendingEvents()
    counts.setdefault("recorded", 0)
    for finding in findings:
        # The providers an administrator took off a path (#1445): a finding only they saw records nothing there.
        if not _kept_for_path(excluded, finding.trigger_path, seen_by.get(finding.host) or provider.split("+")):
            counts["excluded"] = counts.get("excluded", 0) + 1
            continue
        signal, created = await ctem_signals.record_signal(
            db,
            organization_id=organization_id,
            signal=ctem_signals.SignalInput(
                trigger_path=finding.trigger_path,
                source_provider=provider,
                subject_kind="asset",
                subject_ref=finding.host,
                lens="outside",
                subject_label=f"{finding.host}: {finding.reason}",
                observed_at=at,
                # #2436: the pass that recorded it, counted apart on the signal sources view.
                evidence={
                    **finding.evidence,
                    "reason": finding.reason,
                    ctem_signals.SOURCE_KEY_EVIDENCE: f"{provider}:pass",
                },
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
    return pending


# ── the purge re-raise (#2634) ──────────────────────────────────────────────

#: The prefix of a re-arm mark: ``rearm:`` and the key of the signal the purge deleted
#: (``REARM_NEW_HOST`` is the older, host-level form of the new-host mark).
REARM_PREFIX = "rearm:"
#: The paths whose held keys the purge re-arms and a resync may re-raise from the baseline.
REARMED_PATHS: tuple[str, ...] = ("surface_drift", "new_exposure")


def rearm_mark(dedupe_key: str) -> str:
    """The mark the purge leaves for one deleted pass signal (#2634)."""
    return f"{REARM_PREFIX}{dedupe_key}"[:240]


def held_findings(row: Baseline, declared: dict[str, Any]) -> list[Finding]:
    """The findings the last committed pass held on *row* and the purge re-armed (no I/O, #2634).

    A new-host mark raises the drift again unless the host is declared now; a port mark raises
    its path's port finding again (a declared host's port is ``new_exposure``, an undeclared
    host's ``surface_drift``). Keys of other paths are recomputed by every pass, never re-raised here.
    """
    asset = declared.get(row.host) or (declared.get(host_key(row.ip)) if row.ip else None)
    base: dict[str, Any] = {"source": SOURCE, "hostnames": [row.host], "ports": list(row.ports), "re_raised": "purge"}
    if row.ip:
        base["ips"] = [row.ip]
    out: list[Finding] = []
    marker = f":{SOURCE}:{row.host}:"
    for mark in row.signalled:
        if not mark.startswith(REARM_PREFIX):
            continue
        key = mark.removeprefix(REARM_PREFIX)
        if mark == REARM_NEW_HOST or (key.startswith("surface_drift:") and key.endswith(":new_host")):
            if asset is None:
                out.append(
                    Finding(
                        "surface_drift",
                        row.host,
                        "a host neither declared nor seen before the last pass, held when the signals were purged",
                        "new_host",
                        {**base, "baseline": "absent"},
                    )
                )
            continue
        path = key.split(":", 1)[0]
        detail = key.split(marker, 1)[1] if marker in key else ""
        if path not in REARMED_PATHS or not detail.startswith("port:"):
            continue
        port = detail.removeprefix("port:")
        if asset is not None and path == "new_exposure":
            reason = f"{asset.name} answers on {port}, held by the last pass when the signals were purged"
            out.append(Finding(path, row.host, reason, detail, {**base, "asset": asset.name, "new_port": port}))
        elif asset is None and path == "surface_drift":
            reason = f"an undeclared host answers on {port}, held by the last pass when the signals were purged"
            out.append(Finding(path, row.host, reason, detail, {**base, "new_port": port}))
    return list({f.dedupe_key: f for f in out}.values())


async def reraise_held(db: AsyncSession, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Raise again, from the stored baseline, what the last committed pass held when a purge
    deleted it (#2634); the caller commits. Run by the purge resync after its own pass, for the
    marks that pass left (it did not complete: a provider read failed, no credential; or it did
    not see the host this time): the outside paths are not left empty while the undeclared hosts
    the last pass saw still exist. Each re-raised key replaces its mark, so the next pass that
    sees the host dedupes on it. ``None`` when the pass is off.
    """
    from app.services import organization_context as org_context
    from app.services.ctem_signal_sources import parse_excluded
    from app.services.organizations import get_primary

    primary = await get_primary(db)
    if primary is None:
        return None
    _row, profile = await org_context.current_profile(db, primary.id)
    if not profile.attack_surface.enabled:
        return None
    at = now or datetime.now(UTC)
    rows = await _baseline(db, primary.id)
    declared = _declared(profile)
    findings: list[Finding] = []
    for row in rows.values():
        marks = [k for k in row.signalled or [] if k.startswith(REARM_PREFIX)]
        if not marks:
            continue
        found = held_findings(_as_baseline(row), declared)
        findings.extend(found)
        restored = {f.detail.removeprefix("port:") for f in found if f.detail.startswith("port:")}
        known = {f"{p.get('port')}/{p.get('transport') or 'tcp'}" for p in row.ports or [] if isinstance(p, dict)}
        extra = []
        for key in sorted(restored - known):
            number, _, transport = key.partition("/")
            extra.append(
                {"port": int(number) if number.isdigit() else number, "transport": transport or "tcp", "product": ""}
            )
        if extra:
            row.ports = [*(row.ports or []), *extra]
        row.signalled = [k for k in row.signalled or [] if not k.startswith(REARM_PREFIX)]
    counts: dict[str, Any] = {"recorded": 0, "held": len(findings)}
    pending = await _record_findings(
        db,
        organization_id=primary.id,
        findings=findings,
        rows=rows,
        provider=next((r.source_provider for r in rows.values() if r.source_provider), "attack_surface"),
        at=at,
        excluded=parse_excluded(profile),
        seen_by={},
        counts=counts,
    )
    await db.flush()
    return {"counts": counts, "pending": pending}


def empty_reads(statuses: dict[str, str]) -> list[str]:
    """The scopes a pass read ``empty`` (the provider answered no host at all for them), from its
    ``provider|scope`` statuses (#2634: an owned domain the provider knows nothing of)."""
    return sorted({key.split("|", 1)[-1] for key, state in (statuses or {}).items() if state == "empty"})


async def quiet_reasons(
    db: AsyncSession, organization_id: uuid.UUID, *, resync_running: bool = False
) -> dict[str, dict[str, Any]]:
    """Why the attack-surface pass raised nothing on each of its paths (#2634): read from the latest
    committed pass and the baseline, for the source rows of an empty path. Pure reads.

    Each reason is data the screen words in the reader's language (UX round 1 on #2654): ``code``
    (resync_queued, a ``no_pass_reason`` code, empty_read, rearmed, baseline, withheld,
    no_undeclared_host or nothing_new; frontend quiet-reason.ts words each one), ``at`` (the
    last pass, ISO 8601), ``count`` and ``scopes`` when the code needs them, and ``empty_scopes``:
    the scopes the provider answered nothing for on a pass that still read hosts.
    *resync_running*: a purge resync is replaying its routes; its pass runs after them.
    """
    paths = ("new_exposure", "stale_asset", "surface_drift")
    if resync_running:
        return {p: {"code": "resync_queued"} for p in paths}
    latest = await list_passes(db, organization_id, limit=1)
    if not latest:
        code, _sentence = await no_pass_reason(db, organization_id)
        return {p: {"code": code} for p in paths}
    last = latest[0]
    counts = dict(last.counts or {})
    when = last.at.isoformat() if last.at else None
    armed = sum(
        1
        for row in (await _baseline(db, organization_id)).values()
        if any(str(k).startswith(REARM_PREFIX) for k in row.signalled or [])
    )
    empty = empty_reads(dict(last.statuses or {}))
    out: dict[str, dict[str, Any]] = {}
    for path in paths:
        if counts.get(path):
            continue
        reason: dict[str, Any] = {"at": when}
        if empty and not counts.get("observed"):
            reason.update(code="empty_read", scopes=list(empty))
        elif armed:
            reason.update(code="rearmed", count=armed)
        elif path == "surface_drift" and last.previous_at is None:
            reason["code"] = "baseline"
        elif path == "surface_drift" and counts.get("new_host_withheld"):
            reason.update(code="withheld", count=int(counts["new_host_withheld"]))
        elif path == "surface_drift":
            reason["code"] = "no_undeclared_host"
        else:
            reason["code"] = "nothing_new"
        if empty and counts.get("observed"):
            reason["empty_scopes"] = list(empty)
        out[path] = reason
    return out


# ── the pass summaries and the one-off repair (#1498) ───────────────────────

#: Pass summaries one read answers at most.
MAX_PASSES = 50


def pass_to_dict(row: CtemSurfacePass) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "at": row.at.isoformat() if row.at else None,
        "provider": row.provider,
        "observed": row.observed,
        "coverage": list(row.coverage or []),
        "statuses": dict(row.statuses or {}),
        "truncated": any(v == "truncated" for v in (row.statuses or {}).values()),
        "counts": dict(row.counts or {}),
        "previous_at": row.previous_at.isoformat() if row.previous_at else None,
    }


async def list_passes(db: AsyncSession, organization_id: uuid.UUID, *, limit: int = 10) -> list[CtemSurfacePass]:
    """The latest committed passes of the organization, newest first."""
    rows = await db.execute(
        select(CtemSurfacePass)
        .where(CtemSurfacePass.organization_id == organization_id)
        .order_by(CtemSurfacePass.at.desc())
        .limit(max(1, min(int(limit), MAX_PASSES)))
    )
    return list(rows.scalars().all())


async def why_no_pass(db: AsyncSession, organization_id: uuid.UUID) -> str:
    """Why the organization has no pass record, read from the profile as the scheduled pass reads it.

    A pass record is written only by a pass that completes (#1512); nothing deletes one. The
    reason names the first step the schedule stops at (off, no scope, no provider, no reader),
    else says that no pass has completed yet, with what makes one write nothing (#2331).
    """
    _code, sentence = await no_pass_reason(db, organization_id)
    return sentence


async def no_pass_reason(db: AsyncSession, organization_id: uuid.UUID) -> tuple[str, str]:
    """``why_no_pass`` as a code the screen words (pass_off, no_scope, no_provider,
    provider_unreadable, no_pass_completed) and the sentence agents and the API read (#2634)."""
    from app.services import organization_context as org_context

    _row, profile = await org_context.current_profile(db, organization_id)
    surface = profile.attack_surface
    if not surface.enabled:
        return "pass_off", "the attack surface pass is turned off in the organization profile (attack_surface.enabled)"
    if not (profile.owned_domains or profile.address_ranges):
        return "no_scope", "the organization profile declares no owned domains and no address ranges to read"
    reader = await reader_for(db, profile)
    if isinstance(reader, str):
        return ("provider_unreadable" if declared_integrations(surface) else "no_provider"), reader
    return "no_pass_completed", (
        "no pass has completed yet: a pass is recorded only when it completes (records exist since #1512, "
        "none is deleted); a pass whose provider read failed records nothing and is retried after a backoff"
    )


def _signal_dates(row: Any) -> dict[str, Any]:
    """One repaired signal with the dates an operator checks it against (#2331)."""

    def _iso(value: Any) -> str | None:
        return value.isoformat() if value else None

    return {
        "id": str(row.id),
        "host": row.subject_ref,
        "first_seen_at": _iso(getattr(row, "first_seen_at", None)),
        "observed_at": _iso(getattr(row, "observed_at", None)),
        "state": row.state,
    }


#: The states a repaired signal may be in: the gate's open states, never a case.
_REPAIRABLE_STATES: tuple[str, ...] = ("new", "confirmed", "to_validate", "watchlist")
#: The verdict a repair writes: the scope gate reads it as contradicting (``CONTRADICTING_VERDICTS``).
REPAIR_VERDICT = "false_positive"


async def repair_partial_read_new_hosts(
    db: AsyncSession, organization_id: uuid.UUID, *, dry_run: bool = True, now: datetime | None = None
) -> dict[str, Any]:
    """Discard the path 7 ``new_host`` signals of the outside lens raised without a complete
    previous read (#1498): every one raised before a pass summary recorded complete reads on
    both sides carries no ``previous_pass_at`` in its evidence. The hosts stay in the baseline
    (their observation rows are kept). A signal that opened a case is left to the case step.
    The caller commits; *dry_run* answers what it would discard and writes nothing.
    """
    from app.models.ctem_signal import CtemSignal
    from app.services.ctem_signals import SYSTEM_ACTOR

    at = now or datetime.now(UTC)
    rows = (
        (
            await db.execute(
                select(CtemSignal).where(
                    CtemSignal.organization_id == organization_id,
                    CtemSignal.recorded_by == SYSTEM_ACTOR,
                    CtemSignal.trigger_path == "surface_drift",
                    CtemSignal.state.in_(_REPAIRABLE_STATES),
                    CtemSignal.dedupe_key.like(f"%:{SOURCE}:%:new_host"),
                )
            )
        )
        .scalars()
        .all()
    )
    chosen = [r for r in rows if not (r.evidence or {}).get("previous_pass_at")]
    hosts = sorted({r.subject_ref for r in chosen})
    if not dry_run:
        for row in chosen:
            row.evidence = {**(row.evidence or {}), "verdict": REPAIR_VERDICT, "repaired": "partial_previous_read"}
            row.state = "discarded"
            row.decision = {
                **(row.decision or {}),
                "outcome": "discarded",
                "reason": "raised without a complete previous read of the attack surface (#1498)",
                "reason_code": "partial_previous_read",
                "decided_at": at.isoformat(),
            }
            row.updated_at = at
        await db.flush()
    signals = [_signal_dates(r) for r in sorted(chosen, key=lambda r: (r.first_seen_at or at, str(r.id)))]
    return {"dry_run": dry_run, "discarded": len(chosen), "hosts": hosts[:200], "signals": signals[:200]}


# ── the provider reader: Shodan ─────────────────────────────────────────

_SHODAN_URL = "https://api.shodan.io"
#: Pages read per query (#1498): ``/dns/domain`` answers ``more`` while records remain, the host
#: search answers ``total`` at 100 matches a page. A pass costs at most
#: ``_SHODAN_DNS_PAGES + _SHODAN_SEARCH_PAGES`` query credits per owned domain and
#: ``_SHODAN_SEARCH_PAGES`` per range; a query that hits its bound is a truncated read.
_SHODAN_DNS_PAGES = 5
_SHODAN_SEARCH_PAGES = 3
_SHODAN_SEARCH_PAGE_SIZE = 100


class ShodanSurfaceReader:
    """The outside view from Shodan: DNS records of each owned domain, banners of each range and domain.

    Each query is paged up to its bound and recorded in ``coverage`` (pages, records,
    truncated), so the pass knows what it read.
    """

    provider = "shodan"

    def __init__(self, api_key: str):
        self._key = api_key
        self.coverage: list[ReadCoverage] = []

    async def _get(self, path: str, action: str, params: dict[str, Any] | None = None) -> Any:
        from app.core.egress import egress_client
        from app.integrations.http import DEFAULT_TIMEOUT, safe_request

        query = {**(params or {}), "key": self._key}
        async with egress_client(timeout=DEFAULT_TIMEOUT) as client:
            # 404 expected: Shodan answers it for a domain it holds no DNS data for.
            resp, err = await safe_request(
                client, "get", f"{_SHODAN_URL}{path}", "Shodan", action, params=query, expected_statuses={404}
            )
        if resp is not None and resp.status_code == 404:
            return None  # "No information available for that domain": an empty read, not a failure
        if resp is not None and resp.status_code in (401, 403):
            raise SurfaceCredentialError(err or f"Shodan refused the credential to {action}")
        if err or resp is None or not resp.is_success:
            raise SurfaceReadError(err or f"Shodan did not answer to {action}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SurfaceReadError(f"Shodan answered a non-JSON body to {action}") from exc

    async def observe(self, domains: list[str], ranges: list[str]) -> list[Observation]:
        hosts: dict[str, Observation] = {}
        self.coverage = []

        def _host(name: str, scope: str) -> Observation:
            key = host_key(name)
            if key not in hosts:
                hosts[key] = Observation(host=key)
            read = read_key(self.provider, scope)
            if read not in hosts[key].reads:
                hosts[key].reads.append(read)
            return hosts[key]

        # Each query fails alone (#2613): a 5xx or a rate limit on one domain or range leaves
        # that scope unread (``failed`` coverage) and the others are still read. A refused
        # credential stops the read, and a read where every query failed fails the pass.
        for domain in domains:
            try:
                await self._dns(domain, _host)
            except SurfaceCredentialError:
                raise
            except SurfaceReadError as exc:
                self._failed(domain, "dns", exc)
            try:
                matches = await self._search(f"hostname:{domain}", domain)
            except SurfaceCredentialError:
                raise
            except SurfaceReadError as exc:
                self._failed(domain, "search", exc)
                matches = []
            self._banners(matches, lambda name, d=domain: _host(name, d))
        for block in ranges:
            try:
                matches = await self._search(f"net:{block}", block)
            except SurfaceCredentialError:
                raise
            except SurfaceReadError as exc:
                self._failed(block, "search", exc)
                matches = []
            self._banners(matches, lambda name, b=block: _host(name, b))
        if self.coverage and all(c.failed for c in self.coverage):
            raise SurfaceReadError(self.coverage[0].error or "every Shodan query of the pass failed")
        return list(hosts.values())

    def _failed(self, scope: str, kind: str, exc: Exception) -> None:
        logger.warning("CTEM attack surface pass: the Shodan %s read of %s failed: %s", kind, scope, exc)
        self.coverage.append(ReadCoverage(self.provider, scope, kind, 0, 0, failed=True, error=str(exc)))

    async def _dns(self, domain: str, host: Any) -> None:
        """Every page of ``/dns/domain`` up to the bound: records and listed subdomains."""
        from urllib.parse import quote

        records: list[dict[str, Any]] = []
        subdomains: list[str] = []
        pages = 0
        more = False
        for page in range(1, _SHODAN_DNS_PAGES + 1):
            data = (
                await self._get(
                    f"/dns/domain/{quote(domain, safe='')}",
                    f"read the DNS records of {domain}",
                    {"page": page} if page > 1 else None,
                )
                or {}
            )
            pages += 1
            records.extend(r for r in data.get("data") or [] if isinstance(r, dict))
            subdomains.extend(str(s) for s in data.get("subdomains") or [])
            more = bool(data.get("more"))
            if not more:
                break
        self.coverage.append(
            ReadCoverage(self.provider, domain, "dns", pages, len(records) + len(set(subdomains)), truncated=more)
        )
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
                obs = host(name, domain)
                obs.known_since = _earliest(obs.known_since, record.get("last_seen"))
                if value:
                    obs.dns_target = obs.dns_target or value
                    if kind in {"A", "AAAA"}:
                        obs.ip = obs.ip or value
                else:
                    obs.resolves = False
        for sub in dict.fromkeys(subdomains):
            name = f"{sub}.{domain}"
            if host_key(name) not in named:
                host(name, domain).resolves = False

    async def _search(self, query: str, scope: str) -> list[dict[str, Any]]:
        """Every page of the host search up to the bound; truncated when matches remain."""
        matches: list[dict[str, Any]] = []
        total = 0
        pages = 0
        for page in range(1, _SHODAN_SEARCH_PAGES + 1):
            params: dict[str, Any] = {"query": query, "minify": "false"}
            if page > 1:
                params["page"] = page
            data = await self._get("/shodan/host/search", f"search {query}", params) or {}
            pages += 1
            batch = [m for m in data.get("matches") or [] if isinstance(m, dict)]
            matches.extend(batch)
            try:
                total = max(total, int(data.get("total") or 0))
            except (TypeError, ValueError):
                total = max(total, len(matches))
            if len(batch) < _SHODAN_SEARCH_PAGE_SIZE or len(matches) >= total:
                break
        truncated = len(matches) < total
        self.coverage.append(ReadCoverage(self.provider, scope, "search", pages, len(matches), truncated=truncated))
        return matches

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
                obs.known_since = _earliest(obs.known_since, match.get("timestamp"))
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
        self.coverage: list[ReadCoverage] = []

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
        self.coverage = []
        path = f"/search/entities/{quote(f'type:{_GTI_IP_ENTITY}', safe=':')}"
        # ASM is read per project, not per owned domain: its one read covers every
        # scope of the pass (``*``), truncated when a bound left entities unread.
        pages = records = 0
        truncated = len(projects) > _GTI_MAX_PROJECTS
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
                pages += 1
                for entity in _result_list(payload):
                    records += 1
                    self._entity(entity, domains, ranges, hosts)
                token = _next_token(payload) or ""
                if not token:
                    break
            if token:
                truncated = True
        self.coverage.append(ReadCoverage(self.provider, "*", "asm", pages, records, truncated=truncated))
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
        read = read_key(GtiAsmSurfaceReader.provider, "*")
        for name in names:
            obs = hosts.setdefault(name, Observation(host=name))
            obs.ip = obs.ip or ip
            obs.known_since = _earliest(obs.known_since, entity.get("first_seen") or summary.get("first_seen"))
            if read not in obs.reads:
                obs.reads.append(read)
            for port in ports:
                if port.key not in {p.key for p in obs.ports}:
                    obs.ports.append(port)


class SurfaceReadError(RuntimeError):
    """The provider could not be read; the pass records nothing and keeps the baseline."""


class SurfaceCredentialError(SurfaceReadError):
    """The provider refused the credential (401, 403): no other query of the pass can succeed."""


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
            into.reads = list(dict.fromkeys([*into.reads, *obs.reads]))
            if obs.known_since is not None and (into.known_since is None or obs.known_since < into.known_since):
                into.known_since = obs.known_since
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
        self.coverage: list[ReadCoverage] = []

    async def observe(self, domains: list[str], ranges: list[str]) -> list[Observation]:
        per_reader = [(r.provider, await r.observe(domains, ranges)) for r in self.readers]
        self.coverage = [c for r in self.readers for c in reader_coverage(r)]
        return merge_observations(per_reader)


def reader_coverage(reader: Any) -> list[ReadCoverage]:
    """What *reader* says it read in its last ``observe`` (none for a reader that does not say)."""
    return [c for c in getattr(reader, "coverage", None) or [] if isinstance(c, ReadCoverage)]


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
    """Give the slot back at once (the purge resync runs the pass now, whatever the backoff)."""
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        await client.delete(_CADENCE_SLOT_KEY)
    except Exception:  # no Redis: there is no slot to give back
        logger.debug("CTEM attack surface pass slot not released: Redis unavailable")


#: The consecutive passes that did not complete, and the retry backoff they set (#2613):
#: 5 minutes after the first, doubling, never past the cadence window.
_FAILURES_KEY = "ctem:attack-surface:failures"
_BACKOFF_FIRST_SECONDS = 300


def backoff_seconds(failures: int, cadence_hours: int) -> int:
    """The wait before the next try after *failures* passes in a row did not complete."""
    step: int = _BACKOFF_FIRST_SECONDS * (1 << max(0, min(failures, 20) - 1))
    return max(1, min(step, max(1, cadence_hours) * 3600))


async def back_off_pass_slot(cadence_hours: int) -> int:
    """Keep the slot for a backoff after a pass that did not complete, so the next ticks do
    not retry it every minute (and spend provider credits on the same failure). Answers the
    seconds kept (0 when Redis is unavailable: the next tick retries)."""
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        failures = int(await client.incr(_FAILURES_KEY) or 1)
        await client.expire(_FAILURES_KEY, max(1, cadence_hours) * 3600 * 2)
        wait = backoff_seconds(failures, cadence_hours)
        await client.set(_CADENCE_SLOT_KEY, "1", ex=wait)
        return wait
    except Exception:  # no Redis: nothing to keep, the next tick retries
        logger.debug("CTEM attack surface pass backoff not kept: Redis unavailable")
        return 0


async def _clear_failures() -> None:
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        await client.delete(_FAILURES_KEY)
    except Exception:  # no Redis: no count to clear
        logger.debug("CTEM attack surface pass failure count not cleared: Redis unavailable")


async def scheduled_pass(db: AsyncSession) -> dict[str, Any] | None:
    """The tick's pass on the primary organization when its profile turned it on; ``None`` when off."""
    from app.services import ctem_case_loop
    from app.services import organization_context as org_context
    from app.services.organizations import get_primary

    primary = await get_primary(db)
    if primary is None:
        return None
    _row, profile = await org_context.current_profile(db, primary.id)  # assets from the records (#1543)
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
        # A committed pass keeps the slot for the cadence window; a skipped or failed
        # pass (no credential yet, a provider 5xx or rate limit) keeps it for a growing
        # backoff instead, so it is retried in minutes, not at every tick (#2613).
        if not completed:
            wait = await back_off_pass_slot(surface.cadence_hours)
            logger.info("CTEM attack surface pass did not complete: next try in %s s", wait)
    await _clear_failures()
    await ctem_case_loop.publish_pending(result["pending"])
    counts: dict[str, Any] = result["counts"]
    return counts
