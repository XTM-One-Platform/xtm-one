# Initial Access Brokers

> **Knowledge cell.** Baseline knowledge through early 2025, last reviewed **2026-04-05**. Overall confidence: **moderate**.
> This body is a read-only built-in skill: it is re-synced on every platform restart and does not update itself. Broker handles and forum venues change often: before you describe a handle or a venue as "active", check the live sources listed in **Refreshing this picture**, and say how fresh each statement is.

## How to use this cell

- Use it as the baseline picture of the Initial Access Broker (IAB) market: how access is acquired, packaged, priced and sold, the forum venues, the infostealer and edge-vulnerability supply lines, and the link to ransomware.
- For a question about the organization ("is our access for sale?"), match listings against the organization's profile (country, sector, revenue band, access type, technologies) with the dark-web sources below. Listings rarely name the victim: a match is a hypothesis to test, not a confirmation (see **Assessing a listing**).
- Express judgments with a confidence level and calibrated likelihood words. For the credential supply line, load the **Infostealers** skill if it is attached; for the buyers, the **Ransomware Ecosystem** skill.

## Executive Summary

Initial Access Brokers (IABs) are specialized cybercriminal actors who focus exclusively on gaining unauthorized access to corporate networks and then selling that access to other threat actors, most commonly ransomware affiliates. IABs represent a critical node in the cybercrime supply chain, effectively decoupling the intrusion phase from the monetization phase and enabling a division of labor that increases overall ecosystem efficiency. By purchasing pre-validated network access, ransomware operators and affiliates can skip the time-consuming and technically demanding initial intrusion phase, reducing their time-to-impact from weeks to hours.

The IAB marketplace operates primarily on Russian-language cybercrime forums, with Exploit, XSS, and RAMP being the most active venues. Access listings typically specify the victim's country, industry sector, revenue (a key pricing factor), access type (RDP, VPN, web shell, Active Directory credentials), and level of privilege. Pricing varies dramatically: basic RDP access to a small company might sell for $500-$2,000, while domain admin access to a Fortune 500 company can command $20,000-$50,000 or more. The median price for IAB listings is approximately $1,500-$3,000. Some IABs operate through private channels and maintain exclusive relationships with specific ransomware operations rather than selling on open forums.

The infostealer-to-IAB pipeline has become the dominant supply mechanism for access inventory. IABs systematically purchase or harvest infostealer logs from marketplaces (Russian Market, Telegram channels), filter for corporate VPN credentials (Cisco AnyConnect, Fortinet, Palo Alto GlobalProtect, Pulse Secure/Ivanti), validate access, escalate privileges where possible, and list the access for sale. This pipeline means that an employee's malware infection on a personal device (often via malvertising or a trojanized software download) can directly lead to a ransomware attack on their employer weeks or months later. Separately, IABs exploit vulnerabilities in internet-facing infrastructure (VPN appliances, Citrix, Exchange, firewalls) to harvest access at scale.

## Key Actors

| Actor/Handle | Forum Presence | Notable Characteristics | Status |
|-------------|---------------|------------------------|--------|
| Zebra2104 | Multiple forums | Prolific broker; sold access used by multiple ransomware groups | Active (intermittent) |
| Sang_real (Frapochka) | Exploit, XSS | High-volume listings across sectors; established reputation | Active |
| JETKITTEN | Exploit | Targets education and healthcare | Active |
| Roblette | Exploit, XSS | Specializes in European targets | Active |
| montns | RAMP, Exploit | Significant listing volume; medium-enterprise focus | Active |
| Bostaurus | Exploit | Specializes in access via VPN exploitation | Active |
| Various Telegram-based brokers | Telegram channels | Lower-tier access; often direct from infostealer operators | Active |
| Multiple unattributed IABs | Private channels | Operate exclusively through private deals with ransomware groups | Unknown |

Note: IAB handles change frequently. Actors rebrand, retire, and new actors enter the market regularly. The above represents observed active handles as of the review date but is not exhaustive.

## Current Activity

### Infostealer Log-to-Access Pipeline Acceleration (2024-2025)
The volume of corporate credentials available via infostealer logs has grown substantially, driven by the proliferation of Lumma, StealC, and other stealer families. IABs have industrialized the process of filtering corporate VPN/SSO credentials from bulk log purchases, validating access, and listing it for sale. Some have developed automated tools to test credentials at scale across VPN endpoints. The time from employee infostealer infection to corporate access listing has compressed, with some operations completing the cycle within days. KELA and Flare research indicates IAB listings have grown year-over-year, with several hundred active listings at any given time.

### Vulnerability Exploitation for Mass Access Harvesting
IABs increasingly exploit critical vulnerabilities in edge devices to build access inventory at scale. Exploited products in 2023-2025 include Citrix NetScaler/ADC (CVE-2023-4966 "Citrix Bleed"), Fortinet FortiOS (multiple CVEs), Ivanti Connect Secure (CVE-2024-21887/CVE-2023-46805), Cisco ASA/FTD, ConnectWise ScreenConnect (CVE-2024-1709), and Palo Alto PAN-OS (CVE-2024-3400). Actors exploit these vulnerabilities to deploy webshells or create accounts across hundreds of organizations, then sell individual accesses as listings. This approach provides inventory at near-zero marginal cost per victim.

### Ransomware-IAB Relationship Tightening
Evidence from ransomware chat leaks (Conti leaks 2022, Black Basta chat leak early 2025) confirms that ransomware operations maintain regular purchasing relationships with IABs, sometimes with standing arrangements for specific types of access (particular sectors, revenue thresholds, geographic regions). Some ransomware programs have dedicated budget lines for access purchases. Premium ransomware affiliates may negotiate exclusive first-look arrangements with top-tier IABs.

## Historical Events

| Date | Event | Impact |
|------|-------|--------|
| 2019-2020 | IAB market formalization | Dedicated access trading sections established on major forums (Exploit, XSS) |
| 2020 | RDP access sales surge during COVID | Remote work expansion massively increased available RDP/VPN attack surface |
| Feb 2022 | Conti leaks expose IAB purchases | Internal chat logs showed Conti's systematic purchase of access from IABs |
| 2022-2023 | RAMP forum gains prominence | Became significant IAB marketplace after its launch, attracting new sellers |
| Late 2023 | Citrix Bleed mass exploitation | CVE-2023-4966 exploited at scale; access to affected orgs appeared on forums within weeks |
| Jan 2024 | Ivanti Connect Secure mass exploitation | Multiple zero-days in VPN appliance exploited for broad access harvesting |
| Early 2025 | Black Basta chat leaks | Confirmed IAB purchasing patterns and pricing for major ransomware operation |
| 2024-2025 | IAB listing volumes increase | Research from KELA, Flashpoint, and Group-IB shows sustained growth in access listings |

## TTP Evolution

**Access Acquisition Methods**:
- *2019-2021*: Predominantly RDP brute-forcing, VPN credential stuffing from data breaches, and exploitation of common vulnerabilities (BlueKeep, Exchange ProxyLogon/ProxyShell).
- *2022-2023*: Shift toward infostealer log harvesting as primary credential source; increasing exploitation of VPN/edge device vulnerabilities.
- *2024-present*: Mature pipeline combining automated infostealer log processing with rapid exploitation of newly disclosed vulnerabilities in edge infrastructure. Some IABs specialize in one method or the other.

**Access Types Sold**:
- *RDP/VPN credentials*: Most common; buyer needs to do their own reconnaissance and privilege escalation.
- *Web shells*: Pre-deployed persistent access; common from vulnerability exploitation campaigns.
- *Domain admin/enterprise admin*: Premium access where IAB has already escalated privileges; commands highest prices.
- *Citrix/Remote Desktop Gateway*: Provides access to virtualized environments with broader reach.
- *Cloud/SaaS access*: Emerging category; M365 Global Admin, AWS root credentials from infostealer logs.
- *MSP/RMM access*: High value due to downstream access to MSP clients; treated as premium listings.

**Pricing Factors**: Access pricing correlates with: victim revenue (strongest factor), access type and privilege level, country (US/EU commands premium), sector (finance/insurance/healthcare premium), network size, and whether security tools were observed. Forum reputation and seller track record also affect willingness to pay. Auction formats are sometimes used for high-value access.

**Operational Security**: Established IABs use forum escrow services to protect both parties. Listings avoid naming victims directly, instead providing country, sector, revenue, number of hosts, and access type. Some use "check" services where potential buyers can verify the access is still valid before completing purchase. Communication for sensitive details moves to encrypted messengers (Tox, Jabber/XMPP).

## Ecosystem & Infrastructure Patterns

**Forum Marketplace Structure**: Major forums (Exploit, XSS, RAMP) have dedicated sections for access trading with established rules, escrow services, and reputation systems. Sellers build reputations through consistent delivery and positive buyer reviews. Some forums require initial deposits or vetting to participate. Listings follow semi-standardized formats specifying access parameters.

**Supply Chain Position**: IABs sit at the critical junction between initial compromise and monetization. Their upstream suppliers include: infostealer operators (providing raw credential logs), vulnerability researchers (providing exploits), and botnet operators (providing infected machine access). Their downstream customers include: ransomware affiliates (primary buyers), corporate espionage operators, data theft groups, and occasionally state-sponsored actors using criminal infrastructure.

**Seasonal and Event-Driven Patterns**: Access listings spike following major vulnerability disclosures in edge devices, as IABs race to exploit and list access before victims patch. Listing volumes also correlate with infostealer campaign waves. New ransomware programs entering the market can create demand spikes as affiliates stock up on access.

**Quality Assurance**: Sophisticated IABs provide "freshness" guarantees, confirming that access is validated within a recent timeframe. Some offer brief replacement guarantees if access becomes invalid shortly after sale. The most professional operations provide detailed information about the victim environment (domain structure, security tools observed, number of hosts) to help buyers assess the opportunity.

**Pricing Benchmarks** (approximate, based on public research):

| Access Type | Typical Price Range | Notes |
|------------|-------------------|-------|
| Basic RDP (single host, SMB) | $500-$2,000 | Most commoditized |
| VPN credentials (enterprise) | $1,000-$5,000 | Varies by company size |
| Domain Admin access | $5,000-$30,000 | Premium; ready for deployment |
| Citrix/RDS Gateway | $2,000-$10,000 | Broader network reach |
| MSP/RMM access | $5,000-$50,000+ | Multiplier effect on downstream clients |
| Fortune 500 / high revenue | $10,000-$50,000+ | Revenue-dependent premium |
| Web shell (large org) | $1,000-$5,000 | Requires further escalation |
| Cloud admin (M365/AWS) | $1,000-$10,000 | Emerging category |

## Tooling

| Tool | Category | Usage |
|------|----------|-------|
| Russian Market / 2easy | Log Sourcing | Purchasing infostealer logs to extract corporate credentials |
| Shodan/Censys | Reconnaissance | Identifying internet-exposed VPN/RDP/Citrix infrastructure |
| Exploit frameworks (Metasploit, custom) | Exploitation | Exploiting vulnerabilities in edge devices |
| Credential testing tools | Validation | Automated testing of harvested credentials against VPN endpoints |
| Brute-force tools (Hydra, custom) | Access | RDP/SSH brute-forcing (declining but still used) |
| Forum escrow services | Transaction | Protected payment for access trades |
| Tox/Jabber/XMPP | Communication | Encrypted messaging for transaction details |
| Cobalt Strike/Sliver | Post-Exploitation | Used by some IABs for privilege escalation before sale |
| BloodHound | AD Enumeration | Mapping Active Directory to assess access value |
| Cryptocurrency (BTC, XMR) | Payment | Primary payment methods for access purchases |

## Intelligence Gaps

- **Private deal volume**: A significant portion of IAB activity occurs through private channels and direct relationships rather than public forum listings. The ratio of public to private access trading is unknown but likely skews heavily toward private, meaning forum monitoring captures only a fraction of activity.
- **Time-to-exploitation**: The average elapsed time from an access listing appearing on forums to a ransomware affiliate purchasing and deploying ransomware is not well-characterized. Faster timelines reduce the defensive window.
- **IAB-ransomware attribution**: Connecting specific ransomware incidents to specific IAB listings/sellers is extremely difficult without insider access, law enforcement data, or ransomware group chat leaks. This limits disruption targeting.
- **Cloud access pricing and volume**: As corporate environments shift to cloud/SaaS, the IAB market for cloud admin access is emerging but not well-studied compared to traditional network access.
- **IAB operator demographics**: Beyond a few arrested individuals, the demographics, geographic distribution, and organizational structure of IAB operators remain largely unknown.
- **Infostealer log to access conversion rate**: What percentage of corporate credentials in infostealer logs are actually viable for network access (not expired, not MFA-blocked, etc.) is poorly quantified.

## Refreshing this picture

Use only the tools you actually have. When an integration is not connected, say which source was skipped instead of guessing its result. All monitoring here is passive, through commercial intelligence providers: never register on a forum, contact a seller, or buy or "check" an access.

**Access listings and broker activity**

- With the Intel 471 integration: `search_intel471_marketplaces` and `search_intel471_forum_posts` for access listings (search by sector, country, revenue band, access type such as "VPN", "RDP", "Citrix", "domain admin"); `list_intel471_actors` and `get_intel471_actor` for a broker handle and its history across forums; `search_intel471_instant_messages` for Telegram and other messenger chatter; `search_intel471_spot_reports` and `search_intel471_reports` for analyst reporting; `list_intel471_alerts` for configured watchers.
- With the Flashpoint integration: `search_flashpoint_communities` (forums, chat services and marketplaces), then `get_flashpoint_community_post` for one post; `search_flashpoint_reports`.
- With the Cybersixgill integration: `search_cybersixgill_intel_items` / `search_cybersixgill_intel_items_advanced`, then `enrich_cybersixgill_post` for one post and `enrich_cybersixgill_actor` for its author; `list_cybersixgill_alerts` for configured asset alerts.
- With the SOCRadar integration: `search_socradar_dark_web`; `list_socradar_incidents` for incidents raised on monitored assets.
- With the Google Threat Intelligence integration: `list_gti_dtm_alerts`, `get_gti_dtm_alert` and `search_gti_dtm_docs` (Digital Threat Monitoring over forums and markets).
- With the Recorded Future integration: `search_rf_threat_actors`, `search_rf_analyst_notes`, `list_rf_alerts`.

**Credential supply line**

- `search_rf_leaked_credentials`, `lookup_rf_identity_exposures` (Recorded Future), `search_flashpoint_credentials` (Flashpoint), `search_intel471_credentials` (Intel 471) for stealer-log exposure of the organization's VPN, SSO and remote-access accounts. Report counts and affected services, never plaintext secrets.

**Edge-vulnerability supply line**

- With the VulnCheck integration: `get_vulncheck_initial_access` (initial-access exploits by CVE), `get_vulncheck_initial_access_rules` (detection rules for them), `get_vulncheck_kev`, `get_vulncheck_exploits`, `list_vulncheck_ransomware`.
- With Vulnerability Intelligence: `check_vulnintel_kev`, `get_vulnintel_epss`, `assess_vulnintel_cve`.
- With GreyNoise: `lookup_greynoise_cve` and `query_greynoise_gnql` for mass scanning or exploitation of a CVE; with CrowdSec: `get_crowdsec_cve`, `get_crowdsec_cve_ips`, `list_crowdsec_exploitation_events`.
- With the Shodan integration: `search_shodan_hosts` / `count_shodan_hosts` (filter on the organization's netblocks, `org:` or `ssl:` names) and `get_shodan_host_info` to list the organization's internet-exposed VPN, RDP, Citrix and firewall interfaces and their versions; `get_shodan_domain_info` for subdomains pointing at remote-access portals; `list_shodan_alerts` for configured network monitors. Shodan is quota-sensitive: count before you search.
- Cross-check against the organization's exposure in OpenAEV (`search_openaev_findings`, `search_openaev_assets`) or the vulnerability tools connected to the platform, when available.

**What the organization already knows**

- OpenCTI (`search_opencti`, `list_opencti_entities` for Threat Actor or Individual entities representing brokers, `list_opencti_relationships`), MISP (`search_misp_events`), agent memory (`search_memory`).

## Assessing a listing

1. **Match the profile.** Compare country, sector, revenue band, headcount, number of hosts, access type and named technologies (VPN vendor, EDR observed) against the organization. Score the match (strong / partial / weak) and list what does not match.
2. **Check the supply line.** Look for stealer-log exposure of remote-access accounts and for unpatched edge vulnerabilities listed in the same window; either raises the likelihood that the listing is the organization.
3. **Rate the source.** Forum listings are seller claims: rate them F or C on the Admiralty reliability scale unless the seller has a verified track record (Intel 471 or Cybersixgill actor history), and 3-4 on credibility until corroborated.
4. **Act defensively.** Recommend credential resets and session revocation for exposed accounts, patching or isolation of the edge device, and a hunt for persistence (web shells, new accounts), and escalate to incident response when the match is strong. Do not contact the seller.

## Recording new findings

This body cannot be edited by the agent. When a run produces newer or corrected intelligence (a new broker handle, a venue closure, pricing changes):

- if the user or the assignment asks for it, record it in OpenCTI (`create_opencti_note`, `create_opencti_relationship`, or a report), never on your own initiative;
- otherwise store a short dated finding in agent memory with `store_memory` (handle or venue, claim, source, Admiralty rating, date), so later runs can find it with `search_memory`;
- in the answer, state which statements come from this cell (baseline, last reviewed 2026-04-05) and which come from live sources.

## Sources & References

1. KELA - "IAB Landscape Reports" and access listing tracking: https://www.kelacyber.com/
2. Flashpoint - "Initial Access Broker Intelligence": https://flashpoint.io/
3. Group-IB - "Hi-Tech Crime Trends: Initial Access Brokers": https://www.group-ib.com/
4. CrowdStrike - "Access Broker Tracking and ECrime Index": https://www.crowdstrike.com/
5. Mandiant - "FIN12 and Access Broker Relationships": https://www.mandiant.com/resources
6. Secureworks - "Initial Access Broker Marketplace Analysis": https://www.secureworks.com/
7. Digital Shadows (now ReliaQuest) - "IAB Marketplace Monitoring": https://www.reliaquest.com/
8. CISA - Known Exploited Vulnerabilities Catalog (edge device CVEs): https://www.cisa.gov/known-exploited-vulnerabilities-catalog

## Review History

| Date | Change | Source |
|------|--------|--------|
| 2026-04-05 | Initial creation with baseline intelligence through early 2025 | Analyst baseline |
