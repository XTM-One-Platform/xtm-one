# Dark Web Collection

A practical, defender-oriented methodology for collecting cyber threat intelligence from underground forums, marketplaces, ransomware leak sites and Telegram channels. The skill assumes you are supporting a defensive analyst, not a covert operator, and recommends a **vendor-first** posture: in XTM One that means the connected dark-web intelligence integrations (Flashpoint, Intel 471, Cybersixgill, SOCRadar, Recorded Future, Google Threat Intelligence Digital Threat Monitoring, Ransomware.live). Self-hosted DIY collection is documented as a fallback that the analyst performs off-platform.

**Last reviewed:** 2026-04-28 (reference lists verified through 2026-04). Forum and channel lists rot fast; always verify a status before relying on it.

This body is the entry point. The detailed material is bundled with the skill; read it with `read_skill_resource` when you need it:

| Resource path | Contents |
|---|---|
| `references/forums.md` | 35+ sourced underground forums across 8 categories, with status, access tier and source |
| `references/telegram-channels.md` | 30+ sourced Telegram channels across 6 categories, with status and source |
| `references/access-methods.md` | Vendor matrix (with the matching XTM One integrations) and the off-platform DIY playbook |
| `references/opsec.md` | Handler-side OPSEC primer, persona separation, deconfliction, mental health |
| `references/passive-monitoring.md` | Selector strategy, cadence, triage workflow, retention |

## Scope and explicit exclusions

**In scope.** Cybercrime-focused collection: ransomware leak sites and group communications, initial access broker (IAB) markets, infostealer log shops, malware and exploit forums, carding and financial-fraud platforms, breach and data-leak forums, hacktivist coordination channels (where defensible cyber TTPs are documented), and lightly-touched nation-state-cyber-adjacent activity (e.g., GRU-nexus hacktivist fronts, IRGC-aligned cyber operations).

**Explicitly out of scope.** This skill does not list, link, or guide access to:
- Child sexual abuse material (CSAM) — discovery is mandatory-reportable in most jurisdictions; report to NCMEC (US), IWF (UK), Project Arachnid (Canada), or national equivalent and stop further investigation.
- Terrorism-glorifying or terrorism-instructional content — refer to national counter-terrorism units and to specialist platforms (GIFCT, Tech Against Terrorism).
- Violent-extremist or live-violence material — refer to law enforcement immediately.
- Personal-physical-safety threats against named individuals — refer to law enforcement and physical-security teams.

If you encounter any of the above incidentally during cybercrime collection (for example in a vendor search result), log only the URL/identifier, do not download media, screenshot or further analyse, tell the user it must be routed to the appropriate authority, and stop working on that item. Recommend rotating analysts off the source if exposure was sustained. See `references/opsec.md` for the mental-health framing.

## Recommended posture: vendor-first

Commercial CTI vendors are the **default access path**. They operate at scale across forums, languages, and identity tiers a defensive team cannot match; they handle persona maintenance, vetting, language coverage, and legal cover; and they provide audit trails that hold up in regulatory or litigation contexts.

In XTM One, the vendor path is the set of integrations an administrator has connected:

| Need | Integration and tools |
|---|---|
| Forum, marketplace and chat-service posts | Flashpoint: `search_flashpoint_communities`, `get_flashpoint_community_post`; Intel 471: `search_intel471_forum_posts`, `search_intel471_marketplaces`, `search_intel471_instant_messages`, `search_intel471_private_messages`; Cybersixgill: `search_cybersixgill_intel_items`, `search_cybersixgill_intel_items_advanced`, `get_cybersixgill_intel_items_next`; SOCRadar: `search_socradar_dark_web`; GTI Digital Threat Monitoring: `search_gti_dtm_docs` |
| Credential and data-leak exposure | Flashpoint: `search_flashpoint_credentials`; Intel 471: `search_intel471_credentials`, `search_intel471_data_leaks`, `search_intel471_breach_alerts`; Recorded Future: `search_rf_leaked_credentials`, `lookup_rf_identity_exposures`, `check_rf_password_exposure` |
| Actor context | Intel 471: `get_intel471_actor`, `list_intel471_actors`; Cybersixgill: `enrich_cybersixgill_actor`, `enrich_cybersixgill_post`; Recorded Future: `search_rf_threat_actors`; GTI: `search_gti_threats`, `get_gti_threat` |
| Standing selector monitoring and alerts | Intel 471: `create_intel471_watcher`, `manage_intel471_watchers`, `list_intel471_alerts`; GTI DTM: `list_gti_dtm_monitors`, `get_gti_dtm_monitor`, `manage_gti_dtm_monitor`, `list_gti_dtm_alerts`, `get_gti_dtm_alert`; Cybersixgill: `list_cybersixgill_alerts`, `get_cybersixgill_alert_content`, `manage_cybersixgill_organization_assets`; Flashpoint: `list_flashpoint_alerts`; Recorded Future: `list_rf_alerts`, `get_rf_alert`; SOCRadar: `list_socradar_incidents`, `list_socradar_digital_assets`, `list_socradar_brand_protection`, `list_socradar_fraud_protection` |
| Ransomware leak-site claims | Ransomware.live: `list_ransomlive_recent_victims`, `search_ransomlive_victims`, `get_ransomlive_victim`, `list_ransomlive_groups`, `get_ransomlive_group`, `get_ransomlive_iocs`, `get_ransomlive_yara`, `get_ransomlive_ransomnote` |
| Finished vendor reporting | `search_intel471_reports`, `search_flashpoint_reports`, `search_rf_analyst_notes`, `list_cybersixgill_reports`, `search_feedly_articles` |

Creating or changing a watcher, monitor or digital asset is a persistent configuration change in the vendor portal: do it only when the user or the assignment asks for it, and confirm the selector list first.

Vendors without an XTM One integration (KELA, DarkOwl Vision, Webz.io, ZeroFox, Constella, Hudson Rock, IntelligenceX, Have I Been Pwned) are still covered in `references/access-methods.md`; their portals are used by the analyst directly, outside the platform.

Self-hosted DIY (Tails/Whonix + Tor Browser + Telegram user client + sockpuppets) is documented as a **fallback** for budget-constrained teams or for narrow, time-bounded targeted collection where vendor onboarding cost would exceed analyst-hours. The DIY playbook is in `references/access-methods.md`.

**This skill never visits a `.onion` address and never touches criminal infrastructure from the platform.** Do not use `web_fetch` or `browser` on `.onion` addresses, criminal forums, markets, leak sites or Telegram channel pages, and do not register, post, join, buy or interact anywhere. Tor browsing, Tails, sockpuppet maintenance, and any active engagement are the analyst's responsibility, performed off-platform on dedicated research hardware.

Rule for every step: use only the tools you actually have. When an integration is not connected, say which source was skipped instead of guessing its result.

## Collection workflow

A six-step flow to drive when asked to "set up dark-web collection" or "monitor for X on the dark web":

1. **Define PIRs and selectors.** What do you actually need to know? If OpenCTI is connected, start from existing PIRs (`manage_opencti_pir`, `query_opencti_pir_entities`); otherwise check agent memory (`search_memory`) for a stored PIR and selector list, and store the agreed one with `store_memory`. Translate each PIR into selector families per `references/passive-monitoring.md` — brand strings, identity selectors, email/domain patterns, network selectors, technical artefacts. Compose; never use a bare common name.
2. **Pick the access tier.** Vendor integrations (recommended), surface-web aggregators such as Ransomware.live, or off-platform DIY. Choose per the matrix in `references/access-methods.md`.
3. **Collect.** Run the selectors through the connected vendor search tools above (or configure vendor watchers / monitors when asked), pull Ransomware.live on cadence (see the cadence table in `passive-monitoring.md`), or ingest text the analyst collected off-platform (uploaded files, vendor exports) for local matching with `code_interpreter` (see "Local keyword matching" below).
4. **Deduplicate and enrich.** Deduplicate hits by match + selector, then pivot from raw indicators:
   - OpenCTI: `search_opencti`, `list_opencti_observables`, `get_opencti_entity`; MISP: `search_misp_attributes`, `search_misp_events`, `check_misp_warninglists`.
   - IPs and network selectors (IAB "access to ASN / CIDR" listings): `get_shodan_host_info`, `search_shodan_hosts` (e.g. `net:` or `asn:` filters), `get_shodan_org_info`, `lookup_greynoise_ip`, `check_abuseipdb_ip`, `check_abuseipdb_network`, `get_vt_ip_report`, `lookup_crowdsec_ip`.
   - Domains and URLs: `get_vt_domain_report`, `get_vt_url_report`, `search_urlscan_scans`, `get_domaintools_whois`, `search_domaintools_iris`, `get_shodan_domain_info`, `resolve_shodan_dns`.
   - Hashes: `get_vt_file_report`, `lookup_abusech_malware_hash`; any IOC: `search_abusech_iocs` (ThreatFox).
   - Broad enrichment: `enrich_rf_indicators` (Recorded Future), `get_gti_ip_assessment` / `get_gti_domain_assessment` / `get_gti_file_assessment` (GTI), `enrich_cybersixgill_ioc` (Cybersixgill).
   - The source pack also used AlienVault OTX and Censys here; neither has an XTM One integration. Use OpenCTI, MISP, Feedly, abuse.ch ThreatFox, GTI or Recorded Future in place of OTX pulses, and Shodan, GTI, DomainTools or urlscan in place of Censys host and certificate data.
5. **Score the source.** Apply an Admiralty rating (source reliability A-F + information credibility 1-6) to every artefact before it goes into a finished product. The source-rating table at the bottom of this body is the starting point; load the **Source Assessment (Admiralty Scale)** skill if it is attached for the full method.
6. **Report.** Mark handling with TLP (dark-web derived material containing victim data or selectors is usually TLP:AMBER or TLP:AMBER+STRICT), express analytical claims with a confidence level (0-100 or low / moderate / high) and forecasts with calibrated likelihood words (remote, highly unlikely, unlikely, realistic possibility, likely, highly likely, almost certain). Load the **TLP Guide** or **Intelligence Writing** skill if attached for the full product format. Produce the deliverable in the run output or with `generate_report`; write to OpenCTI (Report, Note, observables in a draft with `create_opencti_draft`) or MISP only when asked.

## What to monitor

Short orientation per source type. Detail in the bundled references.

### Ransomware leak sites
Most groups maintain Tor leak sites; many also publish to Telegram. Monitor for new victim claims, countdowns, sample data, group operational announcements, affiliate recruitment posts. The Ransomware.live integration covers victim claims (`list_ransomlive_recent_victims`, `search_ransomlive_victims`), group profiles (`get_ransomlive_group`), IOCs (`get_ransomlive_iocs`), YARA (`get_ransomlive_yara`) and ransom notes (`get_ransomlive_ransomnote`) for most active groups; it is quota-sensitive, so poll on cadence rather than in loops. Cross-reference with the Russian-speaking elite forums (`references/forums.md` section 1) through the vendor forum search tools for affiliate chatter.

### Underground forums
35+ forums catalogued in `references/forums.md` across Russian-speaking elite, English-speaking general / data-leak, carding and fraud, infostealer log marketplaces, exploit / 0day brokers, hacktivist coordination, regional (Chinese, Spanish/LATAM, Persian, Turkish), and doxing/cybercrime overlap. Monitor for IAB listings naming your sector or geography, breach announcements, exploit sales for tech in your stack, malware development discussions, and reputation/vouching dynamics that indicate actor migrations. In XTM One: `search_intel471_forum_posts`, `search_flashpoint_communities`, `search_cybersixgill_intel_items`, `search_socradar_dark_web`, `search_gti_dtm_docs`.

### Telegram channels
30+ channels catalogued in `references/telegram-channels.md` across ransomware group officials, IAB storefronts, infostealer log shops, hacktivist collectives (pro-Russia, pro-Ukraine, pro-Palestine/MENA, mixed-attribution), breach aggregators, and defender-run news aggregators (FalconFeeds, vx-underground, CTI Now). XTM One cannot read Telegram channels directly: the Telegram integration is a bot that sends messages, and bots cannot read arbitrary public channels. Use vendor instant-messaging coverage instead (`search_intel471_instant_messages`, Cybersixgill and Flashpoint chat coverage through `search_cybersixgill_intel_items` and `search_flashpoint_communities`, Recorded Future), and ingest exports the analyst collected off-platform for local matching. Defender-run aggregators are the lowest-effort safe second tier; where they also publish RSS or web pages, `search_rss_entries` and `search_feedly_articles` reach their reporting.

### Paste sites
Pastebin and modern equivalents (DontPad, JustPasteIt, Ghostbin successors). Combo lists, leaked credentials, code-leak snippets, manifestos, IOC dumps from researchers. Vendor-mediated coverage (`search_cybersixgill_intel_items`, `search_intel471_data_leaks`, `search_flashpoint_credentials`) is more reliable than DIY; for DIY, run local keyword matching over bulk paste-site dumps the analyst provides.

### Marketplaces
Treat Russianmarket, Brian's Club, B1ack's Stash, BidenCash successors, Exodus Marketplace, and Hudson Rock-tracked stealer log shops as forum-equivalent for monitoring purposes (they are listed in `references/forums.md` sections 3-4). Monitor for credentials matching your domain selectors and for sector-specific listings (`search_intel471_marketplaces`, `search_flashpoint_credentials`, `search_rf_leaked_credentials`).

### Breach and data-leak corpora
HIBP, IntelligenceX, Constella, Hudson Rock free tier (used by the analyst directly; no XTM One integration), plus the connected credential-exposure tools above. Pivot from a hit (executive email, internal hostname, source-code snippet) into incident response.

## Local keyword matching (replaces the bundled scanner)

The source pack shipped a local regex/keyword scanner. In XTM One, run the same logic with `code_interpreter` over text you already hold (vendor search results, uploaded exports, JSONL the analyst collected off-platform). It makes no network calls.

Selector format (one per line, `#` comments allowed):

| Prefix | Meaning |
|---|---|
| `literal:` (default) | Case-insensitive substring |
| `regex:` | Arbitrary case-insensitive regex |
| `domain:` | Regex anchored so `acme\.com` does not match `notacme.com` |
| `email:` | Regex on an email tail, e.g. `email:@acme\.com` matches `alice@acme.com` |

```python
import re, json
def compile_selectors(lines):
    out = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind, value = "literal", line
        for p in ("literal:", "regex:", "domain:", "email:"):
            if line.startswith(p):
                kind, value = p[:-1], line[len(p):]
                break
        if kind == "literal":
            pat = re.escape(value)
        elif kind == "domain":
            pat = rf"(?<![\w.-]){value}(?![\w.-])"
        elif kind == "email":
            pat = rf"[\w.+-]+{value}"
        else:
            pat = value
        out.append((line, kind, re.compile(pat, re.IGNORECASE)))
    return out

def flatten(line):  # match JSON values, not braces
    try:
        obj = json.loads(line)
    except Exception:
        return line
    parts = []
    def walk(v):
        if isinstance(v, str): parts.append(v)
        elif isinstance(v, dict): [walk(x) for x in v.values()]
        elif isinstance(v, list): [walk(x) for x in v]
    walk(obj)
    return "\n".join(parts)

def scan(records, selectors, radius=120):
    seen, hits = set(), []
    for src, line in records:  # (source label, text line)
        text = flatten(line)
        for raw, kind, pat in selectors:
            for m in list(pat.finditer(text))[:10]:
                key = (m.group(0).lower(), raw)
                if key in seen:
                    continue
                seen.add(key)
                hits.append({"source": src, "selector": raw, "selector_kind": kind,
                             "match": m.group(0),
                             "context_before": text[max(0, m.start()-radius):m.start()],
                             "context_after": text[m.end():m.end()+radius]})
    return hits
```

Deliver hits as a table in the output, or as a JSONL / CSV file with `generate_file` when asked. Redact or truncate victim PII in the context fields before sharing.

## OPSEC summary (top rules)

Read `references/opsec.md` before advising on any DIY collection. The condensed top ten (they apply to the analyst's off-platform work):

1. **Three identities, never one.** Real you ↔ work you ↔ research persona. They never share an email, phone, browser, VM, writing sample, or timezone tell.
2. **Dedicated hardware or VM.** Never the daily driver. Full-disk encryption, snapshot rollback after each session.
3. **Always Tor or research-only VPN.** Never your home/office IP. Defence in depth: VPN → Tor for sensitive targets.
4. **Never reuse a handle, profile photo, or sockpuppet narrative across personae.**
5. **Strip EXIF from every image.** Never post any image that exists elsewhere on the internet (reverse-image search will deanon you).
6. **Tor Browser default settings.** "Safer" or "Safest" slider, no JS, no plugins, no logging into real accounts.
7. **Burner number for Telegram.** Treat the session file as credentials. Never auto-join channels — joining is a deliberate event from the Telegram client, not from automation.
8. **Linguistic credibility.** Russian elite forums catch machine-translation tells fast; use a native-speaker collaborator or stay lurking.
9. **No purchases without legal sign-off.** Buying access or data can constitute receiving stolen property, funding criminal enterprises, or breaching sanctions.
10. **Mental health is load-bearing.** Rotate analysts off dark-web duty every 6-12 months; provide clinical support; mandatory time off after incidental exposure to illegal-content material.

Platform-side OPSEC: the most sensitive selectors (executive names, internal codenames) also leak through vendor portals and query logs. Send to vendor tools only the selectors the PIR needs, and keep the full selector list in OpenCTI PIRs or agent memory rather than in shared chat transcripts.

## Source assessment (Admiralty starting points)

Combine with a per-artefact assessment for the final rating:

| Source type | Reliability | Credibility | Notes |
|---|---|---|---|
| First-party DIY observation on open-tier forum | C | 3 | Sockpuppet, lurking, no engagement; you saw it |
| First-party DIY observation on vetted forum | B-C | 2-3 | Vouched access lifts reliability; engagement risk lifts caveats |
| Vendor-curated dark-web feed (KELA, Flashpoint, Intel 471, DarkOwl, Recorded Future, Cybersixgill) | B | 2 | Vendor handles vetting and OPSEC; check recency |
| Vendor freemium tier (Hudson Rock free, IntelligenceX free, ransomware.live PRO) | B-C | 2-3 | Same source quality, less context, sometimes delayed |
| Surface-web indexer / mirror (Ahmia, dark.fail, ransomwatch, ransomlook) | C | 3 | Indexer freshness varies; cross-check with primary |
| News reporting on dark-web events (BleepingComputer, TheRecord, KrebsOnSecurity) | B | 2-3 | Second-hand but generally well-sourced |
| Government press release (DOJ, Europol, FBI, NCA) | A | 1-2 | Authoritative for the documented event; selective in detail |
| Threat actor self-posts on their own channel | F | 5-6 | Attribution-by-claim only; treat as actor PR until corroborated |

## Composition

Keep chains short; at most two of these alongside this skill in one run:
- **Source Assessment (Admiralty Scale)** — full Admiralty rating per artefact.
- **TLP Guide** — handling classification before any sharing.
- **Confidence Levels**, **Likelihood Language** — analytical language for the resulting product.
- **Intelligence Writing** — the finished product.
- **Ransomware Ecosystem**, **Initial Access Brokers**, **Infostealers**, **Carding and Financial Fraud**, **Hacktivism**, **Threat Actor Profiling** — downstream knowledge and analysis the collection feeds into.
- **Threat Intel Sources** — how to read each enrichment source's verdicts.

## Legal and ethical

Conduct collection within applicable law (jurisdiction varies — EU defenders should run retention practices by counsel against GDPR Art. 6 / Art. 9 even for "publicly available" data). Do not participate in criminal activity to acquire intelligence. Do not purchase access, data, or services without explicit legal sign-off and (often) law-enforcement coordination. Mandatory-reportable content (CSAM in particular) must be reported to the appropriate authority and investigation halted; that work belongs to law enforcement and trained NGO units, not defensive CTI teams. Document retention periods, access controls, and disposal procedures with counsel; default to 90-365 days for raw collection. Material stored in OpenCTI, MISP, knowledge bases or agent memory falls under the same retention rules.
