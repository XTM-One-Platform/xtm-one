# URL Investigation

Investigate a URL end-to-end: scan it live with urlscan.io (unlisted by default), cross-reference its reputation, and extract the parent domain and resolved IP for follow-up investigations. Returns verdict, redirect chain, contacted infrastructure and screenshot, rated.

**Tool rule.** Use only the tools you actually have. When an integration is not connected (its tools are not in your tool list), say which source was skipped instead of guessing its result. Never fabricate a scan verdict, redirect chain or screenshot link. How to read each source's output is detailed in the **Threat Intel Sources** skill; load it if it is attached and you need the interpretation details.

## When to use

- The user asks to scan, check, or investigate a URL
- The user pastes a suspicious link from a phishing email
- A domain investigation or another skill hands off a specific URL for live-scan evidence

## Inputs

```yaml
indicator: URL
context: [optional - phishing email? user report? hunt result?]
tlp_ceiling: [optional, default AMBER - phishing-kit URLs often warrant AMBER+STRICT]
visibility: [unlisted | public | private - default unlisted for operational safety]
```

## Procedure

### 1. Validate

- Confirm it is a valid URL (scheme + host minimum). Refang `hxxp://example[.]com/...` before querying; keep the defanged form in the output. `code_interpreter` (Python `urllib.parse`) is reliable for parsing.
- Extract the parent domain - useful for later pivots.
- Extract path and query parameters, but DO NOT send them anywhere if they contain credentials, session tokens, e-mail addresses or other user-identifying values (see Special cases).

### 2. Primary scan

1. Look for an existing scan first: `search_urlscan_scans` with `page.url:"URL"` (or `task.url`), most recent first. A recent scan (< 24 h) can be reused with `get_urlscan_result` without spending a submission.
2. Otherwise submit: `submit_urlscan_scan` with `visibility` = `unlisted`. It returns a scan UUID.
3. The scan takes 30-90 seconds. While waiting, run the reputation lookups (step 3) in parallel, then fetch the result with `get_urlscan_result` (retry after a short wait if it is not ready yet, at most 3 times; still not ready, report the scan id as pending). Use `get_urlscan_dom` when you need the rendered HTML (credential forms, obfuscated JavaScript, kit fingerprints).

**Visibility default is `unlisted`.** Public submissions expose the IOC on urlscan's global feed and can tip off threat actors that you are investigating their infrastructure. Use `public` only if the user explicitly asks or the URL is already publicly known. Do not scan URLs that belong to the user's own infrastructure unless they explicitly ask.

### 3. Parallel reputation lookups

Issue these calls in the same turn (parallel tool calls), one per connected integration:

| Source (integration) | Tool call(s) | Why |
|---|---|---|
| VirusTotal | `get_vt_url_report` | Aggregate reputation: detections, categories, final redirect target |
| abuse.ch | `lookup_abusech_url` (URLhaus), `search_abusech_iocs` (ThreatFox, search by IOC value) | Malware-distribution status, payloads served, C2 listings |
| OpenCTI (internal) | `list_opencti_observables` (search = the URL, type Url) and `search_opencti` (search = the URL or the parent domain) | Internal correlation: observable, indicators, linked campaigns / malware |
| MISP (internal) | `search_misp_attributes` (value = the URL) | Internal correlation against your own catalogued events |
| CrowdStrike Falcon Intelligence | `list_crowdstrike_intel_indicators` (`filter_query` `indicator:'URL'`) | Malicious confidence, linked actors and malware families, report references |

Add these to the same batch when the integration is connected:

| Source | Tool call(s) | Why |
|---|---|---|
| Google Threat Intelligence | `get_gti_url_assessment` | Verdict, threat score, severity |
| Recorded Future | `lookup_rf_url` | Risk score, triggered rules, malware associations |
| ANY.RUN | `lookup_anyrun_ti` (`url` = the URL) | Existing sandbox detonations of the URL |
| Intel 471 / Flashpoint / Cybersixgill / SOCRadar | `search_intel471_iocs`, `lookup_flashpoint_ioc`, `enrich_cybersixgill_ioc`, `analyze_socradar_indicator` | Underground and vendor context |

Escalation (only when warranted - slower, consumes submission budget, and shares the URL with a third party):

- Fresh VirusTotal analysis: `scan_vt_url`, then `get_vt_analysis` with the returned id.
- Sandbox detonation (formerly a ReversingLabs crawl): `submit_anyrun_url_analysis` with a non-public privacy setting (`opt_privacy_type` `bylink` or stricter) - only when the user asks or the URL is already public, then `get_anyrun_analysis_report` / `get_anyrun_analysis_iocs`.
- Related infrastructure: `get_vt_related_objects` (entity_type `url`; relationships `last_serving_ip_address`, `redirects_to`, `downloaded_files`, `contacted_domains`, `contacted_ips`); `find_urlscan_similar_scans` on the scan UUID (urlscan Pro) to find structurally similar pages (phishing-kit reuse).

Sources from the original pack with no XTM One integration:

- **AlienVault OTX** (community pulses referencing the URL): use OpenCTI, MISP, abuse.ch URLhaus / ThreatFox, Google Threat Intelligence, Recorded Future or Feedly (`search_feedly_articles`).
- **ReversingLabs** (files seen requesting the URL; fresh crawl + sandbox): use VirusTotal `downloaded_files` / `communicating_files`, abuse.ch URLhaus payloads, and the ANY.RUN sandbox.

Quota-sensitive: urlscan submissions and VirusTotal free tiers are tightly rate-limited.

### 4. Consolidate

```yaml
indicator: URL
type: url
parent_domain: EXTRACTED
consolidated_verdict: malicious | suspicious | phishing | clean | unknown
live_scan:
  final_url: AFTER_REDIRECTS
  redirect_chain: [URLS]
  resolved_ip: FROM_URLSCAN
  server: FROM_URLSCAN
  screenshot_url: <...>
  scan_url: URLSCAN_IO_RESULT_PAGE
  behaviors_observed:
    - [e.g., "credential form", "JS obfuscation", "drive-by download attempt", "impersonates brand X"]
reputation:
  vt_detection_ratio: X/Y
  vt_categories: [LIST]
  vendor_scores: [gti / recorded_future / crowdstrike / urlhaus status]
contacted_infrastructure:
  domains: [TOP_10]
  ips: [TOP_10]
internal_context:
  opencti: [observable / indicators / linked entities - or "not found"]
  misp: [events and creator orgs - or "not found"]
community_context:
  attributed_campaigns: [[from vendor / community sources]]
  malware_family: [if the URL serves known malware]
sources_skipped: [[integrations not connected or that errored]]
pivot_candidates:
  - type: domain
    value: PARENT_DOMAIN
    reason: parent
  - type: ip
    value: RESOLVED_IP
    reason: hosts_url
  - type: domain
    value: CONTACTED_DOMAIN
    reason: contacted_by_url
  - type: hash
    value: DROPPED_FILE
    reason: url_delivers
```

Do not average verdicts: reason about agreement between sources and weight each by its Admiralty rating.

### 5. Automatic follow-up (when warranted)

If the URL is malicious / phishing / suspicious AND the user's request was open-ended ("investigate this URL"), continue with:
- the **Domain Investigation** procedure on the parent domain
- the **IP Investigation** procedure on the resolved IP

Load those skills only if they are attached and the run's skill budget allows it (a run loads at most 8 distinct skills); otherwise run their core lookups directly (`get_vt_domain_report`, `get_vt_ip_report`, `lookup_greynoise_ip`, `check_abuseipdb_ip`, `get_shodan_host_info`) and summarize inline.

Do NOT auto-chain if the verdict is clean / benign or the user's request was narrow ("just scan this URL").

### 6. Apply rigor (inline)

Apply these rules to the output yourself. For a finished intelligence product, also load the **Source Assessment (Admiralty Scale)** and **TLP Guide** skills if they are attached.

**Admiralty rating per source** (reliability A-F, credibility 1-6). Starting points, adjust on content:

| Source | Default | Adjust |
|---|---|---|
| urlscan.io | B2 | C3 if the verdict is ambiguous; live-scan evidence is strong, verdicts can be noisy |
| VirusTotal | B2 | C3 if only 1-2 engines detect |
| abuse.ch URLhaus / ThreatFox | B2 | Check online status and dates |
| CrowdStrike Falcon Intelligence | A2 | B3 if malicious confidence is low / unverified |
| Google TI, Recorded Future | B2 | Read the evidence, not only the score |
| OpenCTI, MISP | B2 | Rate the author behind the entity / event: own analysts A1, national CERT / ISAC A1, reputable vendor A2-B2, open feeds B3-C3, anonymous contributor C3 |

**TLP**: default AMBER for investigative findings; consider **AMBER+STRICT** for active phishing kits to avoid tipping off actors; never exceed the `tlp_ceiling`.

**Confidence (0-100)** on the consolidated verdict: Very High 90-100, High 75-89, Moderate 50-74, Low 25-49, Very Low 0-24. Justify the score with the source ratings and independent corroboration.

**Likelihood words** for uncertain or forward-looking statements: Almost certain (>95%), Highly likely (75-95%), Likely (50-75%), Realistic possibility (25-50%), Unlikely (15-25%), Highly unlikely (5-15%), Remote (<5%).

### 7. Return

```yaml
title: "URL Investigation: [url (defanged)]"
tlp: AMBER | AMBER+STRICT
confidence: SCORE_0_100
summary: 2_3_SENTENCES
verdict: <...>
live_scan: <...>
reputation: <...>
contacted_infrastructure: <...>
internal_context: <...>
community_context: <...>
pivot_candidates: TOP_3_5
related_investigations:
  - parent_domain_investigation: INLINE_SUMMARY
  - resolved_ip_investigation: INLINE_SUMMARY
source_ratings: <...>
sources_skipped: [<...>]
investigation_date: ISO8601_TIMESTAMP
recommended_next_steps: <...>
```

## Special cases

- **Phishing kit URLs** - often target a specific brand; note the brand in `behaviors_observed`. Consider the **Sigma Rule Writing** skill to build a detection, and the **Phishing and Social Engineering** skill for TTP context.
- **Redirector chains** - the final URL matters most. Also capture intermediate domains: they are sometimes attacker-controlled infrastructure reused across campaigns.
- **URL with auth tokens / sensitive query parameters** - scrub before submitting anywhere (`code_interpreter` can strip or replace the parameters). If you cannot scrub, do not scan: note the limitation instead.
- **Already-dead URL** - urlscan may report no response; check urlscan history (`search_urlscan_scans`), archived copies (`web_fetch` on the Wayback Machine availability API, `https://archive.org/wayback/available?url=URL`) and URLhaus; do not assume benign.

## Writing back (only when asked)

Do not write to OpenCTI or MISP unless the user or the assignment asks for it. When asked, create the URL observable / indicator inside an OpenCTI draft (`create_opencti_draft`, `create_opencti_observable`, `create_opencti_indicator`) with the TLP marking; in MISP, add to an unpublished event and never publish without review.

## Related skills

- **IP Investigation**, **Domain Investigation**, **Hash Investigation**
- **Malware Analysis** - if the URL delivers a file, follow up on the hash
- **Phishing and Social Engineering** - knowledge cell for phishing TTPs
- **Sigma Rule Writing** - for detection
- **CTI Orchestrator**
