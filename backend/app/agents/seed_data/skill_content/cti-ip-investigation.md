# IP Investigation

Investigate an IP address (IPv4 or IPv6) end-to-end: query the connected threat-intelligence integrations in parallel, consolidate the results, prioritize follow-up IOCs and return a rated investigation summary.

**Tool rule.** Use only the tools you actually have. When an integration is not connected (its tools are not in your tool list), say which source was skipped instead of guessing its result. Never fabricate a detection ratio, score or report count. How to read each source's output (verdict fields, false-positive traps, what "not found" means) is detailed in the **Threat Intel Sources** skill; load it if it is attached and you need the interpretation details.

## When to use

- The user asks to investigate, check, or enrich an IP
- The user pastes an IP and asks "what is this?" / "is this malicious?"
- Another investigation surfaces an IP for follow-up (resolved IP of a domain, C2 of a sample, host of a URL)
- IOC triage. For a list of many indicators, prefer the **IOC Enrichment Workflow** skill (batching and throttling).

## Inputs

```yaml
indicator: [IPv4 or IPv6 address]
context: [optional - why is the user asking? where did this come from?]
tlp_ceiling: [optional - CLEAR/GREEN/AMBER/AMBER+STRICT/RED, default AMBER]
```

## Procedure

### 1. Validate and triage (before spending API quota)

- Reject RFC 1918 / loopback / link-local / CGNAT / documentation ranges (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32) unless the user specifically asks. These are not public infrastructure and enrichment is wasted. Use `code_interpreter` (Python `ipaddress`) if you need to check a range reliably.
- Defang-aware parsing: accept `1[.]2[.]3[.]4` and refang it before querying; keep the defanged form in the output.
- Check whether the IP is already known internally (step 2, internal correlation). If OpenCTI already holds an observable with enrichment, load it and note its age instead of re-querying every source.
- Prior conversations: `search_memory` for earlier investigations of this IP by this agent.

### 2. Parallel lookups

Issue these calls in the same turn (parallel tool calls), one per connected integration:

| Source (integration) | Tool call(s) | Why |
|---|---|---|
| GreyNoise | `lookup_greynoise_ip` (or `lookup_greynoise_community_ip` on the free community tier) | FAST - filter out internet background noise early |
| VirusTotal | `get_vt_ip_report` | Reputation aggregate: detection ratio, ASN, country, certificate, last analysis |
| AbuseIPDB | `check_abuseipdb_ip` | Abuse-report history: confidence 0-100, total reports, distinct reporters, usage type, Tor flag |
| Shodan | `get_shodan_host_info` | Infrastructure fingerprint: open ports, services, banners, OS, CVEs, hostnames |
| OpenCTI (internal) | `list_opencti_observables` (search = the IP, types IPv4-Addr / IPv6-Addr) and `search_opencti` (search = the IP) | Internal correlation: is the IP an observable or in an indicator pattern? linked reports, intrusion sets, malware |
| MISP (internal) | `search_misp_attributes` (value = the IP) | Internal correlation against your own catalogued events |
| CrowdStrike Falcon Intelligence | `list_crowdstrike_intel_indicators` (`filter_query` `indicator:'IP'`) | Vendor view: malicious confidence, linked actors and malware families, report references |

Add these to the same batch when the integration is connected (they replace or complement the community sources the original pack used):

| Source | Tool call(s) | Why |
|---|---|---|
| Google Threat Intelligence | `get_gti_ip_assessment` | Verdict, threat score, severity, associated threats |
| Recorded Future | `lookup_rf_ip` | Risk score, triggered risk rules, C2 / threat-actor links |
| abuse.ch | `lookup_abusech_host` (URLhaus) and `search_abusech_iocs` (ThreatFox, search by IOC value) | Malware-distribution history, botnet C2 listings |
| CrowdSec | `lookup_crowdsec_ip` | Crowd-sourced attack behaviours, MITRE mapping, reputation |
| Microsoft Defender TI | `get_defender_ti_host_reputation` | Reputation verdict and scoring rules |
| Intel 471 / Flashpoint / Cybersixgill / SOCRadar | `search_intel471_iocs`, `lookup_flashpoint_ioc`, `enrich_cybersixgill_ioc`, `analyze_socradar_indicator` | Underground and vendor context |
| VulnCheck | `lookup_vulncheck_ip_intel` (filter on the IP) | Exploitation / initial-access telemetry |

Escalation (separate budget, only when warranted):

- Pivot from the IP with `get_vt_related_objects` (entity_type `ip`; relationships `resolutions`, `communicating_files`, `downloaded_files`, `urls`, `historical_ssl_certificates`). Each relationship is a separate call: request only the ones you need.
- Passive DNS: `get_defender_ti_passive_dns`, `search_domaintools_reverse_ip` (DomainTools), or `reverse_shodan_dns` for current PTR records.
- Neighbourhood: `check_abuseipdb_network` on the /24 when the IP sits in a suspicious hosting range.
- Scanner behaviour over time: `get_greynoise_ip_timeline` (Enterprise tier) to tell a brief campaign from persistent scanning.

Sources from the original pack with no XTM One integration:

- **AlienVault OTX** (community pulses): use OpenCTI, MISP, abuse.ch ThreatFox (`search_abusech_iocs`), Google Threat Intelligence, Recorded Future or Feedly (`search_feedly_articles` with the IP) for community and report context.
- **Censys** (deep host and certificate recon): use Shodan (`get_shodan_host_info`, `search_shodan_hosts`), Google Threat Intelligence, DomainTools or urlscan (`search_urlscan_scans` with `page.ip:IP`).
- **ReversingLabs** (network reputation from a malware corpus, sample fan-out): use VirusTotal `get_vt_related_objects` (`communicating_files`), abuse.ch, CrowdStrike or Google Threat Intelligence.

Quota-sensitive: VirusTotal, Shodan and GreyNoise community tiers are tightly rate-limited. One IP = one call per source; do not loop over a list without throttling.

### 3. Short-circuit on noise

If GreyNoise reports the IP as internet background noise (`noise: true`) with `classification: benign`, AND AbuseIPDB confidence is below 10, it is very likely mass-scanning background noise. Summarize briefly and stop; do not run the full analysis.

- `riot: true` (a known benign common service: CDN, search engine, ISP resolver) is a strong signal to downgrade the severity of any detection against the IP.
- `noise: true` with `classification: malicious` is still interesting: opportunistic attacker infrastructure (mass-scanning threat actor), not a targeted campaign.

### 4. Consolidate

Build a unified view:

```yaml
indicator: IP
type: ipv4 | ipv6
consolidated_verdict: malicious | suspicious | clean | benign-noise | unknown
detection_signals:
  - source: virustotal
    detection_ratio: X/Y
    verdict: <...>
  - source: abuseipdb
    abuse_confidence: SCORE_0_100
    recent_reports: COUNT
    distinct_reporters: COUNT
  - source: greynoise
    noise: BOOL
    riot: BOOL
    classification: <...>
  - source: [gti | recorded_future | crowdstrike | crowdsec | abusech ...]
    verdict_or_score: <...>
infrastructure:
  asn: <...>
  org: <...>
  country: <...>
  open_ports: <[...] from shodan>
  services: [top 5 from shodan]
  vulnerabilities: CVE_LIST
  certificates: PRESENT_ABSENT
internal_context:
  opencti: [observable / indicator / linked reports, intrusion sets, malware - or "not found"]
  misp: [events and their creator orgs - or "not found"]
community_context:
  report_count: [reports, pulses or articles referencing the IP, per source]
  key_actors_attributed: [actors or malware families named by sources]
sources_skipped: [[integrations not connected or that errored]]
pivot_candidates:
  - type: domain
    value: HOSTNAME_FROM_SHODAN_VT
    reason: <"resolves to this IP", "shares cert", etc.>
  - type: hash
    value: [communicating_files from vt]
    reason: <...>
```

Do not average verdicts. Reason about agreement and disagreement between sources, and weight each by its Admiralty rating (below). A single vendor with A-rated reliability and high confidence outweighs several weak community signals.

### 5. Prioritize follow-up IOCs

Rank pivot candidates by investigative value:

1. **Certificate matches** (highest - strong infrastructure link)
2. **Communicating files** (medium-high - active C2 signal)
3. **Passive DNS resolutions** (medium - historical association)
4. **Recent WHOIS / registration overlaps** (medium)
5. **Generic ASN / org neighbours** (low)

Return the top 3-5 candidates with a suggested next step: the **Domain Investigation** or **Hash Investigation** skill, or the **Indicator Pivoting** skill for multi-hop graph traversal.

### 6. Apply rigor (inline)

Apply these rules to the output yourself. For a finished intelligence product, also load the **Source Assessment (Admiralty Scale)** and **Confidence Levels** skills if they are attached.

**Admiralty rating per source** (reliability A-F, credibility 1-6). Starting points, adjust on content:

| Source | Default | Adjust |
|---|---|---|
| VirusTotal | B2 | C3 if only 1-2 engines detect |
| GreyNoise | B2 | `noise: true` is strong evidence the IP is not targeted |
| AbuseIPDB | C3 | B2 if distinct reporters > 10 and confidence > 75 |
| Shodan | B2 | C3 if the banner `last_update` is > 30 days old |
| CrowdStrike Falcon Intelligence | A2 | B3 if malicious confidence is low / unverified |
| OpenCTI, MISP | B2 | Rate the author behind the entity / event: own analysts A1, national CERT / ISAC A1, reputable vendor A2-B2, open feeds B3-C3, anonymous contributor C3 |
| Google TI, Recorded Future | B2 | Vendor-scored; read the triggered rules, not only the score |
| abuse.ch, CrowdSec | B2-C3 | Community-reported; check first / last seen |

**TLP**: mark the output. Default AMBER for investigative findings unless the user specifies otherwise; never exceed the `tlp_ceiling` the user gave; pure OSINT aggregations may be CLEAR.

**Confidence (0-100)** on the consolidated verdict: Very High 90-100, High 75-89, Moderate 50-74, Low 25-49, Very Low 0-24. Justify the score with the source ratings and the level of independent corroboration.

**Likelihood words** for any forward-looking or uncertain statement: Almost certain (>95%), Highly likely (75-95%), Likely (50-75%), Realistic possibility (25-50%), Unlikely (15-25%), Highly unlikely (5-15%), Remote (<5%). Avoid "may", "could", "might" without a band.

### 7. Return

```yaml
title: "IP Investigation: IP"
tlp: AMBER
confidence: SCORE_0_100
summary: 2_3_SENTENCES
verdict: <...>
detection_signals: <...>
infrastructure: <...>
internal_context: <...>
community_context: <...>
pivot_candidates: TOP_3_5
source_ratings:
  virustotal: B2
  shodan: B2
  ...
sources_skipped: [<...>]
investigation_date: ISO8601_TIMESTAMP
recommended_next_steps:
  - [e.g., "pivot on domain X with the Domain Investigation skill"]
```

## When to stop

- GreyNoise benign-noise short-circuit (step 3)
- All lookups returned "not found" -> report no signal; do not escalate
- Clear malicious verdict with high confidence -> return; leave deeper pivots to the user

## Writing back (only when asked)

Do not write to OpenCTI or MISP unless the user or the assignment asks for it. When asked: create the observable with `create_opencti_observable` (or an indicator with `create_opencti_indicator`), preferably inside a draft (`create_opencti_draft`) for analyst review, with the TLP marking and a neutral score (50) unless the evidence justifies more. In MISP, add the attribute to an unpublished event (`add_misp_attribute`) and never publish without review.

## Related skills

- **Domain Investigation**, **Hash Investigation**, **URL Investigation** - sibling investigations
- **Indicator Pivoting** - deeper graph traversal from a starting IOC
- **IOC Enrichment Workflow** - bulk IOC list enrichment
- **Threat Actor Profiling** - if the IP is attributed to a known actor
- **CTI Orchestrator** - typically the entry point
