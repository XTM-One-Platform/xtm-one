# Passive monitoring strategy

> Companion to `access-methods.md` (where the data comes from) and `opsec.md` (how to handle yourself while collecting).

Passive monitoring is the bulk of defensive dark-web work: you build a list of selectors that matter to your org, point them at every collection source you can reach, and route hits into your triage queue. The trap is making the list either too narrow (miss everything) or too noisy (drown your analysts). This page is the field guide.

## Selector taxonomy

Six families. Always combine selectors from at least two families to enrich precision.

| Family | Examples | Use for |
|---|---|---|
| **Brand strings** | `"Acme Corp"`, `"Acme Cloud"`, internal product codenames | Brand mentions on leak sites, marketplace listings, hacktivist target lists |
| **Identity selectors** | Executive names, key engineers' aliases, board members | Doxing prep, executive-protection alerts, IAB targeting |
| **Email patterns** | `email:@brand.com`, `email:@subsidiary.com`, role-based addresses (`security@`, `legal@`) | Credential leaks, infostealer logs, breach corpora |
| **Domain patterns** | `domain:brand\.com`, `domain:[a-z]+-brand\.com`, sub-brands | Phishing kit chatter, typosquats listed for sale, exposed-credential dumps |
| **Network selectors** | IP ranges, CIDR blocks, ASN, BGP prefixes, hostname patterns | IAB access listings ("RDP access to ASNxxx"), CDN-fronted infrastructure mentions |
| **Technical artefacts** | Internal hostnames, project codenames, internal API URLs, certificate CNs, hash prefixes | Source-code leaks, build-pipeline exposure, supply-chain compromise indicators |

## Selector hygiene

Bad selectors create noise. Noise creates analyst fatigue. Analyst fatigue creates missed real signals.

**Rules:**

1. **Never use a bare common name.** "John Smith" matches everything. Pair with at least one other family: `regex:John Smith.{0,200}Acme` or `regex:Acme.{0,200}CFO`.
2. **Anchor domain selectors.** Use the `domain:` prefix in the local keyword matcher (see the skill body) — it word-boundary-anchors so `acme.com` does not match `notacme.com`.
3. **Anchor email selectors.** Use the `email:` prefix — `email:@acme\.com` matches `alice@acme.com` but not `something@notacme.com`.
4. **Compose, don't broaden.** If "Acme" is too noisy, *add* a context word (`Acme.*?(breach|leak|access|rdp|database)`) rather than removing the brand.
5. **Localise.** "Acme" in Russian is "Акме"; in Chinese, transliterations vary. If your org operates internationally, maintain selectors per locale.
6. **Refresh quarterly.** Codenames retire; products rebrand; executives change. Stale selectors miss real hits.
7. **Negative selectors.** When you cannot tighten a selector enough, add a post-match filter that drops hits matching common false-positive context (e.g., a press-release boilerplate that triggers your selector but is just journalism).

## Tool stack

Layered, vendor-first.

Use only the tools you actually have; when an integration is not connected, say which source was skipped instead of guessing its result.

### Layer 1 — Vendor integrations (recommended)

Configure your selectors in vendor portals, or run them through the connected XTM One integrations. Each vendor returns ranked alerts with context, source, and (sometimes) actor attribution.

- **Ad-hoc selector hunts**: `search_flashpoint_communities`, `search_intel471_forum_posts`, `search_intel471_instant_messages`, `search_intel471_marketplaces`, `search_cybersixgill_intel_items`, `search_socradar_dark_web`, `search_gti_dtm_docs`.
- **Standing monitoring** (persistent configuration — only when the user or the assignment asks): Intel 471 watchers (`create_intel471_watcher`, `manage_intel471_watchers`, alerts with `list_intel471_alerts`), GTI DTM monitors (`manage_gti_dtm_monitor`, `list_gti_dtm_alerts`), Cybersixgill organization assets (`manage_cybersixgill_organization_assets`, `list_cybersixgill_alerts`), SOCRadar digital assets (`manage_socradar_digital_asset`, `list_socradar_incidents`), Recorded Future alerts (`list_rf_alerts`), Flashpoint alerts (`list_flashpoint_alerts`).
- **Selectors first**: write the selector list once (keep it with the PIR in OpenCTI via `manage_opencti_pir`, or in agent memory via `store_memory`), then translate it into each vendor's query syntax. Vendors differ in regex support, field-targeting, and de-duplication; budget time for tuning per vendor.
- **Cadence**: most vendor portals deliver daily / weekly digests by default. For ransomware leak sites, set hourly or near-real-time. An XTM One assignment on a schedule trigger can run the selector sweep on cadence.
- **Triage routing**: route hits into your SOAR / case management (for example a TheHive alert with `create_thehive_alert` or a Sentinel incident comment, when asked); do not let alerts pile up in vendor portals.

### Layer 2 — Off-platform DIY plus local matching

For sources vendors do not cover or for budget-constrained teams.

- **Onion-indexer search** (Ahmia, dark.fail, IntelligenceX) and **Telegram channel reading** are done by the analyst off-platform (see `access-methods.md` and `opsec.md`). XTM One has no integration for either.
- **Local keyword matching** over the exported text (and over any other text dump: leak archives, breach corpora, vendor-exported alert dumps) runs in `code_interpreter` with the selector format described in the skill body (`literal:`, `regex:`, `domain:`, `email:`), so you can maintain one canonical selector list.

Suggested pipeline:

```
selector list (PIR) ──┬──> vendor integrations (search tools / watchers / monitors)
                      ├──> Ransomware.live polling (list_ransomlive_recent_victims, search_ransomlive_victims)
                      └──> analyst exports (Telegram, indexers, paste dumps) ──> code_interpreter keyword match ──> triage
```

### Layer 3 — Enrichment integrations

- **Ransomware.live** — poll for victim claims matching your org / sector / country (`search_ransomlive_victims`, `list_ransomlive_recent_victims`). Quota-sensitive: poll on cadence, not in loops.
- **MISP / OpenCTI** — pivot from a hit to your MISP instance or OpenCTI (`search_misp_attributes`, `search_opencti`, `list_opencti_observables`) for IOC enrichment, sharing, and timeline correlation.
- **Network-selector family** — `get_shodan_host_info`, `search_shodan_hosts`, `lookup_greynoise_ip`, `check_abuseipdb_network`. (The source pack also used Censys here; it has no XTM One integration, so use Shodan, GTI or DomainTools instead.)

## Cadence

| Source type | Polling cadence | Why |
|---|---|---|
| Ransomware leak sites (via the Ransomware.live integration) | Hourly | Claims posted in waves; first 12-24h matter most for victim notification |
| Telegram (RaaS comms, hacktivist ops; vendor IM coverage or off-platform client) | 5-15 min | Breaking claims appear here first; ops chatter is high-tempo |
| Surface-web onion indexers (off-platform) | Daily | Indexers themselves crawl slowly; sub-daily polling wastes calls |
| Forum scrapes (vendor-mediated) | Daily or vendor's near-real-time | Most forums are low-tempo; depth of context matters more than freshness |
| Paste sites + breach announcement channels | Hourly | Credential dumps appear without warning |
| Infostealer log marketplaces | Daily | Log shops update in batches; per-domain credential exposure summary is the unit of interest |

## Storage schema

Keep every hit, whatever its origin, in a common record shape so triage and deduplication work across sources:

- `source` — the originating integration, tool or export
- `captured_at` / `query_time` — ISO 8601 UTC
- `selector` / `selector_kind` — what matched and how
- `match` — the literal string matched
- `context_before` / `context_after` — surrounding text for triage

Where records live in XTM One: hits and the triage table in the run output; a JSONL / CSV file produced with `generate_file` when asked; durable selector lists and triage decisions in OpenCTI (PIRs, Notes, observables in a draft) or agent memory. Off-platform raw collection follows the layout in `access-methods.md` (one file per source per day, append-only, encrypted at rest).

## Triage workflow

1. **Daily sweep**: review the day's hits (a scheduled assignment can produce the triage table). For each hit:
   - True-positive operational: open a case, route to SOC / IR / legal as appropriate.
   - True-positive informational: append to weekly intel report; no immediate action.
   - False positive: tune the selector. *Always* tune; never just close-without-action.
2. **Weekly review**: rotation analyst checks selector tune-ups, retires dead selectors, adds new ones from PIRs.
3. **Monthly** retro: vendor coverage gaps, DIY pipeline reliability, mean-time-to-detect by source.
4. **Quarterly**: full selector list refresh; rotate sockpuppets if approaching burn-risk thresholds; revisit vendor mix.

## Retention and legal

- **Raw collection** retention is jurisdiction-dependent. EU defenders should run retention by counsel against GDPR Art. 6 / Art. 9 considerations even for "publicly available" data — courts have not been universally permissive.
- **Typical defender practice**: 90-365 days for raw collection, longer for finished intelligence products that have been TLP-marked.
- **Special handling**: any hit that includes verified victim PII (e.g., infostealer log content with employee credentials) escalates to incident handling and to a separate, restricted-access store.
- **Sharing**: TLP-tag every artefact before it leaves the analyst (the **TLP Guide** skill has the full rules). Qualify every source with an Admiralty rating (the **Source Assessment (Admiralty Scale)** skill has the full method).

## Anti-patterns

- **One mega-selector list applied everywhere.** Different sources have different signal density; what is a useful selector on a Russian forum is noise on a defender-run news channel.
- **Polling vendor APIs faster than they update.** Wastes API budget; some vendors flag aggressive polling as abuse.
- **Letting the alert queue grow.** If 2 weeks of hits sit untriaged, you may already have missed an active intrusion or victim-notification window.
- **No feedback loop from triage to selector tuning.** Selectors should evolve every week.
- **Treating hit count as a metric.** Count of *triaged-true-positive* hits matters; raw count rewards noise.

## See also

- `access-methods.md` — vendor matrix + DIY playbook (where to point selectors).
- `opsec.md` — analyst safety while operating the collection.
- `forums.md`, `telegram-channels.md` — the source landscape.
- **Source Assessment (Admiralty Scale)**, **TLP Guide**, **Confidence Levels**, **Likelihood Language** skills — tradecraft applied to every product derived from this collection.
