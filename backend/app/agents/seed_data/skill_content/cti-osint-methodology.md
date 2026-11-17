# OSINT Methodology

Structured open-source intelligence collection: planning, source categories, search operators, the collection process, documentation of findings, and the ethical and legal boundaries.

Use only the tools you actually have; when an integration is not connected, say which source was skipped instead of guessing its result.

## Collection Tools in XTM One

| Need | Tool |
|------|------|
| Web search (site:, filetype: and quoted operators supported) | `search_google_web` (the built-in web search; Brave Search answers this name too, as `search_brave_web`) |
| Read a page, advisory, report or JSON endpoint | `web_fetch` |
| Pages that need JavaScript, scrolling or clicking through | `browser` |
| Threat-intel news and vendor reporting at scale | Feedly (`search_feedly_articles`, `get_feedly_stream_articles`, `ask_feedly_ai`, `search_feedly_entities`); RSS (`fetch_rss_feeds`, `search_rss_entries`); NewsAPI (`search_newsapi_articles`) |
| Domain / WHOIS / passive DNS history | DomainTools (`get_domaintools_whois_history`, `get_domaintools_hosting_history`, `search_domaintools_iris`, `search_domaintools_reverse_whois`) |
| Internet-exposed hosts, banners, certificates | Shodan host and search tools (`get_shodan_host_info`, `search_shodan_hosts`, `count_shodan_hosts`, `get_shodan_domain_info`) |
| What a URL or domain serves, without visiting it yourself | urlscan (`search_urlscan_scans`, `get_urlscan_result`, `find_urlscan_similar_scans`); submit a new scan with `submit_urlscan_scan` only when asked, and as a private/unlisted scan when the target is sensitive |
| What your organisation already knows | OpenCTI (`search_opencti`), MISP (`search_misp_events`), knowledge bases (`search_knowledge`) |
| Parsing, extraction, deduplication | `code_interpreter` (regex IOC extraction, refang/defang, date normalisation, tables) |

Check internal sources (OpenCTI, MISP, knowledge bases) first: they avoid re-collecting what the team already holds and give the baseline the open sources are compared against.

## Collection Planning

Before collecting, define:
1. **Objective**: What specific intelligence do we need? (Tie it to a PIR when one exists: see the **PIR Management** skill.)
2. **Scope**: What sources are relevant? What's out of scope?
3. **Keywords**: Search terms, aliases, IOCs to look for (resolve actor aliases first: OpenCTI intrusion set aliases, `search_mitre_attack` for the ATT&CK group, Feedly `lookup_feedly_entity`)
4. **Time constraints**: How recent must the intelligence be?
5. **Documentation**: How will findings be recorded? (the finding template below, in the run output; persistent notes in OpenCTI or agent memory only when asked)

## Source Categories

| Category | Sources | Best For |
|----------|---------|----------|
| **Government advisories** | CISA, NCSC, CERT-EU, national CERTs | Authoritative threat alerts, vulnerability warnings |
| **Vendor reports** | Mandiant, CrowdStrike, Microsoft, Recorded Future, Talos | Campaign analysis, actor profiles, malware analysis |
| **Security research** | Blogs, conference talks, academic papers | Novel techniques, deep dives, vulnerability research |
| **Underground** | Forum mirrors, paste sites, Telegram (public), leak aggregators | Threat actor communications, credential dumps, tools |
| **Code repositories** | GitHub, GitLab | Malware source, PoC exploits, attacker tools, leaked configs |
| **Certificate transparency** | crt.sh, Censys | Domain enumeration, infrastructure mapping |
| **DNS/WHOIS** | SecurityTrails, DomainTools, RiskIQ (via web search) | Infrastructure relationships, historical records |
| **News/media** | Tech press, infosec news sites | Incident reports, geopolitical context |
| **Social media** | X/Twitter, LinkedIn, Reddit (public) | OPSEC failures, community intelligence, actor claims |

Notes for XTM One:
- **Censys** has no XTM One integration. For hosts and certificates use Shodan, GTI (`get_gti_ip_assessment`, `get_gti_domain_assessment`), DomainTools or urlscan; for certificate transparency, query crt.sh with `web_fetch` (below).
- **Underground** sources: prefer the vendor collections that index them safely (Flashpoint `search_flashpoint_communities`, Intel 471 `search_intel471_forum_posts` / `search_intel471_instant_messages`, Cybersixgill `search_cybersixgill_intel_items`, SOCRadar `search_socradar_dark_web`). Load the **Dark Web Collection** skill and read its OPSEC guidance before any direct underground collection.
- **Ransomware leak sites**: use Ransomware.live (`search_ransomlive_victims`, `get_ransomlive_group`) rather than visiting leak sites.

## Search Operator Cheat Sheet

### Google Dorks (with `search_google_web`)
```
site:example.com filetype:pdf         # PDFs on a specific site
"threat actor" AND "APT28" -site:reddit.com  # Exclude noise
inurl:"/wp-content/uploads/" filetype:exe    # Exposed uploads
"index of" "/backup"                   # Directory listings
intitle:"login" site:*.example.com     # Login pages on subdomains
```

### GitHub Search
```
org:target-org password                # Leaked credentials
filename:.env DB_PASSWORD              # Exposed environment files
"api_key" language:python              # Hardcoded API keys
"BEGIN RSA PRIVATE KEY"                # Exposed private keys
```

With the GitHub integration, run these queries with `search_github_code` (and `search_github_repositories` / `search_github_commits` for attacker tooling and leaked history); without it, use `search_google_web` with `site:github.com` plus the terms, then `web_fetch` on the result pages. Report exposed secrets of your own organisation to its owners; never test or use a discovered credential.

### Certificate Transparency (crt.sh, with `web_fetch`)
```
https://crt.sh/?q=%.example.com        # All certs for subdomains
https://crt.sh/?q=example.com&output=json  # JSON output
```

Parse the JSON output with `code_interpreter` (deduplicate names, sort by first-seen date).

### Shodan Queries (with the Shodan integration: `search_shodan_hosts`, `count_shodan_hosts`)
```
ssl.cert.subject.cn:"example.com"      # Certificates for domain
http.title:"Dashboard" org:"Target"    # Web dashboards
product:"Cobalt Strike"                # C2 servers
```

Use `count_shodan_hosts` first to size a query before pulling results (quota-sensitive), and `list_shodan_search_filters` when unsure of a filter name.

## Collection Process

### 1. Broad Sweep
Start with broad searches to understand what's available:
- Search for the topic/actor/indicator across major sources
- Note the quantity and quality of available information
- Identify the most promising sources for deeper collection

### 2. Targeted Collection
Narrow down to specific sources and extract detailed intelligence:
- Read full reports, not just headlines (`web_fetch` the report itself, not the search snippet)
- Extract specific IOCs, TTPs, dates, and attribution claims
- Note each source's perspective and potential biases
- Prefer the original publication over re-reporting; trace news items back to the vendor or government source

### 3. Source Assessment
Apply the Admiralty Scale to every finding (full guidance: the **Source Assessment (Admiralty Scale)** skill):
- Rate source reliability (A-F): A completely reliable, B usually reliable, C fairly reliable, D not usually reliable, E unreliable, F cannot be judged
- Rate information credibility (1-6): 1 confirmed by other sources, 2 probably true, 3 possibly true, 4 doubtful, 5 improbable, 6 cannot be judged
- Note whether information is corroborated across sources (two articles quoting the same vendor report are one source, not two)

### 4. Documentation
Record every finding with full attribution:
```markdown
#### Finding: [Title]
- **Source**: [URL or description]
- **Source rating**: [Admiralty code, e.g., B2]
- **Date collected**: YYYY-MM-DD
- **Date of information**: YYYY-MM-DD (when was this information produced?)
- **Summary**: [Key intelligence extracted]
- **Relevance**: [How this relates to the collection objective]
```

Defang indicators in the findings (`hxxp`, `[.]`). Mark the collection output with a TLP no lower than the most restrictive source used.

## Ethical and Legal Boundaries
- Only access publicly available information
- Do not create fake accounts or impersonate anyone
- Do not attempt to access restricted systems
- Respect robots.txt and terms of service
- Be aware of data protection regulations (GDPR)
- Do not engage with threat actors or participate in illegal activity
- Do not collect or open child sexual abuse material or terrorist content; if encountered, stop, do not store it, and report it to the user for referral to the appropriate authority
- Do not download files from untrusted sites; pass suspicious URLs and files to sandbox and reputation services (urlscan, VirusTotal, ANY.RUN) instead

## Related Skills

When OSINT collection surfaces an indicator, route into the appropriate enrichment or pivot (load at most one or two):

- **Bulk IOC enrichment** - the **IOC Enrichment Workflow** skill
- **Single-indicator first hop** - the **IP Investigation**, **Domain Investigation**, **Hash Investigation** or **URL Investigation** skills
- **Multi-hop graph walk** - the **Indicator Pivoting** skill
- **Which source answers which indicator** - the **Threat Intel Sources** skill
- **Internal correlation against your own catalogue** - MISP `search_misp_attributes` / `search_misp_events`, OpenCTI `search_opencti`
- **Ransomware victim-status sweeps on organisation names** - Ransomware.live `search_ransomlive_victims`
- **Underground forums and Telegram** - the **Dark Web Collection** skill (read its OPSEC guidance before any direct collection)
- **Apply rigor to the finished product** - the **Source Assessment (Admiralty Scale)**, **TLP Guide**, **Confidence Levels** and **Likelihood Language** skills
