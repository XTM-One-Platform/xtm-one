# Iran Cyber Espionage

Knowledge cell on Iranian state-sponsored cyber operations: IRGC- and MOIS-attributed actors, campaigns, TTPs, infrastructure and tooling. Use it as a baseline when the user asks about IRGC/MOIS-aligned actors (APT35/Charming Kitten, APT34/OilRig, MuddyWater, Imperial Kitten, etc.), Iranian wiper campaigns, or hacktivist personas active in the Iran-Israel cyber conflict (such as Handala or Predatory Sparrow), then refresh it with live sources (see "Refreshing this picture").

**Freshness:** last reviewed 2026-04-05; seeded with open-source knowledge through early 2025. Overall confidence: moderate. This cell is read-only: anything newer than that date must come from a live, dated source and be cited as such. Individual hacktivist personas are not profiled below: research them with the live sources and attribute them with care, since several personas in this conflict are not Iranian at all.

## Executive Summary

Iran's state-sponsored cyber operations are conducted primarily by two organizations: the Islamic Revolutionary Guard Corps (IRGC) and the Ministry of Intelligence and Security (MOIS). These agencies operate distinct threat groups with overlapping but differentiated mandates. IRGC-affiliated groups (APT33, APT35) tend toward more aggressive and disruptive operations, including destructive attacks on Gulf state critical infrastructure and surveillance of diaspora dissidents. MOIS-affiliated groups (APT34, MuddyWater) typically focus on sustained espionage campaigns targeting regional rivals, with particular emphasis on government, energy, and telecommunications sectors across the Middle East and South Asia.

Iranian cyber capabilities have matured significantly since the Shamoon attacks of 2012, evolving from relatively unsophisticated defacement and DDoS campaigns to a diverse portfolio including credential harvesting at scale, custom backdoor development, supply chain compromise, and destructive wiper operations. A distinctive feature of Iranian operations is the sophistication of their social engineering tradecraft. Groups like APT35/Charming Kitten invest heavily in building elaborate fake personas, maintaining long-term social media presences, and conducting patient multi-stage engagement with targets before attempting credential theft or malware delivery. This human-intensive approach partially compensates for less advanced technical capabilities compared to Chinese or Russian counterparts.

The Israel-Hamas conflict beginning in October 2023 significantly escalated Iranian cyber operations against Israeli targets and organizations perceived as supporting Israel. Groups including Moses Staff, Agrius, and Charming Kitten intensified destructive attacks, data theft, and hack-and-leak operations. Iranian groups increasingly deployed ransomware not for financial gain but as a destructive tool and cover for espionage, a tactic that complicates attribution and response. Concurrently, Iranian-linked influence operations used fabricated hacktivist personas to amplify the impact of cyber operations and spread disinformation.

## Key Actors

| Threat Actor | Aliases | Attribution | Primary Targets | Status |
|---|---|---|---|---|
| APT33 | Elfin, Peach Sandstorm, Refined Kitten, Holmium | IRGC | Aviation, energy, defense (Saudi Arabia, U.S.) | Active |
| APT34 | OilRig, Hazel Sandstorm, Helix Kitten, Crambus | MOIS | Government, financial, telecom (Gulf states, Middle East) | Active |
| APT35 | Charming Kitten, Mint Sandstorm, Phosphorus, TA453 | IRGC (IO) | Academics, journalists, diplomats, dissidents, defense | Active |
| MuddyWater | Mercury, Mango Sandstorm, Static Kitten, Seedworm | MOIS | Government, telecom, defense (Middle East, South Asia) | Active |
| Moses Staff | Marigold Sandstorm | IRGC-linked | Israeli organizations (government, private sector) | Active |
| Agrius | Pink Sandstorm, DEV-0227 | MOIS-linked | Israel (diamond, technology, HR, insurance) | Active |
| Cotton Sandstorm | Neptunium, Emennet Pasargad | IRGC-linked | Election infrastructure, media, influence operations | Active |
| Scarred Manticore | DEV-0861 | MOIS | Telecom, government (Middle East) | Active |
| Imperial Kitten | Tortoiseshell, Crimson Sandstorm | IRGC-CEC | Defense, technology, maritime, logistics (Israel) | Active |

Status reflects the review date above. Alias mappings across vendors are approximate overlaps, not strict equivalences: say which vendor's naming you are using.

## Active Campaigns

### APT35/Charming Kitten Academic and Policy Targeting (2023-Present)

APT35 maintains persistent, high-volume social engineering campaigns targeting academics, think tank researchers, journalists, and current/former government officials in the U.S., U.K., and Israel. The group creates elaborate fake personas posing as fellow researchers, journalists, or conference organizers. Engagement typically begins with benign correspondence on legitimate platforms (email, LinkedIn, WhatsApp), gradually building trust over weeks before delivering a credential harvesting link or malware-laden document. Targets are selected based on their access to policy deliberations related to Iran, nuclear negotiations, and Middle East security. The group has demonstrated increasing interest in targeting individuals associated with presidential campaigns and national security decision-making. In 2024, the group successfully compromised email accounts associated with the Trump campaign.

### Peach Sandstorm/APT33 Password Spray and Defense Targeting (2023-Present)

APT33 has conducted large-scale password spray campaigns targeting thousands of organizations in the defense, satellite, pharmaceutical, and government sectors globally. Successful authentications are followed by lateral movement using AzureHound and Roadtools for Azure AD reconnaissance, followed by deployment of custom backdoors including the cross-platform EagleSpy and a new variant of FalseFont targeting defense industrial base organizations. The group has shown particular interest in space and satellite technology, defense contractors, and organizations involved in Middle East arms sales. Post-compromise activity includes extensive data collection focused on technical specifications and strategic planning documents.

### Agrius and Moses Staff Destructive Operations Against Israel (2023-Present)

Multiple Iranian groups intensified destructive operations against Israeli targets following the October 2023 conflict escalation. Agrius deployed updated variants of its Apostle and Fantasy wiper malware against Israeli organizations, while Moses Staff continued data theft and leak operations. These groups increasingly use ransomware (including BiBi-Linux and BiBi-Windows wipers named provocatively) as destructive tools rather than for financial gain. The operations are amplified through Telegram channels and fake hacktivist personas that claim credit and leak stolen data. Cotton Sandstorm has conducted influence operations targeting Israeli audiences through compromised streaming services and SMS campaigns.

## Historical Campaigns

### Shamoon/Disttrack Attacks (2012, 2016-2017)

The original Shamoon attack in 2012, attributed to Iran, destroyed approximately 35,000 workstations at Saudi Aramco by overwriting the master boot record, representing one of the most destructive cyberattacks against a single organization at that time. Follow-on Shamoon 2 attacks in 2016-2017 targeted additional Saudi and Gulf state organizations in the energy and government sectors. The attacks demonstrated Iran's willingness to conduct destructive operations against regional rivals and established wiper malware as a core element of Iranian cyber strategy.

### APT34 Tool Leaks and Operational Exposure (2019)

In 2019, an entity calling itself "Lab Dookhtegan" publicly leaked APT34's hacking tools, operational infrastructure details, and victim data on Telegram. The leaked tools included DNS-tunneling backdoors (DNSExfiltrator), webshells (HyperShell, TwoFace), and credential harvesters. The leak also exposed the identities of alleged MOIS officers. Despite this exposure, APT34 continued operations with retooled capabilities, demonstrating operational resilience. The incident highlighted tensions within Iranian intelligence and provided researchers with unprecedented visibility into MOIS cyber tradecraft.

### MuddyWater Telecommunications Targeting in the Middle East (2020-2023)

MuddyWater conducted sustained campaigns against telecommunications providers and government organizations across the Middle East, Turkey, and South Asia. The group's initial access methodology relied heavily on spear-phishing emails with malicious attachments exploiting document macros, later transitioning to LNK files and ISO images as Microsoft tightened macro execution defaults. Post-compromise tooling included the Atera and SimpleHelp legitimate remote management tools, alongside custom PowerShell-based implants like PowGoop and MuddyC2Go. The telecommunications targeting provided the MOIS with access to call records and communications metadata of strategic interest.

## TTP Evolution

Iranian cyber operations have evolved across multiple dimensions:

- **Social Engineering Sophistication**: Iranian groups, particularly APT35, have developed exceptionally patient and elaborate social engineering tradecraft, investing weeks or months in building trust with targets before attempting exploitation.
- **Ransomware as Destruction**: Multiple Iranian groups deploy ransomware not for financial gain but as a destructive tool and to complicate attribution by mimicking cybercriminal activity.
- **Fake Hacktivist Personas**: Iranian operations increasingly use fabricated hacktivist brands to claim responsibility for attacks and amplify their impact through social media and Telegram.
- **Cloud Targeting**: Groups like APT33 have developed expertise in Azure AD reconnaissance and exploitation, reflecting victim migration to cloud environments.
- **Legitimate Tool Abuse**: MuddyWater and others increasingly use legitimate remote management tools (Atera, SimpleHelp, ConnectWise) for persistence and C2, reducing reliance on custom malware.
- **Macro Bypass Evolution**: As Microsoft restricted macro execution, Iranian groups adapted to alternative initial access vectors including LNK files, ISO containers, HTML smuggling, and exploitation of internet-facing applications.
- **Wiper Diversification**: From Shamoon as a single wiper family, Iranian groups now maintain a diverse arsenal including Apostle, Fantasy, BiBi variants, and Scarred Manticore's targeted wipers.
- **Convergence with Influence Ops**: Increasing integration of cyber operations with information warfare, using stolen data for strategic leaks timed to maximize psychological impact.

## Infrastructure Patterns

- Adversary-controlled domains mimicking legitimate webmail providers (Google, Microsoft, Yahoo) for credential harvesting
- Use of compromised legitimate websites, particularly in the same region or sector as targets, for watering holes and C2
- Dynamic DNS providers (DDNS) for rapid infrastructure setup and rotation
- VPN services and anonymization tools for operator access
- Shared hosting infrastructure across multiple Iranian groups, suggesting common procurement or support functions
- Cloud storage services (Dropbox, OneDrive, Google Drive) for data exfiltration
- Telegram bots and channels for C2 communications and data exfiltration
- Infrastructure registered using fake personas with consistent patterns in registration data

## Tooling

| Tool | Type | Associated Actors | Notes |
|---|---|---|---|
| Shamoon/Disttrack | Wiper | APT33 | MBR wiper; used in destructive attacks on Gulf energy sector |
| BiBi Wiper | Wiper (Linux/Windows) | Moses Staff-linked | Named to provoke; deployed against Israeli targets post-Oct 2023 |
| Apostle/Fantasy | Ransomware/wiper | Agrius | Ransomware facade concealing destructive intent |
| POWERSTAR | Backdoor | APT35 | PowerShell-based backdoor with modular capabilities |
| BellaCiao | Backdoor | APT35 | .NET implant with custom DNS C2; tailored per victim |
| MuddyC2Go | C2 framework | MuddyWater | Go-based C2 replacing older PowGoop framework |
| LIONTAIL | Backdoor | Scarred Manticore | Passive implant using Windows HTTP stack driver |
| FalseFont | Backdoor | APT33 | Targets defense industrial base with credential theft |
| Sponsor | Backdoor | APT35 | Stores configuration in Windows registry to evade detection |
| Atera/SimpleHelp | RMM (legitimate) | MuddyWater | Abused legitimate remote management tools for persistence |

## Intelligence Gaps

- **IRGC-MOIS coordination**: The degree of coordination, shared resources, or competition between IRGC and MOIS cyber operations is poorly understood. Overlap in targeting suggests limited deconfliction.
- **Contractor ecosystem**: Iran's use of private companies and front organizations to conduct cyber operations (e.g., Emennet Pasargad, Najee Technology, Afkar System) requires further mapping.
- **Supply chain compromise capability**: Iran's demonstrated capability for supply chain attacks is limited compared to other state actors; whether this reflects lack of capability or strategic choice is unclear.
- **Destructive capability against critical infrastructure**: Beyond the energy sector, Iran's ability and willingness to conduct destructive operations against Western critical infrastructure (water, transportation, healthcare) is not well characterized.
- **Influence operation scale**: The full extent of Iranian information operations using fake personas and fabricated hacktivist entities is likely underestimated.
- **Post-conflict posture**: How Iranian cyber operations will evolve following the Israel-Hamas conflict -- whether escalated capabilities and targeting will persist or retract.

## Refreshing this picture

Before answering a question about current activity, check the connected sources for anything newer than the review date. Use only the tools you actually have; when an integration is not connected, say which source was skipped instead of guessing its result.

| Source | What to check | Tools |
|---|---|---|
| OpenCTI | The organisation's own view: Intrusion Set entities for the actors above, their latest reports, targeted sectors and countries, attributed malware | `search_opencti`, `list_opencti_entities` (Intrusion-Set, Campaign, Report), `get_opencti_entity`, `list_opencti_relationships`, `get_opencti_top_related` |
| MITRE ATT&CK | Group entries (e.g. G0049 OilRig) and the techniques and software linked to them | `search_mitre_attack`, `get_mitre_attack_object`, `get_mitre_attack_related` |
| Feedly | Recent open-source reporting and trending threats naming the actors, their malware or hacktivist personas | `search_feedly_entities`, `lookup_feedly_entity`, `get_feedly_entity_relationships`, `search_feedly_articles`, `get_feedly_trending_threats`, `query_feedly_cyberattacks` |
| Google Threat Intelligence | Mandiant-tracked actor and campaign collections, timeline, ATT&CK tree | `search_gti_threats`, `get_gti_threat`, `get_gti_threat_timeline`, `get_gti_threat_mitre_tree`, `get_gti_threat_related_objects` |
| CrowdStrike | Adversary profiles ("Kitten" cryptonyms, e.g. Charming Kitten, Imperial Kitten), latest finished intelligence | `list_crowdstrike_actors`, `get_crowdstrike_actor`, `list_crowdstrike_intel_reports`, `download_crowdstrike_intel_report`, `list_crowdstrike_intel_indicators` |
| Recorded Future | Threat actor entities, threat map, Insikt analyst notes | `search_rf_threat_actors`, `lookup_rf_entity`, `get_rf_threat_map`, `search_rf_analyst_notes`, `get_rf_analyst_note` |
| Microsoft Defender TI | Microsoft "Sandstorm" actor profiles and articles | `list_defender_intel_profiles`, `get_defender_intel_profile`, `list_defender_ti_articles` |

Rules for combining the cell with live sources:
- A dated live source beats this cell. When they disagree (status, attribution, new tooling), report the live finding with its date and source, and flag the difference.
- Rate each source (Admiralty) and give a confidence level for any attribution. Persona claims of responsibility on Telegram or social media are low-reliability by default until corroborated. Load the **Threat Actor Profiling** skill, if it is attached, for a full structured actor profile; the **Hacktivism** skill covers hacktivist personas.
- Map new TTPs to ATT&CK IDs with the MITRE ATT&CK tools rather than from memory.
- Vendor finished intelligence (CrowdStrike, GTI, Recorded Future reports) is often TLP:AMBER or higher: cite report IDs internally, do not redistribute their bodies.

### Recording new findings

This built-in skill is read-only and is re-synced on every platform restart: never try to edit it. Record new findings instead:
- in agent memory (`store_memory`) as a dated note per actor ("2026-10: MuddyWater - new RMM tool abused, reported by SOURCE"), and check it with `search_memory` before the next answer;
- in OpenCTI, only when the user or the assignment asks: a Note on the Intrusion Set (`create_opencti_note`), a relationship (`create_opencti_relationship`), or a Report.

## Sources & References

1. Microsoft Threat Intelligence: "Peach Sandstorm password spray campaigns enable intelligence collection at high-value targets" (September 2023)
2. Mandiant: "APT42: Crooked Charms, Cons, and Compromises" (September 2022)
3. CISA Advisory AA22-055A: "Iranian Government-Sponsored Actors Conduct Cyber Operations Against Global Government and Commercial Networks" (2022)
4. CrowdStrike: "Imperial Kitten Deploys Novel Malware Families in Middle East-Focused Operations" (2023)
5. Check Point Research: "Scarred Manticore's LIONTAIL: A New Passive Implant Framework" (2023)
6. FBI Flash Alert: "Iranian Cyber Group Emennet Pasargad Conducting Hack-and-Leak Operations" (2024)
7. Proofpoint: "TA453 Uses LNK Files and Mac Malware in Social Engineering Operations" (2023)
8. Recorded Future Insikt Group: "Iran's Cyber Threat Activities: Trends and Outlook" (2024)

## Change Log

| Date | Change | Source |
|---|---|---|
| 2026-04-05 | Initial cell creation; seeded with open-source knowledge through early 2025 | Training data |
