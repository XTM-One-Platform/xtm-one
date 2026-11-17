# Domain Investigation

Investigate a domain or hostname end-to-end: reputation, registration, resolution, hosting fingerprint, rendered content, ransomware-claim status and pivot candidates, consolidated into a rated investigation summary.

**Tool rule.** Use only the tools you actually have. When an integration is not connected (its tools are not in your tool list), say which source was skipped instead of guessing its result. Never fabricate a detection ratio, registration date or scan verdict. How to read each source's output is detailed in the **Threat Intel Sources** skill; load it if it is attached and you need the interpretation details.

## When to use

- The user asks to investigate, check, or enrich a domain / hostname
- The user pastes a suspicious domain and asks "what is this?"
- A URL investigation hands off the parent domain for deeper context
- An IP investigation surfaced a hostname worth investigating

## Inputs

```yaml
indicator: DOMAIN_OR_HOSTNAME
context: OPTIONAL
tlp_ceiling: [optional, default AMBER]
```

## Procedure

### 1. Validate and triage

- Confirm it parses as a valid domain (reject IPs and URLs - route those to the **IP Investigation** or **URL Investigation** skill). Refang `example[.]com` before querying; keep the defanged form in the output.
- Check for obvious false positives: very popular domains (top-1M lists), your own organisation's infrastructure, well-known CDNs and cloud providers. With MISP connected, `check_misp_warninglists` on the domain is a fast false-positive triage.
- Prior knowledge: OpenCTI / MISP correlation in step 2; `search_memory` for earlier investigations by this agent.

### 2. Parallel lookups

Issue these calls in the same turn (parallel tool calls), one per connected integration:

| Source (integration) | Tool call(s) | Why |
|---|---|---|
| VirusTotal | `get_vt_domain_report` | Reputation, detection ratio, categories, WHOIS summary, last DNS records, certificate |
| urlscan.io | `search_urlscan_scans` (query `page.domain:DOMAIN`, most recent first) | Existing scans - search, do NOT submit a new scan for a bare domain |
| Shodan | `resolve_shodan_dns` (the hostname), then `get_shodan_host_info` on the resolved IP | DNS resolution + hosting fingerprint of the resolved IP |
| Ransomware.live | `search_ransomlive_victims` (query = org candidate, see below) | Victim-status sweep: is the domain's owner claimed on a leak site? |
| OpenCTI (internal) | `list_opencti_observables` (search = the domain, type Domain-Name) and `search_opencti` (search = the domain) | Internal correlation: observable, indicators, linked reports, intrusion sets, malware |
| MISP (internal) | `search_misp_attributes` (value = the domain) | Internal correlation against your own catalogued events |
| CrowdStrike Falcon Intelligence | `list_crowdstrike_intel_indicators` (`filter_query` `indicator:'DOMAIN'`) | Malicious confidence, linked actors and malware families, report references |

Add these to the same batch when the integration is connected:

| Source | Tool call(s) | Why |
|---|---|---|
| DomainTools | `get_domaintools_parsed_whois` (or `get_domaintools_whois`), `get_domaintools_risk_score` | Registrar, creation date, registrant, name servers; risk score with evidence |
| Google Threat Intelligence | `get_gti_domain_assessment` | Verdict, threat score, severity, associated threats |
| Recorded Future | `lookup_rf_domain` | Risk score, triggered rules, related infrastructure |
| abuse.ch | `lookup_abusech_host` (URLhaus) and `search_abusech_iocs` (ThreatFox, search by IOC value) | Malware-distribution history, C2 listings |
| Microsoft Defender TI | `get_defender_ti_host_reputation`, `get_defender_ti_passive_dns` | Reputation verdict; passive DNS history |
| Intel 471 / Flashpoint / Cybersixgill / SOCRadar | `search_intel471_iocs`, `lookup_flashpoint_ioc`, `enrich_cybersixgill_ioc`, `analyze_socradar_indicator` | Underground and vendor context |

**Ransomware.live sweep.** Derive an org candidate from the domain by stripping the public suffix and any common subdomains (`www.acme-corp.com` -> `acme-corp` or `acme corp`). If the apex contains a hyphen or dot, also try the first label alone. That is at most two queries. Narrow with `country` / `sector` filters when the result set is large: the API returns full result sets.

Escalation (separate budget, only when warranted):

- Infrastructure relationships with `get_vt_related_objects` (entity_type `domain`; relationships `resolutions`, `subdomains`, `siblings`, `communicating_files`, `downloaded_files`, `historical_whois`, `historical_ssl_certificates`, `urls`). One call per relationship: request only what you need.
- Subdomains and DNS records in one call: `get_shodan_domain_info` (costs a Shodan query credit).
- Certificate pivoting (formerly Censys): `count_shodan_hosts` first, then `search_shodan_hosts` with `ssl.cert.subject.CN:"DOMAIN"`; also VirusTotal `historical_ssl_certificates` and DomainTools `search_domaintools_iris`.
- Registrant / name-server clusters: `search_domaintools_reverse_whois`, `search_domaintools_reverse_nameserver`, `get_domaintools_whois_history`.

Sources from the original pack with no XTM One integration:

- **AlienVault OTX** (community pulses and passive DNS): use OpenCTI, MISP, abuse.ch ThreatFox, Google Threat Intelligence, Recorded Future or Feedly (`search_feedly_articles`) for pulses / reports; Defender TI, DomainTools or VirusTotal `resolutions` for passive DNS.
- **Censys** (certificate search): use Shodan certificate queries, VirusTotal `historical_ssl_certificates`, DomainTools Iris.
- **ReversingLabs** (files seen resolving / contacting the domain): use VirusTotal `communicating_files` / `downloaded_files`, abuse.ch URLhaus, Google Threat Intelligence.

Quota-sensitive: VirusTotal, Shodan (query credits) and urlscan search have tight free-tier quotas. One domain = one call per source.

### 3. Consolidate

```yaml
indicator: DOMAIN
type: domain
consolidated_verdict: malicious | suspicious | clean | unknown
reputation:
  vt_detection_ratio: X/Y
  vt_categories: [LIST]
  vendor_scores: [gti / recorded_future / domaintools risk / crowdstrike confidence]
registration:
  registrar: <...>
  creation_date: <...>
  age_days: CALC
dns:
  resolved_ip: <...>
  passive_dns: [HISTORICAL_RESOLUTIONS]
  ns: [NAME_SERVERS]
hosting:
  asn: [from shodan on resolved ip]
  org: <...>
  country: <...>
  certificates: [present/absent, recent renewal?]
content:
  urlscan_verdict: FROM_EXISTING_SCANS
  urlscan_screenshot: URL_IF_AVAILABLE
internal_context:
  opencti: [observable / indicators / linked entities - or "not found"]
  misp: [events and creator orgs - or "not found"]
community_context:
  report_count: [reports / articles referencing the domain, per source]
  malware_families: [<...>]
  attributed_actors: [<...>]
ransomware_status:
  matched_total: N             # total leak-site claims matching org-candidate
  high_confidence_matches:       # only entries where website OR title clearly maps to the domain
    - victim_id: <...>
      title: <...>
      group: RANSOMWARE_GROUP
      country: CC
      sector: <...>
      published: ISO_DATE
      website: [victim website if present]
      description_excerpt: [first 200 chars - quote sparingly, criminal-written]
      permalink: [ransomware.live record URL]
  notes: [attribution caveat - name collisions are common, e.g. "Acme" is many companies]
sources_skipped: [[integrations not connected or that errored]]
pivot_candidates:
  - type: ip
    value: RESOLVED_IP
    reason: resolves_to
  - type: domain
    value: CERT_SIBLING
    reason: shares_tls_cert
  - type: hash
    value: COMMUNICATING_FILE
    reason: downloaded_from
```

Do not average verdicts: reason about agreement between sources and weight each by its Admiralty rating.

### 4. Prioritize pivots

1. **Resolved IP** (always - then the **IP Investigation** skill on it if suspicious)
2. **TLS certificate siblings** (strong infrastructure link)
3. **Newly-registered-domain clusters** (if created < 30 days ago + similar TLD / naming pattern)
4. **Communicating files from VirusTotal** (medium)
5. **Passive DNS neighbours** (low-medium)

### 5. Apply rigor (inline)

Apply these rules to the output yourself. For a finished intelligence product, also load the **Source Assessment (Admiralty Scale)** and **Confidence Levels** skills if they are attached.

**Admiralty rating per source** (reliability A-F, credibility 1-6). Starting points, adjust on content:

| Source | Default | Adjust |
|---|---|---|
| VirusTotal | B2 | C3 if only 1-2 engines detect |
| urlscan.io | B2 | C3 if the verdict is ambiguous; live-scan evidence is strong, verdicts can be noisy |
| Shodan | B2 | C3 if the banner `last_update` is > 30 days old |
| Ransomware.live | B2 for victim name, country, sector, dates, group | B3-B4 for the criminal-written `description` |
| CrowdStrike Falcon Intelligence | A2 | B3 if malicious confidence is low / unverified |
| OpenCTI, MISP | B2 | Rate the author behind the entity / event: own analysts A1, national CERT / ISAC A1, reputable vendor A2-B2, open feeds B3-C3, anonymous contributor C3 |
| DomainTools, Google TI, Recorded Future | B2 | WHOIS can be privacy-redacted; read the risk evidence, not only the score |

**TLP**: default AMBER for investigative findings unless the user specifies otherwise; never exceed the `tlp_ceiling`; pure OSINT aggregations may be CLEAR.

**Confidence (0-100)** on the consolidated verdict: Very High 90-100, High 75-89, Moderate 50-74, Low 25-49, Very Low 0-24. Justify the score with the source ratings and independent corroboration.

**Likelihood words** for uncertain or forward-looking statements: Almost certain (>95%), Highly likely (75-95%), Likely (50-75%), Realistic possibility (25-50%), Unlikely (15-25%), Highly unlikely (5-15%), Remote (<5%).

### 6. Return

```yaml
title: "Domain Investigation: DOMAIN"
tlp: AMBER
confidence: SCORE_0_100
summary: 2_3_SENTENCES
verdict: <...>
reputation: <...>
registration: <...>
dns: <...>
hosting: <...>
content: <...>
internal_context: <...>
community_context: <...>
ransomware_status: <...>
pivot_candidates: TOP_3_5
source_ratings: <...>
sources_skipped: [<...>]
investigation_date: ISO8601_TIMESTAMP
recommended_next_steps: <...>
```

## Special cases

- **Newly-registered domains (< 7 days)** - very common in phishing campaigns; flag even if the reputation score is still low. Pivot aggressively to find sibling domains.
- **Parked / sinkhole domains** - urlscan will show a parking page; do not treat as malicious automatically.
- **Subdomain of a known CDN** (e.g. `xyz.cloudfront.net`) - the interesting data is the hostname pattern and what it fronts, not the CDN itself.

### Ransomware-claim hits - handle with care

A match in `ransomware_status` does **not** prove the queried domain's owner has been breached. Validate before reporting:

1. **Cross-check the `website` field on the victim record** (`get_ransomlive_victim` for detail). If it contains the queried domain (or its apex), confidence is high. If it is blank or a different domain, treat it as a possible name collision.
2. **Common-name collisions.** Generic apex strings (`acme`, `apple`, `delta`, `chase`) match many unrelated organisations. Always inspect the top 2-3 results rather than trusting `matched_total` alone.
3. **Credibility split.** Group attribution and dates are B2 (probably true), but the criminal-written `description` field is B3-B4 (often inflated, written to coerce payment). Quote selectively - it can contain victim personal data or stolen credentials - and never present a leak-site claim as a confirmed breach without independent corroboration.
4. **No hit does not mean clean.** Ransomware.live only tracks public *leak-site* claims. Quietly-paid victims and intrusions that did not escalate to extortion never appear.

## Writing back (only when asked)

Do not write to OpenCTI or MISP unless the user or the assignment asks for it. When asked, create the observable / indicator inside an OpenCTI draft (`create_opencti_draft`, `create_opencti_observable`, `create_opencti_indicator`) with the TLP marking and a neutral score unless the evidence justifies more; in MISP, add to an unpublished event and never publish without review.

## Related skills

- **IP Investigation**, **URL Investigation**, **Hash Investigation**
- **Indicator Pivoting** - deeper graph pivoting
- **Threat Actor Profiling** - if the domain attributes to a known actor (for a ransomware group, `get_ransomlive_group` gives the group profile)
- **Ransomware Ecosystem** - knowledge cell to consult when a victim hit lands
- **CTI Orchestrator**
