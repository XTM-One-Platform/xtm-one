# Ransomware Ecosystem

> **Knowledge cell.** Baseline knowledge through early 2025, last reviewed **2026-04-05**. Overall confidence: **moderate**.
> This body is a read-only built-in skill: it is re-synced on every platform restart and does not update itself. Group status changes fast in this ecosystem: before you describe a group as "active", "defunct" or "leading", check the live sources listed in **Refreshing this picture**, and say how fresh each statement is.

## How to use this cell

- Use it as the baseline picture of the ransomware ecosystem: the RaaS model, affiliate markets, lineage and rebrands, law enforcement disruptions, leak-site and negotiation behaviour, TTPs and tooling.
- Use live leak-site data (Ransomware.live) for anything about recent victims, volumes or sector trends; use this cell for the structure and history that explain those numbers.
- Leak-site postings are claims made by criminals: they are not confirmed compromises. Report them as "claimed by GROUP on DATE", and rate them accordingly (typically F or C on the Admiralty reliability scale until corroborated by the victim, a regulator filing or a trusted vendor).
- Express judgments with a confidence level and calibrated likelihood words. For a structured group profile, load the **Threat Actor Profiling** skill if it is attached; for the access market feeding ransomware, the **Initial Access Brokers** skill.

## Executive Summary

The ransomware ecosystem operates predominantly through a Ransomware-as-a-Service (RaaS) model, where operator groups develop and maintain ransomware payloads, negotiate with victims, and manage leak sites, while affiliates (independent contractors) handle the actual intrusion, lateral movement, and deployment. This division of labor has driven the industrialization of ransomware since roughly 2019, enabling groups to scale operations far beyond what a single team could achieve. Revenue sharing typically follows a 70/30 or 80/20 split favoring the affiliate, though elite affiliates can negotiate better terms. The double-extortion model (encrypting data AND threatening to leak it) is now standard, with some groups pursuing triple extortion by adding DDoS threats or contacting victims' customers directly.

Law enforcement has achieved significant disruptions in 2023-2025, including the takedown of Hive (January 2023), the disruption of LockBit infrastructure in Operation Cronos (February 2024), the ALPHV/BlackCat exit scam following the Change Healthcare payment, and sustained pressure on Cl0p. However, the ecosystem demonstrates remarkable resilience: displaced affiliates migrate to new platforms, operators rebrand, and new RaaS programs emerge to fill gaps. The total ransomware economy is estimated at over $1 billion annually in direct payments, with substantially higher costs when accounting for downtime, remediation, and reputational damage.

The current landscape as of early 2026 features a more fragmented ecosystem following the disruptions of the major groups. RansomHub emerged as a significant player absorbing former LockBit and ALPHV affiliates. Akira, Play, and Black Basta continue active operations. Smaller, more agile groups proliferate, potentially making the ecosystem harder to disrupt through single-point takedowns. Healthcare, education, manufacturing, and critical infrastructure remain heavily targeted sectors.

## Key Actors

| Group | Status | Notable Characteristics | Peak Activity |
|-------|--------|------------------------|---------------|
| LockBit | Disrupted (Feb 2024), attempted comeback | Most prolific RaaS 2022-2024; LockBitSupp identified as Dmitry Khoroshev | 2022-2024 |
| ALPHV/BlackCat | Defunct (exit scam, Mar 2024) | Rust-based ransomware; $22M Change Healthcare payment; FBI seized leak site Dec 2023, group reclaimed it | 2022-2024 |
| Cl0p | Intermittently active | Specializes in mass exploitation of file transfer vulnerabilities (MOVEit, GoAnywhere, Cleo) | 2023 |
| Play | Active | Closed affiliate model; targets Latin America and Europe heavily | 2022-present |
| Akira | Active | Emerged mid-2023; possible Conti lineage; targets SMBs and VPN vulnerabilities | 2023-present |
| Black Basta | Active | Conti successor; uses sophisticated social engineering; internal chat leaks in early 2025 | 2022-present |
| RansomHub | Active | Emerged 2024; absorbed former LockBit/ALPHV affiliates; aggressive 90/10 split to attract affiliates | 2024-present |
| Medusa | Active | Increasing activity through 2024-2025; targets education sector | 2023-present |
| Royal/BlackSuit | Active | Conti lineage; rebranded from Royal to BlackSuit | 2022-present |
| BianLian | Active | Shifted to exfiltration-only (no encryption) model | 2022-present |

Statuses are as of the review date. Check `get_ransomlive_group` and the latest victim postings before repeating any of them.

## Current Activity

### RansomHub's Rise as Ecosystem Leader (2024-2025)
Following the disruptions of LockBit and ALPHV/BlackCat, RansomHub rapidly filled the vacuum by offering affiliates an extremely generous 90/10 revenue split and fewer restrictions on targeting. The group attracted experienced affiliates displaced from defunct operations, enabling rapid scaling. RansomHub demonstrated sophisticated operations including exploitation of recent CVEs and the use of EDR-killing tools.

### Shift Toward Exfiltration-Only Operations
An increasing number of groups, led by BianLian and followed by others, have abandoned encryption entirely in favor of pure data theft and extortion. This approach reduces operational complexity, avoids triggering endpoint detection that monitors for encryption behavior, and still provides significant leverage over victims. This trend suggests the ecosystem is optimizing for stealth and reliability over maximum disruption.

### Healthcare Sector Escalation
The Change Healthcare attack (February 2024) demonstrated the catastrophic potential of ransomware against healthcare infrastructure, disrupting prescription processing for millions. Despite increased scrutiny, healthcare targeting has continued, with groups exploiting the sector's high willingness to pay and complex, often outdated IT environments.

## Historical Events

| Date | Event | Impact |
|------|-------|--------|
| May 2019 | Baltimore city ransomware attack (RobbinHood) | $18M+ in damages; highlighted municipal vulnerability |
| May 2021 | Colonial Pipeline (DarkSide) | Fuel shortages in US Southeast; $4.4M ransom (partially recovered); triggered executive order on cybersecurity |
| Jul 2021 | Kaseya VSA attack (REvil) | 1,500+ businesses affected via supply chain; $70M ransom demand |
| Jan 2022 | Conti internal leaks | 60,000+ messages exposed Conti operations; accelerated group's fragmentation into Akira, Royal, Black Basta, etc. |
| Jan 2023 | Hive takedown | FBI infiltrated Hive for 7 months, saved $130M in ransom demands, seized infrastructure |
| Jun 2023 | MOVEit exploitation (Cl0p) | 2,500+ organizations affected; estimated $10B+ in total damages |
| Feb 2024 | Operation Cronos (LockBit) | NCA/FBI/Europol seized LockBit infrastructure, obtained decryption keys, identified LockBitSupp |
| Feb 2024 | Change Healthcare (ALPHV/BlackCat) | $22M ransom paid; ALPHV exit scammed affiliate; disrupted US healthcare billing nationwide |
| Dec 2024 | Continued law enforcement pressure | Multiple arrests of ransomware affiliates across Europe and North America |

## TTP Evolution

**Initial Access**: Ransomware groups have shifted from relying on phishing and RDP brute-forcing (2019-2021) to purchasing access from Initial Access Brokers (IABs) and exploiting zero-day/one-day vulnerabilities in edge devices (VPNs, firewalls, file transfer appliances). Credential theft via infostealers has become a primary pipeline.

**Defense Evasion**: Modern ransomware operations routinely employ EDR killers (Terminator, AuKill, Poortry/Stonestop using signed driver exploits), BYOVD (Bring Your Own Vulnerable Driver) techniques, and living-off-the-land approaches. Safe Mode reboots to disable security tools remain in use.

**Lateral Movement**: Heavy reliance on legitimate tools: Cobalt Strike, Brute Ratel, Sliver for C2; Impacket, PsExec, RDP for movement; Mimikatz, SharpHound/BloodHound for credential harvesting and AD enumeration. Increasingly, groups use RMM tools (AnyDesk, ConnectWise/ScreenConnect, Splashtop) to blend with legitimate admin traffic.

**Exfiltration**: Rclone to cloud storage and custom exfiltration tools remain dominant. WinSCP, FileZilla, and MEGA uploads are common. Some groups use purpose-built exfiltration tools like BlackCat's ExMatter or Black Basta's custom tools.

**Encryption**: Modern ransomware uses intermittent encryption (encrypting portions of files) for speed, multi-threaded encryption, and targets VMware ESXi environments with Linux variants. Rust and Go-based payloads are increasingly common for cross-platform support.

## Ecosystem & Infrastructure Patterns

**RaaS Economic Model**: The RaaS ecosystem mirrors legitimate SaaS businesses with admin panels, customer support, SLAs for decryptor delivery, and reputation management. Affiliate recruitment occurs on underground forums (Exploit, XSS, RAMP) with vetting processes. Some programs restrict targeting (no hospitals, no CIS countries) while others have fewer restrictions.

**Cryptocurrency Laundering**: Ransomware payments flow through mixing services (Tornado Cash, sanctioned), cross-chain bridges (Ren, THORChain), privacy coins (Monero, increasingly demanded), nested exchanges, and OTC desks. Sanctions against services like Tornado Cash, Sinbad, and ChipMixer have disrupted but not eliminated laundering. Russian-linked exchanges (Garantex, sanctioned) have processed significant ransomware funds.

**Rebranding and Fragmentation**: When groups face law enforcement pressure or internal disputes, they rebrand rather than dissolve. Conti fragmented into Royal/BlackSuit, Black Basta, Akira, and Meow. DarkSide became BlackMatter. This pattern makes attribution challenging but relationship mapping possible through code reuse, affiliate overlap, and operational patterns.

**Victim Negotiation**: Professional negotiation via Tor chat portals is standard. Groups set ransom demands based on victim revenue (often 1-5% of annual revenue). Cyber insurance has both enabled payments and professionalized the negotiation process. Some groups use data publication timers to create urgency.

## Tooling

| Tool | Category | Usage |
|------|----------|-------|
| Cobalt Strike | C2 Framework | Most common post-exploitation framework; cracked versions widespread |
| Brute Ratel C4 | C2 Framework | Growing adoption as CS alternative; better EDR evasion |
| Sliver | C2 Framework | Open-source; increasingly adopted by multiple groups |
| Mimikatz | Credential Theft | Standard for Windows credential dumping |
| BloodHound | AD Enumeration | Maps Active Directory attack paths |
| Rclone | Exfiltration | Cloud storage sync for data theft |
| AnyDesk/ScreenConnect | Remote Access | Legitimate RMM tools used for persistent access |
| Terminator/AuKill | EDR Killer | BYOVD-based EDR/AV disabling tools |
| PsExec/Impacket | Lateral Movement | Remote execution across Windows networks |
| Mega.nz | Exfiltration | Cloud storage for staging stolen data |

## Intelligence Gaps

- **Affiliate identity and overlap**: The extent to which top-tier affiliates operate across multiple RaaS programs simultaneously remains poorly understood. Cross-program affiliate tracking is a critical intelligence need.
- **True payment volumes**: Blockchain analysis captures only a portion of ransomware payments. Monero adoption and evolving laundering techniques create significant blind spots in financial intelligence.
- **Pre-ransom access dwell time**: The typical timeline between initial access purchase from IABs and ransomware deployment is not well-characterized across groups, limiting defensive window estimation.
- **State nexus**: The relationship between Russian ransomware operators and Russian intelligence services remains ambiguous. Some operators appear to have tacit permission rather than direct tasking, but the full extent of state awareness/facilitation is unclear.
- **Victim non-reporting**: A substantial percentage of ransomware incidents go unreported, skewing both volume statistics and sector targeting analysis.

## Refreshing this picture

Use only the tools you actually have. When an integration is not connected, say which source was skipped instead of guessing its result. Give the date range of the data you counted, and the date of the newest item you relied on.

**Live leak-site data (Ransomware.live integration)**

| Question | Tool |
|---|---|
| Is the integration reachable, and how fresh is its data? | `get_ransomlive_status` |
| Which groups exist and which are tracked as active? | `list_ransomlive_groups` |
| One group: leak sites, status, description, victim count | `get_ransomlive_group` |
| Latest victim postings across all groups | `list_ransomlive_recent_victims` |
| Victims by name, group, country, sector or date | `search_ransomlive_victims`, then `get_ransomlive_victim` for one posting |
| Volumes and trends | `get_ransomlive_stats`, `list_ransomlive_sectors` |
| Group IOCs and detection content | `get_ransomlive_iocs`, `get_ransomlive_yara` |
| Ransom notes | `list_ransomlive_ransomnotes`, `get_ransomlive_ransomnote` |
| Negotiation behaviour (demands, discounts, timers) | `list_ransomlive_negotiations`, `get_ransomlive_negotiation_chat` |
| Press coverage and regulator disclosures | `list_ransomlive_press`, `list_ransomlive_8k_filings` (SEC 8-K material-incident filings) |
| Who to notify for a victim country | `get_ransomlive_csirt_contacts` |

Reading the data: a posting date is when the group published the claim, not when the intrusion happened; groups repost, inflate and sometimes list victims of other groups; a quiet leak site can mean disruption, a rebrand, a move to private negotiation or simple data lag. Count victims per group over a stated window (for example the last 90 days) before calling a group "leading".

**Vendor reporting and context**

- With the CrowdStrike integration: `list_crowdstrike_actors`, `get_crowdstrike_actor` (eCrime adversaries, "Spider" cryptonyms), `list_crowdstrike_intel_reports`. With Google Threat Intelligence: `search_gti_threats`, `get_gti_threat`, `get_gti_threat_timeline`, `search_gti_threat_iocs`. With Recorded Future: `search_rf_threat_actors`, `lookup_rf_malware`, `get_rf_malware_threat_map`. With Intel 471: `search_intel471_malware_families`, `search_intel471_reports`, `search_intel471_data_leaks`.
- With the VulnCheck integration: `list_vulncheck_ransomware` (CVEs exploited by ransomware groups) and `get_vulncheck_kev`; with Vulnerability Intelligence: `check_vulnintel_kev` (the CISA KEV "known ransomware campaign use" field).
- With the Feedly integration: `query_feedly_cyberattacks` and `search_feedly_articles` for recent attacks and disruptions; `get_feedly_trending_threats`.
- With the MITRE ATT&CK integration: `search_mitre_attack` and `get_mitre_attack_related` for the group or software entry and its techniques.

**What the organization already knows**

- With the OpenCTI integration: `search_opencti` / `list_opencti_entities` for the Intrusion Set or Malware, `list_opencti_relationships` for victims, sectors and lineage links, `get_opencti_entity_stats` for reporting trends. With MISP: `search_misp_events`. Agent memory: `search_memory`.

**Open sources**

- `search_google_web`, `search_newsapi_articles`, `search_rss_entries` and `web_fetch` for #StopRansomware advisories, law enforcement press releases and vendor quarterly reports. Do not browse leak sites directly and never download leaked victim data.

## Recording new findings

This body cannot be edited by the agent. When a run produces newer or corrected intelligence (a takedown, a rebrand, a new group, a change of status):

- if the user or the assignment asks for it, record it in OpenCTI (`create_opencti_note` on the Intrusion Set, `create_opencti_relationship` for a lineage or targeting link, or a report), never on your own initiative;
- otherwise store a short dated finding in agent memory with `store_memory` (group, claim, source, Admiralty rating, date), so later runs can find it with `search_memory`;
- in the answer, state which statements come from this cell (baseline, last reviewed 2026-04-05) and which come from live sources.

## Sources & References

1. CISA - "#StopRansomware" Advisory Series (ongoing): https://www.cisa.gov/stopransomware
2. Chainalysis - "2024 Crypto Crime Report: Ransomware": https://www.chainalysis.com/blog/ransomware-2024/
3. Europol - "Internet Organised Crime Threat Assessment (IOCTA)": https://www.europol.europa.eu/iocta-report
4. NCA - "Operation Cronos: LockBit Disruption" (February 2024): https://www.nationalcrimeagency.gov.uk/
5. Mandiant - "Ransomware Rebrand: Tracking Cluster Transformations": https://www.mandiant.com/resources
6. Recorded Future - "2024 Annual Ransomware Report": https://www.recordedfuture.com/research
7. Coveware - "Quarterly Ransomware Reports": https://www.coveware.com/blog
8. Microsoft - "Digital Defense Report 2024": https://www.microsoft.com/en-us/security/security-insider/microsoft-digital-defense-report-2024

## Review History

| Date | Change | Source |
|------|--------|--------|
| 2026-04-05 | Initial creation with baseline intelligence through early 2025 | Analyst baseline |
