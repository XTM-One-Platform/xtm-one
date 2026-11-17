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

"""What a Deep Investigation established, as STIX knowledge for OpenCTI.

The core reads the module (the `opencti` source kind) and writes back to it:
the seed and the IPs it resolved to, the newsroom-named sites a pivot found on
an IP, a note per site whose fingerprint matched - recorded in the caller's
OpenCTI by the conversation's agent, through the OpenCTI integration's own
tools, in a draft the analyst reviews and validates there (the integration
refuses an interactive write outside a draft).

What this module adds is the one thing the agent must not decide: WHICH
objects. `build_knowledge` is pure and reads only the run's stored steps, and
`read_investigation_report` hands its list to the agent, so what lands in
OpenCTI is what the sources answered, not what a model wrote about them.

A shared IP serves hundreds of sites, and the pivot keeps the ones whose NAME
reads like a newsroom - a triage, which lets `livechat.rs` through. So a
co-hosted site is recorded only once the run did more than read its name: it
qualified the site (its fingerprint found signals) or OpenCTI already holds
it. The rest stay in the report, where the triage is said for what it is.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from app.services.investigation_packs import classify_subject, site_of

logger = logging.getLogger(__name__)

#: Most observables one draft carries: a run's own findings, bounded.
MAX_DRAFT_OBSERVABLES = 120

#: How the `opencti` source labels a value the platform holds - written by the
#: querier, read back here to tell a known co-hosted site from a triaged one.
KNOWN_LABEL_SUFFIX = " - known in OpenCTI"


#: Between a seed and its newest IP in a list run's finding label: written by
#: the passive-DNS querier, read back here.
SEED_IP_SEPARATOR = " -> "


def known_label(value: str) -> str:
    return f"{value}{KNOWN_LABEL_SUFFIX}"


@dataclass(frozen=True)
class Observable:
    type: str  # "Domain-Name" | "IPv4-Addr" | "IPv6-Addr"
    value: str


@dataclass
class OpenCTIKnowledge:
    """What a run established, as STIX objects to create."""

    observables: list[Observable] = field(default_factory=list)
    #: ``(from value, to value, relationship type, description)``.
    relationships: list[tuple[str, str, str, str]] = field(default_factory=list)
    #: ``(observable value, note content)``.
    notes: list[tuple[str, str]] = field(default_factory=list)

    def add(self, obs_type: str, value: str) -> None:
        value = value.strip().lower()
        if not value or len(self.observables) >= MAX_DRAFT_OBSERVABLES:
            return
        if all(o.value != value for o in self.observables):
            self.observables.append(Observable(obs_type, value))

    def has(self, value: str) -> bool:
        return any(o.value == value.strip().lower() for o in self.observables)


#: The STIX types of an address, by family.
ADDRESS_TYPES = ("IPv4-Addr", "IPv6-Addr")


def _observable_type(value: str) -> str | None:
    """The STIX type of *value*: an address is typed by its family (a
    VirusTotal resolution may be an AAAA record), a domain is `Domain-Name`."""
    kind = classify_subject(value)
    if kind == "ip":
        return "IPv6-Addr" if ":" in value else "IPv4-Addr"
    return {"domain": "Domain-Name"}.get(kind)


def _ref(step: Any) -> dict:
    return step.source_ref if isinstance(step.source_ref, dict) else {}


def _evidence(step: Any) -> list[dict]:
    return [e for e in (step.evidence or []) if isinstance(e, dict)]


def _site_from_vt_link(href: str | None) -> str | None:
    """The site a VirusTotal domain link names (the label may be its decoded form)."""
    if not href:
        return None
    parts = urlsplit(href).path.rstrip("/").split("/")
    return parts[-1] if len(parts) >= 2 and parts[-2] == "domain" else None


def _site_key(value: str) -> str:
    """One spelling per site: an internationalised name as its punycode."""
    value = (value or "").strip().lower().rstrip(".")
    if value.isascii():
        return value
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError:
        return value


def _fingerprint_signals(step: Any) -> list[str]:
    signals = [str(item.get("label") or "").split(" — ")[0].strip() for item in _evidence(step)]
    return [s for s in signals if s]


def _vouched_sites(seed: str, found: Sequence[Any]) -> set[str]:
    """The sites the run did more than read the name of: qualified by their
    fingerprint, or already held by OpenCTI."""
    sites: set[str] = set()
    for step in found:
        ref = _ref(step)
        if ref.get("kind") == "site_fingerprint" and _fingerprint_signals(step):
            sites.add(_site_key(str(ref.get("subject") or seed)))
        elif ref.get("kind") == "opencti":
            for item in _evidence(step):
                label = str(item.get("label") or "")
                if label.endswith(KNOWN_LABEL_SUFFIX):
                    sites.add(_site_key(label[: -len(KNOWN_LABEL_SUFFIX)]))
    return sites


def build_knowledge(subject_value: str, steps: Sequence[Any]) -> OpenCTIKnowledge:
    """The STIX objects a run's steps establish. Pure: reads the record only."""
    knowledge = OpenCTIKnowledge()
    seed = (urlsplit(subject_value).hostname if "://" in subject_value else subject_value) or subject_value
    seed = seed.strip().lower()
    seed_type = _observable_type(seed)
    if seed_type:
        knowledge.add(seed_type, seed)

    found = [s for s in steps if (s.status or "") in ("completed", "degraded")]
    vouched = _vouched_sites(seed, found)
    for step in found:
        ref = _ref(step)
        hop = ref.get("subject")
        if ref.get("kind") == "passive_dns":
            if not hop and ref.get("seeds"):
                # A list run's seeds: each finding is one seed and its newest IP.
                for item in _evidence(step):
                    domain, _, ip = str(item.get("label") or "").partition(SEED_IP_SEPARATOR)
                    domain, ip = domain.strip().lower(), ip.strip()
                    ip_type = _observable_type(ip)
                    if _observable_type(domain) == "Domain-Name" and ip_type in ADDRESS_TYPES:
                        knowledge.add("Domain-Name", domain)
                        knowledge.add(ip_type, ip)
                        knowledge.relationships.append((domain, ip, "resolves-to", str(item.get("quote") or "")[:500]))
            elif not hop and seed_type == "Domain-Name":
                # The seed's resolutions: newest first, each a resolves-to.
                for item in _evidence(step):
                    ip = str(item.get("label") or "").strip()
                    ip_type = _observable_type(ip)
                    if ip_type in ADDRESS_TYPES:
                        knowledge.add(ip_type, ip)
                        knowledge.relationships.append((seed, ip, "resolves-to", str(item.get("quote") or "")[:500]))
            elif hop and (hop_type := _observable_type(hop)) in ADDRESS_TYPES:
                knowledge.add(hop_type, hop)
                for item in _evidence(step):
                    site = _site_from_vt_link(item.get("href")) or site_of(str(item.get("label") or ""))
                    if site and _observable_type(site) == "Domain-Name" and site != seed and _site_key(site) in vouched:
                        knowledge.add("Domain-Name", site)
                        if knowledge.has(site):
                            knowledge.relationships.append(
                                (site, hop, "resolves-to", str(item.get("quote") or "")[:500])
                            )
        elif ref.get("kind") == "site_fingerprint":
            site = str(hop or seed).strip().lower()
            signals = _fingerprint_signals(step)
            if signals and _observable_type(site) == "Domain-Name":
                knowledge.add("Domain-Name", site)
                knowledge.notes.append(
                    (
                        site,
                        f"Deep Investigation site fingerprint of {site}: "
                        + ", ".join(signals)
                        + ". Signals observed on the home page; not a verdict.",
                    )
                )
    # A relationship needs both ends created.
    knowledge.relationships = [
        r for r in dict.fromkeys(knowledge.relationships) if knowledge.has(r[0]) and knowledge.has(r[1])
    ]
    return knowledge


def format_knowledge(knowledge: OpenCTIKnowledge) -> str:
    """The knowledge as the section `read_investigation_report` appends: the
    exact objects to record, so the agent creates these and nothing else."""
    if not knowledge.observables:
        return ""
    lines = [
        "## Knowledge to record in OpenCTI",
        "",
        "Derived from the run's steps, not from its prose: a co-hosted site is listed once the run qualified it or "
        "OpenCTI already holds it. The Report container holds them all.",
        "",
        "Observables:",
        *[f"- {o.type} {o.value}" for o in knowledge.observables],
    ]
    if knowledge.relationships:
        lines += ["", "Relationships (from, type, to - and the description to give it):"]
        lines += [f"- {a} {t} {b} - {d}" if d else f"- {a} {t} {b}" for a, b, t, d in knowledge.relationships]
    if knowledge.notes:
        lines += ["", "Notes (attached to the observable):"]
        lines += [f"- on {value}: {content}" for value, content in knowledge.notes]
    return "\n".join(lines)


def opencti_record_request(subject_value: str, investigation_id: str) -> str:
    """The instruction "Send to OpenCTI" sends to the conversation's agent."""
    return (
        f"Record the findings of the Deep Investigation of {subject_value} above in OpenCTI, in a new draft named "
        f'"Deep Investigation - {subject_value}". Read them with read_investigation_report (investigation_id '
        f'{investigation_id}): its "Knowledge to record in OpenCTI" section lists the observables, relationships '
        "and notes to create. Create the draft first, then each observable, relationship and note in it, then a Report "
        "container holding all of them, with the cited report as its description. Create nothing the list does not "
        "hold, and end with the link to the draft."
    )
