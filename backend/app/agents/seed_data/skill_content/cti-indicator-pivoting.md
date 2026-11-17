# Indicator Pivoting

Pivoting is the art of using one known indicator to discover related infrastructure, expanding a single seed into an understanding of an adversary's wider operations. Each pivot is a directed-graph walk: the node is an indicator (typed), the edge is a relation derived from a lookup, and the cost is one or more tool calls plus the analyst time to interpret the result.

This skill is the canonical "how to walk the IOC graph" reference. The four investigation skills (**IP Investigation**, **Domain Investigation**, **Hash Investigation**, **URL Investigation**) chain a fixed set of lookups for a known seed type; this skill takes over when you need to keep walking past their first-hop output.

## Tools and integrations

Every pivot below is expressed as an XTM One integration tool call. The integrations involved are VirusTotal, Shodan, urlscan.io, AbuseIPDB, GreyNoise, MISP, OpenCTI, Ransomware.live and CrowdStrike, completed where they help by DomainTools (WHOIS, reverse-WHOIS, passive DNS, hosting history), Google Threat Intelligence (GTI), Recorded Future, abuse.ch (ThreatFox, URLhaus, MalwareBazaar), Microsoft Defender Threat Intelligence and Feedly.

**Rules for tool use:**

- Use only the tools you actually have. When an integration is not connected, say which source was skipped instead of guessing its result, and lower the corroboration (fewer sources = lower confidence).
- Three sources used by the original method have no XTM One integration: **AlienVault OTX**, **Censys** and **ReversingLabs**. Each pivot that relied on them names the closest available alternative below. Never claim an OTX pulse, a Censys result or a ReversingLabs verdict you did not get from a tool.
- Several pivots are quota-sensitive (VirusTotal premium relationships and Intelligence search, Shodan query credits, urlscan Pro structure search, DomainTools). Plan the pivot before paying the call.
- Defang indicators in prose (`login-acme[.]net`, `198.51.100[.]42`) but pass the refanged value to tools.

## When to use this skill

- The user has a single seed IOC and asks "what else is related to this?"
- An investigation skill returned pivot candidates and you need to walk further than its routing did
- You suspect a campaign cluster (multiple indicators in incident telemetry) and want to confirm common infrastructure
- A threat-actor profile mentions an indicator and you want to see the wider footprint
- You need to clearly document a pivot chain so a reviewer can trace your reasoning end-to-end

## Pivot decision trees, with the tool calls to make

For each starting indicator type: a tree of pivots, then the concrete calls. Calls are written as `tool(argument=value)`; argument names are indicative, follow each tool's own schema.

### Starting from an IP address

```
IP address
├── Reputation (cheap, run in parallel first)
│   ├── VirusTotal: get_vt_ip_report
│   ├── AbuseIPDB: check_abuseipdb_ip (abuse score + categories), list_abuseipdb_ip_reports
│   └── GreyNoise: lookup_greynoise_ip (noise/scanner classification - filters out internet-wide scanners)
├── Service fingerprint
│   ├── Shodan: get_shodan_host_info          -> ports, banners, products, vulns, hostnames
│   └── (Censys host view: no integration)    -> GTI get_gti_ip_assessment, urlscan search_urlscan_scans "ip:IP"
├── DNS pivots
│   ├── Shodan: reverse_shodan_dns            -> reverse-DNS hostname(s)
│   ├── VirusTotal: get_vt_related_objects(relationship="resolutions")  -> passive-DNS history
│   ├── DomainTools: search_domaintools_reverse_ip  -> domains hosted on the IP
│   ├── Defender TI: get_defender_ti_passive_dns    -> passive DNS (when connected)
│   └── (OTX community domains: no integration) -> search_abusech_iocs, search_misp_attributes, search_opencti
├── Cert pivots
│   ├── Shodan: get_shodan_host_info          -> certificate fingerprint in the TLS banner
│   └── VirusTotal: get_vt_related_objects(relationship="historical_ssl_certificates")
│       -> certificate fingerprint(s) -> pivot to other hosts (see "Starting from a TLS certificate")
├── JARM / TLS-fingerprint pivots
│   └── Shodan: search_shodan_hosts('ssl.jarm:"JARM"')  -> other hosts with the same JARM (often a C2 family)
├── Range pivots (use sparingly - high false-positive risk)
│   └── Shodan: search_shodan_hosts('net:CIDR') or count_shodan_hosts first  -> neighbours in the same /24
└── Malware pivots
    └── VirusTotal: get_vt_related_objects(relationship="communicating_files" | "downloaded_files")
```

**Concrete calls:**

```
# Reputation triple
get_vt_ip_report(ip="185.220.101.45")
check_abuseipdb_ip(ip="185.220.101.45", verbose=true)
lookup_greynoise_ip(ip="185.220.101.45")

# Service fingerprint (with banner history)
get_shodan_host_info(ip="185.220.101.45", history=true)
get_gti_ip_assessment(ip="185.220.101.45")            # closest substitute for a Censys host view

# Reverse DNS, passive DNS + malware relationships
reverse_shodan_dns(ips="185.220.101.45")
get_vt_related_objects(entity_type="ip", entity_id="185.220.101.45", relationship="resolutions")
get_vt_related_objects(entity_type="ip", entity_id="185.220.101.45", relationship="communicating_files")
search_domaintools_reverse_ip(ip="185.220.101.45")

# Community / internal context (replaces the OTX lookup)
search_abusech_iocs(query="185.220.101.45")
search_misp_attributes(value="185.220.101.45")
search_opencti(search="185.220.101.45")

# JARM pivot to find C2 siblings
count_shodan_hosts(query='ssl.jarm:"3fd21b20d00000021c43d21b21b43d41226ab2434c2cd1a5e1b3c2bc5f4f4e7"')
search_shodan_hosts(query='ssl.jarm:"3fd21b20d00000021c43d21b21b43d41226ab2434c2cd1a5e1b3c2bc5f4f4e7"')
```

Run `count_shodan_hosts` before `search_shodan_hosts` on any broad query: if the count is in the thousands, the fingerprint is a default-config tool or a CDN and the pivot is noise.

### Starting from a domain

```
Domain
├── Reputation (parallel)
│   ├── VirusTotal: get_vt_domain_report      -> detection ratio + categories + WHOIS-ish
│   ├── DomainTools: get_domaintools_risk_score / get_domaintools_reputation (when connected)
│   ├── (OTX pulses + passive DNS: no integration) -> search_abusech_iocs, lookup_abusech_host, search_opencti, search_misp_attributes
│   └── urlscan: search_urlscan_scans("domain:D")   -> existing scans (do not re-submit by default)
├── DNS resolution (always)
│   ├── Shodan: resolve_shodan_dns            -> current A/AAAA records
│   ├── VirusTotal: get_vt_related_objects(relationship="resolutions")  -> historical
│   ├── DomainTools: get_domaintools_hosting_history  -> IP / registrar / name-server history
│   └── pivot the resolved IPs as IPs (above)
├── Subdomain enumeration
│   ├── Shodan: get_shodan_domain_info        -> subdomains Shodan knows about
│   ├── VirusTotal: get_vt_related_objects(relationship="subdomains" | "siblings")
│   └── (Censys CT-derived names: no integration) -> VT historical_ssl_certificates, urlscan search
├── Certificate transparency
│   ├── VirusTotal: get_vt_related_objects(relationship="historical_ssl_certificates")
│   │   -> cert SAN list -> pivot each SAN sibling as a domain
│   └── Shodan: search_shodan_hosts('ssl.cert.subject.cn:"D"')  -> other hosts presenting a cert for this name
├── WHOIS / registrant
│   ├── VirusTotal: get_vt_domain_report      -> registrar, creation_date, registrant email if not redacted
│   ├── VirusTotal: get_vt_related_objects(relationship="historical_whois")
│   ├── DomainTools: get_domaintools_parsed_whois, get_domaintools_whois_history
│   └── registrant email / org -> DomainTools search_domaintools_reverse_whois or search_domaintools_iris
├── Ransomware-claim sweep
│   └── Ransomware.live: search_ransomlive_victims(ORG_CANDIDATE)
│       -> derive the org candidate by stripping the public suffix and "www."; see the Domain Investigation skill for caveats
└── Content
    └── urlscan: get_urlscan_result / get_urlscan_dom on the latest scan  -> screenshot, DOM, page tech
```

**Concrete calls:**

```
# Reputation + content
get_vt_domain_report(domain="evil.example.com")
search_urlscan_scans(query="domain:evil.example.com")
search_abusech_iocs(query="evil.example.com")

# DNS now + historical
resolve_shodan_dns(hostnames="evil.example.com")
get_vt_related_objects(entity_type="domain", entity_id="evil.example.com", relationship="resolutions")
get_domaintools_hosting_history(domain="evil.example.com")

# Certificate pivot (replaces the paid Censys CN search)
get_vt_related_objects(entity_type="domain", entity_id="evil.example.com", relationship="historical_ssl_certificates")
search_shodan_hosts(query='ssl.cert.subject.cn:"evil.example.com"')

# WHOIS / reverse WHOIS
get_domaintools_parsed_whois(domain="evil.example.com")
search_domaintools_reverse_whois(terms="registrant@example.net")

# Ransomware-claim sweep
search_ransomlive_victims(query="example")
```

### Starting from a URL

```
URL
├── Live scan (when a fresh capture is wanted)
│   └── urlscan: submit_urlscan_scan(URL) then get_urlscan_result  -> DOM, network log, screenshot, verdict
├── Existing scans
│   └── urlscan: search_urlscan_scans('page.url:"URL"')  -> past captures (faster, no re-submit)
├── Reputation
│   ├── VirusTotal: get_vt_url_report         -> detection ratio + redirect chain
│   └── abuse.ch URLhaus: lookup_abusech_url  -> malware-distribution history
├── Hostname pivot
│   └── extract the hostname -> pivot as a domain (above)
└── Path / kit fingerprint
    ├── urlscan: search_urlscan_scans('page.url:"DISTINCTIVE_PATH"')  -> other URLs with the same kit path
    └── urlscan Pro: find_urlscan_similar_scans(SCAN_ID)  -> pages built from the same template on other domains
```

**Concrete calls:**

```
submit_urlscan_scan(url="https://login-acme.net/secure/portal", tags=["phishing"])   # only if no recent capture exists
search_urlscan_scans(query='page.url:"/secure/portal/"')
get_vt_url_report(url="https://login-acme.net/secure/portal")
find_urlscan_similar_scans(scan_id="[uuid of the confirmed phishing scan]")
```

A urlscan submission is visible to other urlscan users unless its visibility is set to private or unlisted. Ask before submitting a URL that may contain a victim identifier, a token or internal hostnames.

### Starting from a file hash

```
Hash (md5 / sha1 / sha256)
├── Reputation + family
│   ├── VirusTotal: get_vt_file_report        -> detection stats, family tags, sandbox verdicts
│   ├── abuse.ch MalwareBazaar: lookup_abusech_malware_hash  -> family tag, first seen, delivery
│   ├── GTI: get_gti_file_assessment          -> verdict, attribution to threats / campaigns
│   └── (OTX community pulses: no integration) -> search_abusech_iocs, search_opencti, Feedly search_feedly_articles
├── C2 extraction (highest-value pivot)
│   └── VirusTotal: get_vt_related_objects(relationship="contacted_ips" | "contacted_domains" | "contacted_urls")
│       -> pivot each contacted indicator
├── Family lineage
│   └── VirusTotal: get_vt_related_objects(relationship="dropped_files" | "execution_parents" | "bundled_files")
│       -> walk the dropper / payload chain; "similar_files" requires a premium key
├── Submitter / source
│   └── VirusTotal: get_vt_related_objects(relationship="submissions")   (premium)
├── Compilation artefacts
│   └── VirusTotal: get_vt_file_report        -> PDB path, compiler, packers in the response
│       -> distinctive PDB path -> search_vt_intelligence  (premium)
├── Vendor attribution
│   └── CrowdStrike: list_crowdstrike_intel_indicators (filter on the hash) -> linked actors + malware families
└── Internal correlation
    ├── MISP: search_misp_attributes(value=HASH)   -> events that already include this hash
    └── OpenCTI: list_opencti_observables / search_opencti  -> observables / indicators already in your knowledge base
```

**Concrete calls:**

```
# Detection + family
get_vt_file_report(hash="5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8")
lookup_abusech_malware_hash(hash="5e884898...")

# C2 extraction (highest value) - one relationship per call
get_vt_related_objects(entity_type="file", entity_id="5e884898...", relationship="contacted_ips", limit=25)
get_vt_related_objects(entity_type="file", entity_id="5e884898...", relationship="contacted_domains", limit=25)
get_vt_related_objects(entity_type="file", entity_id="5e884898...", relationship="contacted_urls", limit=25)

# Family lineage
get_vt_related_objects(entity_type="file", entity_id="5e884898...", relationship="dropped_files", limit=10)
get_vt_related_objects(entity_type="file", entity_id="5e884898...", relationship="execution_parents", limit=10)

# Community attribution (replaces the OTX file lookup)
get_gti_file_assessment(hash="5e884898...")
search_abusech_iocs(query="5e884898...")

# Internal correlation via MISP / OpenCTI
search_misp_attributes(value="5e884898...")
search_opencti(search="5e884898...")
```

### Starting from a TLS certificate (SHA-256 or CN)

```
Certificate
├── Hosts presenting this cert
│   ├── Shodan: search_shodan_hosts('ssl.cert.fingerprint:SHA256')   -> IPs serving the cert
│   └── (Censys certificate search: no integration) -> VT search_vt_intelligence, urlscan search_urlscan_scans
└── Other certs by the same subject
    └── Shodan: search_shodan_hosts('ssl.cert.subject.cn:"CN"')  -> issuer, validity, sibling SANs in the banners
```

**Concrete calls:**

```
count_shodan_hosts(query='ssl.cert.fingerprint:"5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"')
search_shodan_hosts(query='ssl.cert.fingerprint:"5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8"')
```

Shodan indexes certificates by the fingerprint it records in its banner; check which hash form the host record shows before pivoting. The Censys certificate view (issuer, validity, all SANs for a certificate id) has no direct equivalent: rebuild it from the Shodan banners and from VirusTotal `historical_ssl_certificates`, and record the gap.

### Starting from a registrant email or org name

```
Registrant identity
├── Reverse WHOIS
│   ├── DomainTools: search_domaintools_reverse_whois  -> domains registered with the same email / org
│   └── DomainTools: search_domaintools_iris           -> registrant, name server, SSL and IP facets in one query
├── Internal MISP correlation
│   └── MISP: search_misp_attributes(type="whois-registrant-email", value=EMAIL)
├── Internal OpenCTI correlation
│   └── OpenCTI: search_opencti(EMAIL_OR_ORG)
└── Domains attributed to the same actor in public reporting
    └── (OTX pulse search: no integration) -> Feedly search_feedly_articles, GTI search_gti_threats, Recorded Future search_rf_entities
```

Without DomainTools, reverse WHOIS is not available in XTM One: say so instead of guessing, and fall back on MISP / OpenCTI correlation.

### Starting from an actor name, malware family, or campaign label

```
Actor / family / campaign string
├── Ransomware group (if applicable)
│   ├── Ransomware.live: get_ransomlive_group(NAME)  -> group profile, victims
│   ├── Ransomware.live: get_ransomlive_iocs(NAME)   -> IOC dump for this group
│   └── Ransomware.live: get_ransomlive_yara(NAME)   -> YARA rules attributed to the group
├── Public reporting (replaces OTX pulse search)
│   ├── abuse.ch ThreatFox: search_abusech_iocs / resolve_abusech_malware_label  -> IOCs tagged with the family
│   ├── GTI: search_gti_threats, get_gti_threat_related_objects
│   ├── Recorded Future: lookup_rf_malware, search_rf_threat_actors
│   └── Feedly: search_feedly_entities, get_feedly_entity_relationships
├── MISP events
│   └── MISP: search_misp_events(tags=["actor=NAME"])  -> previously catalogued IOCs
├── OpenCTI knowledge base
│   └── OpenCTI: search_opencti(ACTOR_OR_FAMILY)  -> intrusion sets, malware, reports, campaigns;
│       then get_opencti_entity and list_opencti_relationships on the id
├── Vendor actor profile
│   └── CrowdStrike: get_crowdstrike_actor, list_crowdstrike_intel_reports, list_crowdstrike_intel_indicators
├── ATT&CK reference
│   └── MITRE ATT&CK: get_mitre_attack_object / get_mitre_attack_related (techniques and software the group uses)
└── Underground-forum / Telegram mentions
    └── the Dark Web Collection skill (selectors built from the actor's known aliases)
```

### Starting from a Telegram channel handle or onion mirror

```
Channel handle / .onion address
├── Telegram channel handle
│   ├── Intel 471: search_intel471_instant_messages  -> collected instant-messaging content
│   ├── Flashpoint: search_flashpoint_communities    -> chat and forum collections
│   └── Cybersixgill: search_cybersixgill_intel_items
│       -> match channel content against your IOC selectors (code_interpreter for keyword matching over collected text)
└── .onion address
    ├── Intel 471 search_intel471_forum_posts / Flashpoint search_flashpoint_communities / SOCRadar search_socradar_dark_web
    │   -> other mirrors or posts that mention the same distinctive string
    └── (no onion-indexer search such as Ahmia or IntelligenceX in XTM One)
```

XTM One cannot read arbitrary Telegram channels (the Telegram integration is a bot that sends messages). Load the **Dark Web Collection** skill, if attached, for OPSEC and access posture before working these pivots.

## Pivot quality scoring

Not all pivots are equal. Score each before reporting:

| Pivot | Confidence | False-positive risk | Why |
|---|---|---|---|
| C2 IP/domain extracted from a reverse-engineered sample | High | Low | Direct, sample-grounded |
| TLS cert SHA-256 reused across hosts | High | Low | Cert reuse is intentional; the actor pinned it |
| Cert subject CN reused across hosts | Medium-High | Low-Medium | CN can be reused legitimately on shared certs |
| JARM fingerprint match | Medium-High | Medium | Strong for unusual JARMs; weak for default-config tools |
| Passive DNS (domain -> IP, current) | Medium-High | Low-Medium | Direct; check the dates |
| Passive DNS (domain -> IP, historical only) | Medium | Medium | Stale; could pre-date the campaign |
| Same WHOIS registrant email | Medium | Medium | Many real users share an email across domains |
| Same page structure (urlscan structure search) | Medium | Medium | Strong for custom kits; weak for mass-market phishing kits |
| Same name server | Low | High | Popular NS providers host millions of unrelated domains |
| Same /24 IP range | Low | High | Cloud and CDN sharing |
| Same registrar | Very low | Very high | Registrars are infrastructure; near-meaningless alone |
| Same JA3/JA3S TLS client fingerprint | Medium | Medium | Strong for custom clients; weak for OS-default stacks |
| Same MITRE technique | Very low | Very high | Technique-level overlap is not infrastructure overlap |

**Rule of thumb**: a pivot chain is only as strong as its weakest link. Three High pivots in series is roughly High overall. One High + two Mediums is roughly Medium. Anything with a Low link is Low overall, regardless of length.

## Pivot routing by goal

| Goal | First choice | Second |
|---|---|---|
| IP reputation | VirusTotal `get_vt_ip_report` | AbuseIPDB `check_abuseipdb_ip`, GreyNoise `lookup_greynoise_ip` |
| Filter out internet-wide scanners | GreyNoise `lookup_greynoise_ip` | CrowdSec `lookup_crowdsec_ip` |
| Service banner / vuln view | Shodan `get_shodan_host_info` | GTI `get_gti_ip_assessment` (Censys has no integration) |
| Cert fingerprint pivot | Shodan `search_shodan_hosts` (`ssl.cert.fingerprint:`) | VirusTotal `historical_ssl_certificates` relationship |
| JARM pivot | Shodan `search_shodan_hosts` (`ssl.jarm:`) | none (Censys has no integration) |
| Passive DNS | VirusTotal `get_vt_related_objects` (`resolutions`) | DomainTools `get_domaintools_hosting_history`, Defender TI `get_defender_ti_passive_dns` |
| Subdomain enumeration | Shodan `get_shodan_domain_info` | VirusTotal `subdomains` relationship |
| Current DNS resolution / reverse DNS | Shodan `resolve_shodan_dns`, `reverse_shodan_dns` | VirusTotal `get_vt_domain_report` (last DNS records) |
| Reverse WHOIS | DomainTools `search_domaintools_reverse_whois` | DomainTools `search_domaintools_iris` |
| Live URL analysis | urlscan `submit_urlscan_scan` | VirusTotal `get_vt_url_report`, ANY.RUN `submit_anyrun_url_analysis` |
| Existing URL captures | urlscan `search_urlscan_scans` | none |
| Same phishing kit on other domains | urlscan `find_urlscan_similar_scans` (Pro) | urlscan `search_urlscan_scans` on the distinctive path |
| File reputation | VirusTotal `get_vt_file_report` | GTI `get_gti_file_assessment`, MalwareBazaar `lookup_abusech_malware_hash` (ReversingLabs has no integration) |
| Authoritative classification + ATT&CK mapping | VirusTotal `get_vt_file_behaviour` | CrowdStrike sandbox `get_crowdstrike_sandbox_report`, ANY.RUN `get_anyrun_analysis_report`, then the **Malware Analysis** skill |
| Siblings by malware family / threat name | abuse.ch `search_abusech_malware`, `search_abusech_iocs` | VirusTotal `search_vt_intelligence` (premium), GTI `get_gti_threat_related_objects` |
| Parent container / extracted children | VirusTotal `execution_parents`, `bundled_files`, `dropped_files` relationships | none |
| C2 extraction from a hash | VirusTotal `contacted_ips`, `contacted_domains`, `contacted_urls` relationships | ANY.RUN `get_anyrun_analysis_iocs`, CrowdStrike `get_crowdstrike_sandbox_report` |
| Community / actor attribution | OpenCTI `search_opencti`, MISP `search_misp_events` | CrowdStrike `list_crowdstrike_intel_indicators`, GTI, Recorded Future `enrich_rf_indicators` |
| Actor -> TTPs / reports / related infrastructure | CrowdStrike `get_crowdstrike_actor`, `list_crowdstrike_intel_reports` | MITRE ATT&CK `get_mitre_attack_related`, then the **Threat Actor Profiling** skill |
| Internal correlation against your own catalogued IOCs | MISP `search_misp_attributes` | OpenCTI `list_opencti_observables`, then `list_opencti_relationships` |
| Ransomware-group profile / IOCs / YARA | Ransomware.live `get_ransomlive_group`, `get_ransomlive_iocs`, `get_ransomlive_yara` | none |
| Ransomware-victim sweep on an org name | Ransomware.live `search_ransomlive_victims` | none |
| Underground-forum / Telegram chatter | the **Dark Web Collection** skill | Intel 471, Flashpoint, Cybersixgill, SOCRadar tools |

## Worked example: phish URL to a phishing-infrastructure cluster

A walkthrough of a realistic multi-hop chain. Source rating in brackets.

**Seed.** `https://login-acme[.]net/secure/portal/login.html` reported by an internal user as a suspicious password-reset email. Source: internal phishing inbox (B2).

**Hop 1: characterise the URL.**

```
search_urlscan_scans(query='page.domain:"login-acme.net"')
get_vt_url_report(url="https://login-acme.net/secure/portal/login.html")
```

Result: urlscan shows two prior submissions in the last 14 days (both flagged phishing), the screenshot is an Acme-cloned login page, and the JS posts credentials to `https://api.login-acme[.]net/auth`. VT detection ratio 12/89, phishing. Pivot candidate: domain `login-acme.net` (B2).

**Hop 2: domain to resolved IP and cert.**

```
resolve_shodan_dns(hostnames="login-acme.net,api.login-acme.net")
get_vt_related_objects(entity_type="domain", entity_id="login-acme.net", relationship="resolutions")
```

Result: both names currently resolve to `198.51.100.42`; passive DNS shows the names appeared together on 2026-04-12 (10 days ago: newly weaponised). Pivot candidates: IP `198.51.100.42` (B2), creation cluster of `*.login-acme.net` subdomains.

**Hop 3: IP to service fingerprint and cert SHA-256.**

```
get_shodan_host_info(ip="198.51.100.42", history=true)
get_vt_related_objects(entity_type="ip", entity_id="198.51.100.42", relationship="historical_ssl_certificates")
lookup_greynoise_ip(ip="198.51.100.42")
```

Result: nginx on 443; TLS cert SHA-256 `8f3c...b22a` issued by Let's Encrypt for `login-acme.net`, `api.login-acme.net`, and **`accounts-globex[.]net`** (a separate brand). GreyNoise: not a known scanner; quiet host, consistent with phishing infrastructure. Pivot candidate: domain `accounts-globex.net` (B2).

**Hop 4: cert pivot, who else serves this cert?**

```
count_shodan_hosts(query='ssl.cert.fingerprint:"8f3c...b22a"')
search_shodan_hosts(query='ssl.cert.fingerprint:"8f3c...b22a"')
find_urlscan_similar_scans(scan_id="[hop 1 scan id]")      # optional, urlscan Pro
```

Result: three more IPs in the same hosting AS serving the same cert, each with a Let's Encrypt cert covering a different `*-cloud[.]net` look-alike domain. Pattern: brand-impersonation phishing infrastructure templated by the same operator. Pivot candidate: cluster of 5 domains, 4 IPs, 1 cert (B2, high-confidence cluster). The original method ran the same query on Censys; without it, Shodan coverage is the only certificate index, so note that the host list may be incomplete.

**Hop 5: community and family attribution.**

```
search_opencti(search="login-acme.net")
search_abusech_iocs(query="accounts-globex.net")
search_misp_attributes(value="198.51.100.42")
search_feedly_articles(query="login-acme.net")
```

Result: both domains appear in a single public vendor write-up dated 2026-04-15 (found through Feedly and already ingested in OpenCTI as a Report), attributed to a known credential-phishing kit ("BabyShark-Mailer-v3"). MISP returns one event with the IP from a sibling intrusion two weeks prior.

**Synthesis.** Five domains and four IPs sharing a single cert, all newly created in a 14-day window, served from one AS, attributed to a documented kit. Confidence: **High** (multiple High-rated pivots in series). Recommended action: block the cluster, hand off to the **Threat Actor Profiling** skill for kit attribution, and, when the user asks, publish the cluster to the sharing community as a MISP event (`create_misp_event` + `add_misp_attribute`) or an OpenCTI draft (`create_opencti_draft`).

```yaml
seed: https://login-acme[.]net/secure/portal/login.html
investigation_date: 2026-04-28
chain:
  - hop: 1; type: url;    method: urlscan + VT;                       confidence: High
  - hop: 2; type: domain; method: Shodan DNS + VT passive DNS;        confidence: High
  - hop: 3; type: ip;     method: Shodan + VT certs + GreyNoise;      confidence: High
  - hop: 4; type: cert;   method: Shodan cert-fingerprint pivot;      confidence: High
  - hop: 5; type: actor;  method: OpenCTI + Feedly + MISP attribution; confidence: Medium-High
skipped_sources: [Censys (no integration), AlienVault OTX (no integration)]
overall_confidence: High
cluster:
  domains: [login-acme.net, api.login-acme.net, accounts-globex.net, ...]
  ips:     [198.51.100.42, ...]
  certs:   [sha256:8f3c...b22a]
  family:  BabyShark-Mailer-v3
recommended_next:
  - Threat Actor Profiling skill for the kit operator
  - MISP event or OpenCTI draft to publish (only when asked)
  - Sigma Rule Writing skill for a detection on the kit's distinctive POST path
```

## Pivot documentation template

Record every hop. Future readers need the chain.

```markdown
## Pivot Chain: SEED_INDICATOR

### Hop 1: INDICATOR_TYPE -> PIVOT_TYPE
- **Method**: [integration + tool]
- **Call**: `TOOL(ARGUMENTS)`
- **Result**: [one-line summary; numbers matter]
- **Pivot candidates produced**: [LIST]
- **Rejected candidates**: [[list + reason, e.g. "CDN range", "default JARM, 40k hosts"]]
- **Source rating**: ADMIRALTY_A1_F6
- **Confidence**: [High/Medium/Low, see the scoring table]

### Hop 2: ...

### Sources skipped
- [integration not connected or not available in XTM One, and what it would have answered]

### Synthesis
- **Cluster**: [indicators that hang together]
- **Overall confidence**: WEAKEST_LINK
- **TLP**: [marking inherited from the most restrictive source]
- **Recommended next**: SKILL_HANDOFFS
```

## Rigor before publishing (compact form)

Apply these inline; load the full skills only if the product is going out to stakeholders:

- **Admiralty rating per source** (reliability A-F, credibility 1-6). Established tool APIs are usually B; automated enrichment with no human judgment is F6 until corroborated; a hit on a previously curated MISP event or OpenCTI entity lifts credibility one step. Full method: the **Source Assessment (Admiralty Scale)** skill.
- **TLP**: the chain inherits the most restrictive marking of any input (an internal phishing-inbox seed is usually TLP:AMBER).
- **Confidence 0-100** on the cluster, following the weakest-link rule, and the words High / Moderate / Low in the prose.
- **Likelihood words** for forward-looking judgments ("likely", "almost certainly") per the **Likelihood Language** skill, never bare percentages without the matching term.

## Common pitfalls

- **Shared hosting / CDN false positives.** Many domains share IPs on Cloudflare, Fastly, AWS CloudFront. Same-IP pivots from a CDN range are nearly meaningless: check the org/ASN in `get_shodan_host_info` before treating same-IP as a relationship.
- **Privacy WHOIS.** Most public WHOIS is now redacted. `get_vt_domain_report` returns what is available; unredacted history and reverse WHOIS need DomainTools (`get_domaintools_whois_history`, `search_domaintools_reverse_whois`).
- **Historical vs. current resolution.** A passive-DNS edge from 2022 is not evidence of the same operator's involvement in 2026. Check the resolution dates and prefer recent edges.
- **Overcounting pivots.** A chain with one Low-confidence link is a Low-confidence chain regardless of length. Five Low pivots stacked do not become a High pivot.
- **Stopping too early.** The hop-1 lookup almost never gives you the cluster; it gives you hop-2 candidates. Budget for at least 3-4 hops on any non-trivial seed.
- **Letting cost decide direction.** Shodan query credits, VirusTotal premium relationships (`submissions`, `similar_files`), Intelligence search and urlscan Pro cost quota. Plan the pivot before paying the call; do not burn the budget enumerating noise.
- **Not recording rejected pivots.** A pivot you tried and discarded is just as important as one you kept. Record both.
- **Not recording skipped sources.** A source that was not connected changes the corroboration count; say which one was skipped.
- **Confusing infrastructure overlap with intent overlap.** Two campaigns can share infrastructure without being the same actor (rented hosting, leaked toolkit). Two campaigns can share TTPs without sharing infrastructure (commodity malware). Pivoting addresses infrastructure; attribution needs more.

## Writing back (only when asked)

Pivoting is read-only by default. When the user or the assignment asks to record the cluster:

- **OpenCTI**: prefer a draft (`create_opencti_draft`, then `use_opencti_draft`) holding the observables (`create_opencti_observable`), indicators (`create_opencti_indicator`) and relationships (`create_opencti_relationship`), so an analyst reviews before it reaches the knowledge graph. Attach them to a report or grouping with `add_opencti_to_container`.
- **MISP**: `create_misp_event`, `add_misp_attribute` per indicator, `add_misp_tag` for TLP and actor tags; publish (`publish_misp_event`) only on explicit request.
- **Agent memory**: `store_memory` for a short note of the cluster and its confidence, so a later run can resume the chain.

## Related skills

- **IP Investigation**, **Domain Investigation**, **Hash Investigation**, **URL Investigation**: first-hop chains; this skill takes over for deeper traversal
- **Threat Intel Sources**: how to read each integration's verdict fields and false-positive traps
- **Dark Web Collection**: when the pivot leads into forum or Telegram chatter
- **Threat Actor Profiling**, **Campaign Tracking**: what to do with the cluster once you have it
- **Malware Analysis**, **YARA Rule Writing**, **Sigma Rule Writing**: sample-grounded follow-ups to a hash pivot
- **Source Assessment (Admiralty Scale)**, **TLP Guide**, **Confidence Levels**, **Likelihood Language**: full rigor for the finished pivot chain
