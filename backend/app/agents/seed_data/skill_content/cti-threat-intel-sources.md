# Threat Intel Sources

One reference guide for every threat-intelligence source an investigation can query from XTM One: which integration answers which question, the exact tools per indicator type, how to read each result (verdict fields, false-positive traps, what "not found" means), the default Admiralty rating of each source, and the quota or privacy caveats that matter.

This guide is about **retrieval**: getting the right data from the right source and reporting it faithfully. Reasoning about the data (assessment, attribution, confidence) belongs to the investigation or analysis skill that called the lookups.

**Tool rule.** Use only the tools you actually have. When an integration is not connected (its tools are not in your tool list), say which source was skipped instead of guessing its result. Never fabricate a detection ratio, score, pulse count or report reference. A tool error (authentication, entitlement, quota) is reported as such, naming the source: it is not "no result".

Integrations and their API keys are configured once by an administrator in **XTM One > Integrations**. Never ask the user to paste an API key in the chat (see "Setting up sources" at the end).

## Which source for which indicator

Plan the parallel batch from this matrix. Core sources are queried first; the complementary sources (detailed further down) add depth when connected.

| Question / indicator | Core sources (tools) | Also useful |
|---|---|---|
| IP reputation | VirusTotal `get_vt_ip_report`, AbuseIPDB `check_abuseipdb_ip`, CrowdStrike `list_crowdstrike_intel_indicators` | GTI, Recorded Future, CrowdSec, Defender TI, abuse.ch |
| IP: scanner noise or targeted? | GreyNoise `lookup_greynoise_ip` | CrowdSec `lookup_crowdsec_ip` |
| IP: exposed services | Shodan `get_shodan_host_info` | VulnCheck, Defender TI |
| Domain reputation | VirusTotal `get_vt_domain_report`, CrowdStrike | GTI, Recorded Future, DomainTools |
| Domain: WHOIS, DNS, hosting history | Shodan `get_shodan_domain_info`, VirusTotal `get_vt_related_objects` | DomainTools, Defender TI passive DNS |
| URL: render and redirects | URLScan `search_urlscan_scans`, `submit_urlscan_scan`, `get_urlscan_result` | ANY.RUN, CrowdStrike sandbox |
| URL reputation | VirusTotal `get_vt_url_report` | GTI, Recorded Future, abuse.ch URLhaus |
| File hash | VirusTotal `get_vt_file_report`, `get_vt_file_behaviour`, CrowdStrike | GTI, abuse.ch MalwareBazaar, ANY.RUN, Recorded Future |
| Already known internally? | OpenCTI `search_opencti`, `list_opencti_observables`; MISP `search_misp_attributes` | ThreatConnect, Sekoia |
| Community / campaign context for an IOC | OpenCTI reports, MISP events | abuse.ch ThreatFox, Feedly, GTI, Recorded Future |
| Infrastructure pivot (cert CN, JARM, favicon, banner) | Shodan `count_shodan_hosts` then `search_shodan_hosts`; URLScan `search_urlscan_scans`, `find_urlscan_similar_scans` | `search_vt_intelligence` (premium), DomainTools Iris |
| Threat actor profile | CrowdStrike `list_crowdstrike_actors`, `get_crowdstrike_actor`, `list_crowdstrike_intel_reports`; OpenCTI | MITRE ATT&CK, GTI, Recorded Future, Intel 471 |
| Ransomware victim / group | Ransomware.live `search_ransomlive_victims`, `get_ransomlive_group` | OpenCTI, VulnCheck, Feedly |
| CVE context | Vulnerability Intelligence `assess_vulnintel_cve`, GreyNoise `lookup_greynoise_cve` | VulnCheck, CrowdSec, Shodan `vuln:` count |

## Rules that apply to every source

- **Parallel and separate.** Issue independent lookups in the same turn. Report each source's answer separately before combining; never merge two sources' numbers into one score. Keep the raw key fields (ratio, confidence, classification, dates) and the query time.
- **"Not found" is not "clean".** An empty result means the source has no record: say "no record in SOURCE" and treat it as absence of evidence. Only explicit benign signals (GreyNoise business service / RIOT, a MISP warninglist hit) count as benign evidence, and they must be named as such.
- **Writing back.** MISP and OpenCTI are two-way. Never create, tag, publish or delete anything during an enrichment run unless the user or the assignment explicitly asks; deletions always need explicit user confirmation.
- **Privacy.** VirusTotal is not private: uploaded files (`scan_vt_file`) and submitted URLs become visible to other users; never upload a confidential document, prefer hash lookups. URLScan submissions stay `unlisted` unless the user explicitly asks for `public` (public scans tip off the actor). Do not submit the user's own infrastructure to an external scanner unless asked. Licensed vendor reporting (CrowdStrike, GTI, Recorded Future, Intel 471, Flashpoint) is usually TLP:AMBER or stricter: cite report IDs, do not paste report bodies into products shared outside the organisation.

Normalised result block for each lookup:

```yaml
source: [virustotal | shodan | abuseipdb | greynoise | urlscan | misp | opencti | ransomware.live | crowdstrike | ...]
indicator: QUERIED_VALUE
type: ip | domain | url | hash | actor | query
query_time: ISO_8601
status: found | not_found | skipped (integration not connected) | error (REASON)
verdict: malicious | suspicious | clean | unknown   # the source's own verdict, not yours
key_fields: {[fields listed per source below]}
admiralty: LETTERDIGIT   # source default, adjusted per the rules below
notes: [false-positive traps, staleness, quota used]
```

### Admiralty defaults at a glance

From the source pack's registry; apply the per-source adjustments and justify any change in one line. Load the **Source Assessment (Admiralty Scale)** skill if it is attached and you need the full scale.

| Source | Default | Main adjustment |
|---|---|---|
| VirusTotal | B2 | 1-2 engines only: C3; 30+ engines and clearly malicious behaviour: A1 |
| Shodan | B2 | `last_update` older than 30 days: C3; spoofed banner: D4 |
| AbuseIPDB | C3 | more than 10 distinct reporters and confidence above 75: B2 |
| GreyNoise | B2 | scanner noise is strong evidence of "not targeted" |
| URLScan | B2 | ambiguous verdict: C3; malicious with credential form or drive-by: A1 |
| MISP | B2 | rate the authoring organisation (CERT / ISAC A1, vendor A2-B2, anonymous C3, own org A1) |
| OpenCTI | B2 | rate the `createdBy` author (own analysts A1, vendor A2-B2, open feeds B3-C3) |
| Ransomware.live | B2 | metadata credibility 2; criminal-written descriptions 3-4 |
| CrowdStrike Falcon Intelligence | A2 | `low` / `unverified`: B3; `high` + named actor + report: A1 |
| AlienVault OTX (no integration) | C3 | known vendor author: B2; single unknown author: D4 |
| Censys (no integration) | A2 | `last_updated` older than 30 days: B3 |
| ReversingLabs (no integration) | A2 | `unknown` / weak `suspicious`: B3; strong malicious evidence: A1 |

The pack did not rate the complementary sources (GTI, Recorded Future, DomainTools, abuse.ch, CrowdSec, Intel 471, Flashpoint ...): rate them with the Source Assessment rules and say the rating is yours, not a default.

---

## VirusTotal

**Integration:** VirusTotal. **Indicators:** IPv4/IPv6, domain, URL, file hash (MD5, SHA-1, SHA-256). Aggregates 70+ antivirus engines and URL scanners, plus community comments, sandbox behaviour and passive DNS.

| Indicator | Lookup | Pivot with `get_vt_related_objects` (relationship names) |
|---|---|---|
| IP | `get_vt_ip_report` | `resolutions`, `communicating_files`, `downloaded_files`, `urls`, `historical_whois`, `historical_ssl_certificates` |
| Domain | `get_vt_domain_report` | `resolutions`, `subdomains`, `siblings`, `communicating_files`, `downloaded_files`, `urls`, `historical_whois`, `historical_ssl_certificates` |
| URL | `get_vt_url_report` (raw URL); never analysed: `scan_vt_url` then `get_vt_analysis` | `last_serving_ip_address`, `downloaded_files`, `redirects_to`, `network_location` |
| File hash | `get_vt_file_report`; sandbox behaviour and ATT&CK techniques: `get_vt_file_behaviour` | `contacted_ips`, `contacted_domains`, `contacted_urls`, `dropped_files`, `bundled_files`, `execution_parents`, `itw_urls` |

Also: `search_vt` (any value, type unknown), `search_vt_intelligence` (premium corpus queries such as `type:peexe positives:10+ fs:2024-01-01+`), `get_vt_comments` / `get_vt_votes` (community view), and for YARA testing (premium) `create_vt_retrohunt_job`, `get_vt_retrohunt_matches`, `list_vt_hunting_notifications`.

**Reading the result.** Key fields: detection ratio (malicious / total engines in the last analysis), community reputation, suggested threat label and tags for files, registrar and creation date for domains, AS owner and country for IPs, final URL for URLs. Many engines = malicious; a few = suspicious; zero detections on an analysed object = "no engine detection", weak evidence of clean, not proof.

False-positive traps: 1-2 detections are often generic or heuristic signatures; old hashes of commonly packed files attract generic detections; shared hosting and CDN IPs inherit detections from unrelated tenants; a domain detection may reflect one bad URL on a large legitimate site. Not found = never submitted; for a targeted sample, an unknown hash can itself be meaningful (not widely distributed).

**Admiralty default: B2** (aggregator; engine quality varies). **C3** when only 1-2 engines detect or for an old hash in a generic packer; **A1** when 30+ engines detect and behaviour analysis shows clearly malicious actions.

**Quota.** Quota-sensitive on the public API (4 requests per minute, 500 per day). Each relationship pivot is a separate request: a report plus four relationships costs five calls. Pull only the relationships the current pivot needs; for batches, one report per indicator and pivot only on confirmed hits.

## Shodan

**Integration:** Shodan. **Indicators:** IPv4/IPv6 (primary), domain (via DNS), search queries. Internet-wide scan data: ports, banners, TLS certificates, detected CVEs, hostnames, organisation.

| Need | Tool |
|---|---|
| Host detail for an IP | `get_shodan_host_info` (`history` for historical banners on paid plans, `minify` for ports only) |
| Domain to IP / reverse DNS | `resolve_shodan_dns` / `reverse_shodan_dns` |
| Subdomains and DNS records | `get_shodan_domain_info` (one query credit) |
| Size a query, free of credits | `count_shodan_hosts` (with `facets`) |
| Hosts matching a fingerprint | `search_shodan_hosts` (`facets` e.g. `country,org`) |
| Filter / facet references, credits | `list_shodan_search_filters`, `list_shodan_search_facets`, `get_shodan_api_info` |

Pivot filters, strongest first (always count before searching): `ssl.jarm:FINGERPRINT` (TLS stack, very strong); `ssl.cert.subject.CN:"badcorp.example"`; `http.favicon.hash:MMH3` (phishing kits, panels); `product:"Cobalt Strike Team Server"` (C2 frameworks); `http.title:"Login Page"` / `http.html:MARKER`; `vuln:CVE-2024-21887`; scoping with `org`, `asn`, `country`, `port`, `hostname`, `tag`, `before` / `after`. Useful facets: `country`, `org`, `port`, `product`, `version`, `asn`, `tag`, `vuln`, `ssl.version`.

**Reading the result.** Key fields: `hostnames`, `org`, `isp`, `country`, `os`, open ports, per-service product / version / banner, `vulns` (paid), `last_update`. Check `last_update` before relying on the service list. A Shodan CVE is inferred from the banner version, not confirmed exploitable. Hundreds of open ports with odd banners usually means a honeypot or tarpit. Not found = no responsive public service in Shodan's scans, not "unused".

**Admiralty default: B2** (empirical; banners can be spoofed or stale). **C3** when `last_update` is older than 30 days; **D4** for an obviously spoofed banner (impossible service / version combination); **A1** for a fresh scan (under 7 days) of known-vulnerable software with a confirmed CVE.

**Quota.** Host lookups and DNS resolution cost no query credits; filtered searches, pages past the first and domain info do. Scope with `count_shodan_hosts` and check `get_shodan_api_info` before a search-heavy session.

## AbuseIPDB

**Integration:** AbuseIPDB. **Indicators:** IPv4 and IPv6 only (CIDR for range checks). Community reports of abusive IPs.

| Need | Tool |
|---|---|
| IP reputation (default 90-day window) | `check_abuseipdb_ip` (`max_age_in_days`, `verbose` for individual reports) |
| Paginated report history | `list_abuseipdb_ip_reports` |
| A whole range (/24, hosting block) | `check_abuseipdb_network` |
| Bulk high-confidence block list | `get_abuseipdb_blacklist` |
| Category names | `list_abuseipdb_categories` |

**Reading the result.** Key fields: abuse confidence (0-100), total reports, distinct reporters, last reported, ISP, usage type (Data Center, ISP ...), country, domain, Tor flag, report categories. Confidence bands: 0 no reports; 1-25 low; 26-50 moderate; 51-75 high; 76-100 very high, widely reported.

Categories: 1 DNS Compromise, 2 DNS Poisoning, 3 Fraud Orders, 4 DDoS, 5 FTP Brute-Force, 6 Ping of Death, 7 Phishing, 8 Fraud VoIP, 9 Open Proxy, 10 Web Spam, 11 Email Spam, 12 Blog Spam, 13 VPN IP, 14 Port Scan, 15 Hacking, 16 SQL Injection, 17 Spoofing, 18 Brute-Force, 19 Bad Web Bot, 20 Exploited Host, 21 Web App Attack, 22 SSH, 23 IoT Targeted.

Traps: a low score does NOT mean clean, only not widely reported; Tor exits, VPN egress and shared cloud IPs collect reports about unrelated users; "Port Scan"-only reports usually mean opportunistic scanning (cross-check GreyNoise). Confidence below 15 is no signal, not negative evidence.

**Admiralty default: C3** (community-reported). **B2** when distinct reporters exceed 10 and confidence exceeds 75, or for consistent recent reports across several categories; **D4** for a single reporter with a generic category.

**Quota.** Quota-sensitive (1,000 checks per day on the free tier). The write tools (`report_abuseipdb_ip`, `bulk_report_abuseipdb_ips`, `clear_abuseipdb_address`) are never used during an investigation unless the user explicitly asks.

## GreyNoise

**Integration:** GreyNoise. **Indicators:** IPs, GNQL queries, CVEs. Answers "is this IP scanning the whole internet, or targeting us specifically?"

| Need | Tool |
|---|---|
| Full context for one IP | `lookup_greynoise_ip` (`quick` for a one-line verdict) |
| Free community tier | `lookup_greynoise_community_ip` |
| Batch (up to 1000 IPs) | `lookup_greynoise_ips` |
| Activity over up to 90 days | `get_greynoise_ip_timeline` |
| Scanners by characteristic | `query_greynoise_gnql`; bucket counts `get_greynoise_gnql_stats` |
| Exploitation activity for a CVE | `lookup_greynoise_cve`, `list_greynoise_cves` |

GNQL fields: `classification`, `tags:"Mirai"`, `actor`, `metadata.country`, `metadata.asn`, `metadata.organization`, `metadata.category`, `raw_data.scan.port`, `last_seen:1d|7d|30d`, `first_seen`, `vpn:true`, `spoofable:false`; combine with AND / OR / NOT and parentheses, e.g. `classification:malicious raw_data.scan.port:22 metadata.country:RU last_seen:1d`.

**Reading the result.** The full lookup returns internet scanner intelligence (classification, actor, tags, CVEs, ASN / org / geography, spoofable / bot / VPN / Tor flags, first / last seen) and business service intelligence (formerly RIOT: known-good providers with a trust level). The community tier returns `noise`, `riot`, `classification`, `name`, `last_seen`, `message`.

| Result | Meaning | Action |
|---|---|---|
| benign + scanning | Known benign scanner (research, search engines) | Likely false positive, deprioritise |
| malicious + scanning | Known malicious mass scanner | Opportunistic, not targeted; worth blocking |
| unknown + scanning | Unclassified scanner | Investigate further |
| not scanning, not a business service | Not a known scanner | May be targeted, higher priority |
| business service / RIOT | Known benign service (CDN, DNS, cloud) | Deprioritise; detections against it are likely false positives |

Not found = never seen in background scanning; it does NOT mean clean (a targeted attacker's IP usually has no record). `spoofable: true` means the source address could be forged (no completed handshake).

**Admiralty default: B2** (empirical honeypot observations, strong data hygiene).

**Quota.** The community tier is heavily rate-limited; context, GNQL, timeline and batch need a paid key. On an entitlement error, fall back to `lookup_greynoise_community_ip` and say so.

## URLScan.io

**Integration:** urlscan. **Indicators:** URL, domain, IP, resource hash. Loads a page in an instrumented browser: redirects, contacted domains and IPs, resources, certificates, screenshot, verdicts.

| Need | Tool |
|---|---|
| Existing scans (search first; reuse one under 24 h old) | `search_urlscan_scans` |
| Fresh scan | `submit_urlscan_scan` (`visibility` defaults to `unlisted`; `country`, `custom_user_agent`, `referer`, `tags`) |
| Result / rendered HTML | `get_urlscan_result` / `get_urlscan_dom` (scan UUID) |
| Same kit on other domains (Pro) | `find_urlscan_similar_scans` |
| Remaining quota | `get_urlscan_quotas` |

Scans are asynchronous: wait about 30 seconds after submitting, then retry `get_urlscan_result` if not ready, at most 3 times about 30 seconds apart; still not ready, report the scan id as pending. Search fields: `page.domain`, `page.url`, `page.ip`, `page.country`, `page.asn`, `task.tags`, `task.url` (requested, before redirects), `verdicts.overall.malicious:true`, `hash:SHA256` (favicon or JS resource: strong fingerprint), `filename`, `date:>now-30d`; AND / OR / NOT, wildcards. Examples: `page.domain:malicious.example AND date:>now-30d`; `task.tags:phishing AND page.country:RU`.

**Reading the result.** Key fields: overall verdict, final URL, resolved IP and country, server, contacted domains and IPs, technologies, screenshot, result page. A benign verdict on a phishing page is common when the kit cloaks (geofencing, user-agent checks, one-time links): re-scan with another `country` or user agent before concluding. Not found in search = nobody scanned it visibly, not safe.

**Admiralty default: B2.** **C3** for an ambiguous verdict or edge case (obfuscated JavaScript, short session); **A1** for a malicious verdict with clearly malicious behaviour (credential form, drive-by download).

**Quota and privacy.** Free tier: about 100 scans per day. Submit `unlisted` unless the user opts in to `public`; an unlisted result page is still reachable by anyone with its link; `private` needs Pro.

## MISP

**Integration:** MISP. **Indicators:** any attribute type (ip-dst, domain, url, sha256, email ...), events, objects, galaxies. A curated sharing platform, not a scanning service.

| Need | Tool |
|---|---|
| Is this value known? | `search_misp_attributes` (value = indicator; `enforce_warninglist` drops known-benign hits) |
| Events by actor, tag, date range | `search_misp_events`; full event `get_misp_event`; objects `search_misp_objects` |
| Known-benign check before acting | `check_misp_warninglists` |
| Sightings, tags, galaxies | `list_misp_sightings`, `list_misp_tags`, `list_misp_galaxy_clusters` |
| Write (only when asked) | `create_misp_event`, `add_misp_attribute`, `add_misp_tag`, `add_misp_sighting`, `publish_misp_event` |

**Reading the result.** Note event id, title (`info`), creating organisation (`Orgc`), date, published flag, threat level, analysis state, tags (TLP, kill chain, galaxies) and per attribute `to_ids` and sightings. A warninglist hit means likely benign infrastructure (public resolvers, CDN ranges, top domains). Not found = your communities have not shared it; it says nothing about maliciousness. Prefer explicit rating tags when present (`admiralty-scale:source-reliability="b"`, `admiralty-scale:information-credibility="2"`); otherwise judge on: published, tagged or sighted by other organisations, `to_ids` set, primary report cited.

**Admiralty default: B2**, but rate the organisation behind the event: sector CSIRT or national CERT (CIRCL, FIRST, ISAC) A, A1 for their primary reporting; reputable vendor A2 / B2; community open instance B-C; anonymous contributor C3; your own organisation A1.

**Writing back (only when asked).** Distribution is sticky: a published event synced to connected servers cannot be recalled. 0 your organisation only (drafting, unverified, sensitive); 1 this community (reviewed intel); 2 connected communities; 3 all communities; 4 sharing group (named partners); 5 inherit event. Default to 0 while drafting and never publish unreviewed intel. Apply TLP, kill-chain and galaxy tags: they drive routing to subscribers. Threat level 1 High, 2 Medium, 3 Low, 4 Undefined; analysis 0 Initial, 1 Ongoing, 2 Completed.

**Quota.** No public limit; on shared community servers use structured filters rather than fetching and filtering client-side.

## OpenCTI

**Integration:** OpenCTI. **Indicators:** every STIX 2.1 entity and observable. Your internal knowledge graph and the first place to check whether an indicator or actor is already known (MISP is usually the community exchange layer; many teams sync both).

| Need | Tool |
|---|---|
| Is this IOC known? | `list_opencti_observables` (search = value, type filter) and `search_opencti` (also catches indicator patterns) |
| Search / list / filter entities | `search_opencti` (`types`), `list_opencti_entities` |
| Detail, relationships, history | `get_opencti_entity`, `list_opencti_relationships`, `get_opencti_top_related`, `get_opencti_entity_history` |
| Full report or other container | `get_opencti_container_full` |
| PIRs / connector health | `manage_opencti_pir`, `query_opencti_pir_entities` / `manage_opencti_connector` (action `list`) |
| Write (only when asked) | `create_opencti_indicator`, `create_opencti_observable`, `promote_opencti_observable`, `create_opencti_relationship`, `add_opencti_label`, `add_opencti_marking`, `update_opencti_field`, `create_opencti_note` |

**Indicators versus observables.** An observable is a fact ("this IP appears in our data"); an indicator is an assertion ("this pattern is malicious") with a STIX pattern, score and validity window. Check both and report which matched. When writing, create an indicator for anything that should feed detections and keep the observable linked (`promote_opencti_observable` creates it from an observable with a `based-on` relationship). Deleting one does not cascade to the other.

**Reading the result.** Note `createdBy`, `confidence` (0-100, maps to the confidence bands), `x_opencti_score`, labels, markings, dates, linked reports, intrusion sets and malware. Not found = not yet ingested or created by your organisation (new to you), not benign.

**Admiralty default: B2**, but rate the `createdBy` author: own analysts A1; vendor connector A2 / B2; open feeds (abuse.ch, OTX, MISP feeds) B3 / C3. Use the entity's `confidence` for credibility and count independent authors on related entities.

**Writing back (only when asked).** Mutations go into a draft for human review (`create_opencti_draft`, `use_opencti_draft`; an autonomous run gets one automatically). An integration whose administrator turned Live writes on writes straight to the graph and also offers `apply_opencti_draft` (validate a draft) and `create_opencti_label`; without it those tools are absent, so never promise them. `list_opencti_labels` reads the labels to reuse, `delete_opencti_draft` drops a draft created in this run. Do not inflate scores: they drive decay and detection exports, 50 is neutral. Reuse existing labels before inventing new ones. For a full STIX 2.1 bundle, load the built-in **OpenCTI STIX Bundle Construction** skill, validate with `stix_bundle_validator`, and attach it to an entity with `upload_opencti_file`. Deletions are permanent: confirm first.

## Ransomware.live

**Integration:** Ransomware.live. **Indicators:** organisation name, domain, group, sector, country. Leak-site scrapes across hundreds of groups.

| Need | Tool |
|---|---|
| Has this organisation been claimed? | `search_ransomlive_victims` (`query`; `group`, `sector`, `country`, `year`, `month`, `order`, `limit`) |
| Latest claims / one victim | `list_ransomlive_recent_victims` / `get_ransomlive_victim` |
| Groups by victim count / group profile (description, TTPs, tools, exploited CVEs, leak-site URLs, first / last seen) | `list_ransomlive_groups` / `get_ransomlive_group` |
| Group IOCs and YARA rules | `get_ransomlive_iocs`, `get_ransomlive_yara` |
| Ransom notes, negotiations | `list_ransomlive_ransomnotes`, `get_ransomlive_ransomnote`, `list_ransomlive_negotiations`, `get_ransomlive_negotiation_chat` |
| CSIRT contacts, sectors, stats, press, 8-K filings | `get_ransomlive_csirt_contacts`, `list_ransomlive_sectors`, `get_ransomlive_stats`, `list_ransomlive_press`, `list_ransomlive_8k_filings` |

Pivots: from a domain or IP investigation, search the owner's organisation name; for a briefing, filter by `country` (ISO 3166 alpha-2) or `sector` (values from `list_ransomlive_sectors`); for a group profile, chain IOCs, YARA and negotiations; for detection engineering, start from `get_ransomlive_yara`.

**Reading the result.** Victim fields: name, group, country, sector, discovered (scraped) and published (posted) dates, description, website, post URL (often .onion), screenshot. The group can be missing from recent-victim listings; it populates in search and victim detail. A leak-site claim is a **claim**, not a confirmed breach: victims dispute or never acknowledge, and some posts repackage older breaches under a new brand. Not found = no claim on a tracked leak site, not "not breached". Group TTPs are the general repertoire, not a per-victim chain, and the source says nothing about a victim's initial-access vector (load the **Initial Access Brokers** skill if attached).

**Admiralty default: B2.** Credibility per field: victim name, country, sector, dates, group attribution 2 (corroborated by the post itself); breach description 3-4 (criminal-written extortion copy that overstates volume, sensitivity and access); YARA rules and IOCs 2 (public researchers; cross-check before deploying).

**Handling.** Results are full sets: narrow with filters. Descriptions can contain victim PII or stolen credentials: quote selectively, never paste full responses into shared documents.

## CrowdStrike Falcon Intelligence

**Integration:** CrowdStrike. **Indicators:** IP, domain, URL, hash, email and other IOC types; actors; reports; sandbox submissions. Vendor finished intelligence; needs a Falcon Intelligence subscription with the Intel read scopes.

| Need | Tool |
|---|---|
| IOC reputation | `list_crowdstrike_intel_indicators` with `filter_query` `indicator:'VALUE'` |
| Latest malicious IOCs | same tool, `filter_query` e.g. `malicious_confidence:'high'` (+ `type`, `actors`, `malware_families`, `published_date`) |
| Named adversary | `list_crowdstrike_actors` (`query`) then `get_crowdstrike_actor` |
| Actors by origin / target / motivation | `list_crowdstrike_actors` with `filter_query` |
| Finished reports / PDF | `list_crowdstrike_intel_reports` / `download_crowdstrike_intel_report` |
| Sandbox detonation | `submit_crowdstrike_sandbox_analysis`, then `get_crowdstrike_sandbox_report` |

Falcon Query Language: equality `malicious_confidence:'high'`, `name:'FANCY BEAR'` (exact); country slugs are ISO alpha-2 codes, not names (`origins.slug:'ru'`, `'cn'`, `'ir'`, `'kp'`, `target_countries.slug:'us'`); other arrays use word slugs (`target_industries.slug:'financial-services'`, `motivations.slug:'state-sponsored'`, `actors.slug:'mustang-panda'`); dates `created_date:>'2025-01-01'`; `+` is AND, `,` is OR. Sorting actors by `name` silently returns nothing: sort by `last_activity_date`. Indicator types include `ip_address`, `domain`, `url`, `hash_md5`, `hash_sha1`, `hash_sha256`, `email_address`, `file_name`, `mutex_name`, `registry`, `x509_serial`.

**Reading the result.** Indicator: `malicious_confidence` (high / medium / low / unverified), `actors`, `malware_families`, `kill_chains`, `threat_types`, `reports`, `labels`, `published_date`, `last_updated`. Actor: `known_as`, `origins`, `target_countries`, `target_industries`, `motivations`, `capability`, `actor_type`, first / last activity. Report: title, `created_date`, actors, targets, `tags`, `report_type`, `url`. A 403 "scope not permitted" on indicators while actors and reports work is an entitlement error, not "not found". Not found = not published by CrowdStrike, common for targeted or fresh indicators. Do not infer attribution from an actor tag alone: read the report.

For an actor's ATT&CK techniques, the integration does not expose CrowdStrike's technique mapping: use MITRE ATT&CK (`search_mitre_attack`, `get_mitre_attack_related`), GTI `get_gti_threat_mitre_tree` or the intrusion set's attack patterns in OpenCTI (`list_opencti_relationships`).

**Admiralty default: A2.** **B3** when confidence is `low` / `unverified`, the actor record is sparse or provisional, or the judgement rests on one hedged report; **A1** when confidence is `high`, the IOC is linked to a named actor or family, and a finished report corroborates.

**Handling.** Per-tenant rate limit: back off when refused. Report content is licensed: cite IDs, do not redistribute bodies.

---

## Complementary sources in XTM One

Not part of the original pack, but they answer the same questions; use them when connected.

| Integration | Best for | Tools |
|---|---|---|
| abuse.ch | C2 IOCs (ThreatFox), malware URLs (URLhaus), samples (MalwareBazaar) | `search_abusech_iocs`, `lookup_abusech_host`, `lookup_abusech_url`, `lookup_abusech_malware_hash` |
| Google Threat Intelligence | IOC verdicts and threat scores, threat profiles, ATT&CK trees | `get_gti_ip_assessment`, `get_gti_domain_assessment`, `get_gti_url_assessment`, `get_gti_file_assessment`, `search_gti_threats`, `get_gti_threat_mitre_tree` |
| Recorded Future | Risk scores with triggered rules, analyst notes, actors | `lookup_rf_ip`, `lookup_rf_domain`, `lookup_rf_url`, `lookup_rf_hash`, `enrich_rf_indicators`, `search_rf_analyst_notes`, `search_rf_threat_actors` |
| DomainTools | WHOIS and history, hosting history, reverse lookups, Iris pivots, risk | `get_domaintools_whois`, `get_domaintools_whois_history`, `get_domaintools_hosting_history`, `search_domaintools_iris`, `search_domaintools_reverse_ip`, `get_domaintools_risk_score` |
| Defender Threat Intelligence | Host reputation, passive DNS | `get_defender_ti_host_reputation`, `get_defender_ti_host`, `get_defender_ti_passive_dns` |
| CrowdSec | Crowd-sourced attack behaviour per IP, CVE exploitation | `lookup_crowdsec_ip`, `get_crowdsec_cve` |
| ANY.RUN | Sandbox, TI lookup over past analyses | `lookup_anyrun_ti`, `submit_anyrun_file_analysis`, `submit_anyrun_url_analysis`, `get_anyrun_analysis_report` |
| Vulnerability Intelligence / VulnCheck | NVD, EPSS, KEV, exploits, initial-access telemetry | `assess_vulnintel_cve`, `check_vulnintel_kev`, `lookup_vulncheck_cve`, `get_vulncheck_exploits`, `lookup_vulncheck_ip_intel` |
| Feedly | Open-source reporting and IOCs in articles | `search_feedly_articles`, `lookup_feedly_entity`, `get_feedly_stream_iocs` |
| Intel 471, Flashpoint, Cybersixgill, SOCRadar | Underground context for an IOC or actor | `search_intel471_iocs`, `lookup_flashpoint_ioc`, `enrich_cybersixgill_ioc`, `analyze_socradar_indicator` |
| MITRE ATT&CK | Technique, group and software reference (no local dataset) | `search_mitre_attack`, `get_mitre_attack_object`, `get_mitre_attack_related` |

Open-web corroboration: `search_google_web`, `web_fetch`. Parsing, defanging, deduplicating or scoring a batch: `code_interpreter`.

---

## Sources with no XTM One integration yet

The original pack also queried three services that XTM One has no integration for. Do not invent tools for them. If the user pastes their output, read it with the notes below; otherwise use the alternatives.

### AlienVault OTX

**Used for:** community "pulses" (IOC and TTP collections tied to a campaign, family or actor) referencing an IP, domain, hash or URL: pulse count, key pulses with tags, TLP and author, related indicators, passive DNS, WHOIS; keyword search for pulses.

**Reading pasted output.** A high pulse count means widely reported: corroboration, not confirmation. Quality varies dramatically: check the author and the references; pulses without references, or with IOC lists that look scraped, are weak.

**Admiralty default: C3.** **B2** when the author is a known reliable organisation (major vendor research teams, abuse.ch); **B3** when three or more independent pulses reference the indicator; **D4** for a single pulse from an unknown author; **E5** for no references and scraped-looking IOCs.

**Alternatives.** OTX content often reaches OpenCTI or MISP through feed connectors: check `search_opencti` and `search_misp_attributes` first (rate OTX-fed entities B3-C3). Community campaign context: abuse.ch ThreatFox (`search_abusech_iocs`), Feedly (`search_feedly_articles`), GTI (`search_gti_threats`), Recorded Future (`search_rf_analyst_notes`). Passive DNS: VirusTotal `resolutions`, DomainTools, Defender TI `get_defender_ti_passive_dns`.

### Censys

**Used for:** high-fidelity host and TLS-certificate reconnaissance: services and banners, certificate chains, ASN and location, host timeline and past-date views, certificate lookup by SHA-256, search by certificate CN, JARM, body hash or labels. Preferred over Shodan for certificate pivoting.

**Reading pasted output.** Key fields: `services[]` (port, service name, transport, banner), `services[].tls.certificates` (subject CN, issuer, validity, fingerprint), `autonomous_system`, `location`, `operating_system`, `last_updated_at`. Search syntax: `services.tls.certificates.leaf_data.subject.common_name: DOMAIN`, `services.jarm.fingerprint: JARM`, `services.http.response.body_hash: HASH`, `labels: LABEL`.

**Admiralty default: A2** (authoritative scan dataset). **B3** when `last_updated` is older than 30 days.

**Alternatives.** Shodan for services and banners (`get_shodan_host_info`) and pivots on `ssl.cert.subject.CN`, `ssl.jarm`, `http.favicon.hash` (`count_shodan_hosts`, `search_shodan_hosts`; weaker certificate data than Censys). Complement with VirusTotal `historical_ssl_certificates`, urlscan hash and domain searches, DomainTools Iris and GTI assessments. Certificate-transparency search remains a gap.

### ReversingLabs (Spectra Analyze / A1000)

**Used for:** authoritative file classification (malicious / suspicious / goodware / unknown) with a 0-10 risk score and threat name, AV multi-scan summary, static decomposition, sandbox behaviour with ATT&CK mapping, parent container and extracted-file relationships, sibling-sample search by threat name, imphash or fuzzy hash, YARA ruleset matches, and network reputation for URLs, domains and IPs.

**Reading pasted output.** Key fields: `classification`, `riskscore` (0-10), `classification_result` (threat name, e.g. `Win32.Ransomware.Lockbit`), `av_scanners_summary` (count, matches, percent), `tags`, `ticore` (static analysis), `attack` (ATT&CK tactics and techniques), `rl_cloud_sandbox` (dynamic analysis), `networkthreatintelligence`, `extracted_file_count`.

**Admiralty default: A2.** **B3** when the classification is `unknown`, when it is `suspicious` with fewer than 3 scanner matches, or when the sample is under 24 hours old and the sandbox output is empty; **A1** when `malicious` with 5+ scanner matches, behavioural sandbox evidence and a threat name mapping to a publicly reported family.

**Alternatives.** Reputation and threat label: VirusTotal (`get_vt_file_report`, `get_vt_file_behaviour`), GTI (`get_gti_file_assessment`), abuse.ch MalwareBazaar (`lookup_abusech_malware_hash`). Detonation: ANY.RUN (`submit_anyrun_file_analysis`) or CrowdStrike sandbox (`submit_crowdstrike_sandbox_analysis`). Parent / child: VirusTotal `execution_parents`, `bundled_files`, `dropped_files`. Sibling samples: `search_vt_intelligence` (premium). YARA matches: `create_vt_retrohunt_job` or ANY.RUN `search_anyrun_yara`.

---

## Setting up sources

- An administrator connects each integration once in **XTM One > Integrations**; the agent sees its tools only when it is connected and enabled for the agent. No environment variables, local files or command-line wrappers are involved.
- If the user pastes an API key in the chat, do not echo it, do not store it in memory or a knowledge base, and advise rotating it at the provider and having an administrator configure the integration.
- A useful starter set for IP, domain, URL and hash work: VirusTotal, URLScan, AbuseIPDB and abuse.ch; add Shodan and GreyNoise for IP-heavy work, OpenCTI and MISP for internal correlation, and licensed vendor feeds (CrowdStrike, GTI, Recorded Future) when subscribed.
- When a source misbehaves, check `get_greynoise_status`, `get_urlscan_quotas`, `get_shodan_api_info`, `get_ransomlive_status` or `get_misp_version`.

Related skills, when attached: **IP Investigation**, **Domain Investigation**, **Hash Investigation**, **URL Investigation**, **IOC Enrichment Workflow**, **Indicator Pivoting**, **Source Assessment (Admiralty Scale)**.
