# China Cyber Espionage

Knowledge cell on Chinese state-sponsored cyber operations: MSS- and PLA-attributed actors, campaigns, TTPs, infrastructure and tooling. Use it as a baseline when the user asks about PRC-aligned actors (APT41, Volt Typhoon, Mustang Panda, APT10, APT31, Salt Typhoon, etc.), MSS/PLA-attributed campaigns, or PRC sector targeting, then refresh it with live sources (see "Refreshing this picture").

**Freshness:** last reviewed 2026-04-05; seeded with open-source knowledge through early 2025. Overall confidence: moderate. This cell is read-only: anything newer than that date must come from a live, dated source and be cited as such.

## Executive Summary

China operates the most extensive state-sponsored cyber espionage apparatus globally, with operations conducted primarily by the Ministry of State Security (MSS) and the People's Liberation Army (PLA). MSS-affiliated groups such as APT41, APT10, and APT31 focus on intellectual property theft, technology acquisition, and political espionage aligned with national strategic priorities including the Made in China 2025 initiative. PLA-affiliated groups, most notably Volt Typhoon, have shifted toward pre-positioning in critical infrastructure networks, representing an evolution from traditional espionage toward operational preparation for potential future conflict scenarios.

A defining characteristic of Chinese cyber operations is the blurring of lines between state-directed espionage and financially motivated cybercrime. APT41 exemplifies this dual-purpose model, conducting espionage operations on behalf of the MSS while simultaneously pursuing financially motivated intrusions including ransomware deployment and cryptocurrency theft. This complicates attribution and response. Chinese operators have increasingly adopted living-off-the-land (LOTL) techniques, abusing legitimate system tools and signed binaries to evade detection, a trend most prominently associated with Volt Typhoon's infiltration of U.S. critical infrastructure.

The 2023-2025 period saw significant escalation in Chinese cyber operations targeting telecommunications infrastructure. Salt Typhoon's compromise of major U.S. telecom providers exposed lawful intercept systems and call detail records of senior government officials, representing one of the most consequential intelligence collection operations in recent years. Concurrently, Mustang Panda continued aggressive targeting of government and diplomatic entities across Southeast Asia, reflecting Beijing's geopolitical priorities in the South China Sea region.

## Key Actors

| Threat Actor | Aliases | Attribution | Primary Targets | Status |
|---|---|---|---|---|
| APT41 | Winnti, Wicked Panda, Barium, Double Dragon | MSS (Chengdu) | Technology, gaming, healthcare, telecom | Active |
| APT10 | Stone Panda, MenuPass, Red Apollo | MSS (Tianjin) | MSPs, technology, aerospace, defense | Active |
| APT31 | Zirconium, Judgment Panda, Violet Typhoon | MSS (Wuhan) | Government, political entities, democracy activists | Active |
| Volt Typhoon | Bronze Silhouette, Vanguard Panda, DEV-0391 | PLA | U.S. critical infrastructure (energy, water, comms, transport) | Active |
| Salt Typhoon | GhostEmperor, FamousSparrow | MSS-linked | Telecommunications providers | Active |
| Mustang Panda | Bronze President, RedDelta, Earth Preta, Stately Taurus | MSS-linked | Government, NGOs (Southeast Asia, Europe) | Active |
| APT27 | Emissary Panda, Lucky Mouse, Iron Tiger, Budworm | MSS | Defense, technology, government | Active |
| APT3 | Gothic Panda, Buckeye, UPS Team | MSS (Guangdong) | Defense, aerospace, technology | Reduced activity |
| Hafnium | Silk Typhoon | MSS-linked | Email infrastructure, Exchange servers | Active |

Status reflects the review date above. Alias mappings across vendors are approximate overlaps, not strict equivalences: say which vendor's naming you are using.

## Active Campaigns

### Volt Typhoon Critical Infrastructure Pre-Positioning (2023-Present)

Volt Typhoon has maintained persistent access to U.S. critical infrastructure networks spanning energy, water treatment, telecommunications, and transportation sectors. The group employs almost exclusively LOTL techniques, using built-in Windows tools such as `wmic`, `ntdsutil`, `netsh`, and `PowerShell` to move laterally and maintain persistence. Initial access is typically achieved through exploitation of internet-facing network appliances, particularly Fortinet FortiGuard, Ivanti Connect Secure, Netgear ProSAFE, and SOHO routers. The campaign is assessed as pre-positioning for disruptive or destructive operations in the event of a major crisis or military conflict involving Taiwan. CISA issued multiple advisories (AA24-038A) coordinated with Five Eyes partners.

### Salt Typhoon Telecommunications Compromise (2024-Present)

Salt Typhoon conducted one of the most significant cyber espionage operations against U.S. telecommunications infrastructure, compromising networks of at least nine major providers including AT&T, Verizon, T-Mobile, and Lumen Technologies. The actors accessed lawful intercept systems, call detail records, and communications metadata of senior U.S. government and political figures. The operation exploited Cisco IOS XE vulnerabilities and leveraged legitimate network management protocols for lateral movement. The breach prompted an unprecedented FBI/CISA joint advisory urging adoption of end-to-end encrypted messaging.

### Mustang Panda Southeast Asia Diplomatic Targeting (2024-Present)

Mustang Panda continues aggressive spear-phishing campaigns against government ministries and diplomatic entities in the Philippines, Vietnam, Myanmar, Indonesia, and Mongolia. The group uses USB propagation malware and themed lure documents related to ASEAN summits and territorial disputes. Custom PlugX variants with DLL sideloading remain the primary payload, increasingly supplemented by TONESHELL and DOPLUGS backdoors. The campaigns align closely with Beijing's South China Sea territorial interests.

## Historical Campaigns

### APT10 Cloud Hopper (2016-2019)

APT10's "Cloud Hopper" campaign targeted managed service providers (MSPs) to gain indirect access to the networks of hundreds of organizations across at least 12 countries. By compromising MSP infrastructure, the group accessed client networks in aerospace, defense, healthcare, and technology sectors. The campaign demonstrated the strategic value of supply chain compromise and led to the 2018 DoJ indictment of two Chinese nationals associated with MSS operations in Tianjin. Tools included QuasarRAT, PlugX, and custom Scorpion and Haymaker implants.

### APT41 Supply Chain and Dual-Purpose Operations (2019-2022)

APT41 conducted multiple supply chain compromises including the ASUS Live Update and CCleaner attacks, alongside targeted intrusions into at least 14 countries. The group uniquely combined state-sponsored espionage with financially motivated operations including video game virtual currency theft and ransomware deployment. The 2020 DoJ indictment of five Chinese nationals revealed the group's connection to Chengdu 404 Network Technology, a front company linked to the MSS. APT41 also exploited Log4Shell, ProxyLogon, and other zero-days at speed.

### Hafnium/Silk Typhoon Exchange Server Exploitation (2021)

In early 2021, Hafnium conducted mass exploitation of four zero-day vulnerabilities in Microsoft Exchange Server (ProxyLogon, CVE-2021-26855 and related CVEs), compromising an estimated 250,000+ servers globally. The operation began as targeted espionage but rapidly escalated to mass exploitation once the vulnerabilities became public. The campaign prompted an emergency CISA directive and an unusual FBI operation to remotely remove web shells from compromised U.S. servers.

## TTP Evolution

Chinese cyber operations have undergone significant tactical evolution over the past five years:

- **LOTL Dominance**: Volt Typhoon pioneered the near-exclusive use of built-in operating system tools, avoiding custom malware entirely. This approach has spread to other Chinese groups, dramatically complicating detection.
- **Edge Device Targeting**: Systematic exploitation of network perimeter devices (VPN appliances, firewalls, routers) has become a hallmark. Ivanti, Fortinet, Citrix, and Cisco devices are consistently targeted, often with zero-day exploits.
- **Operational Relay Boxes (ORBs)**: Chinese groups increasingly route traffic through compromised SOHO routers and IoT devices to create proxy networks that obscure origin infrastructure, replacing traditional VPS-based C2.
- **Supply Chain Focus**: From Cloud Hopper to SolarWinds-adjacent operations, Chinese actors increasingly target upstream providers, software supply chains, and managed service providers.
- **Speed of Exploitation**: Chinese groups consistently demonstrate the ability to weaponize newly disclosed vulnerabilities within hours to days, often outpacing patch deployment cycles.
- **Reduced Malware Footprint**: A general shift away from signature-heavy custom malware toward fileless techniques, memory-only implants, and abuse of legitimate remote access tools.

## Infrastructure Patterns

- Compromised SOHO routers (particularly ASUS, Cisco, Netgear, TP-Link) used as operational relay boxes
- Leased infrastructure from U.S.-based hosting providers and cloud services to blend with legitimate traffic
- Domain fronting and CDN abuse for C2 communications
- Fast-flux DNS and dynamic DNS services for rapid infrastructure rotation
- Exploitation of legitimate cloud services (Azure, AWS, Google) for data staging and exfiltration
- VPN appliance implants that survive firmware updates and reboots
- Tor and multi-hop proxy chains for operator access to C2 infrastructure
- Use of code-signing certificates (often stolen) to sign malware and tooling

## Tooling

| Tool | Type | Associated Actors | Notes |
|---|---|---|---|
| ShadowPad | Modular backdoor | APT41, APT10, multiple MSS groups | Successor to PlugX; shared among MSS contractors |
| PlugX | RAT/backdoor | Mustang Panda, APT10, APT27, APT41 | Decades-old tool still actively developed; DLL sideloading delivery |
| Cobalt Strike | C2 framework | APT41, APT27, multiple groups | Widely used legitimate red team tool; cracked copies prevalent |
| TONESHELL | Backdoor | Mustang Panda | Custom shellcode loader with multiple C2 protocols |
| DOPLUGS | Backdoor loader | Mustang Panda | Enhanced PlugX variant with additional evasion |
| Winnti | Backdoor/rootkit | APT41 | Kernel-level rootkit for long-term persistence |
| China Chopper | Web shell | Multiple groups | Lightweight (~4KB) web shell; widely deployed post-exploitation |
| Deadeye/LOWKEY | Backdoor | APT41 | Passive backdoor activated by magic packet |
| KV Botnet | Botnet/proxy | Volt Typhoon | Comprised of compromised SOHO routers and firewalls |
| KEYPLUG | Backdoor | APT41 | Cross-platform (Windows/Linux) modular backdoor |

## Intelligence Gaps

- **Full scope of Volt Typhoon pre-positioning**: The true extent of compromised critical infrastructure remains unknown; confirmed cases likely represent a fraction of actual access.
- **Salt Typhoon data access**: The complete scope of data exfiltrated from telecom providers has not been publicly disclosed; intelligence damage assessment is ongoing.
- **MSS contractor ecosystem**: The full network of private companies and contractors supporting MSS cyber operations is poorly understood. Chengdu 404 and I-Soon are confirmed, but many others likely exist.
- **Zero-day acquisition pipeline**: The mechanisms by which Chinese groups acquire zero-day exploits (internal development, vulnerability research competitions like Tianfu Cup, or purchase) remain partially opaque.
- **Coordination between MSS and PLA operations**: The degree of operational coordination, deconfliction, and intelligence sharing between MSS and PLA cyber units is unclear.
- **AI integration**: The extent to which Chinese cyber operators are leveraging large language models and AI tools for vulnerability research, social engineering, and operational planning.

## Refreshing this picture

Before answering a question about current activity, check the connected sources for anything newer than the review date. Use only the tools you actually have; when an integration is not connected, say which source was skipped instead of guessing its result.

| Source | What to check | Tools |
|---|---|---|
| OpenCTI | The organisation's own view: Intrusion Set entities for the actors above, their latest reports, targeted sectors and countries, attributed malware | `search_opencti`, `list_opencti_entities` (Intrusion-Set, Campaign, Report), `get_opencti_entity`, `list_opencti_relationships`, `get_opencti_top_related` |
| MITRE ATT&CK | Group entries (e.g. G0096 APT41) and the techniques and software linked to them | `search_mitre_attack`, `get_mitre_attack_object`, `get_mitre_attack_related` |
| Feedly | Recent open-source reporting and trending threats naming the actors or their malware | `search_feedly_entities`, `lookup_feedly_entity`, `get_feedly_entity_relationships`, `search_feedly_articles`, `get_feedly_trending_threats`, `query_feedly_cyberattacks` |
| Google Threat Intelligence | Mandiant-tracked actor and campaign collections, timeline, ATT&CK tree | `search_gti_threats`, `get_gti_threat`, `get_gti_threat_timeline`, `get_gti_threat_mitre_tree`, `get_gti_threat_related_objects` |
| CrowdStrike | Adversary profiles ("Panda" cryptonyms), latest finished intelligence | `list_crowdstrike_actors`, `get_crowdstrike_actor`, `list_crowdstrike_intel_reports`, `download_crowdstrike_intel_report`, `list_crowdstrike_intel_indicators` |
| Recorded Future | Threat actor entities, threat map, Insikt analyst notes | `search_rf_threat_actors`, `lookup_rf_entity`, `get_rf_threat_map`, `search_rf_analyst_notes`, `get_rf_analyst_note` |
| Microsoft Defender TI | Microsoft "Typhoon" actor profiles and articles | `list_defender_intel_profiles`, `get_defender_intel_profile`, `list_defender_ti_articles` |

Rules for combining the cell with live sources:
- A dated live source beats this cell. When they disagree (status, attribution, new tooling), report the live finding with its date and source, and flag the difference.
- Rate each source (Admiralty) and give a confidence level for any attribution. Load the **Threat Actor Profiling** skill, if it is attached, for a full structured actor profile.
- Map new TTPs to ATT&CK IDs with the MITRE ATT&CK tools rather than from memory.
- Vendor finished intelligence (CrowdStrike, GTI, Recorded Future reports) is often TLP:AMBER or higher: cite report IDs internally, do not redistribute their bodies.

### Recording new findings

This built-in skill is read-only and is re-synced on every platform restart: never try to edit it. Record new findings instead:
- in agent memory (`store_memory`) as a dated note per actor ("2026-10: Volt Typhoon - new ORB cluster reported by SOURCE"), and check it with `search_memory` before the next answer;
- in OpenCTI, only when the user or the assignment asks: a Note on the Intrusion Set (`create_opencti_note`), a relationship (`create_opencti_relationship`), or a Report.

## Sources & References

1. CISA Advisory AA24-038A: "PRC State-Sponsored Actors Compromise and Maintain Persistent Access to U.S. Critical Infrastructure" (February 2024)
2. Microsoft Threat Intelligence: "Volt Typhoon targets US critical infrastructure with living-off-the-land techniques" (May 2023)
3. Mandiant APT41 Report: "Double Dragon: APT41, a Dual Espionage and Cyber Crime Operation" (2022)
4. CISA/FBI Joint Advisory: "People's Republic of China-Linked Actors Compromise Telecom Networks" (December 2024)
5. CrowdStrike 2025 Global Threat Report: China-nexus adversary activity analysis
6. Recorded Future Insikt Group: "Chinese State-Sponsored Cyber Espionage: Trends and Outlook" (2024)
7. DOJ Indictment: United States v. Zhang Haoran et al., APT41 members (September 2020)
8. Secureworks Counter Threat Unit: "Bronze Silhouette Targets U.S. Government and Defense Organizations" (2023)

## Change Log

| Date | Change | Source |
|---|---|---|
| 2026-04-05 | Initial cell creation; seeded with open-source knowledge through early 2025 | Training data |
