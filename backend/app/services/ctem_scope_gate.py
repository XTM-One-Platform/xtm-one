# SPDX-License-Identifier: Apache-2.0

"""CTEM reconcile and scope gate (#847): the two gates between a signal and a case.

Pure rules, no I/O and no model call: :func:`decide` takes a signal, the
other signals on the same subject, the organization profile and, when
OpenCRQ was read, the risk scenarios that hold each supporting asset, and
answers one :class:`Decision`. ``services/ctem_scope_gate_store.py`` reads
those inputs and writes the decision on the signal. The design, the
severity formula and the decision record are ``dev-docs/ctem-scope-gate.md``.

1. **Reconcile** ("does the signal survive the cross-check?"): the subject
   is resolved to profile assets, asset groups, primary assets or software
   products on stable keys (hostname, IP, URL host, cloud id, external id);
   the lenses that saw the same subject are counted. A verdict that
   contradicts it (``not_exposed``, ``not_affected``, ``false_positive``,
   ``fixed``) or a policy exclusion discards it; a subject nothing resolves
   is ``to_validate``; else it is confirmed and goes to the scope gate.
2. **Scope gate** ("does it land where it matters?"): the subject's
   criticality is the highest of its assets, the primary assets they run and
   the risk scenarios that hold them; at or above
   ``scope_min_criticality`` the outcome is ``case`` (state ``confirmed``,
   the case step opens it), below it ``watchlist`` with the threshold named.
   The policy thresholds (CVSS, EPSS, KEV, internet exposure, primary asset
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

from app.services.ctem_primary_assets import (
    PrimaryAssetIndex,
    contains_word,
    is_family_word,
    is_generic_word,
    is_url_like,
    link_basis,
    reference_names,
)
from app.services.organization_context import (
    PROFILE_ASSET_CAP,
    AssetEntry,
    OrganizationProfile,
    ShippedProduct,
    effective_criticality,
    groups_of,
)

#: Version of the decision record; a reader keys on it.
DECISION_VERSION = 1
#: The chains a primary asset source keeps as examples; ``chain_total`` and ``assets_total`` count them all (#1979).
CHAINS_KEPT = 5
#: Outcomes, and the signal state each one writes.
OUTCOME_STATE: dict[str, str] = {
    "case": "confirmed",
    "watchlist": "watchlist",
    "to_validate": "to_validate",
    "discarded": "discarded",
}
#: The scope scale (the primary-asset criticality of the profile), lowest first.
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
#: #2542: how an OpenCTI software read of the gate went (``reconcile.cve_lookup`` / ``threat_lookup``).
#: ``read``: it answered. ``not needed``: the evidence resolved the subject, nothing was read.
#: ``not applicable``: the signal names no CVE (no OpenCTI threat id). ``not read``: needed, not taken
#: by this pass. Any other value is why OpenCTI could not answer (``ctem_cve_software.LOOKUP_*``).
LOOKUP_READ = "read"
LOOKUP_NOT_NEEDED = "not needed"
LOOKUP_NOT_APPLICABLE = "not applicable"
LOOKUP_NOT_READ = "not read"
#: The lookups that settle a decision; any other one leaves it incomplete (#2542).
LOOKUP_SETTLED: frozenset[str] = frozenset({LOOKUP_READ, LOOKUP_NOT_NEEDED, LOOKUP_NOT_APPLICABLE})
#: #2523: the OpenCTI types a threat signal is recorded from (the ``relevant_threat`` listener).
THREAT_STIX_TYPES: frozenset[str] = frozenset(
    {"intrusion-set", "threat-actor", "threat-actor-group", "campaign", "malware"}
)

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
    #: #2171: the software (name or CPE) OpenCTI relates by ``has`` to the CVEs the signal names, read
    #: by the store with the recorder's own access when the evidence names no software.
    cve_software: tuple[str, ...] = ()
    #: #2171: how that read went: ``""`` not attempted, ``"read"``, else why it could not answer.
    cve_lookup: str = ""
    #: #2523: the software (name or CPE) OpenCTI says a threat subject targets or uses, read by the
    #: store with the recorder's own access when the evidence alone does not resolve the threat.
    threat_software: tuple[str, ...] = ()
    #: #2523: how that read went: ``""`` not attempted, ``"read"``, else why it could not answer.
    threat_lookup: str = ""
    #: #2631: the CVSS, EPSS and KEV OpenCTI holds on the vulnerabilities the signal names, read by the
    #: store when the policy sets a score minimum and the evidence carries no score.
    cve_cvss: float | None = None
    cve_epss: float | None = None
    cve_kev: bool = False
    #: #2631: how that read went: ``""`` not attempted, ``"read"``, else why it could not answer.
    score_lookup: str = ""


#: #2171: the evidence keys the gate reads; any other key is kept on the signal but never reconciled.
ASSET_EVIDENCE_KEYS: tuple[str, ...] = ("assets", "asset", "hostnames", "urls", "ips")
#: #2701: ``reconcile.scope`` of a signal whose evidence names its assets: those assets are the whole reach.
EVIDENCE_ASSETS_SCOPE = "evidence_assets"
SOFTWARE_EVIDENCE_KEYS: tuple[str, ...] = ("software", "products", "cpe")
READ_EVIDENCE_KEYS: tuple[str, ...] = (
    *ASSET_EVIDENCE_KEYS,
    *SOFTWARE_EVIDENCE_KEYS,
    "cve",
    "cves",
    "primary_assets",
)


def evidence_read_keys(evidence: Any) -> list[str]:
    """The keys of *evidence* the gate reads that hold a value (#2171)."""
    if not isinstance(evidence, dict):
        return []
    return [k for k in READ_EVIDENCE_KEYS if _evidence_list(evidence, k)]


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


#: #2631: the evidence keys a score is read from, in order: the gate's own, the OpenCTI listener's
#: (``x_opencti_`` prefix removed), the GitHub advisory listener's.
CVSS_EVIDENCE_KEYS: tuple[str, ...] = ("cvss", "cvss_base_score", "cvss_score")
EPSS_EVIDENCE_KEYS: tuple[str, ...] = ("epss", "epss_score")
KEV_EVIDENCE_KEYS: tuple[str, ...] = ("kev", "cisa_kev")


def _score(evidence: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """The first number *evidence* holds under *keys* (a ``{"score": x}`` object read too)."""
    for key in keys:
        value = evidence.get(key)
        if isinstance(value, dict):
            value = value.get("score")
        if isinstance(value, bool):
            continue
        number = _float(value)
        if number is not None:
            return number
    return None


def _kev(evidence: dict[str, Any]) -> bool:
    """Whether *evidence* flags the vulnerability in CISA KEV (a ``"true"`` string read too)."""
    for key in KEV_EVIDENCE_KEYS:
        value = evidence.get(key)
        if value is True or (isinstance(value, str) and value.strip().lower() == "true"):
            return True
    return False


def signal_scores(facts: SignalFacts) -> dict[str, Any]:
    """#2631: the CVSS, EPSS and KEV of a signal, each with where it was read.

    The evidence first (any key of :data:`CVSS_EVIDENCE_KEYS` / :data:`EPSS_EVIDENCE_KEYS`),
    then the vulnerability in OpenCTI (``facts.cve_*``, read by the store); ``None`` when
    neither holds it, never a silent pass.
    """
    out: dict[str, Any] = {}
    for name, keys, remote in (
        ("cvss", CVSS_EVIDENCE_KEYS, facts.cve_cvss),
        ("epss", EPSS_EVIDENCE_KEYS, facts.cve_epss),
    ):
        value = _score(facts.evidence, keys)
        if value is not None:
            out[name], out[f"{name}_source"] = value, "evidence"
        elif remote is not None:
            out[name], out[f"{name}_source"] = remote, "opencti"
        else:
            out[name], out[f"{name}_source"] = None, None
    out["kev"] = _kev(facts.evidence) or facts.cve_kev
    return out


def scores_in_evidence(evidence: dict[str, Any]) -> bool:
    """#2631: whether *evidence* carries both a CVSS and an EPSS (the store then reads nothing)."""
    return _score(evidence, CVSS_EVIDENCE_KEYS) is not None and _score(evidence, EPSS_EVIDENCE_KEYS) is not None


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
    named_primary_assets: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    #: CTEM-29: the owned domain an identity subject belongs to.
    domains: list[str] = field(default_factory=list)
    #: #1505: the owned domain a host subject sits under (``app.corp.example`` under ``corp.example``).
    #: It places the host in the inventory but carries no criticality: such a host goes to the
    #: watchlist, never one case per host, until an asset or a primary asset names it.
    hosted_domains: list[str] = field(default_factory=list)
    #: #1432: the shipped products the software names, each ``{product, by, repositories}``.
    shipped: list[dict[str, Any]] = field(default_factory=list)
    #: #1432: their source repositories (``owner/name``), the subject's assets.
    repositories: list[str] = field(default_factory=list)
    #: #1591 S3: the primary assets reached through the inventory links, each with its chain
    #: (software -> asset or asset group -> primary asset, role, link source).
    primary_assets: list[dict[str, Any]] = field(default_factory=list)
    #: #1979: per primary asset key, how many inventory assets reach it ``{assets, direct, via_group}``.
    primary_asset_totals: dict[str, dict[str, int]] = field(default_factory=dict)
    #: #1720: the software references the organization's software and shipped products were matched against.
    software_refs: list[str] = field(default_factory=list)
    #: #2701: ``evidence_assets`` when the evidence names the assets: the reach is those assets alone,
    #: the software only labels and version-checks them; ``""`` when the software sets the reach.
    scope: str = ""

    @property
    def resolved(self) -> bool:
        return bool(
            self.assets
            or self.groups
            or self.named_primary_assets
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
    """A software product named in a software reference (a name, or a CPE's product part).

    #2628: a bare OS or vendor family (``Windows``, ``Linux``) names only itself, never
    ``Windows Server`` or ``Linux Kernel``; #2685: nor does a bare generic word (``Client``).
    """
    p, r = product.strip().lower(), ref.strip().lower()
    if not p or not r:
        return False
    # #2711: a compound package name or long title names a profile product only by its full name,
    # else ``Mail`` (a profile product) would join the references through ``mail-mcp-bridge``.
    named = reference_names(product, ref)
    if named is not None:
        return named
    if r.startswith("cpe:"):
        parts = r.split(":")
        r = parts[4] if len(parts) > 4 else r
    if (is_family_word(p) or is_generic_word(p)) and p != r.replace("_", " "):
        return False
    # #2679: a URL or host name label (a web-app shortcut row) names only itself.
    if p != r and (is_url_like(p) or is_url_like(r)):
        return False
    return contains_word(p, r)


#: #2628: the paths whose subject is a vulnerability on software the organization may run.
VERSION_PATHS: frozenset[str] = frozenset({"new_vulnerability"})


def version_not_verified(facts: SignalFacts, resolution: Resolution) -> int:
    """How many inventory assets an unproven CVE match reached, 0 when the match is proven (#2628).

    A ``new_vulnerability`` signal resolved only through the inventory software index
    (no asset key, no typed software row, no shipped product or repository, no proof)
    names a product family at most: when no reached asset reports the product from an
    installed row with a version, nobody verified the running version, so it goes to
    ``to_validate``. A versioned installed row, a typed software row, an asset or
    repository the signal names, or a proving verdict keeps today's path to a case.
    """
    if facts.trigger_path not in VERSION_PATHS or not name_only_reach(resolution):
        return 0
    if _verdict(facts.evidence) in PROVING_VERDICTS:
        return 0
    return max(1, sum(int(row.get("assets") or 0) for row in resolution.primary_asset_totals.values()))


def name_only_reach(resolution: Resolution) -> bool:
    """Whether *resolution* reached the organization by a software name alone (#2628, #2685).

    Only inventory software chains, each naming the software it matched, none from an
    installed row that reports a version, and no asset key, typed software row (a bare
    family excepted), repository or domain: nobody verified the organization runs it.
    """
    if not resolution.primary_assets:
        return False
    # A typed software row that is a bare family (``Windows``) proves no product either.
    if (
        resolution.assets
        or resolution.groups
        or resolution.named_primary_assets
        or any(not is_family_word(p) for p in resolution.products)
        or resolution.repositories
        or resolution.domains
        or resolution.hosted_domains
    ):
        return False
    hits = resolution.primary_assets
    if not all(str(hit.get("software") or "") for hit in hits):
        return False
    return not any(hit.get("version_verified") for hit in hits)


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


#: #1720: the relationships of a threat that name the software it reaches (OpenCTI ``targets``,
#: ``uses``, and ``has`` read from the software side).
THREAT_SOFTWARE_RELATIONSHIPS: frozenset[str] = frozenset({"targets", "uses", "has"})
#: #1720: the evidence keys a threat's targeted or used software is listed under.
THREAT_SOFTWARE_KEYS: tuple[str, ...] = ("targets", "uses", "targeted_software", "targeted_products")
#: #1720: the scorer's matched field naming the products a threat matched, ``products (A, B)``.
_MATCHED_PRODUCTS = re.compile(r"\b(?:tech[ _]stack|software|products?)\s*\(([^()]*)\)", re.IGNORECASE)


def threat_software(evidence: dict[str, Any]) -> list[str]:
    """#1720: the software a threat subject (a campaign, an intrusion set, a malware) names.

    Read from the lists of :data:`THREAT_SOFTWARE_KEYS`, from ``relationships``
    (``{relationship_type, target}`` or ``{type, name}`` objects, kept when the
    type is one of :data:`THREAT_SOFTWARE_RELATIONSHIPS`), and from the
    scorer's ``matched_fields`` (``products (Microsoft 365, GitHub)``). Each
    name is then matched to the organization's software like any software reference.
    """
    found = _evidence_list(evidence, *THREAT_SOFTWARE_KEYS)
    relationships = evidence.get("relationships")
    for rel in relationships if isinstance(relationships, list) else []:
        if not isinstance(rel, dict):
            continue
        kind = str(rel.get("relationship_type") or rel.get("type") or "").strip().lower()
        name = str(rel.get("target") or rel.get("name") or "").strip()
        if kind in THREAT_SOFTWARE_RELATIONSHIPS and name:
            found.append(name)
    for text in _evidence_list(evidence, "matched_fields"):
        for group in _MATCHED_PRODUCTS.findall(text):
            found += [part.strip() for part in group.split(",") if part.strip()]
    return list(dict.fromkeys(found))


def runs_in_profile(profile: OrganizationProfile, name: str) -> list[str]:
    """The software products and assets of *profile* a software or asset *name* names (CTEM-08).

    *name* is the source of an OpenCTI ``has`` relationship (a Software name
    or CPE, an Infrastructure or System name). Empty: the organization does
    not run it, and the "vulnerability on software we run" rule drops it.
    """
    text = str(name or "").strip()
    if not text:
        return []
    found = [
        entry.product
        for entry in profile.software
        if _product_matches(entry.product, text) or (entry.cpe and entry.cpe.lower() == text.lower())
    ]
    key = stable_key(text)
    found += [a.name for a in profile.assets_exposure if key in {stable_key(a.name), stable_key(a.external_id)}]
    return list(dict.fromkeys(found))


def evidence_fixed_version(evidence: dict[str, Any]) -> str | None:
    """The fixed version a signal's evidence carries (its own, else its case fix's), ``None`` when unknown (#2747)."""
    fix = evidence.get("case_fix")
    for value in (evidence.get("fixed_version"), fix.get("fixed_version") if isinstance(fix, dict) else None):
        text = str(value or "").strip()
        if text:
            return text[:100]
    return None


def resolve(
    profile: OrganizationProfile, facts: SignalFacts, primary_assets: PrimaryAssetIndex | None = None
) -> Resolution:
    """Resolve the subject (and the assets its evidence names) to the profile.

    With *primary_assets* (#1591 S3), the software the signal names and the subject's
    keys are also followed to the inventory assets that run it and the primary assets
    they or their groups support.
    """
    out = Resolution()
    evidence_keys = {k for k in (stable_key(v) for v in _evidence_list(facts.evidence, *ASSET_EVIDENCE_KEYS)) if k}
    keys = [stable_key(facts.subject_ref), stable_key(facts.source_external_id)]
    keys += [stable_key(v) for v in _evidence_list(facts.evidence, *ASSET_EVIDENCE_KEYS)]
    keys = [k for k in dict.fromkeys(keys) if k]
    out.keys = keys
    wanted = set(keys)
    # #2701: whether an asset, group or primary asset was named by the evidence itself (not the subject).
    narrow = False
    for asset in profile.assets_exposure:
        names = {stable_key(asset.name), stable_key(asset.external_id), stable_key(asset.hostname)} - {""}
        if names & wanted:
            out.assets.append(asset)
            narrow = narrow or bool(names & evidence_keys)
    for group in profile.asset_groups:
        names = {stable_key(group.name), stable_key(group.external_id)} - {""}
        if names & wanted:
            out.groups.append(group.name)
            narrow = narrow or bool(names & evidence_keys)
    # CTEM-29: the primary assets a tracked organization's services support, named in the evidence.
    named = {n.strip().lower() for n in _evidence_list(facts.evidence, "primary_assets")}
    for pa in profile.primary_assets:
        names = {stable_key(pa.name), stable_key(pa.opencrq_asset_id)} - {""}
        if names & wanted or pa.name.lower() in named:
            out.named_primary_assets.append(pa.name)
            narrow = narrow or bool(names & evidence_keys) or pa.name.lower() in named
    if not narrow and evidence_keys and primary_assets is not None:
        # An inventory asset the profile's capped list does not hold still names the reach.
        narrow = any(evidence_keys & r.keys for r in primary_assets.reachers)
    if narrow:
        out.scope = EVIDENCE_ASSETS_SCOPE
    if facts.subject_kind == "identity":
        owned = owned_domain_of(profile, facts.subject_ref)
        if owned:
            out.domains.append(owned)
    elif facts.subject_kind in HOST_SUBJECT_KINDS and not (out.assets or out.groups or out.named_primary_assets):
        hosted = host_owned_domain(profile, facts.subject_ref)
        if hosted:
            out.hosted_domains.append(hosted)
    software = _evidence_list(facts.evidence, *SOFTWARE_EVIDENCE_KEYS)
    if facts.subject_kind == "software":
        software.insert(0, facts.subject_ref)
    if facts.subject_kind == "threat":
        software += threat_software(facts.evidence)
    # #2171: the software OpenCTI says has the CVEs the signal names.
    software += [s for s in facts.cve_software if str(s or "").strip()]
    # #2523: the software OpenCTI says the threat targets or uses.
    software += [s for s in facts.threat_software if str(s or "").strip()]
    software = list(dict.fromkeys(software))
    out.software_refs = software
    for entry in profile.software:
        if any(
            _product_matches(entry.product, ref) or (entry.cpe and entry.cpe.lower() == ref.lower()) for ref in software
        ):
            out.products.append(entry.product)
    if not narrow:
        # #2701: named assets are the whole reach; the software adds no repository of its own.
        _resolve_shipped(profile, software, out)
        _resolve_repositories(profile, software, out)
    if primary_assets is not None:
        refs = list(dict.fromkeys([*software, *out.products]))
        out.primary_assets = primary_assets.reach(
            refs, keys, keys_only=narrow, fixed_version=evidence_fixed_version(facts.evidence)
        )
        out.primary_asset_totals = primary_assets.totals(refs, keys, keys_only=narrow)
    if out.products and not narrow:
        names = {p.lower() for p in out.products}
        for asset in profile.assets_exposure:
            if asset not in out.assets and names & {t.lower() for t in asset.products}:
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


def _repository_token(value: str) -> str:
    """A repository or product name folded for comparison: lower case, ``-``, ``_`` and spaces dropped."""
    return re.sub(r"[\s_\-.]+", "", value.strip().lower())


def repository_match(asset: AssetEntry, ref: str) -> bool:
    """Whether a software reference names an imported repository asset (harness gap x12-02, #1774).

    The GitHub and GitLab imports write each repository as an asset of subcategory
    ``code_repository`` named ``owner/name``: a CPE whose product part, or a plain
    name, is the repository's name (case, ``-`` and ``_`` folded) names it. A fork
    never does: the organization does not ship it.
    """
    if asset.subcategory != "code_repository" or "fork" in asset.tags:
        return False
    name = asset.name.rsplit("/", 1)[-1]
    text = str(ref or "").strip()
    named = _cpe_vendor_product(text)
    wanted = _repository_token(named[1] if named else text)
    return bool(wanted) and wanted == _repository_token(name)


def _resolve_repositories(profile: OrganizationProfile, software: list[str], out: Resolution) -> None:
    """A product the organization ships, read from its imported repositories with no typed row (#1774).

    A repository a ``shipped_products`` row already resolved is left to it.
    """
    if not software:
        return
    known = {r.lower() for r in out.repositories}
    for asset in profile.assets_exposure:
        if asset.name.lower() in known or not any(repository_match(asset, ref) for ref in software):
            continue
        out.shipped.append({"product": asset.name.rsplit("/", 1)[-1], "by": "repository", "repositories": [asset.name]})
        out.repositories.append(asset.name)
        if asset not in out.assets:
            out.assets.append(asset)


def _confidence(
    facts: SignalFacts, related: list[RelatedSignal], *, name_only: bool = False
) -> tuple[str, list[str], bool]:
    """``(confidence, lenses, proven)`` of a signal.

    #2685: KEV proves exploitation in the wild, not that the organization runs the product:
    a KEV CVE reached by a software name alone, or not reached (*name_only*), is not proven by its KEV flag.
    """
    lenses = sorted({facts.lens, *(r.lens for r in related if r.verdict not in CONTRADICTING_VERDICTS)})
    proven = (
        facts.trigger_path in PROVING_PATHS
        or _verdict(facts.evidence) in PROVING_VERDICTS
        or any(r.verdict in PROVING_VERDICTS for r in related)
        or (not name_only and (_kev(facts.evidence) or facts.cve_kev))
    )
    if proven or len(lenses) >= 3:
        return "high", lenses, proven
    return ("medium" if len(lenses) == 2 else "low"), lenses, proven


# ── scope gate ─────────────────────────────────────────────────────────


def _primary_assets_of(profile: OrganizationProfile, resolution: Resolution) -> list[tuple[str, str]]:
    """``(name, criticality)`` of the primary assets the resolved assets and groups run."""
    names = {a.name.lower() for a in resolution.assets} | {a.group.lower() for a in resolution.assets if a.group}
    # #1773: the groups an asset joins by rule count as its groups too.
    names |= {g.name.lower() for a in resolution.assets for g in groups_of(profile, a)}
    names |= {g.lower() for g in resolution.groups}
    named = {n.lower() for n in resolution.named_primary_assets}
    # #2680: a hop reached only by a bare OS family reference is a possible exposure, never a
    # running product: it sets no scope criticality.
    linked = {str(h["primary_asset_key"]) for h in resolution.primary_assets if not h.get("family_only")}
    out: list[tuple[str, str]] = []
    for pa_row in profile.primary_assets:
        if (
            pa_row.name.lower() in named
            or names & {n.lower() for n in pa_row.asset_names}
            or " ".join(pa_row.name.split()).casefold()[:200] in linked
        ):
            out.append((pa_row.name, pa_row.criticality))
    return out


def _primary_asset_basis(sources: list[dict[str, Any]], criticality: str | None) -> str | None:
    """``proven`` or ``inferred``: the link of the primary asset sources at *criticality*, else ``None`` (#2396).

    Several primary assets at that criticality: ``proven`` when one of them is.
    """
    bases = {
        s.get("basis") or "inferred"
        for s in sources
        if s.get("from") == "primary_asset" and criticality and s.get("criticality") == criticality
    }
    if not bases:
        return None
    return "proven" if "proven" in bases else "inferred"


def signal_cve_ids(subject_ref: str | None, subject_label: str | None, evidence: Any) -> list[str]:
    """The CVE ids a signal names (its subject, label and evidence ``cve`` / ``cves``), upper case, in order."""
    listed = _evidence_list(evidence, "cve", "cves") if isinstance(evidence, dict) else []
    found = _CVE.findall(" ".join([subject_ref or "", subject_label or "", *listed]))
    return list(dict.fromkeys(c.upper() for c in found))


def _cve_ids(facts: SignalFacts) -> list[str]:
    return signal_cve_ids(facts.subject_ref, facts.subject_label, facts.evidence)


def threat_opencti_id(facts: SignalFacts) -> str:
    """#2523: the OpenCTI id of a threat signal's subject, ``""`` when it has none (pure).

    Only an OpenCTI-recorded threat of :data:`THREAT_STIX_TYPES` carries one: its
    source external id (the internal id, #2505), else the STIX id of its evidence.
    """
    if facts.subject_kind != "threat" or facts.source_provider != "opencti":
        return ""
    evidence = facts.evidence if isinstance(facts.evidence, dict) else {}
    if str(evidence.get("stix_type") or "").strip().lower() not in THREAT_STIX_TYPES:
        return ""
    return str(facts.source_external_id or evidence.get("stix_id") or "").strip()


def lookup_value(recorded: str, *, applicable: bool, resolved: bool) -> str:
    """#2542: the explicit ``cve_lookup`` / ``threat_lookup`` of a decision, never empty (pure).

    *recorded* is what the store wrote in the facts (``""`` when it took no read);
    *applicable* whether the signal has something to read (a CVE, an OpenCTI threat
    id); *resolved* whether the subject resolved without the read.
    """
    if recorded:
        return recorded
    if not applicable:
        return LOOKUP_NOT_APPLICABLE
    return LOOKUP_NOT_NEEDED if resolved else LOOKUP_NOT_READ


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
    # #2542: every decision says how each OpenCTI software read went, never None; a read the subject
    # needed and OpenCTI did not answer leaves an unresolved decision incomplete (retried by the store).
    cve_lookup = lookup_value(facts.cve_lookup, applicable=bool(_cve_ids(facts)), resolved=resolution.resolved)
    threat_lookup = lookup_value(
        facts.threat_lookup, applicable=bool(threat_opencti_id(facts)), resolved=resolution.resolved
    )
    incomplete = not resolution.resolved and any(value not in LOOKUP_SETTLED for value in (cve_lookup, threat_lookup))
    # #2685: KEV proves the organization runs nothing; an unresolved or name-only subject is not proven by it.
    unproven_reach = not resolution.resolved or name_only_reach(resolution)
    confidence, lenses, proven = _confidence(facts, related, name_only=unproven_reach)
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
            # #2701: "evidence_assets" when the evidence named the assets and the software widened nothing.
            "scope": resolution.scope or None,
            # UX r2 B7 on #1995: each name once, and whether the profile's asset list was cut at its
            # model cap (more assets may match than the list holds: a reader never counts it as the total).
            "assets": list(dict.fromkeys(a.name for a in resolution.assets)),
            "assets_capped": len(profile.assets_exposure) >= PROFILE_ASSET_CAP,
            "asset_groups": resolution.groups,
            "primary_assets_named": resolution.named_primary_assets,
            "products": resolution.products,
            "shipped_products": resolution.shipped,
            "repositories": resolution.repositories,
            "software_refs": resolution.software_refs,
            # #2171: the software read from OpenCTI for the signal's CVEs, and how that read went.
            "cve_software": list(facts.cve_software),
            "cve_lookup": cve_lookup,
            # #2523: the software read from OpenCTI for a threat subject, and how that read went.
            "threat_software": list(facts.threat_software),
            "threat_lookup": threat_lookup,
            "hosted_under": resolution.hosted_domains,
            "primary_assets": resolution.primary_assets,
            "inventory_links_read": bool(primary_assets is not None and primary_assets.loaded),
            # #1979: the real counts behind the chains, and whether the inventory read was cut.
            "primary_asset_totals": resolution.primary_asset_totals,
            "inventory_links_truncated": bool(primary_assets is not None and primary_assets.truncated),
            "lenses": lenses,
            "confidence": confidence,
            "proven": proven,
            # #2685: KEV says exploited in the wild, apart from whether the organization runs it.
            "exploited_in_wild": bool(_kev(facts.evidence) or facts.cve_kev),
            "contradicted_by": contradicted,
        },
        "scope": None,
        "policy_checks": [],
        "severity": None,
        "sla_hours": None,
        # #2542: an OpenCTI software read the subject needed did not answer; the store retries it.
        "incomplete": incomplete,
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
            # CTEM-29: a third party no primary asset depends on is watched, never a case.
            return done(
                "watchlist",
                "no primary asset depends on this organization",
                code="no_dependency",
            )
        return done(
            "to_validate",
            "subject not resolved to an asset, a primary asset, the software inventory or a shipped product",
            code="subject_unresolved",
            params={"keys": len(resolution.keys), "software": len(resolution.software_refs)},
        )

    unverified = version_not_verified(facts, resolution)
    if unverified:
        # #2628: a CVE reached only through inventory software whose running version nobody
        # verified (no typed software row, no asset key) is a candidate, never a case.
        return done(
            "to_validate",
            "version not verified: the CVE matched inventory software only, no running version proven",
            code="version_not_verified",
            params={"assets": unverified},
        )

    # Scope criticality: assets, their primary assets, the risk scenarios holding them.
    sources: list[dict[str, str]] = []
    for asset in resolution.assets:
        if asset.criticality in ASSET_TO_SCOPE:
            sources.append({"from": "asset", "name": asset.name, "criticality": ASSET_TO_SCOPE[asset.criticality]})
            continue
        # #1773: an asset of unknown criticality takes the one its groups give their members.
        inherited = effective_criticality(profile, asset)
        if inherited in ASSET_TO_SCOPE:
            sources.append({"from": "asset_group", "name": asset.name, "criticality": ASSET_TO_SCOPE[inherited]})
    pa_rows = _primary_assets_of(profile, resolution)
    chains: dict[str, list[dict[str, Any]]] = {}
    for hit in resolution.primary_assets:
        chains.setdefault(str(hit["primary_asset_key"]), []).append(hit)
    for n, c in pa_rows:
        source: dict[str, Any] = {"from": "primary_asset", "name": n, "criticality": c}
        key = " ".join(n.split()).casefold()[:200]
        chain = chains.get(key)
        # #2396: whether a recorded link (manual, OpenCRQ) supports it, read on every chain before the cut.
        source["basis"] = link_basis(chain)
        if chain:
            source["chain"] = chain[:CHAINS_KEPT]
            # #1979: the record keeps the first chains and says how many there are, never a silent cut.
            source["chain_total"] = len(chain)
            source["assets_total"] = resolution.primary_asset_totals.get(key, {}).get("assets", 0)
        sources.append(source)
    sources += [
        {"from": "owned_domain", "name": d, "criticality": policy.identity_criticality} for d in resolution.domains
    ]
    pa_row_ids = [
        j.opencrq_asset_id for j in profile.primary_assets if j.name in {n for n, _ in pa_rows} and j.opencrq_asset_id
    ]
    asset_ids = [a.external_id for a in resolution.assets if a.source == "opencrq" and a.external_id] + pa_row_ids
    # CTEM-29: the OpenCRQ supporting assets a tracked organization's services are.
    asset_ids += _evidence_list(facts.evidence, "opencrq_asset_ids")
    held = scenarios.for_keys(asset_ids, [a.name.lower() for a in resolution.assets]) if scenarios.connected else []
    sources += [{"from": "risk_scenario", "name": s.name, "criticality": s.criticality} for s in held]
    criticality = _highest([s["criticality"] for s in sources])
    threshold = policy.scope_min_criticality
    record["scope"] = {
        "criticality": criticality or None,
        # #2396: when a primary asset sets the scope criticality, whether its link is proven or inferred.
        "primary_asset_basis": _primary_asset_basis(sources, criticality),
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

    # CVE-bearing signals: the score thresholds of the policy, read from the evidence first, then
    # from the vulnerability in OpenCTI (#2631); a score read nowhere follows ``missing_score``.
    scores = signal_scores(facts)
    if record["inputs"]["cves"] or facts.subject_kind == "vulnerability":
        record["reconcile"]["score_lookup"] = facts.score_lookup or (
            LOOKUP_NOT_NEEDED if scores_in_evidence(facts.evidence) else LOOKUP_NOT_READ
        )
        if scores["kev"] and policy.kev_always_in:
            checks.append({"rule": "kev_always_in", "value": True, "passed": True})
        else:
            for rule, name, minimum in (("cvss_min", "cvss", policy.cvss_min), ("epss_min", "epss", policy.epss_min)):
                if minimum is None:
                    continue
                value = scores[name]
                score = name.upper()
                if value is None:
                    hold_missing = policy.missing_score == "hold"
                    checks.append(
                        {
                            "rule": rule,
                            "value": None,
                            "threshold": minimum,
                            "passed": not hold_missing,
                            "note": "no score read",
                            "missing_score": policy.missing_score,
                        }
                    )
                    if hold_missing:
                        return done(
                            "watchlist",
                            f"no score read: {score} held for validation (policy minimum {minimum})",
                            code="score_missing",
                            params={"score": score, "minimum": minimum},
                        )
                    continue
                passed = value >= minimum
                checks.append(
                    {
                        "rule": rule,
                        "value": value,
                        "threshold": minimum,
                        "passed": passed,
                        "source": scores[f"{name}_source"],
                    }
                )
                if not passed:
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
    if policy.min_primary_asset_criticality:
        best = _highest([c for _, c in pa_rows])
        passed = _rank(best) >= _rank(policy.min_primary_asset_criticality)
        checks.append(
            {
                "rule": "min_primary_asset_criticality",
                "value": best or None,
                # #2396: proven or inferred, the link of the primary asset that gives the value.
                "basis": _primary_asset_basis(sources, best),
                "threshold": policy.min_primary_asset_criticality,
                "passed": passed,
            }
        )
        if not passed:
            return done(
                "watchlist",
                f"no primary asset at or above {policy.min_primary_asset_criticality}",
                code="primary_asset_below_policy",
                params={"minimum": policy.min_primary_asset_criticality},
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
