# SPDX-License-Identifier: Apache-2.0

"""CTEM reconcile and scope gate (#847): the two gates between a signal and a case.

Pure rules, no I/O and no model call: :func:`decide` takes a signal, the
other signals on the same subject, the organization profile and, when
OpenCRQ was read, the risk scenarios that hold each supporting asset, and
answers one :class:`Decision`. ``services/ctem_scope_gate_store.py`` reads
those inputs and writes the decision on the signal. The design, the
severity formula and the decision record are ``dev-docs/ctem-scope-gate.md``.

1. **Reconcile** ("does the signal survive the cross-check?"): the subject
   is resolved to profile assets, asset groups, crown jewels or tech stack
   products on stable keys (hostname, IP, URL host, cloud id, external id);
   the lenses that saw the same subject are counted. A verdict that
   contradicts it (``not_exposed``, ``not_affected``, ``false_positive``,
   ``fixed``) or a policy exclusion discards it; a subject nothing resolves
   is ``to_validate``; else it is confirmed and goes to the scope gate.
2. **Scope gate** ("does it land where it matters?"): the subject's
   criticality is the highest of its assets, the crown jewels they run and
   the risk scenarios that hold them; at or above
   ``scope_min_criticality`` the outcome is ``case`` (state ``confirmed``,
   the case step opens it), below it ``watchlist`` with the threshold named.
   The policy thresholds (CVSS, EPSS, KEV, internet exposure, crown jewel
   criticality, accepted risks, a required risk scenario) route to the
   watchlist with their reason.
3. **Severity** P1 to P3 = scope criticality points + lens confidence
   points (section 4 of the dev-doc), with its SLA from the policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.services.ctem_primary_assets import PrimaryAssetIndex
from app.services.organization_context import AssetEntry, OrganizationProfile, ShippedProduct

#: Version of the decision record; a reader keys on it.
DECISION_VERSION = 1
#: Outcomes, and the signal state each one writes.
OUTCOME_STATE: dict[str, str] = {
    "case": "confirmed",
    "watchlist": "watchlist",
    "to_validate": "to_validate",
    "discarded": "discarded",
}
#: The scope scale (the crown-jewel criticality of the profile), lowest first.
CRITICALITY_ORDER: tuple[str, ...] = ("low", "medium", "high", "critical")
#: Profile asset criticality (OpenAEV's scale) read on the scope scale.
ASSET_TO_SCOPE: dict[str, str] = {"low": "low", "medium": "medium", "high": "high", "very_high": "critical"}
#: Points of the severity formula.
CRITICALITY_POINTS: dict[str, int] = {"low": 1, "medium": 2, "high": 3, "critical": 4}
CONFIDENCE_POINTS: dict[str, int] = {"low": 0, "medium": 1, "high": 2}
#: A verdict in a signal's evidence that says the exposure is not there.
CONTRADICTING_VERDICTS: frozenset[str] = frozenset({"not_exposed", "not_affected", "false_positive", "fixed"})
#: A verdict that proves it (a test reached it).
PROVING_VERDICTS: frozenset[str] = frozenset({"exposed", "exploitable", "proven"})
#: The paths whose signal is a proof by itself.
PROVING_PATHS: frozenset[str] = frozenset({"proven_exploitable", "mitigation_regression"})
#: The criticality a risk scenario gives an asset it holds when OpenCRQ names none.
SCENARIO_DEFAULT_CRITICALITY = "high"

_CVE = re.compile(r"\bCVE-\d{4}-\d{4,}\b", re.IGNORECASE)


@dataclass(frozen=True)
class SignalFacts:
    """What the gate reads of the signal being decided."""

    trigger_path: str
    lens: str
    subject_kind: str
    subject_ref: str
    source_provider: str = ""
    source_external_id: str | None = None
    subject_label: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RelatedSignal:
    """Another signal of the organization on the same subject (any recorder)."""

    id: str
    lens: str
    source_provider: str
    verdict: str = ""


@dataclass(frozen=True)
class RiskScenarioRef:
    """A risk scenario that holds a supporting asset (read from OpenCRQ)."""

    id: str
    name: str
    criticality: str = SCENARIO_DEFAULT_CRITICALITY


@dataclass
class ScenarioIndex:
    """Risk scenarios by supporting asset id and by lower-cased asset name.

    ``connected`` says whether OpenCRQ was read for this decision; ``note``
    says why not when it was not (the decision record names it).
    """

    connected: bool = False
    note: str = "OpenCRQ not read"
    by_asset_id: dict[str, list[RiskScenarioRef]] = field(default_factory=dict)
    by_asset_name: dict[str, list[RiskScenarioRef]] = field(default_factory=dict)

    def for_keys(self, asset_ids: list[str], names: list[str]) -> list[RiskScenarioRef]:
        found: dict[str, RiskScenarioRef] = {}
        for key in asset_ids:
            for ref in self.by_asset_id.get(key, []):
                found.setdefault(ref.id, ref)
        for name in names:
            for ref in self.by_asset_name.get(name, []):
                found.setdefault(ref.id, ref)
        return list(found.values())


@dataclass
class Decision:
    """The outcome of both gates, with the record stored on the signal."""

    outcome: str
    reason: str
    severity: str | None
    record: dict[str, Any]

    @property
    def state(self) -> str:
        return OUTCOME_STATE[self.outcome]


# ── keys ───────────────────────────────────────────────────────────────


def stable_key(value: object) -> str:
    """A hostname, IP, URL or name reduced to the key two lenses share.

    ``https://Shop.Example.com:443/login`` and ``shop.example.com.`` give
    ``shop.example.com``; a cloud id or external id is only lower-cased.
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
        text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        text = text.rsplit("@", 1)[-1]
        if text.count(":") == 1:
            text = text.split(":", 1)[0]
    return text.rstrip(".")


def _rank(criticality: str) -> int:
    return CRITICALITY_ORDER.index(criticality) if criticality in CRITICALITY_ORDER else -1


def _highest(values: list[str]) -> str:
    known = [v for v in values if v in CRITICALITY_ORDER]
    return max(known, key=_rank) if known else ""


def _evidence_list(evidence: dict[str, Any], *names: str) -> list[str]:
    out: list[str] = []
    for name in names:
        value = evidence.get(name)
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            out.extend(str(v).strip() for v in value if str(v or "").strip())
    return out


def _float(value: object) -> float | None:
    try:
        return None if value is None or value == "" else float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _verdict(evidence: dict[str, Any]) -> str:
    return str(evidence.get("verdict") or "").strip().lower()


def _account_disabled(evidence: dict[str, Any]) -> bool:
    """Whether the evidence says the account is disabled (OpenCTI ``is_disabled``, a directory check)."""
    return evidence.get("account_disabled") is True or evidence.get("account_enabled") is False


# ── reconcile ──────────────────────────────────────────────────────────

#: #1505: the subject kinds a hostname names, matched to the owned domains it sits under.
HOST_SUBJECT_KINDS: frozenset[str] = frozenset({"asset"})


@dataclass
class Resolution:
    """What the subject resolved to in the profile."""

    assets: list[AssetEntry] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    crown_jewels: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    #: CTEM-29: the owned domain an identity subject belongs to.
    domains: list[str] = field(default_factory=list)
    #: #1505: the owned domain a host subject sits under (``app.corp.example`` under ``corp.example``).
    #: It places the host in the inventory but carries no criticality: such a host goes to the
    #: watchlist, never one case per host, until an asset or a crown jewel names it.
    hosted_domains: list[str] = field(default_factory=list)
    #: #1432: the shipped products the software names, each ``{product, by, repositories}``.
    shipped: list[dict[str, Any]] = field(default_factory=list)
    #: #1432: their source repositories (``owner/name``), the subject's assets.
    repositories: list[str] = field(default_factory=list)
    #: #1591 S3: the primary assets reached through the inventory links, each with its chain
    #: (software -> asset or asset group -> primary asset, role, link source).
    primary_assets: list[dict[str, Any]] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return bool(
            self.assets
            or self.groups
            or self.crown_jewels
            or self.products
            or self.domains
            or self.repositories
            or self.hosted_domains
            or self.primary_assets
        )


def identity_domain(ref: str) -> str:
    """The domain of an account or an email address (``jdoe@corp.example`` gives ``corp.example``)."""
    text = str(ref or "").strip().lower().rstrip(".")
    return text.rsplit("@", 1)[1] if "@" in text else ""


def owned_domain_of(profile: OrganizationProfile, ref: str) -> str:
    """The owned domain of *profile* an identity *ref* is on (a subdomain counts), ``""`` when none."""
    domain = identity_domain(ref)
    if not domain:
        return ""
    return _under_owned(profile, domain)


def _under_owned(profile: OrganizationProfile, domain: str) -> str:
    for owned in profile.owned_domains:
        if domain == owned or domain.endswith(f".{owned}"):
            return owned
    return ""


def host_owned_domain(profile: OrganizationProfile, ref: str) -> str:
    """#1505: the owned domain a hostname or URL *ref* sits under, ``""`` for an IP or no match."""
    host = stable_key(ref)
    if not host or "@" in host or "." not in host or host.replace(".", "").replace(":", "").isdigit():
        return ""
    return _under_owned(profile, host)


def _product_matches(product: str, ref: str) -> bool:
    """A tech stack product named in a software reference (a name, or a CPE's product part)."""
    p, r = product.strip().lower(), ref.strip().lower()
    if not p or not r:
        return False
    if r.startswith("cpe:"):
        parts = r.split(":")
        r = parts[4] if len(parts) > 4 else r
    return p == r or re.search(rf"(^|[^a-z0-9]){re.escape(p)}($|[^a-z0-9])", r) is not None


def _cpe_vendor_product(ref: str) -> tuple[str, str] | None:
    """``(vendor, product)`` of a CPE 2.3 (``cpe:2.3:a:v:p:...``) or 2.2 (``cpe:/a:v:p``) string, else ``None``."""
    text = ref.strip().lower()
    if text.startswith("cpe:2.3:"):
        parts = text.split(":")
        return (parts[3], parts[4]) if len(parts) > 4 and parts[3] and parts[4] else None
    if text.startswith("cpe:/"):
        parts = text[len("cpe:/") :].split(":")
        return (parts[1], parts[2]) if len(parts) > 2 and parts[1] and parts[2] else None
    return None


def _cpe_token(value: str) -> str:
    """A vendor or product name as a CPE writes it (lower case, spaces as underscores)."""
    return re.sub(r"\s+", "_", value.strip().lower())


def shipped_match(entry: ShippedProduct, ref: str) -> str:
    """How a software reference names a shipped product (#1432): ``cpe``, ``product`` or ``""`` (no match).

    By CPE first: a CPE reference whose vendor and product are those of the
    row's CPE (any version). Then by product and vendor: a CPE reference whose
    vendor and product are the row's names, or a name that is the product
    itself, or that names the product and, when the row has one, its vendor.
    """
    text = str(ref or "").strip()
    if not text:
        return ""
    named = _cpe_vendor_product(text)
    own = _cpe_vendor_product(entry.cpe) if entry.cpe else None
    if named and own and named == own:
        return "cpe"
    if named:
        product_ok = named[1] == _cpe_token(entry.product)
        return "product" if product_ok and (not entry.vendor or named[0] == _cpe_token(entry.vendor)) else ""
    if text.lower() == entry.product.lower():
        return "product"
    if not _product_matches(entry.product, text):
        return ""
    return "product" if entry.vendor and _product_matches(entry.vendor, text) else ""


def runs_in_profile(profile: OrganizationProfile, name: str) -> list[str]:
    """The tech stack products and assets of *profile* a software or asset *name* names (CTEM-08).

    *name* is the source of an OpenCTI ``has`` relationship (a Software name
    or CPE, an Infrastructure or System name). Empty: the organization does
    not run it, and the "vulnerability on software we run" rule drops it.
    """
    text = str(name or "").strip()
    if not text:
        return []
    found = [
        entry.product
        for entry in profile.tech_stack
        if _product_matches(entry.product, text) or (entry.cpe and entry.cpe.lower() == text.lower())
    ]
    key = stable_key(text)
    found += [a.name for a in profile.assets_exposure if key in {stable_key(a.name), stable_key(a.external_id)}]
    return list(dict.fromkeys(found))


def resolve(
    profile: OrganizationProfile, facts: SignalFacts, primary_assets: PrimaryAssetIndex | None = None
) -> Resolution:
    """Resolve the subject (and the assets its evidence names) to the profile.

    With *primary_assets* (#1591 S3), the software the signal names and the subject's
    keys are also followed to the inventory assets that run it and the primary assets
    they or their groups support.
    """
    out = Resolution()
    keys = [stable_key(facts.subject_ref), stable_key(facts.source_external_id)]
    keys += [stable_key(v) for v in _evidence_list(facts.evidence, "assets", "asset", "hostnames", "urls", "ips")]
    keys = [k for k in dict.fromkeys(keys) if k]
    out.keys = keys
    wanted = set(keys)
    for asset in profile.assets_exposure:
        if {stable_key(asset.name), stable_key(asset.external_id)} & wanted - {""}:
            out.assets.append(asset)
    for group in profile.asset_groups:
        if {stable_key(group.name), stable_key(group.external_id)} & wanted - {""}:
            out.groups.append(group.name)
    # CTEM-29: the crown jewels a tracked organization's services support, named in the evidence.
    named = {n.strip().lower() for n in _evidence_list(facts.evidence, "crown_jewels")}
    for jewel in profile.crown_jewels:
        if {stable_key(jewel.name), stable_key(jewel.opencrq_asset_id)} & wanted - {""} or jewel.name.lower() in named:
            out.crown_jewels.append(jewel.name)
    if facts.subject_kind == "identity":
        owned = owned_domain_of(profile, facts.subject_ref)
        if owned:
            out.domains.append(owned)
    elif facts.subject_kind in HOST_SUBJECT_KINDS and not (out.assets or out.groups or out.crown_jewels):
        hosted = host_owned_domain(profile, facts.subject_ref)
        if hosted:
            out.hosted_domains.append(hosted)
    software = _evidence_list(facts.evidence, "software", "products", "cpe")
    if facts.subject_kind == "software":
        software.insert(0, facts.subject_ref)
    for entry in profile.tech_stack:
        if any(
            _product_matches(entry.product, ref) or (entry.cpe and entry.cpe.lower() == ref.lower()) for ref in software
        ):
            out.products.append(entry.product)
    _resolve_shipped(profile, software, out)
    if primary_assets is not None:
        refs = list(dict.fromkeys([*software, *out.products]))
        out.primary_assets = primary_assets.reach(refs, keys)
    if out.products:
        names = {p.lower() for p in out.products}
        for asset in profile.assets_exposure:
            if asset not in out.assets and names & {t.lower() for t in asset.tech_stack}:
                out.assets.append(asset)
    return out


def _resolve_shipped(profile: OrganizationProfile, software: list[str], out: Resolution) -> None:
    """#1432: a vulnerability on a product the organization ships resolves to its repositories.

    A row matched by CPE wins over a match by product and vendor; each
    repository is the subject's asset, and the profile asset of the same name
    (a repository the GitHub or GitLab import wrote) joins ``out.assets`` with
    its criticality.
    """
    if not software or not profile.shipped_products:
        return
    matches: list[tuple[ShippedProduct, str]] = []
    for entry in profile.shipped_products:
        ways = {shipped_match(entry, ref) for ref in software} - {""}
        if ways:
            matches.append((entry, "cpe" if "cpe" in ways else "product"))
    if any(by == "cpe" for _, by in matches):
        matches = [(entry, by) for entry, by in matches if by == "cpe"]
    for entry, by in matches:
        out.shipped.append({"product": entry.product, "by": by, "repositories": list(entry.repositories)})
        out.repositories += [r for r in entry.repositories if r not in out.repositories]
    wanted = {r.lower() for r in out.repositories}
    for asset in profile.assets_exposure:
        if asset not in out.assets and stable_key(asset.name) in wanted:
            out.assets.append(asset)


def _confidence(facts: SignalFacts, related: list[RelatedSignal]) -> tuple[str, list[str], bool]:
    lenses = sorted({facts.lens, *(r.lens for r in related if r.verdict not in CONTRADICTING_VERDICTS)})
    proven = (
        facts.trigger_path in PROVING_PATHS
        or _verdict(facts.evidence) in PROVING_VERDICTS
        or any(r.verdict in PROVING_VERDICTS for r in related)
        or bool(facts.evidence.get("kev"))
    )
    if proven or len(lenses) >= 3:
        return "high", lenses, proven
    return ("medium" if len(lenses) == 2 else "low"), lenses, proven


# ── scope gate ─────────────────────────────────────────────────────────


def _jewels_of(profile: OrganizationProfile, resolution: Resolution) -> list[tuple[str, str]]:
    """``(name, criticality)`` of the crown jewels the resolved assets and groups run."""
    names = {a.name.lower() for a in resolution.assets} | {a.group.lower() for a in resolution.assets if a.group}
    names |= {g.lower() for g in resolution.groups}
    named = {n.lower() for n in resolution.crown_jewels}
    linked = {str(h["primary_asset_key"]) for h in resolution.primary_assets}
    out: list[tuple[str, str]] = []
    for jewel in profile.crown_jewels:
        if (
            jewel.name.lower() in named
            or names & {n.lower() for n in jewel.asset_names}
            or " ".join(jewel.name.split()).casefold()[:200] in linked
        ):
            out.append((jewel.name, jewel.criticality))
    return out


def _cve_ids(facts: SignalFacts) -> list[str]:
    found = _CVE.findall(
        " ".join([facts.subject_ref, facts.subject_label or "", *_evidence_list(facts.evidence, "cve", "cves")])
    )
    return list(dict.fromkeys(c.upper() for c in found))


def _accepted(
    profile: OrganizationProfile, facts: SignalFacts, resolution: Resolution, today: date
) -> dict[str, Any] | None:
    subjects = {facts.subject_ref.strip().lower(), *(c.lower() for c in _cve_ids(facts))}
    subjects |= {p.lower() for p in resolution.products}
    places = {a.name.lower() for a in resolution.assets} | {g.lower() for g in resolution.groups}
    places |= {a.group.lower() for a in resolution.assets if a.group}
    for risk in profile.prioritization_policy.accepted_risks:
        if risk.expires_on < today or risk.subject.strip().lower() not in subjects:
            continue
        if risk.scope and risk.scope.strip().lower() not in places:
            continue
        return {
            "subject": risk.subject,
            "scope": risk.scope,
            "owner": risk.owner,
            "expires_on": risk.expires_on.isoformat(),
        }
    return None


def _excluded(profile: OrganizationProfile, resolution: Resolution) -> dict[str, Any] | None:
    products = {p.lower() for p in resolution.products}
    groups = {g.lower() for g in resolution.groups} | {a.group.lower() for a in resolution.assets if a.group}
    tags = {t.lower() for a in resolution.assets for t in a.tags}
    for rule in profile.prioritization_policy.exclusions:
        value = rule.value.strip().lower()
        pool = {"product": products, "asset_group": groups, "tag": tags}[rule.kind]
        if value in pool:
            return {"kind": rule.kind, "value": rule.value, "reason": rule.reason}
    return None


def severity_of(criticality: str, confidence: str) -> str:
    """P1 at 5 points or more, P2 at 4, P3 below (dev-doc section 4)."""
    points = CRITICALITY_POINTS.get(criticality, 1) + CONFIDENCE_POINTS.get(confidence, 0)
    return "P1" if points >= 5 else "P2" if points == 4 else "P3"


def decide(
    profile: OrganizationProfile,
    facts: SignalFacts,
    *,
    related: list[RelatedSignal] | None = None,
    scenarios: ScenarioIndex | None = None,
    primary_assets: PrimaryAssetIndex | None = None,
    today: date,
) -> Decision:
    """Both gates on one signal; the record lists every input it read.

    *primary_assets* (#1591 S3) is the organization's inventory link index
    (``ctem_primary_assets.load_index``); every path that decides passes it.
    """
    related = list(related or [])
    scenarios = scenarios or ScenarioIndex()
    policy = profile.prioritization_policy
    resolution = resolve(profile, facts, primary_assets)
    confidence, lenses, proven = _confidence(facts, related)
    contradicted = [r.id for r in related if r.verdict in CONTRADICTING_VERDICTS]
    if _verdict(facts.evidence) in CONTRADICTING_VERDICTS:
        contradicted.insert(0, "this signal")
    record: dict[str, Any] = {
        "version": DECISION_VERSION,
        "inputs": {
            "trigger_path": facts.trigger_path,
            "lens": facts.lens,
            "subject": {"kind": facts.subject_kind, "ref": facts.subject_ref},
            "source_provider": facts.source_provider,
            "verdict": _verdict(facts.evidence) or None,
            "cves": _cve_ids(facts),
            "related_signals": [
                {"id": r.id, "lens": r.lens, "provider": r.source_provider, "verdict": r.verdict or None}
                for r in related
            ],
        },
        "reconcile": {
            "keys": resolution.keys,
            "assets": [a.name for a in resolution.assets],
            "asset_groups": resolution.groups,
            "crown_jewels_named": resolution.crown_jewels,
            "tech_stack": resolution.products,
            "shipped_products": resolution.shipped,
            "repositories": resolution.repositories,
            "hosted_under": resolution.hosted_domains,
            "primary_assets": resolution.primary_assets,
            "inventory_links_read": bool(primary_assets is not None and primary_assets.loaded),
            "lenses": lenses,
            "confidence": confidence,
            "proven": proven,
            "contradicted_by": contradicted,
        },
        "scope": None,
        "policy_checks": [],
        "severity": None,
        "sla_hours": None,
    }

    def done(
        outcome: str,
        reason: str,
        severity: str | None = None,
        *,
        code: str,
        params: dict[str, str | int | float] | None = None,
    ) -> Decision:
        """Close the record: ``reason`` is the English line agents read,
        ``reason_code`` and ``reason_params`` the stable key and values the
        frontend translates (dev-docs/ctem-loop-visibility.md section 3)."""
        record["outcome"], record["state"], record["reason"] = outcome, OUTCOME_STATE[outcome], reason
        record["reason_code"], record["reason_params"] = code, dict(params or {})
        return Decision(outcome=outcome, reason=reason, severity=severity, record=record)

    if contradicted:
        others = [c for c in contradicted if c != "this signal"]
        code = "contradicted_by_signals" if others else "contradicted_by_evidence"
        return done("discarded", f"contradicted by {', '.join(contradicted)}", code=code, params={"count": len(others)})
    exclusion = _excluded(profile, resolution)
    if exclusion:
        record["policy_checks"].append({"rule": "exclusion", **exclusion, "passed": False})
        return done(
            "discarded",
            f"excluded by the policy ({exclusion['kind']} {exclusion['value']})",
            code="excluded_by_policy",
            params={"kind": str(exclusion["kind"]), "value": str(exclusion["value"])},
        )
    if facts.subject_kind == "identity" and _account_disabled(facts.evidence):
        # CTEM-29: a leaked credential of a disabled account is no way in.
        return done("discarded", "not exposed: the account is disabled", code="account_disabled")
    if not resolution.resolved:
        if facts.subject_kind == "third_party":
            # CTEM-29: a third party no crown jewel depends on is watched, never a case.
            return done(
                "watchlist",
                "no crown jewel depends on this organization",
                code="no_dependency",
            )
        return done(
            "to_validate",
            "subject not resolved to an asset, a crown jewel, the tech stack or a shipped product",
            code="subject_unresolved",
        )

    # Scope criticality: assets, their crown jewels, the risk scenarios holding them.
    sources: list[dict[str, str]] = []
    for asset in resolution.assets:
        if asset.criticality in ASSET_TO_SCOPE:
            sources.append({"from": "asset", "name": asset.name, "criticality": ASSET_TO_SCOPE[asset.criticality]})
    jewels = _jewels_of(profile, resolution)
    chains: dict[str, list[dict[str, Any]]] = {}
    for hit in resolution.primary_assets:
        chains.setdefault(str(hit["primary_asset_key"]), []).append(hit)
    for n, c in jewels:
        source: dict[str, Any] = {"from": "crown_jewel", "name": n, "criticality": c}
        chain = chains.get(" ".join(n.split()).casefold()[:200])
        if chain:
            source["chain"] = chain[:5]
        sources.append(source)
    sources += [
        {"from": "owned_domain", "name": d, "criticality": policy.identity_criticality} for d in resolution.domains
    ]
    jewel_ids = [
        j.opencrq_asset_id for j in profile.crown_jewels if j.name in {n for n, _ in jewels} and j.opencrq_asset_id
    ]
    asset_ids = [a.external_id for a in resolution.assets if a.source == "opencrq" and a.external_id] + jewel_ids
    # CTEM-29: the OpenCRQ supporting assets a tracked organization's services are.
    asset_ids += _evidence_list(facts.evidence, "opencrq_asset_ids")
    held = scenarios.for_keys(asset_ids, [a.name.lower() for a in resolution.assets]) if scenarios.connected else []
    sources += [{"from": "risk_scenario", "name": s.name, "criticality": s.criticality} for s in held]
    criticality = _highest([s["criticality"] for s in sources])
    threshold = policy.scope_min_criticality
    record["scope"] = {
        "criticality": criticality or None,
        "sources": sources,
        "threshold": threshold,
        # #1591 S3: where the threshold comes from (the policy's scope field, else its default).
        "threshold_source": "policy" if "scope_min_criticality" in policy.model_fields_set else "default",
        "require_risk_scenario": policy.require_risk_scenario,
        "opencrq": {"connected": scenarios.connected, "note": "" if scenarios.connected else scenarios.note},
        "risk_scenarios": [{"id": s.id, "name": s.name} for s in held],
    }
    checks: list[dict[str, Any]] = record["policy_checks"]

    accepted = _accepted(profile, facts, resolution, today)
    if accepted:
        checks.append({"rule": "accepted_risk", **accepted, "passed": False})
        return done(
            "watchlist",
            f"accepted risk owned by {accepted['owner']} until {accepted['expires_on']}",
            code="accepted_risk",
            params={"owner": str(accepted["owner"]), "until": str(accepted["expires_on"])},
        )

    # CVE-bearing signals: the score thresholds of the policy.
    kev = bool(facts.evidence.get("kev"))
    cvss, epss = _float(facts.evidence.get("cvss")), _float(facts.evidence.get("epss"))
    if record["inputs"]["cves"] or facts.subject_kind == "vulnerability":
        if kev and policy.kev_always_in:
            checks.append({"rule": "kev_always_in", "value": True, "passed": True})
        else:
            for rule, value, minimum in (("cvss_min", cvss, policy.cvss_min), ("epss_min", epss, policy.epss_min)):
                if minimum is None:
                    continue
                if value is None:
                    checks.append(
                        {"rule": rule, "value": None, "threshold": minimum, "passed": True, "note": "not scored"}
                    )
                    continue
                passed = value >= minimum
                checks.append({"rule": rule, "value": value, "threshold": minimum, "passed": passed})
                if not passed:
                    score = rule.split("_")[0].upper()
                    return done(
                        "watchlist",
                        f"{score} {value} below the policy minimum {minimum}",
                        code="score_below_policy",
                        params={"score": score, "value": value, "minimum": minimum},
                    )
    if policy.internet_exposure_required:
        facing = any(a.internet_facing == "yes" for a in resolution.assets) or facts.lens == "outside"
        checks.append({"rule": "internet_exposure_required", "value": facing, "passed": facing})
        if not facing:
            return done(
                "watchlist",
                "no internet-facing asset, and the policy requires internet exposure",
                code="internet_exposure_required",
            )
    if policy.min_crown_jewel_criticality:
        best = _highest([c for _, c in jewels])
        passed = _rank(best) >= _rank(policy.min_crown_jewel_criticality)
        checks.append(
            {
                "rule": "min_crown_jewel_criticality",
                "value": best or None,
                "threshold": policy.min_crown_jewel_criticality,
                "passed": passed,
            }
        )
        if not passed:
            return done(
                "watchlist",
                f"no crown jewel at or above {policy.min_crown_jewel_criticality}",
                code="crown_jewel_below_policy",
                params={"minimum": policy.min_crown_jewel_criticality},
            )
    if policy.require_risk_scenario:
        passed = bool(held)
        checks.append({"rule": "require_risk_scenario", "value": len(held), "passed": passed})
        if not passed:
            if scenarios.connected:
                return done("watchlist", "no risk scenario holds the subject", code="no_risk_scenario")
            return done(
                "watchlist",
                f"a risk scenario is required ({scenarios.note})",
                code="risk_scenario_unreadable",
                params={"note": scenarios.note},
            )
    if not criticality:
        return done(
            "watchlist",
            f"criticality unknown, below the scope threshold {threshold}",
            code="criticality_unknown",
            params={"threshold": threshold},
        )
    if _rank(criticality) < _rank(threshold):
        return done(
            "watchlist",
            f"criticality {criticality} below the scope threshold {threshold}",
            code="below_scope_threshold",
            params={"criticality": criticality, "threshold": threshold},
        )
    severity = severity_of(criticality, confidence)
    record["severity"] = severity
    record["sla_hours"] = getattr(policy.sla_hours, severity.lower())
    return done(
        "case",
        f"in scope: criticality {criticality} at or above {threshold}, {confidence} confidence",
        severity,
        code="in_scope",
        params={"criticality": criticality, "threshold": threshold, "confidence": confidence},
    )
