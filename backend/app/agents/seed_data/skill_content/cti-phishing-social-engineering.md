# Phishing and Social Engineering

Knowledge cell on phishing and social engineering: Adversary-in-the-Middle (AitM) phishing kits and Phishing-as-a-Service (PhaaS) platforms, MFA bypass, Business Email Compromise (BEC), quishing, vishing and callback phishing, consent / device-code phishing, AI-enabled impersonation, and the state and criminal actors that rely on them. Use it as background for assessments, campaign write-ups and awareness material; refresh it with the tools listed in "Refreshing this picture" before you state anything as current.

**Last reviewed:** 2026-04-05 (baseline intelligence through early 2025). **Overall confidence:** moderate. This cell is read-only and is not updated automatically: treat every "Active" status below as a starting hypothesis to verify, and say how old the baseline is when you rely on it.

## Executive Summary

Phishing and social engineering remain the most common initial access vectors across both cybercriminal and state-sponsored operations. The landscape has undergone significant evolution beyond traditional email-based credential harvesting. Adversary-in-the-Middle (AitM) phishing platforms — notably EvilProxy and Evilginx — now enable real-time interception of credentials AND session tokens, effectively bypassing most forms of multi-factor authentication (MFA). This development has fundamentally altered the threat model for organizations relying on MFA as a primary defense, shifting the security boundary toward phishing-resistant MFA methods such as FIDO2/WebAuthn hardware keys.

The Phishing-as-a-Service (PhaaS) market has matured substantially, with platforms like EvilProxy, Greatness, Tycoon 2FA, and others offering turnkey AitM phishing kits with subscription models, customer support, and real-time dashboards. These services target Microsoft 365 accounts disproportionately, reflecting the platform's dominance in enterprise environments. Pricing ranges from $200-$1,000+ per month depending on features and target capabilities. Meanwhile, Business Email Compromise (BEC) continues to generate the highest financial losses of any cybercrime category according to FBI IC3 data — over $2.9 billion in reported losses in 2023 alone — despite being less technically sophisticated than other threats.

Emerging vectors include QR code phishing ("quishing"), which evades email security gateways by embedding malicious URLs in images; vishing and callback phishing campaigns that move victims to phone conversations to bypass email controls; AI-generated deepfake voice and video for executive impersonation; and consent phishing that abuses OAuth application permissions to gain persistent access to cloud mailboxes. The combination of AI-enhanced content generation (eliminating grammatical tells), sophisticated infrastructure (legitimate cloud hosting, CAPTCHA protection, geofencing), and AitM session hijacking has made modern phishing significantly harder to detect and defend against.

## Key Actors

| Actor/Platform | Type | Notable Characteristics | Status (baseline) |
|---------------|------|------------------------|--------|
| EvilProxy | PhaaS Platform | Leading AitM phishing service; targets M365, Google, etc.; subscription-based | Active |
| Tycoon 2FA | PhaaS Platform | AitM kit; heavy use of CAPTCHAs and anti-analysis to evade detection | Active |
| Greatness | PhaaS Platform | M365-focused; easy setup; gained popularity 2023-2024 | Active |
| Evilginx | Open-Source Tool | Open-source AitM framework by Kuba Gretzky; basis for many PhaaS platforms | Active (tool) |
| Storm-1167/Star Blizzard | State-Sponsored (Russia) | Sophisticated spearphishing targeting government, academia, think tanks | Active |
| Scattered Spider | Cybercrime Collective | Expert social engineering; vishing help desk staff; SIM swapping; young Western actors | Active |
| Midnight Blizzard (APT29) | State-Sponsored (Russia) | Teams phishing, token theft; targeted Microsoft itself | Active |
| Various BEC Networks | Organized Fraud | West African and Eastern European BEC operations; increasingly use deepfakes | Active |
| Kimsuky (APT43) | State-Sponsored (DPRK) | Prolific credential harvesting targeting Korea experts, think tanks, journalists | Active |
| Muddled Libra | Cybercrime | Overlaps with Scattered Spider; targets BPO/telecom for downstream access | Active |

Naming note: vendor names for the same cluster differ (for example Scattered Spider / Muddled Libra). Check aliases with the MITRE ATT&CK or OpenCTI tools below before you merge or split actors in a product.

## Current Activity (baseline)

### AitM Phishing at Scale (2024-2025)
AitM phishing kits have become the dominant method for compromising enterprise accounts protected by traditional MFA (push notifications, SMS codes, TOTP). Platforms like EvilProxy and Tycoon 2FA operate as reverse proxies, relaying victim interactions to legitimate login pages in real time while capturing both credentials and session cookies. Campaigns routinely target thousands of organizations simultaneously, with phishing emails delivered from compromised legitimate accounts to bypass reputation-based filtering. Session cookies captured via AitM enable immediate account takeover without triggering new-device MFA prompts.

### QR Code Phishing ("Quishing") Surge
Starting in late 2023 and accelerating through 2024-2025, QR code-based phishing has surged. Attackers embed malicious QR codes in phishing emails, PDFs, or even physical media, directing victims to credential harvesting sites on their mobile devices — which typically lack the email security gateway protections and DNS filtering present on corporate laptops. Microsoft, SharePoint, and DocuSign impersonation campaigns using QR codes have been particularly prevalent. The technique exploits the gap between desktop email security and mobile device protection.

### AI-Enhanced Social Engineering
The use of AI-generated content for phishing and social engineering has moved from theoretical to practical. AI voice cloning has been used in vishing attacks impersonating executives to authorize wire transfers (multiple confirmed incidents with losses in the millions). Large language models generate grammatically flawless phishing emails in any language, eliminating traditional detection signals. While large-scale AI-generated phishing campaigns are difficult to distinguish from human-authored ones, the most impactful use has been in targeted BEC and vishing operations.

## Historical Events

| Date | Event | Impact |
|------|-------|--------|
| 2020 | SolarWinds campaign included spearphishing | State-sponsored phishing as one vector in major supply chain operation |
| 2021 | Microsoft warns of consent phishing campaigns | OAuth app abuse for persistent mailbox access without credentials |
| Sep 2022 | Uber breach via MFA fatigue | Scattered Spider-linked actor bombarded employee with MFA pushes; gained access |
| Jan 2023 | Reddit employee phished | Sophisticated targeted phishing led to internal system access |
| Aug 2023 | Microsoft Storm-0558 token theft | Stolen MSA signing key enabled forging Azure AD tokens for government email |
| Late 2023 | QR code phishing surge begins | Major increase in quishing campaigns targeting corporate users |
| Jan 2024 | Midnight Blizzard phishes Microsoft | Russian APT compromised Microsoft corporate email via password spray then OAuth abuse |
| 2024 | $25M deepfake video call BEC (Hong Kong) | Finance employee tricked by deepfake video call impersonating CFO and colleagues |
| 2024 | EvilProxy campaigns hit thousands of orgs | Mass AitM phishing campaigns compromise M365 accounts at scale |
| 2024-2025 | Callback phishing (BazarCall variants) | Phone-based social engineering directing victims to install remote access tools |

## TTP Evolution

**Email Delivery**: Attackers have shifted from bulk commodity spam to abusing compromised legitimate accounts, legitimate email marketing platforms (SendGrid, Mailchimp), and trusted cloud services (SharePoint file shares, OneNote pages, Google Forms) as delivery mechanisms. This approach exploits trust relationships and bypasses domain reputation filtering. HTML smuggling (embedding encoded payloads in HTML attachments) is used to evade gateway scanning.

**MFA Bypass**: The progression from simple credential theft (pre-MFA era) → MFA fatigue/push bombing → AitM session hijacking → device code phishing (OAuth) represents a continuous arms race. AitM phishing currently poses the most scalable threat to non-phishing-resistant MFA. Device code/OAuth phishing targets Azure AD device authorization flows, making the victim authorize a device the attacker controls.

**Landing Page Sophistication**: Modern phishing pages employ CAPTCHA challenges (Cloudflare Turnstile), fingerprint checks, geofencing to block security researchers, user-agent validation, and IP reputation checks. Some pages serve benign content to scanners while displaying phishing content to real victims. Anti-analysis measures include time-delayed page activation and single-use URLs.

**Brand Impersonation**: Microsoft remains the most impersonated brand, followed by Google, Apple, Amazon, and financial institutions. Phishing kits include pixel-perfect reproductions of login pages with dynamic branding (pulling the target organization's logo via Microsoft's brand API). DocuSign, SharePoint, and voicemail notification lures are consistently effective.

**Vishing and Hybrid Attacks**: Callback phishing (initially pioneered by BazarCall/BazaCall) has spawned numerous variants where phishing emails direct victims to call a phone number, where operators social-engineer them into installing remote access software (AnyDesk, ScreenConnect, Quick Assist). Scattered Spider demonstrated the effectiveness of direct vishing to IT help desks to reset MFA and gain corporate access.

## Ecosystem and Infrastructure Patterns

**PhaaS Market Structure**: The PhaaS market mirrors other -as-a-service models with subscription tiers, admin panels, real-time credential viewers, and API integrations. Some platforms offer geo-targeting, A/B testing of lures, and automated session token extraction. Customer support via Telegram is standard. The low barrier to entry (no technical skill required) has expanded the pool of actors conducting sophisticated phishing.

**Infrastructure**: Phishing campaigns increasingly use legitimate cloud hosting (Azure, AWS, Cloudflare Workers, Firebase) to benefit from trusted domains and SSL certificates. Domains often use typosquatting, homograph attacks (using Unicode characters that visually resemble legitimate domains), or long subdomains that push the actual domain off-screen in mobile browsers. URL shorteners and redirectors (including open redirects on legitimate sites) obscure final destination URLs.

**Post-Compromise Actions**: After capturing credentials and session tokens, automated systems immediately begin data harvesting: downloading emails (especially those containing financial data, credentials, or confidential attachments), setting up mail forwarding rules for persistence, and registering new MFA devices. For BEC operations, actors monitor mailboxes for financial transaction threads to insert themselves via reply-chain hijacking.

**BEC Ecosystem**: BEC operations involve role specialization: phishers who compromise accounts, operators who monitor email for financial opportunities, money mule managers who control laundering networks, and sometimes document forgers who create fake invoices. The FBI estimates BEC has caused over $50 billion in global losses since 2013.

## Tooling

| Tool/Platform | Category | Usage |
|--------------|----------|-------|
| Evilginx | AitM Framework | Open-source reverse proxy for AitM phishing; foundation for many PhaaS |
| EvilProxy | PhaaS Service | Commercial AitM platform targeting M365, Google Workspace, etc. |
| Tycoon 2FA | PhaaS Service | AitM kit with advanced anti-detection |
| GoPhish | Phishing Framework | Open-source phishing simulation (used by both red teams and criminals) |
| Modlishka | AitM Tool | Open-source AitM reverse proxy |
| SET (Social-Engineer Toolkit) | Framework | Credential harvesting and social engineering automation |
| Cloudflare Turnstile | Anti-Analysis | CAPTCHA service used on phishing pages to block automated scanning |
| HTML Smuggling techniques | Delivery | Embedding encoded payloads in HTML to bypass email gateways |
| Voice cloning AI (ElevenLabs, etc.) | Vishing | AI-generated voice for executive impersonation calls |
| Residential proxies | Infrastructure | Used to access compromised accounts from victim's geographic region |

## Intelligence Gaps

- **AI phishing detection**: The ability to reliably distinguish AI-generated phishing from human-authored content remains limited, and the detection challenge will grow as models improve.
- **Quishing scale and impact**: The true volume and success rate of QR code phishing campaigns is poorly measured since QR scans on personal mobile devices bypass most corporate telemetry.
- **Voice deepfake prevalence**: Confirmed cases of AI voice clone attacks are likely the tip of the iceberg; most successful vishing attacks are never forensically analyzed for AI use.
- **AitM session token longevity**: How long stolen session tokens remain valid across different platforms and configurations, and the effectiveness of token revocation policies, is inconsistently documented.
- **PhaaS platform revenue**: The total revenue and customer base of major PhaaS platforms is not publicly known; estimates vary widely.

## Refreshing this picture

Before you present any status, actor or trend from this cell as current, check for newer activity with the tools you actually have. Use only the tools available to you; when an integration is not connected, say which source was skipped instead of guessing its result. Lookups are read-only; never write to OpenCTI, MISP, a vendor portal or a takedown queue unless the user or the assignment asks for it.

| Question | Tools (integration) |
|---|---|
| What does our own knowledge base already hold on a kit, PhaaS platform or actor? | `search_opencti`, `list_opencti_entities`, `get_opencti_entity`, `list_opencti_observables` (OpenCTI); `search_misp_events`, `search_misp_attributes` (MISP) |
| Is this URL or landing page a known phishing page, and what does it render? | `search_urlscan_scans`, `submit_urlscan_scan`, `get_urlscan_result`, `get_urlscan_dom`, `find_urlscan_similar_scans` (urlscan; submit with unlisted / private visibility for anything tied to your organization); `get_vt_url_report`, `get_vt_domain_report`, `get_vt_related_objects` (VirusTotal); `lookup_abusech_url`, `search_abusech_urls` (abuse.ch URLhaus); `submit_anyrun_url_analysis`, `get_anyrun_analysis_report` (ANY.RUN, for pages that cloak against scanners) |
| Same kit elsewhere (shared DOM, favicon, page structure, hosting) | `find_urlscan_similar_scans`, `search_urlscan_scans` (urlscan); `search_vt_intelligence` (VirusTotal); `search_gti_threat_iocs`, `get_gti_url_assessment` (GTI); `get_shodan_host_info`, `search_shodan_hosts` (Shodan, for hosting, open ports and TLS certificates shared across kit servers); `search_domaintools_reverse_ip` (DomainTools, co-hosted domains) |
| Lookalike / typosquat / homograph domains for our brand | `search_domaintools_iris`, `list_domaintools_detect_domains`, `check_domaintools_brand_monitor`, `get_domaintools_risk_score`, `get_domaintools_whois` (DomainTools); `list_socradar_brand_protection` (SOCRadar); `list_gti_dtm_alerts` (GTI Digital Threat Monitoring) |
| Brand impersonation and takedown status (sites, social, ads, apps) | `list_doppel_brands`, `list_doppel_alerts`, `get_doppel_alert`, `scan_doppel_url` (Doppel); raising a new alert with `create_doppel_alert` or a takedown with `create_socradar_takedown` only when asked |
| Did the lure reach our mailboxes, and who clicked? | Email-security integrations (Sublime Security, Proofpoint) when connected, through the message-search tools they expose; `run_defender_hunting_query` (Microsoft Defender advanced hunting) or `run_sentinel_kql_query` (Microsoft Sentinel) over email, URL-click and sign-in tables; `run_splunk_search` (Splunk) |
| Actor and alias resolution, TTP mapping | `search_mitre_attack`, `get_mitre_attack_object`, `get_mitre_attack_related` (MITRE ATT&CK; for example Phishing T1566, Adversary-in-the-Middle T1557, Steal Web Session Cookie T1539, Multi-Factor Authentication Request Generation T1621); `search_gti_threats` (GTI); `search_rf_threat_actors` (Recorded Future); `get_crowdstrike_actor` (CrowdStrike) |
| Recent public reporting on kits, campaigns and BEC trends | `search_feedly_articles`, `get_feedly_trending_threats` (Feedly); `search_rf_analyst_notes` (Recorded Future); `search_rss_entries` (RSS); `search_google_web` (built-in web search, Google Search or Brave Search) |
| Underground chatter on PhaaS sales and kit updates | `search_intel471_forum_posts` (Intel 471); `search_flashpoint_communities` (Flashpoint); `search_cybersixgill_intel_items` (Cybersixgill) |

Defensive follow-ups, only when asked: detection content goes to the output first (load the **Sigma Rule Writing** or **KQL Query Writing** skill if attached); an OpenAEV phishing simulation can be prepared with `manage_openaev_phishing` to test user resilience against a lure family.

How to record what you find: when the user asks, record new facts in OpenCTI (a Note with `create_opencti_note`, indicators with `create_opencti_indicator`, or a relationship with `create_opencti_relationship`) or in a report produced with `generate_report`; otherwise keep durable observations in agent memory with `store_memory` (and check `search_memory` for earlier findings before you start). Always tell the reader which statements come from this baseline (2026-04-05) and which come from a fresh lookup, with its date. Defang URLs and domains in written products.

Apply the usual rigor to anything you publish from this cell: an Admiralty rating per source, a TLP marking, a confidence level and calibrated likelihood words. Load the **Source Assessment (Admiralty Scale)** or **Likelihood Language** skill if it is attached and you need the full scales. Related knowledge cells: **Carding and Financial Fraud**, **Infostealers**, **Initial Access Brokers**.

## Sources and References

1. FBI Internet Crime Complaint Center (IC3) - "2023 Internet Crime Report" — https://www.ic3.gov/
2. Microsoft - "Digital Defense Report 2024" — https://www.microsoft.com/en-us/security/security-insider/
3. Proofpoint - "State of the Phish 2024" — https://www.proofpoint.com/us/resources/threat-reports/state-of-phish
4. Kuba Gretzky - "Evilginx" project and research — https://breakdev.org/evilginx/
5. Mandiant - "Phishing and AitM Campaign Analysis" — https://www.mandiant.com/resources
6. Cisco Talos - "Phishing Trends and Techniques Research" — https://blog.talosintelligence.com/
7. KnowBe4 - "Phishing Benchmarking Reports" — https://www.knowbe4.com/
8. Sekoia - "Tycoon 2FA and PhaaS Landscape Analysis" — https://blog.sekoia.io/

## Review History

| Date | Change | Source |
|------|--------|--------|
| 2026-04-05 | Initial creation with baseline intelligence through early 2025 | Training knowledge |
| 2026-10 | Adapted for XTM One | cti-skills pack |
