# Hash Investigation

Investigate a file hash (MD5, SHA-1, SHA-256) end-to-end: detection signals, malware family attribution, behavioural tags and infrastructure pivots (C2 IPs and domains, parent and dropped files), then decide whether deeper malware analysis is warranted.

**Tool rule.** Use only the tools you actually have. When an integration is not connected (its tools are not in your tool list), say which source was skipped instead of guessing its result. Never fabricate a detection ratio, family name or sandbox verdict. How to read each source's output is detailed in the **Threat Intel Sources** skill; load it if it is attached and you need the interpretation details.

## When to use

- The user asks to check a hash, identify malware, or enrich a sample
- Another investigation surfaces a hash (communicating files of an IP, file served by a URL)
- The user asks "what is this malware" with a hash as input

## Inputs

```yaml
indicator: [MD5 | SHA-1 | SHA-256 hash]
context: [optional - where did this hash come from?]
tlp_ceiling: [optional, default AMBER]
request_malware_analysis: [optional bool - load the Malware Analysis skill after lookup]
```

## Procedure

### 1. Validate

- Confirm the hash is a valid length (MD5 = 32, SHA-1 = 40, SHA-256 = 64 hex characters).
- Reject it if not; route to the right skill if it looks like a different indicator type.
- Hashes of empty files and common benign files (for example MD5 `d41d8cd98f00b204e9800998ecf8427e`, the empty file) are frequent false positives in IOC lists: flag them, do not investigate them. With MISP connected, `check_misp_warninglists` catches known-benign hashes.

### 2. Parallel lookups

Issue these calls in the same turn (parallel tool calls), one per connected integration:

| Source (integration) | Tool call(s) | Why |
|---|---|---|
| VirusTotal | `get_vt_file_report` | Detection stats, popular threat classification (family), names, type, size, signature, first / last seen, tags |
| abuse.ch | `lookup_abusech_malware_hash` (MalwareBazaar), `search_abusech_iocs` (ThreatFox, search by file hash), `lookup_abusech_payload` (URLhaus, MD5 / SHA-256 only) | Sample dossier and family tag; C2 IOCs linked to the sample; distribution URLs |
| OpenCTI (internal) | `list_opencti_observables` (search = the hash, type StixFile) and `search_opencti` (search = the hash) | Internal correlation: observable, indicators, linked malware, intrusion sets, reports |
| MISP (internal) | `search_misp_attributes` (value = the hash) | Internal correlation against your own catalogued events |
| CrowdStrike Falcon Intelligence | `list_crowdstrike_intel_indicators` (`filter_query` `indicator:'HASH'`) | Falcon Intel verdict: malicious confidence, linked actors and malware families, report references |

Add these to the same batch when the integration is connected (do not demote them to "later, maybe": vendor verdicts are the strongest single-source signal on a hash):

| Source | Tool call(s) | Why |
|---|---|---|
| Google Threat Intelligence | `get_gti_file_assessment` | Verdict, threat score, severity, associated threats and campaigns |
| Recorded Future | `lookup_rf_hash` | Risk score, malware family, triggered rules |
| ANY.RUN | `lookup_anyrun_ti` (`sha256` / `sha1` / `md5` = the hash) | Existing public sandbox detonations, verdict, tags |
| Microsoft Defender for Endpoint | `get_defender_file_info`, `get_defender_file_stats` (SHA-1) | Reputation and, above all, prevalence in your own estate |
| Intel 471 / Flashpoint / Cybersixgill / SOCRadar | `search_intel471_iocs`, `lookup_flashpoint_ioc`, `enrich_cybersixgill_ioc`, `analyze_socradar_indicator` | Underground and vendor context |

Escalation (run after the parallel batch, only when warranted - for example when you need MITRE ATT&CK mapping, sandbox detonation output, or the C2 indicators extracted from the sample):

- Sandbox behaviour: `get_vt_file_behaviour` (processes, network, MITRE techniques); CrowdStrike sandbox: `list_crowdstrike_sandbox_reports` (filter on the SHA-256) then `get_crowdstrike_sandbox_report`.
- Infrastructure and lineage with `get_vt_related_objects` (entity_type `file`; relationships `contacted_ips`, `contacted_domains`, `contacted_urls`, `dropped_files`, `execution_parents`, `bundled_files`, `itw_urls`). One call per relationship: request only what you need.
- Family clustering: YARA matches surfaced in the VirusTotal report or MalwareBazaar dossier.

Sources from the original pack with no XTM One integration:

- **AlienVault OTX** (community pulses, campaign attribution): use OpenCTI, MISP, abuse.ch ThreatFox / MalwareBazaar, Google Threat Intelligence, Recorded Future or Feedly (`search_feedly_articles` with the hash).
- **ReversingLabs** (vendor-authoritative classification, MITRE ATT&CK, sandbox, sample fan-out): use VirusTotal (`get_vt_file_report`, `get_vt_file_behaviour`), Google Threat Intelligence, ANY.RUN, abuse.ch MalwareBazaar and the CrowdStrike sandbox.

Quota-sensitive: VirusTotal free tier is tightly rate-limited and each relationship is a separate call.

### 3. Check whether it is known-benign

If the VirusTotal detection ratio is 0/Y with Y > 30, AND no community or vendor source references it, AND the file has standard legitimate characteristics (signed by a major vendor, common packaging), treat it as likely benign. Stop and summarize; do not spend analyst time.

### 4. Consolidate

```yaml
indicator: HASH
type: hash
hash_type: md5 | sha1 | sha256
consolidated_verdict: malicious | suspicious | clean | unknown
detection_stats:
  vt_ratio: X/Y
  first_seen: DATE
  last_analysis: DATE
  names: [ALIASES]
classification:
  malware_family: [popular_threat_classification from VT, MalwareBazaar signature, vendor family]
  tags: [[from VT / MalwareBazaar]]
  attributed_actor: [if a vendor or internal source names one]
file_details:
  type: [PE, ELF, Office, script, etc.]
  size: BYTES
  packers: [IF_DETECTED]
  signed: BOOLEAN
  signer: IF_SIGNED
behavior_summary:
  sandbox_verdicts: [from VT / ANY.RUN / CrowdStrike sandbox]
  notable_behaviors: [[from VT tags / behaviour summary]]
  mitre_techniques: [[if reported by a sandbox, or inferable]]
internal_context:
  opencti: [observable / indicators / linked entities - or "not found"]
  misp: [events and creator orgs - or "not found"]
  estate_prevalence: [Defender org prevalence, if available]
community_context:
  report_count: [reports / articles referencing the hash, per source]
  campaign_references: [<...>]
sources_skipped: [[integrations not connected or that errored]]
pivot_candidates:
  - type: ip
    value: [c2 ip from contacted_ips]
    reason: c2_comm
  - type: domain
    value: C2_DOMAIN
    reason: c2_comm
  - type: hash
    value: PARENT_OR_DROPPED
    reason: child_parent
```

Do not average verdicts: reason about agreement between sources and weight each by its Admiralty rating.

### 5. Prioritize pivots

1. **C2 IPs / domains from contacted_ips / contacted_domains** - active infrastructure
2. **Parent hashes** (droppers / packers, `execution_parents`) - same campaign lineage
3. **Dropped hashes** (payloads) - behavioural family
4. **YARA rule matches** (if exposed) - strong family clustering
5. **Command-line strings** - targeting / configuration hints

### 6. Decide on deeper analysis

If the detection ratio is high and the malware family is known, the lookup may be sufficient. If the hash is novel (low detection, recent first seen, no known family), load the **Malware Analysis** skill (if attached) for a structural and behavioural review.

Also go to deeper analysis if:
- The user explicitly asked for analysis (`request_malware_analysis: true`)
- This is part of a targeted incident response, not routine IOC triage

### 7. Apply rigor (inline)

Apply these rules to the output yourself. For a finished intelligence product, also load the **Source Assessment (Admiralty Scale)** and **Confidence Levels** skills if they are attached.

**Admiralty rating per source** (reliability A-F, credibility 1-6). Starting points, adjust on content:

| Source | Default | Adjust |
|---|---|---|
| VirusTotal | B2 | C3 if only 1-2 engines detect (often a generic heuristic or false positive) |
| CrowdStrike Falcon Intelligence | A2 | B3 if malicious confidence is low / unverified; consider A1 when high confidence and a named actor / family corroborated by several reports |
| Google TI, Recorded Future | B2 | Read the evidence, not only the score |
| abuse.ch MalwareBazaar / ThreatFox | B2 | Community-submitted; check the reporter and dates |
| Sandbox reports (VT, ANY.RUN, CrowdStrike) | B2 | B3 if the sample is < 24 h old or the detonation looks incomplete (evasion, no network) |
| OpenCTI, MISP | B2 | Rate the author behind the entity / event: own analysts A1, national CERT / ISAC A1, reputable vendor A2-B2, open feeds B3-C3, anonymous contributor C3 |

**TLP**: default AMBER for investigative findings unless the user specifies otherwise; never exceed the `tlp_ceiling`. Hashes from an internal incident stay at least AMBER.

**Confidence (0-100)** on the consolidated verdict and on any family / actor attribution: Very High 90-100, High 75-89, Moderate 50-74, Low 25-49, Very Low 0-24. Justify the score with the source ratings and independent corroboration.

**Likelihood words** for uncertain or forward-looking statements: Almost certain (>95%), Highly likely (75-95%), Likely (50-75%), Realistic possibility (25-50%), Unlikely (15-25%), Highly unlikely (5-15%), Remote (<5%).

### 8. Return

```yaml
title: "Hash Investigation: HASH"
tlp: AMBER
confidence: SCORE_0_100
summary: [2-3 sentences with malware family if known]
verdict: <...>
detection_stats: <...>
classification: <...>
file_details: <...>
behavior_summary: <...>
internal_context: <...>
community_context: <...>
pivot_candidates: TOP_3_5
source_ratings: <...>
sources_skipped: [<...>]
investigation_date: ISO8601_TIMESTAMP
recommended_next_steps:
  - [e.g., "deeper review with the Malware Analysis skill", "pivot on C2 IP with the IP Investigation skill"]
```

## Special cases

- **Hash not found in VirusTotal** - does not mean clean. It could be very new, rare, or deleted. Check the other sources; go to the **Malware Analysis** skill if the user has the sample.
- **Very low detection (1-2 engines)** - often a false positive or generic heuristic. Downgrade the source rating; do not alarm the user without corroboration.
- **Signed by a legitimate vendor but flagged** - possible supply-chain scenario. Flag for special review (the **Supply Chain Threats** skill covers the patterns).

## Writing back (only when asked)

Do not write to OpenCTI or MISP unless the user or the assignment asks for it. When asked, create the file observable / indicator inside an OpenCTI draft (`create_opencti_draft`, `create_opencti_observable`, `create_opencti_indicator`) with the TLP marking; in MISP, add to an unpublished event and never publish without review.

## Related skills

- **IP Investigation**, **Domain Investigation**, **URL Investigation**
- **Malware Analysis** - for novel or targeted samples
- **YARA Rule Writing** - if the sample should produce a detection rule
- **Indicator Pivoting** - multi-hop IOC graphing
- **CTI Orchestrator**
