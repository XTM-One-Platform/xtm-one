# IOC Enrichment Workflow

This workflow defines how to enrich a batch of raw indicators of compromise: parse and deduplicate them, route each type to the right XTM One integration tools, and synthesise one enrichment record per indicator before triage or before pushing anything into a sharing platform.

For a single seed indicator, the **IP Investigation**, **Domain Investigation**, **Hash Investigation** and **URL Investigation** skills give a deeper first-hop chain; this workflow is their bulk-list equivalent. For graph traversal beyond the first hop, hand off to the **Indicator Pivoting** skill.

## Rules for tool use

- Use only the tools you actually have. When an integration is not connected, say which source was skipped instead of guessing its result, and lower the corroboration accordingly.
- Three sources used by the original workflow have no XTM One integration: **AlienVault OTX** (pulses, community reputation), **Censys** (services, certificates) and **ReversingLabs** (file and network reputation from a malware corpus). Their rows in the routing tables below name the closest available substitute. Never report an OTX, Censys or ReversingLabs result.
- Prefer **batch tools** over one call per indicator: GreyNoise `lookup_greynoise_ips` (up to 1,000 IPs), CrowdSec `lookup_crowdsec_ips` (up to 100 IPs), Google Threat Intelligence `get_gti_ioc_summaries` (up to 100 mixed IOCs), Recorded Future `enrich_rf_indicators` (up to 1,000 mixed IOCs) and `triage_rf_indicators` (yes/no verdict), MISP `check_misp_warninglists` (comma-separated values).
- Use `code_interpreter` for the mechanical work: parsing pasted text or an uploaded file, refanging, regex validation, deduplication, batching, merging tool outputs into records and scoring. Do not do this by hand on more than a handful of indicators.
- Enrichment is read-only. Never write to OpenCTI, MISP, a SIEM or an EDR unless the user or the assignment asks for it (see Step 6).

## Seeding the queue (optional)

When the user does not already have a batch but wants fresh leads:

- CrowdStrike: `list_crowdstrike_intel_indicators` (filter on malicious confidence high, last 7 days, and optionally a type or an actor) returns the latest high-confidence Falcon Intelligence IOCs.
- abuse.ch: `list_abusech_recent_iocs` (ThreatFox), `list_abusech_recent_urls` (URLhaus), `list_abusech_recent_malware` (MalwareBazaar).
- OpenCTI: `list_opencti_observables` or `list_opencti_entities` filtered on recent creation, a label or a PIR (`query_opencti_pir_entities`).
- Feedly: `get_feedly_stream_iocs` on a threat-intelligence stream.

Feed the returned indicators into the per-type routing below.

## Enrichment routing by IOC type

### IPv4 / IPv6 address

| Order | Integration and tool | What to extract |
|-------|-------|----------------|
| 0 | MISP `check_misp_warninglists` | Known-benign infrastructure (public resolvers, CDNs, cloud ranges): flag as likely false positive before spending quota |
| 1 | VirusTotal `get_vt_ip_report` | Detection ratio, community score, associated domains, last analysis results |
| 2 | AbuseIPDB `check_abuseipdb_ip` | Abuse confidence score, report count, ISP, usage type, country |
| 3 | GreyNoise `lookup_greynoise_ips` (batch) or `lookup_greynoise_ip` | Classification (benign/malicious/unknown), noise status, actor, tags |
| 4 | Shodan `get_shodan_host_info`; `reverse_shodan_dns` (batch) for PTR records | Open ports, banners, services, OS, hostnames, vulns, last update |
| 5 | Community reputation (OTX has no integration): abuse.ch `search_abusech_iocs`, `lookup_abusech_host`; GTI `get_gti_ioc_summaries` | ThreatFox / URLhaus hits with malware family and confidence; GTI verdict and threat score |
| 6 | CrowdSec `lookup_crowdsec_ips` (batch) | Reputation, behaviours, noise, AS (second scanner/noise opinion) |
| 7 | CrowdStrike `list_crowdstrike_intel_indicators` (filter on the value) | Falcon Intel malicious confidence, linked actors and malware families, report references. Run when connected. |
| 8 | Recorded Future `enrich_rf_indicators` (batch) | Risk score, criticality band, most critical rule. Run when connected. |
| 9 | Service and certificate detail (Censys has no integration): Shodan banners, VirusTotal `get_vt_related_objects` (`historical_ssl_certificates`) | Services, certificates, autonomous system, location |
| 10 | MISP `search_misp_attributes` | Internal correlation: prior catalogued events, tags |
| 11 | OpenCTI `list_opencti_observables` / `search_opencti` | Internal correlation: existing observables and indicators, score, labels |

ReversingLabs "files seen contacting the IP" is replaced by VirusTotal `get_vt_related_objects` with `communicating_files`, run only for IPs that survive the first triage (quota).

### Domain

| Order | Integration and tool | What to extract |
|-------|-------|----------------|
| 0 | MISP `check_misp_warninglists` | Top-site lists, known-benign domains |
| 1 | VirusTotal `get_vt_domain_report` | Detection ratio, WHOIS, DNS records, categories |
| 2 | urlscan `search_urlscan_scans` (`domain:D`) | Existing scans (search, do not re-submit by default): screenshot, page content, redirects, technologies, IPs resolved |
| 3 | Shodan `resolve_shodan_dns` (batch, comma-separated) | Current DNS resolution; open ports on resolved IPs via `get_shodan_host_info` |
| 4 | Community reputation (OTX has no integration): abuse.ch `lookup_abusech_host`, `search_abusech_iocs`; GTI `get_gti_ioc_summaries` | URLhaus / ThreatFox hits; GTI verdict; passive DNS from VirusTotal `resolutions` |
| 5 | DomainTools `get_domaintools_risk_score`, `get_domaintools_parsed_whois` | Risk score, registrant, creation date (young domains are a strong signal). Run when connected. |
| 6 | CrowdStrike `list_crowdstrike_intel_indicators` | Falcon Intel malicious confidence, linked actors and malware families. Run when connected. |
| 7 | Recorded Future `enrich_rf_indicators` | Risk score and rule. Run when connected. |
| 8 | Certificate history (Censys has no integration): VirusTotal `get_vt_related_objects` (`historical_ssl_certificates`) | Certificate history, associated IPs |
| 9 | MISP `search_misp_attributes` | Internal correlation |
| 10 | OpenCTI `list_opencti_observables` / `search_opencti` | Existing observables and indicators in your knowledge base |
| 11 | Ransomware.live `search_ransomlive_victims` (org candidate) | Leak-site claims that match the apex domain (see the Domain Investigation skill for caveats) |

ReversingLabs "files seen resolving the domain" is replaced by VirusTotal `communicating_files` / `downloaded_files` relationships.

### URL

| Order | Integration and tool | What to extract |
|-------|-------|----------------|
| 1 | VirusTotal `get_vt_url_report` | Detection ratio, final URL, redirections, downloaded files |
| 2 | urlscan `search_urlscan_scans` (`page.url:"URL"`); `submit_urlscan_scan` only if no recent capture exists | Screenshot, DOM, requests, IPs contacted, technologies |
| 3 | abuse.ch URLhaus `lookup_abusech_url` | Malware-distribution status, payloads served, tags |
| 4 | GTI `get_gti_ioc_summaries` / `get_gti_url_assessment` | Verdict and attribution (replaces OTX pulse associations) |
| 5 | CrowdStrike `list_crowdstrike_intel_indicators` | Falcon Intel verdict, actors, families. Run when connected. |
| 6 | ANY.RUN `submit_anyrun_url_analysis` (only when a fresh crawl + sandbox is wanted and the user agrees) | Redirect chain, dropped payloads, verdict (replaces ReversingLabs URL submission) |
| 7 | MISP `search_misp_attributes` | Internal correlation |
| 8 | OpenCTI `list_opencti_observables` / `search_opencti` | Existing observables and indicators |

Submitting a URL to urlscan or ANY.RUN can expose it to other users of those services depending on the visibility setting. Ask before submitting URLs that may contain victim identifiers or tokens.

### File hash (MD5, SHA-1, SHA-256)

| Order | Integration and tool | What to extract |
|-------|-------|----------------|
| 1 | VirusTotal `get_vt_file_report` | Detection ratio, file type, size, names, popular threat label, ATT&CK tags |
| 2 | abuse.ch MalwareBazaar `lookup_abusech_malware_hash` | Family signature, first seen, delivery method, tags |
| 3 | GTI `get_gti_ioc_summaries` / `get_gti_file_assessment` | Verdict, threat score, attribution (replaces the ReversingLabs classification) |
| 4 | CrowdStrike `list_crowdstrike_intel_indicators` | Falcon Intel verdict, threat actors and malware families linked to the hash, report references. Run when connected. |
| 5 | VirusTotal `get_vt_file_behaviour` (only for hashes that matter) | Sandbox verdicts, network IOCs, ATT&CK techniques |
| 6 | MISP `search_misp_attributes` | Internal correlation |
| 7 | OpenCTI `list_opencti_observables` / `search_opencti` | Existing observables and indicators |
| 8 | Ransomware.live `get_ransomlive_iocs` and `get_ransomlive_yara` (group name) | Only if the hash hits a known ransomware family from VirusTotal / MalwareBazaar / GTI classification |

### Email address

| Order | Integration and tool | What to extract |
|-------|-------|----------------|
| 1 | VirusTotal `search_vt` | Associated domains and files (may return little without a premium key) |
| 2 | DomainTools `search_domaintools_reverse_whois` | Domains registered with this email. Run when connected. |
| 3 | MISP `search_misp_attributes` (type email) | Internal correlation |
| 4 | OpenCTI `list_opencti_observables` / `search_opencti` | Existing observables and indicators |
| 5 | Credential-exposure sources, only when the user asks about exposure: Recorded Future `lookup_rf_identity_exposures`, Flashpoint `search_flashpoint_credentials`, Intel 471 `search_intel471_credentials` | Breach and infostealer exposure. Treat results as sensitive personal data. |

## Enrichment process

### Step 1: Parse and classify

Read the input list (pasted text, an uploaded file, an OpenCTI container via `get_opencti_container_full`, or a MISP event via `get_misp_event`). With `code_interpreter`, for each indicator:

1. Refang (`hxxp` -> `http`, `[.]` -> `.`, `[@]` -> `@`), strip whitespace and trailing punctuation.
2. Determine the type (IPv4, IPv6, domain, URL, MD5 / SHA-1 / SHA-256, email).
3. Validate the format (regex, hash length, IP parsing). Drop private, loopback and reserved IPs into a separate "not enriched" list with the reason.
4. Deduplicate (same value + same type = one entry; normalise case for domains and hashes).
5. Run MISP `check_misp_warninglists` on the whole list when MISP is connected, and mark hits as likely false positives.

### Step 2: Batch and route

Group IOCs by type. For each group, call the relevant tools from the routing tables:

- Use the batch tools first (GreyNoise, CrowdSec, GTI, Recorded Future) for the whole group.
- Then make per-indicator calls (VirusTotal, AbuseIPDB, Shodan, urlscan) in batches of about 10, and only for indicators the batch pass did not already settle as benign noise.
- For a single IOC, make all relevant calls in parallel.

Quota-sensitive sources (VirusTotal on a standard key, Shodan query credits, urlscan, DomainTools) deserve prioritisation: enrich the indicators that matter for the decision first, and say which ones were left unenriched for quota.

### Step 3: Synthesise results

For each IOC, combine the results into a single enrichment record (build them with `code_interpreter` for a batch):

```yaml
indicator: 203.0.113.42
type: ipv4-addr
enrichment_date: 2026-04-04
source_assessment: F6  # automated enrichment, no human judgment yet

virustotal:
  detection_ratio: 12/87
  community_score: -45
  associated_domains: [evil.example.com, bad.example.org]

abuseipdb:
  abuse_confidence: 95
  total_reports: 234
  isp: "Bulletproof Hosting Inc"
  country: RU

greynoise:
  classification: malicious
  noise: false
  tags: [c2, cobalt-strike]

shodan:
  ports: [80, 443, 8443]
  os: Linux
  vulns: [CVE-2024-12345]

threatfox:
  hits: 2
  malware: cobalt_strike
  confidence_level: 75

misp:
  matched_attributes: 3
  matched_events: [42, 137]   # event IDs in the connected instance
  prior_tags: ["tlp:amber", "actor:apt28"]

opencti:
  known_as: [observable, indicator]
  indicator_score: 85          # x_opencti_score on the existing indicator
  prior_labels: [apt28, c2]

skipped_sources: [AlienVault OTX (no integration), Censys (no integration), CrowdStrike (not connected)]

synthesis:
  verdict: malicious
  confidence: 85
  context: "Known C2 server associated with APT28 campaigns. Hosted on bulletproof infrastructure in Russia. Multiple community reports plus prior MISP events confirm malicious activity."
  tags: [apt28, c2, cobalt-strike]
  mitre_attack: [T1071.001]
```

For a batch, also produce a summary table (indicator, type, verdict, confidence, key evidence, sources used, sources skipped) and the counts per verdict. Count only what was actually enriched: an indicator skipped for quota or a missing integration is "not enriched", never "clean".

### Step 4: Assess and tag

Apply a source assessment (Admiralty Scale) to each enrichment record, inline:

- Source reliability: B (established tool APIs, usually reliable).
- Information credibility from corroboration across tools: 3+ tools agree -> 1 (Confirmed); 2 agree -> 2 (Probably true); single source -> 3 (Possibly true).
- A MISP or OpenCTI hit on a previously curated event or entity lifts credibility one step (your team has already vetted it once).
- A warninglist hit, a GreyNoise "benign" classification or a known business service (GreyNoise business-service trust) pushes toward a false-positive verdict regardless of vendor detections on shared infrastructure.

Express confidence as 0-100 with the matching word (High / Moderate / Low), and carry the most restrictive TLP of the inputs. For the full method, load the **Source Assessment (Admiralty Scale)** or **Confidence Levels** skill if attached.

### Step 5: Deliver

Return the records in the run or chat output. For more than a screenful, produce a file with `generate_file` (CSV or JSON, see the **IOC Export** skill for columns) or a report with `generate_report`. Apply the TLP before sharing outside the team (the **TLP Guide** skill has the rules).

### Step 6 (optional, only when asked): Push back

If the enrichment confirms previously unknown malicious indicators and the user or the assignment asks to record them, push them into your own platform so future enrichments hit your catalogue first:

- **OpenCTI**: create a draft (`create_opencti_draft`, then `use_opencti_draft`) and add the observables (`create_opencti_observable`) and indicators (`create_opencti_indicator`, with a score and labels) so an analyst reviews before they reach the knowledge graph. For an existing observable, `promote_opencti_observable` creates the matching indicator. Mark with `add_opencti_marking`.
- **MISP**: `add_misp_attribute` on an existing event, or `create_misp_event` for a fresh cluster, `add_misp_tag` for TLP and context; publish only on explicit request.
- **Detection platforms** (Sentinel `create_sentinel_threat_indicator`, Defender `create_defender_indicator`, CrowdStrike `create_crowdstrike_ioc`, Splunk ES `add_splunk_es_threat_intel`): only on explicit request, since these change blocking or alerting behaviour.

### Step 7 (optional): Pivot

If the enrichment surfaces strong cluster candidates (certificate siblings, JARM matches, communicating files), hand off to the **Indicator Pivoting** skill for the next hop.

## Quota awareness

| Service | Typical constraint | Mitigation |
|---------|-----------------|------------|
| VirusTotal (standard key) | Low per-minute and per-day quota | Enrich priority indicators first; skip relationships unless needed |
| urlscan.io | Daily submission quota | Prefer `search_urlscan_scans` over `submit_urlscan_scan`; check `get_urlscan_quotas` |
| Shodan | Query credits | `get_shodan_host_info` per IP; avoid broad searches during enrichment |
| AbuseIPDB | Daily check quota | IPs only |
| GreyNoise | Plan-dependent | Use `lookup_greynoise_ips` for the batch |
| CrowdSec, GTI, Recorded Future | Plan-dependent | Use the batch tools |
| DomainTools | Per-call credits | Only for domains that matter |
| MISP, OpenCTI | Host-bound | No public limit |
| Ransomware.live | Plan-dependent | Org-candidate sweeps only for domains that matter |

## Handling missing integrations

If an integration is not connected (or not available in XTM One):

1. Skip that enrichment source.
2. Note in the synthesis that the source was unavailable (`skipped_sources`).
3. Adjust confidence accordingly (fewer sources = lower corroboration).
4. Continue with the available sources.

Integrations are configured once by an administrator in XTM One > Integrations; tell the user which one would have helped.

## Related skills

- **Threat Intel Sources**: how to read each integration's verdict fields and its false-positive traps
- **IP Investigation**, **Domain Investigation**, **Hash Investigation**, **URL Investigation**: single-seed first-hop chains
- **Indicator Pivoting**: when an enrichment opens new pivot candidates
- **IOC Export**: packaging the enriched set as CSV, STIX 2.1 or a MISP event
- **Source Assessment (Admiralty Scale)**, **TLP Guide**, **Confidence Levels**: full rigor for each finished enrichment record
