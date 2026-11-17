# DPRK Cyber Operations

> **Knowledge cell.** Baseline knowledge through early 2025, last reviewed **2026-04-05**. Overall confidence: **moderate**.
> This body is a read-only built-in skill: it is re-synced on every platform restart and does not update itself. Before you describe anything as "current", check the live sources listed in **Refreshing this picture**, and say how fresh each statement is.

## How to use this cell

- Use it as the baseline picture of North Korean state-sponsored cyber operations: actor clusters, revenue-generation campaigns, the IT-worker scheme, cryptocurrency and supply-chain targeting, tooling and intelligence gaps.
- Treat every "Active" status, figure and campaign as of the review date above. Newer reporting found with the integrations overrides this cell; say so explicitly when it does.
- Express judgments with a confidence level and calibrated likelihood words (almost certainly, likely, roughly even chance, unlikely...). For a full structured actor profile, load the **Threat Actor Profiling** skill if it is attached; for ATT&CK technique IDs, the **MITRE ATT&CK Reference** skill.

## Executive Summary

North Korea operates a uniquely structured cyber program where revenue generation and intelligence collection are equally prioritized national objectives. Under the Reconnaissance General Bureau (RGB), DPRK cyber operators have stolen an estimated $3-6 billion in cryptocurrency and financial assets since 2017, making the cyber program one of the regime's primary sources of hard currency and a critical enabler of its nuclear and ballistic missile programs. This dual-purpose model distinguishes DPRK operations from all other nation-state cyber programs, where espionage is typically the primary objective and financial operations are secondary or opportunistic.

The DPRK cyber ecosystem is organized into several distinct clusters operating under the RGB umbrella. Lazarus Group (Diamond Sleet/HIDDEN COBRA) is the most broadly capable unit, conducting both espionage operations against defense and aerospace targets and major financial theft operations. APT38/BlueNoroff specializes in financial sector targeting, including SWIFT network attacks and cryptocurrency platform compromise. Kimsuky (Emerald Sleet) focuses on espionage targeting South Korean government, research institutions, think tanks, and North Korea policy experts. Andariel (Onyx Sleet) conducts espionage against defense and nuclear technology targets, with secondary ransomware operations. These groups share tooling, infrastructure, and techniques while maintaining distinct operational mandates.

The period from 2022-2025 has been defined by three major trends: an explosive escalation in cryptocurrency theft (including the $625 million Ronin Network hack and the $1.5 billion Bybit exchange compromise), the expansion of the IT worker fraudulent employment scheme generating an estimated $250-600 million annually, and increasingly sophisticated supply chain attacks exemplified by the 3CX compromise. DPRK operators have demonstrated remarkable adaptability, pivoting quickly to target emerging DeFi protocols, cryptocurrency bridges, and Web3 infrastructure while maintaining persistent espionage campaigns against defense industrial base and nuclear technology targets aligned with regime weapons program priorities.

## Key Actors

| Threat Actor | Aliases | Attribution | Primary Targets | Status |
|---|---|---|---|---|
| Lazarus Group | Diamond Sleet, HIDDEN COBRA, Zinc, Labyrinth Chollima | RGB 3rd Bureau | Defense, aerospace, cryptocurrency, banks | Active |
| APT38/BlueNoroff | Sapphire Sleet, Stardust Chollima, Nickel Academy | RGB | SWIFT/banking, cryptocurrency exchanges, DeFi | Active |
| Kimsuky | Emerald Sleet, Velvet Chollima, Thallium, Black Banshee | RGB 5th Bureau | South Korean government, think tanks, academics, NK policy experts | Active |
| Andariel | Onyx Sleet, Silent Chollima, Plutonium, DarkSeoul | RGB 3rd Bureau | Defense, nuclear, aerospace, healthcare (ransomware) | Active |
| TraderTraitor | Jade Sleet, UNC4899 | RGB-linked | Cryptocurrency developers, blockchain companies | Active |
| Citrine Sleet | DEV-0139 | RGB-linked | Cryptocurrency traders, financial technology | Active |
| APT43 | Kimsuky sub-cluster, Springtail | RGB | Crypto, South Korean policy targets | Active |
| ScarCruft | Reaper, Ricochet Chollima, InkySquid, APT37 | MSS (State Security) | South Korean government, defectors, journalists, human rights | Active |

Vendor naming note: CrowdStrike uses the "Chollima" cryptonym for DPRK state-nexus actors and Microsoft the "Sleet" weather family. Resolve aliases before counting activity, so one cluster is not reported as two.

## Active Campaigns

### Cryptocurrency Exchange and DeFi Platform Theft (2023-Present)

DPRK-linked operators have dramatically escalated cryptocurrency theft operations, with estimated annual proceeds exceeding $1.5 billion. The February 2025 Bybit exchange hack alone netted approximately $1.5 billion, representing the single largest cryptocurrency theft in history. Operators exploit vulnerabilities in smart contracts, compromise developer credentials via social engineering, and target cryptocurrency bridge protocols. The TraderTraitor cluster specifically targets blockchain developers through fake job offers containing trojanized Node.js projects and cryptocurrency trading applications. Stolen funds are laundered through chain-hopping, Tornado Cash and other mixers, peer-to-peer exchanges, and a network of complicit OTC brokers, primarily in China. The UN Panel of Experts has documented these operations funding DPRK weapons of mass destruction programs.

### IT Worker Fraudulent Employment Scheme (2022-Present)

Thousands of DPRK IT workers operate under false identities to secure remote employment contracts at technology companies globally, primarily in the United States, Europe, and Japan. Workers use stolen or synthetic identities, U.S.-based laptop farms (managed by facilitators who receive company-issued laptops on behalf of the remote workers), and AI-assisted video interview tools to appear as legitimate contractors. These workers generate an estimated $250-600 million annually in salary revenue for the regime. Beyond revenue, the access provides intelligence collection opportunities and potential for insider-enabled cyberattacks. The DOJ has indicted multiple facilitators and the FBI has issued repeated advisories. Companies impacted include Fortune 500 firms and well-funded startups across the software development, cryptocurrency, and technology sectors.

### Lazarus Defense and Nuclear Espionage (2023-Present)

Lazarus Group and Andariel continue persistent espionage campaigns against defense contractors, aerospace companies, and nuclear technology organizations in the U.S., South Korea, Japan, Europe, and India. Initial access is frequently achieved through fabricated recruiter personas on LinkedIn offering fake job opportunities at prestigious defense companies. Targets receive weaponized job descriptions or skills assessments containing custom backdoors. The group also exploits known vulnerabilities in enterprise software (Zoho ManageEngine, JetBrains TeamCity, Atlassian Confluence) for initial access. Collected intelligence directly supports DPRK ballistic missile, nuclear weapons, submarine, and drone development programs.

## Historical Campaigns

### 3CX Supply Chain Attack (2023)

In March 2023, Lazarus Group compromised the software build pipeline of 3CX, a widely used enterprise VoIP/PBX provider with approximately 600,000 customer organizations and 12 million daily users. The attack was notable as a cascading supply chain compromise: Lazarus first compromised Trading Technologies, a financial trading software company, and used that access to target a 3CX employee via a trojanized X_TRADER application. The compromised 3CX desktop application distributed the TAXHAUL/SIMPLESEA backdoor. This represented the first publicly documented instance of one supply chain attack enabling another, demonstrating the increasing sophistication of DPRK software supply chain operations.

### Bangladesh Bank SWIFT Heist (2016)

APT38 attempted to steal $951 million from the Bangladesh Bank's account at the Federal Reserve Bank of New York by injecting fraudulent SWIFT transfer messages. A spelling error in one transfer request ("fandation" instead of "foundation") triggered scrutiny that limited actual losses to $81 million, which was routed through Philippine casinos. The operation demonstrated DPRK's willingness and ability to target the global financial infrastructure. The attack involved months of reconnaissance, custom SWIFT manipulation malware (NESTEGG, DYEPACK), and knowledge of bank clearing processes. It spurred a global overhaul of SWIFT security controls.

### Ronin Network/Axie Infinity Hack (2022)

In March 2022, Lazarus Group stole approximately $625 million in Ethereum and USDC from the Ronin Network, a blockchain bridge supporting the Axie Infinity game. The attack exploited compromised private keys of validator nodes, obtained through a social engineering campaign involving a fake job offer sent to a senior Sky Mavis engineer via LinkedIn. The operation demonstrated the convergence of social engineering sophistication with deep understanding of blockchain architecture and DeFi protocols. The FBI attributed the theft to Lazarus Group and Treasury's OFAC sanctioned the associated wallet addresses.

## TTP Evolution

DPRK cyber operations have undergone significant evolution:

- **Cryptocurrency Specialization**: From traditional banking (SWIFT) to comprehensive cryptocurrency targeting including exchanges, DeFi protocols, bridges, hot wallets, and individual high-value holders. Operators demonstrate deep understanding of blockchain technology and smart contract vulnerabilities.
- **Social Engineering via Professional Networks**: LinkedIn-based fake recruiter campaigns have become the primary initial access vector for both espionage and financial operations. The fake job offer lure is remarkably effective and consistently refined.
- **Supply Chain Attacks**: Progression from direct targeting to sophisticated supply chain compromises (3CX), including cascading supply chain attacks that chain multiple compromises.
- **macOS Targeting**: Increasing development of macOS-specific malware and payloads, reflecting the prevalence of macOS in cryptocurrency developer and technology company environments.
- **AI-Assisted Operations**: Use of AI tools for generating realistic personas, conducting video interviews (IT worker scheme), writing code, and potentially automating aspects of cryptocurrency laundering.
- **Insider Threat Model**: The IT worker scheme represents a fundamentally different threat model: obtaining legitimate authorized access rather than exploiting vulnerabilities.
- **Living-off-the-Land**: Growing use of native operating system tools and legitimate software for post-exploitation, supplementing custom tooling.
- **Rapid Laundering**: Development of increasingly sophisticated and rapid cryptocurrency laundering pipelines to convert stolen assets before they can be frozen.

## Infrastructure Patterns

- VPN services and commercial proxy networks for operator anonymity
- Compromised web servers (particularly WordPress and small business websites) for C2 staging
- GitHub, GitLab, and other code repositories for malware delivery via fake projects
- NPM/PyPI package poisoning for supply chain delivery
- Cloud hosting services (AWS, Azure, DigitalOcean) for infrastructure that blends with legitimate traffic
- Domain registration patterns using cryptocurrency-themed or recruitment-themed domains
- Use of legitimate communication platforms (Telegram, Slack, Discord) for C2
- Extensive network of cryptocurrency wallets, mixers, and chain-hopping infrastructure for money laundering
- U.S.-based laptop farms (residential addresses) for IT worker scheme
- VoIP numbers and virtual phone services for synthetic identity support

## Tooling

| Tool | Type | Associated Actors | Notes |
|---|---|---|---|
| AppleJeus | Cryptocurrency trojan | Lazarus, BlueNoroff | Trojanized crypto trading apps targeting macOS and Windows |
| HOPLIGHT | Backdoor | Lazarus | Custom tunneling tool for proxy communications |
| BLINDINGCAN/DTrack | RAT | Lazarus, Andariel | Full-featured RAT with keylogging, screen capture, file exfiltration |
| TAXHAUL/SIMPLESEA | Loader/backdoor | Lazarus | Deployed via 3CX supply chain compromise |
| BeaverTail | Infostealer | TraderTraitor | JavaScript-based stealer targeting crypto wallets and browser credentials |
| InvisibleFerret | Backdoor | TraderTraitor | Python-based backdoor deployed alongside BeaverTail |
| FastCash | ATM malware | APT38 | Intercepts ISO 8583 transactions to authorize fraudulent cash withdrawals |
| ELECTRICFISH | Tunneling | Lazarus | Custom tunneling/proxy tool for maintaining covert communications |
| KANDYKORN | macOS backdoor | BlueNoroff | Full-featured macOS RAT targeting crypto developers |
| RandomQuery/FlowerPower | Reconnaissance | Kimsuky | Information collection tools deployed via spear-phishing |

## Intelligence Gaps

- **Full IT worker infiltration scope**: The true number of DPRK IT workers employed at Western companies and the extent of their intelligence access is unknown. Detected cases are likely a fraction of the total.
- **Cryptocurrency laundering networks**: The complete infrastructure of OTC brokers, facilitators, and conversion mechanisms used to launder billions in stolen cryptocurrency is only partially mapped.
- **Revenue allocation**: How stolen funds flow from RGB cyber units to weapons programs, regime leadership, and operational reinvestment is poorly characterized.
- **Training pipeline**: Where and how DPRK cyber operators are trained (domestically, in China, in Russia), the size of the workforce, and the specialization pipeline is opaque.
- **Chinese facilitation**: The extent of Chinese individuals and entities facilitating DPRK cyber operations (infrastructure, laundering, identity documents) requires further investigation.
- **Zero-day capability**: Whether DPRK groups develop zero-day exploits internally or acquire them from brokers or allied states is unclear. Their reliance on social engineering and N-day exploitation may mask developing capabilities.
- **AI tool adoption**: The degree to which DPRK operators leverage LLMs and AI tools for code generation, social engineering content, and operational planning is emerging.

## Refreshing this picture

Use only the tools you actually have. When an integration is not connected, say which source was skipped instead of guessing its result. Rate each source you cite (Admiralty scale, for example B2 for an established vendor report, F3 for an unverified forum claim), keep the vendor's TLP, and give the date of the newest item you relied on.

**Actor profiles and latest vendor reporting**

- With the CrowdStrike integration: `list_crowdstrike_actors` (filter on North Korean origin to enumerate the DPRK adversaries CrowdStrike tracks), `get_crowdstrike_actor` for one actor ("Labyrinth Chollima", "Stardust Chollima", "Velvet Chollima", "Silent Chollima": origins, target countries and industries, motivations, capability, aliases), `list_crowdstrike_intel_reports` and `download_crowdstrike_intel_report` for the latest reporting, `list_crowdstrike_intel_indicators` for current indicators. CrowdStrike report bodies are typically TLP:AMBER or higher: cite report IDs internally and do not redistribute.
- With the Google Threat Intelligence integration: `search_gti_threats` and `get_gti_threat` for the actor or campaign, `get_gti_threat_timeline` for recent activity, `get_gti_threat_mitre_tree` for TTPs, `get_gti_threat_related_objects` and `search_gti_threat_iocs` for associated malware and indicators.
- With the Recorded Future integration: `search_rf_threat_actors`, `lookup_rf_entity`, `search_rf_analyst_notes`, `get_rf_threat_map`.
- With the Intel 471 integration: `list_intel471_actors`, `get_intel471_actor`, `search_intel471_reports`. With Microsoft Defender: `list_defender_intel_profiles`, `get_defender_intel_profile`, `list_defender_ti_articles` (Microsoft "Sleet" names).
- With the Feedly integration: `lookup_feedly_entity`, `get_feedly_entity_relationships`, `search_feedly_articles` for recent open reporting on the actor, malware or campaign.

**What the organization already knows**

- With the OpenCTI integration: `search_opencti` or `list_opencti_entities` for the Intrusion Set, `get_opencti_entity`, `list_opencti_relationships` and `get_opencti_top_related` for linked malware, campaigns, targeted sectors and countries, `get_opencti_entity_stats` for reporting volume over time, `query_opencti_pir_entities` to check whether a Priority Intelligence Requirement covers DPRK activity.
- With the MISP integration: `search_misp_events`, `list_misp_galaxy_clusters`.
- Agent memory: `search_memory` for findings recorded in earlier runs.

**TTPs and tooling**

- With the MITRE ATT&CK integration: `search_mitre_attack` for the group (G0032 Lazarus Group, G0082 APT38, G0094 Kimsuky, G0138 Andariel, G0067 APT37), `get_mitre_attack_object`, and `get_mitre_attack_related` for the techniques and software attributed to it.
- With the abuse.ch integration: `search_abusech_malware` and `search_abusech_iocs` for recent samples and IOCs tagged with the malware families above (for example BeaverTail, InvisibleFerret). With VirusTotal: `search_vt_intelligence` and `get_vt_related_objects` to pivot on a sample.

**Open sources**

- `search_google_web`, `search_newsapi_articles`, `search_rss_entries` and `web_fetch` for government advisories (FBI, CISA, Treasury/OFAC, DOJ indictments), UN Panel of Experts reports and blockchain-analytics publications. There is no blockchain-analytics integration: wallet tracing and laundering figures come from published reports only.

## Recording new findings

This body cannot be edited by the agent. When a run produces newer or corrected intelligence:

- if the user or the assignment asks for it, record it in OpenCTI (`create_opencti_note` on the Intrusion Set, `create_opencti_relationship` for a new link, or a report), never on your own initiative;
- otherwise store a short dated finding in agent memory with `store_memory` (actor, claim, source, Admiralty rating, date), so later runs can find it with `search_memory`;
- in the answer, state which statements come from this cell (baseline, last reviewed 2026-04-05) and which come from live sources.

## Sources & References

1. FBI/CISA/Treasury Joint Advisory: "TraderTraitor: North Korean State-Sponsored APT Targets Blockchain Companies" (April 2022)
2. Mandiant: "APT43: North Korean Group Uses Cybercrime to Fund Espionage Operations" (March 2023)
3. CrowdStrike: "LABYRINTH CHOLLIMA's 3CX Supply Chain Operation" (April 2023)
4. Chainalysis: "2024 Crypto Crime Report: North Korea-Linked Cryptocurrency Theft" (2024)
5. Microsoft Threat Intelligence: "Diamond Sleet supply chain compromise distributes a modified CyberLink installer" (November 2023)
6. UN Panel of Experts Report S/2024/215: DPRK sanctions implementation findings on cyber-enabled theft
7. FBI Public Service Announcement: "North Korean IT Workers Infiltrate U.S. Companies" (October 2023)
8. Google TAG/Mandiant: "A Cascade of Compromises: Unveiling Lazarus' New Campaign" (3CX analysis, 2023)

## Review History

| Date | Change | Source |
|---|---|---|
| 2026-04-05 | Initial cell creation; seeded with knowledge through early 2025 | Analyst baseline |
