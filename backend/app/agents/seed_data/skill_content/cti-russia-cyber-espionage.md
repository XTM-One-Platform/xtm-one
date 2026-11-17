# Russia Cyber Espionage

Knowledge cell on Russian state-sponsored cyber operations: GRU-, SVR- and FSB-attributed actors, campaigns, TTPs, infrastructure and tooling. Use it as a baseline when the user asks about Russian state actors (APT28/Fancy Bear, Sandworm, Cozy Bear/APT29, Turla, etc.), GRU-affiliated hacktivist fronts (such as CARR or NoName057(16)), wartime ICS/OT campaigns, or pro-Russian information operations, then refresh it with live sources (see "Refreshing this picture").

**Freshness:** last reviewed 2026-04-05; seeded with open-source knowledge through early 2025. Overall confidence: moderate. This cell is read-only: anything newer than that date must come from a live, dated source and be cited as such. The hacktivist fronts named above are not profiled below: research them with the live sources.

## Executive Summary

Russia maintains one of the most capable and aggressive state-sponsored cyber operations programs globally, distributed across three primary intelligence services: the GRU (military intelligence), SVR (foreign intelligence), and FSB (federal security service). Each agency operates distinct threat groups with different mandates, tradecraft, and targeting priorities. GRU units are responsible for the most disruptive and destructive operations, including election interference, critical infrastructure attacks, and wiper malware campaigns. The SVR conducts sophisticated, long-term espionage operations exemplified by the SolarWinds supply chain compromise. The FSB focuses on domestic surveillance and regional operations, particularly targeting Ukraine and former Soviet states.

The Russia-Ukraine conflict that escalated in February 2022 dramatically reshaped Russian cyber operations. The conflict saw an unprecedented volume of wiper malware variants (WhisperGate, HermeticWiper, CaddyWiper, IsaacWiper, AcidRain, and others) deployed against Ukrainian targets, alongside persistent espionage campaigns targeting NATO member states, defense contractors, and humanitarian organizations. Sandworm (GRU Unit 74455) emerged as the most prolific operator in the conflict, conducting both destructive attacks and espionage operations. The tempo of operations revealed both the breadth of Russia's offensive capabilities and the limits of cyber operations as a standalone tool of coercion.

Beyond the Ukraine theater, Russian cyber actors continue to target Western governments, defense organizations, think tanks, political entities, and critical infrastructure. APT29's operations against cloud environments and identity providers represent a sophisticated evolution in targeting, while APT28 continues aggressive credential harvesting and exploitation campaigns against government and political targets across NATO nations. Gamaredon maintains high-volume but lower-sophistication operations focused almost exclusively on Ukrainian government entities, serving as a persistent harassment and intelligence collection tool for the FSB.

## Key Actors

| Threat Actor | Aliases | Attribution | Primary Targets | Status |
|---|---|---|---|---|
| APT28 | Fancy Bear, Forest Blizzard, Sofacy, Pawn Storm, Strontium | GRU Unit 26165 | Government, defense, political orgs (NATO states) | Active |
| APT29 | Cozy Bear, Midnight Blizzard, The Dukes, Nobelium | SVR | Government, technology, cloud providers, diplomats | Active |
| Sandworm | Seashell Blizzard, Iridium, Voodoo Bear, Electrum | GRU Unit 74455 | Critical infrastructure, Ukraine, elections | Active |
| Turla | Secret Blizzard, Venomous Bear, Snake, Krypton | FSB (Center 16) | Government, military, diplomatic (global) | Active |
| Gamaredon | Armageddon, Primitive Bear, Aqua Blizzard, Shuckworm | FSB (Crimea) | Ukrainian government and military | Active |
| Star Blizzard | Callisto, ColdRiver, Seaborgium | FSB (Center 18) | Think tanks, journalists, former intelligence officers | Active |
| Cadet Blizzard | DEV-0586 | GRU | Ukrainian government and IT sector | Active |
| Ember Bear | UNC2589 | GRU-linked | Ukrainian and Eastern European government | Active |

Status reflects the review date above. Alias mappings across vendors are approximate overlaps, not strict equivalences: say which vendor's naming you are using.

## Active Campaigns

### APT29 Cloud and Identity Infrastructure Targeting (2023-Present)

APT29 has systematically pivoted to targeting cloud environments, identity providers, and authentication infrastructure following the broad migration of government and enterprise workloads to cloud platforms. The group compromised Microsoft's corporate environment in late 2023 via a password spray attack on a legacy test tenant lacking MFA, then leveraged that access to exfiltrate email from senior Microsoft executives and access source code repositories. The campaign extended to other technology companies and U.S. government agencies using Microsoft 365. APT29 demonstrates particular expertise in abusing OAuth applications, service principals, and federated identity trusts. They exploit misconfigurations in Azure AD/Entra ID, manipulate SAML tokens, and leverage compromised identity providers for downstream access to multiple victim organizations.

### Sandworm Ukraine Conflict Operations (2022-Present)

Sandworm continues as the primary Russian cyber warfare operator in the Ukraine conflict, conducting a sustained campaign combining destructive attacks with intelligence collection. The group deployed multiple wiper malware families against Ukrainian government, energy, telecommunications, and financial sector targets. Notable operations include the April 2022 Industroyer2 attack targeting Ukrainian electrical substations (building on the precedent of the 2016 Industroyer/CrashOverride attack) and the AcidRain attack that disabled Viasat KA-SAT modems at the outset of the invasion. In 2023-2025, Sandworm increasingly combined destructive operations with espionage, using access to compromised networks for intelligence gathering before deploying wipers. The group has also expanded use of LOTL techniques and commodity tooling to supplement custom capabilities.

### Star Blizzard Credential Harvesting Campaigns (2023-Present)

Star Blizzard (formerly Callisto/ColdRiver) conducts persistent spear-phishing campaigns targeting current and former intelligence officials, government employees, think tank researchers, journalists, and NGO staff in the U.S. and U.K. The group creates elaborate social engineering pretexts, often impersonating known contacts or colleagues. Targets receive emails containing links to adversary-controlled credential harvesting pages mimicking legitimate email providers. Compromised accounts are used for intelligence collection, and in some cases stolen documents have been selectively leaked for influence operations. The DOJ and Microsoft jointly seized over 100 domains used by the group in October 2024.

## Historical Campaigns

### SolarWinds Supply Chain Compromise (2020-2021)

APT29 executed one of the most sophisticated supply chain attacks in history by compromising the build system of SolarWinds Orion, a widely deployed network management platform. The trojanized SUNBURST backdoor was distributed via legitimate software updates to approximately 18,000 organizations, with the SVR selectively exploiting access in approximately 100 high-value targets including U.S. Treasury, Commerce, State Department, DHS, and major technology firms. The operation demonstrated exceptional operational security, including dormancy periods, traffic blending with legitimate Orion communications, and anti-analysis checks. Post-compromise activity used TEARDROP and Raindrop loaders to deploy Cobalt Strike beacons.

### NotPetya (2017)

Sandworm deployed the NotPetya destructive malware via a compromised update to M.E.Doc, a Ukrainian tax accounting software. While disguised as ransomware, NotPetya was a wiper designed to cause maximum disruption. The malware spread laterally using EternalBlue and Mimikatz-based credential harvesting, escaping Ukraine's borders to cause an estimated $10+ billion in global damages. Maersk, Merck, FedEx/TNT Express, and Mondelez were among the most severely impacted. NotPetya remains the most destructive cyberattack in history and led to U.S., U.K., and EU attribution statements identifying the GRU.

### Ukraine Power Grid Attacks (2015-2016)

Sandworm conducted two pioneering attacks against Ukraine's power grid. The December 2015 attack on Kyivoblenergo and two other distribution companies used BlackEnergy malware for initial access and the KillDisk wiper, causing power outages for approximately 225,000 customers. The December 2016 attack deployed Industroyer/CrashOverride, the first known malware specifically designed to attack electrical grid control systems (ICS/SCADA), targeting the Pivnichna substation in Kyiv. These attacks demonstrated the real-world potential of cyber operations to disrupt critical infrastructure.

## TTP Evolution

Russian cyber operations have evolved significantly across several dimensions:

- **Wiper Proliferation**: The Ukraine conflict produced at least 10 distinct wiper malware families in 2022 alone, reflecting a strategic decision to develop disposable destructive tools rather than reuse detectable capabilities.
- **Cloud Pivot**: APT29 in particular has shifted focus from on-premises network exploitation to cloud identity abuse, OAuth manipulation, and attacks on federated authentication infrastructure.
- **Credential Harvesting at Scale**: Both APT28 and Star Blizzard rely heavily on credential phishing targeting webmail and cloud services, reflecting the value of email access for intelligence collection.
- **LOTL Adoption**: Russian groups increasingly use built-in OS tools, living-off-the-land binaries (LOLBins), and legitimate remote administration tools, particularly in the Ukraine theater.
- **Speed and Recklessness**: GRU operations in particular prioritize speed and impact over stealth, accepting the risk of detection and attribution in exchange for operational tempo.
- **Hack-and-Leak Operations**: Integration of cyber operations with information warfare, using stolen data for strategic leaks (e.g., DNC 2016, Star Blizzard operations).
- **Satellite and Communications Targeting**: AcidRain demonstrated willingness to target space-based communications infrastructure.
- **Ransomware Ecosystem Ties**: Circumstantial links between Russian intelligence services and ransomware groups, with some groups showing apparent tolerance from or tasking by state actors.

## Infrastructure Patterns

- Extensive use of compromised legitimate websites for C2 and watering hole attacks
- Tor network for operator anonymity and C2 communications
- Bulletproof hosting providers in jurisdictions with limited cooperation agreements
- Domain typosquatting and homoglyph domains for credential harvesting
- Compromised email accounts for spear-phishing delivery
- VPN services and residential proxy networks to geolocate traffic near targets
- PowerShell Empire, Cobalt Strike, and Sliver for post-exploitation
- Encrypted communication channels using custom protocols over HTTPS
- Infrastructure shared across operations with periodic rotation

## Tooling

| Tool | Type | Associated Actors | Notes |
|---|---|---|---|
| WhisperGate | Wiper (MBR/file) | Cadet Blizzard | Disguised as ransomware; deployed against Ukraine Jan 2022 |
| HermeticWiper | Wiper | Sandworm | Deployed hours before Feb 2022 invasion; abused EaseUS driver |
| CaddyWiper | Wiper | Sandworm | Lightweight file wiper deployed against Ukrainian targets |
| Industroyer2 | ICS malware | Sandworm | Updated variant of 2016 Industroyer; targets IEC-104 protocol |
| AcidRain | Wiper | Sandworm | Targeted Viasat KA-SAT satellite modems |
| SUNBURST | Supply chain backdoor | APT29 | Deployed via trojanized SolarWinds Orion updates |
| Brute Ratel C4 | C2 framework | APT29 | Commercial red team tool adopted for operations |
| GraphicalProton | Backdoor | APT28 | Uses Microsoft Graph API for C2 communications |
| Pterodo/Pteranodon | RAT | Gamaredon | High-volume VBScript/PowerShell backdoor; rapid iteration |
| Snake | Implant/P2P network | Turla | Sophisticated P2P covert communications network; disrupted by FBI in 2023 |

## Intelligence Gaps

- **GRU-ransomware nexus**: The precise relationship between Russian intelligence services and ransomware groups (safe harbor, tacit approval, active direction, or recruitment) remains poorly defined.
- **Pre-positioned access in Western infrastructure**: The extent of Russian pre-positioning in NATO member critical infrastructure outside Ukraine is largely unknown.
- **Post-Snake Turla operations**: Following the FBI's disruption of the Snake network in May 2023, Turla's current primary tooling and operational posture is not well characterized publicly.
- **Coordination across agencies**: The degree of operational coordination or competition between GRU, SVR, and FSB cyber units, particularly in the Ukraine theater.
- **Cyber reserve forces**: Russia's use of patriotic hackers, cyber militias, and private military company-affiliated cyber units alongside formal intelligence operations.
- **Lessons learned integration**: How Russian operators have adapted their approach based on operational successes and failures in the Ukraine conflict.

## Refreshing this picture

Before answering a question about current activity, check the connected sources for anything newer than the review date. Use only the tools you actually have; when an integration is not connected, say which source was skipped instead of guessing its result.

| Source | What to check | Tools |
|---|---|---|
| OpenCTI | The organisation's own view: Intrusion Set entities for the actors above, their latest reports, targeted sectors and countries, attributed malware | `search_opencti`, `list_opencti_entities` (Intrusion-Set, Campaign, Report), `get_opencti_entity`, `list_opencti_relationships`, `get_opencti_top_related` |
| MITRE ATT&CK | Group entries (e.g. G0007 APT28) and the techniques and software linked to them | `search_mitre_attack`, `get_mitre_attack_object`, `get_mitre_attack_related` |
| Feedly | Recent open-source reporting and trending threats naming the actors, their malware or hacktivist fronts | `search_feedly_entities`, `lookup_feedly_entity`, `get_feedly_entity_relationships`, `search_feedly_articles`, `get_feedly_trending_threats`, `query_feedly_cyberattacks` |
| Google Threat Intelligence | Mandiant-tracked actor and campaign collections, timeline, ATT&CK tree | `search_gti_threats`, `get_gti_threat`, `get_gti_threat_timeline`, `get_gti_threat_mitre_tree`, `get_gti_threat_related_objects` |
| CrowdStrike | Adversary profiles ("Bear" cryptonyms, e.g. Fancy Bear, Cozy Bear, Voodoo Bear), latest finished intelligence | `list_crowdstrike_actors`, `get_crowdstrike_actor`, `list_crowdstrike_intel_reports`, `download_crowdstrike_intel_report`, `list_crowdstrike_intel_indicators` |
| Recorded Future | Threat actor entities, threat map, Insikt analyst notes | `search_rf_threat_actors`, `lookup_rf_entity`, `get_rf_threat_map`, `search_rf_analyst_notes`, `get_rf_analyst_note` |
| Microsoft Defender TI | Microsoft "Blizzard" actor profiles and articles | `list_defender_intel_profiles`, `get_defender_intel_profile`, `list_defender_ti_articles` |

Rules for combining the cell with live sources:
- A dated live source beats this cell. When they disagree (status, attribution, new tooling), report the live finding with its date and source, and flag the difference.
- Rate each source (Admiralty) and give a confidence level for any attribution. Load the **Threat Actor Profiling** skill, if it is attached, for a full structured actor profile; the **Hacktivism** skill covers hacktivist fronts.
- Map new TTPs to ATT&CK IDs with the MITRE ATT&CK tools rather than from memory.
- Vendor finished intelligence (CrowdStrike, GTI, Recorded Future reports) is often TLP:AMBER or higher: cite report IDs internally, do not redistribute their bodies.

### Recording new findings

This built-in skill is read-only and is re-synced on every platform restart: never try to edit it. Record new findings instead:
- in agent memory (`store_memory`) as a dated note per actor ("2026-10: APT29 - new OAuth abuse technique reported by SOURCE"), and check it with `search_memory` before the next answer;
- in OpenCTI, only when the user or the assignment asks: a Note on the Intrusion Set (`create_opencti_note`), a relationship (`create_opencti_relationship`), or a Report.

## Sources & References

1. Microsoft Threat Intelligence: "Midnight Blizzard: Guidance for responders on nation-state attack" (January 2024)
2. CISA Advisory AA22-110A: "Russian State-Sponsored and Criminal Cyber Threats to Critical Infrastructure" (April 2022)
3. Mandiant: "APT29 Targets Microsoft 365 Environments" (2024)
4. ESET Research: "Industroyer2: Sandworm's Cyberwarfare Targets Ukraine's Power Grid Again" (April 2022)
5. SentinelLabs: "AcidRain: A Modem Wiper Rains Down on Europe" (March 2022)
6. DOJ Press Release: "Justice Department Announces Court-Authorized Disruption of Snake Malware Network" (May 2023)
7. CrowdStrike 2025 Global Threat Report: Russia-nexus adversary activity analysis
8. NSA/CISA/FBI Joint Advisory: "Russian GRU Conducting Global Brute Force Campaign" (2021)

## Change Log

| Date | Change | Source |
|---|---|---|
| 2026-04-05 | Initial cell creation; seeded with open-source knowledge through early 2025 | Training data |
