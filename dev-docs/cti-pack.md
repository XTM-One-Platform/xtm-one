# CTI pack: Feedly prompt templates and cti-skills

XTM One mode ships a threat-intelligence starter pack built from two public
sources:

- **Feedly's CTI Prompt Library** (44 prompts, four TI Essentials posts) becomes
  nine **agent templates** for the creation wizard (`app/agents/cti_templates.py`,
  content under `app/agents/seed_data/cti_templates/<slug>/`);
- **cti-skills** by Liberty91 (MIT, 72 skills) becomes fifty-one **built-in
  skills** (`app/agents/cti_skills.py`, bodies under
  `app/agents/seed_data/skill_content/cti-*.md`), the pack's scripts replaced by
  XTM One integrations.

Both ship with `platform_targets: ["xtm_one", "dev"]`; Copilot mode keeps its
general-purpose templates (`["copilot", "dev"]`). Licence and attribution:
`app/agents/seed_data/NOTICE-cti-pack.md`.

## Templates

One template per role, one manual assignment per Feedly prompt:

| Template | Assignments (Feedly prompt) |
|---|---|
| `cti-intrusion-analyst` Intrusion Analyst | Diamond Model (R1.01), Diamond Model + business risk (R3.03), visual attack flow (R1.03), IOC extraction and enrichment (R3.01) |
| `cti-adversary-analyst` Adversary Intelligence Analyst | threat actor profile (R1.12), actors to TTPs (R1.05), ATT&CK Navigator layer (R3.02), MITRE ATLAS mapping (R2.03), red team emulation plan (R1.02) |
| `cti-detection-engineer` Detection Engineer | Sentinel KQL opportunities (R2.04), Splunk SPL conversion (R2.05), detection validation and handoff (R2.06), detection rules in Sigma or another format (R3.06 + the R3.05 adapter as a `rule_format` input) |
| `cti-threat-hunter` Threat Hunter | hunt hypotheses (R1.04), hunt lead extraction (R2.07), hunt lead feasibility (R2.08), hunt package (R3.07) |
| `cti-vulnerability-analyst` Vulnerability Intelligence Analyst | assessment table (R1.06), CVE chaining (R1.07), advisory (R1.11) |
| `cti-strategic-analyst` Strategic Threat Intelligence Analyst | industry report (R1.10), threat trends (R2.09), structured threat assessment (R2.11), geopolitical implications (R1.09), tariffs monitoring (R1.08) |
| `cti-intelligence-qa` Intelligence Quality Reviewer | critic QA audit (R2.01), Admiralty source evaluation (R2.02), stakeholder feedback synthesis (R2.10) |
| `cti-risk-communications` Cyber Risk and Awareness Advisor | mitigation plan (R2.13), control-gap recommendations (R3.04), stakeholder awareness brief (R3.08), phishing newsletter (R2.12) |
| `cti-financial-services` Financial Services CTI Analyst | the ten BFSI prompts (R4.01 - R4.10) |

R2.14 ("generic chain trigger") is not converted: chaining prompts is what a
Flow or a handover between agents does in XTM One.

How a Feedly prompt maps onto the platform:

- **Organization variables** (job role, sector, region, stakeholder teams, and
  the stack lines some roles need) are the persona's `## Organization context`
  section, a `persona_field` requirement of the wizard, completed by the
  Company Context knowledge base. Each prompt keeps Feedly's defaults for a
  field left as a placeholder.
- **"Paste your report here" and per-prompt variables** are the assignment's
  `manual_trigger_parameters` (`source_material`, `layer_name`,
  `reporting_period`, `rule_format`, ...). Their values reach the run in the
  trigger payload, which the prompt reads by key - never as `{{...}}`.
- **Empty `source_material`**: each prompt has a `<collection>` block naming
  the integration tools to gather material with (OpenCTI, Feedly, the
  enrichment sources, MITRE ATT&CK / ATLAS, vulnerability intelligence, web
  search), "use only the tools you have; name the sources you skipped".
- **Writes** happen only on an opt-in input (`write_to_opencti`, into an
  OpenCTI draft) and human-in-the-loop is on for every assignment.
- **Default skills**: each template names the CTI skills its agent starts with
  (`AgentTemplate.default_skills`, below).

### Template default skills

`agent_templates.default_skills` (migration `c1cti5696001`) holds built-in
skill slugs - the `slug:<x>` tag the seed gives every company-managed built-in
skill, the only reference that survives a skill being re-created.
`app/services/agent_template_skills.py` resolves them per caller through
`user_visible_filter` (enabled, company-managed, visible; an
`access_restricted` skill only to an admin), in template order, skipping a
slug nothing carries. One reader for every surface:

- `GET /api/v1/agents/templates` serves `default_skills` and the caller's
  `default_skill_ids`; the wizard preselects them, the Skills step can drop
  them, and the wizard then sends its list even when empty;
- `POST /api/v1/agents` with a `template_slug` and no `skill_ids` attaches
  them; an explicit list (even `[]`) is kept as sent;
- the agent-builder `create_agent` tool, which takes no skill list, attaches
  them.

Template default assignments may now carry `manual_trigger_parameters`; both
create paths copy them (`default_assignment_manual_parameters`, which drops an
entry without a non-blank string `key`).

## Skills

| Group | Skills |
|---|---|
| Orchestration | CTI Orchestrator, CTI Hyperloop Framework |
| Sources | Threat Intel Sources (the 21 lookup / API skills of the pack in one guide: which source for which indicator, the tools, how to read a verdict, Admiralty defaults) |
| Investigation | IP / Domain / Hash / URL Investigation, Indicator Pivoting, IOC Enrichment Workflow, IOC Export, STIX Bundle Authoring, Malware Analysis, MITRE ATT&CK Reference |
| Analysis | Analysis of Competing Hypotheses, Key Assumptions Check, Red Team Analysis, Structured Analytic Techniques, Horizon Scanning, Threat Assessment, Threat Actor Profiling, Campaign Tracking, Vulnerability Intelligence |
| Tradecraft | Source Assessment (Admiralty Scale), TLP Guide, Confidence Levels, Likelihood Language, Intelligence Quality Control |
| Production and programme | Intelligence Writing, Writing Assessments, CTI Standard Operating Procedures, PIR Management, Intelligence Feedback Loops, CTI Stakeholder Management, Intelligence Sharing, CTI Maturity Assessment, OSINT Methodology |
| Detection | Sigma Rule Writing, YARA Rule Writing, KQL Query Writing |
| Knowledge cells | China / Russia / Iran Cyber Espionage, DPRK Cyber Operations, Ransomware Ecosystem, Infostealers, Initial Access Brokers, Hacktivism, Carding and Financial Fraud, Phishing and Social Engineering, Supply Chain Threats |
| Collection | Dark Web Collection (with five bundled `references/` files) |

Conversion rules: `node`/`python` CLIs, `curl`, env-var API keys and
`/lookup-*` skills became the XTM One tools (`tests/test_cti_pack.py` checks
every cited tool is registered); `data/` files became platform objects (OpenCTI
PIRs, drafts, indicators with the rule's `pattern_type`, notes, agent memory,
`generate_report` / `generate_file`); the self-updating knowledge cells became
read-only (they are re-synced on every boot) with a "last reviewed" date and a
"Refreshing this picture" table of tools; skills reference each other by
display name and never require more than two others (a run loads at most
`skill_load_budget` = 8 distinct skills). The pack's `cti-setup` and its
Claude Code plumbing were dropped: integrations are configured once by an
administrator.

A seed skill flagged `"resources": True` ships the files of
`seed_data/skill_resources/<slug>/` as `agent_skill_resources` rows, read with
`read_skill_resource`, and `"resource_files": {"LICENSE.md": "licenses/..."}`
ships one shared seed file under a path; both are full-synced by path and
checksum on every boot (`seed._sync_skill_resources`).

Licence (MIT), outside the text the agent reads - a copyright line and its
link in a skill body read like a task. Every CTI skill carries the notice as
`AgentSkill.license` (the Agent Skills spec's frontmatter `license`: read from
a SKILL.md on create, refresh and package import, served by the API, carried by
JSON and agent-bundle exports and catalog clones, edited in the Catalog tab)
and bundles the full text as `LICENSE.md`. The Catalog tab's *License and
attribution* section shows both (`GET /skills/{id}/license`, the skill's access
rule); the package format declares the licence's place, `LICENSE.md` at the
root (`SKILL_LICENSE_PATH`), and the agent's skill catalog does not advertise
it (`is_licence_file`); it stays readable and exportable. A public-catalog
clone now copies a skill's resources (`copy_skill_resources`, both clone paths
of `public_catalog.py`).

## Integration gaps

What the two sources rely on and XTM One cannot do yet. Every skill or prompt
concerned says so and names the closest available tools.

### New integrations

| Integration | What it would cover | Used by | Interim alternative in the content |
|---|---|---|---|
| **AlienVault OTX** (LevelBlue) | community pulses per IP / domain / hash / URL, pulse search, passive DNS | IP, domain, hash, URL investigations, Indicator Pivoting, IOC Enrichment, Threat Actor Profiling, Vulnerability Intelligence, YARA test sets, Orchestrator | OpenCTI / MISP (OTX feed connectors), abuse.ch ThreatFox, Feedly, GTI, Recorded Future |
| **Censys** | host and TLS certificate reconnaissance, certificate search by CN / fingerprint, JARM, aggregations, exposure counts per CPE | IP and domain investigations, Indicator Pivoting, OSINT, Vulnerability Intelligence | Shodan, VirusTotal certificate relationships, urlscan, DomainTools Iris, GTI |
| **ReversingLabs** (Spectra Analyze / Spectra Intelligence) | authoritative file classification and risk score, static decomposition, sandbox with ATT&CK mapping, sample fan-out, YARA corpus validation, network reputation | Hash and URL investigations, Malware Analysis, Indicator Pivoting, IOC Enrichment, YARA Rule Writing | VirusTotal, GTI, ANY.RUN, abuse.ch MalwareBazaar, CrowdStrike sandbox |
| **Certificate Transparency** (crt.sh or similar) | all certificates issued for a domain | Threat Intel Sources, OSINT, Domain Investigation | `web_fetch` on crt.sh, VirusTotal certificates |
| **Onion-indexer search** (IntelligenceX, Ahmia) | finding current `.onion` mirrors and leak mentions | Dark Web Collection, Indicator Pivoting | Intel 471, Flashpoint, Cybersixgill, SOCRadar dark-web search |
| **Telegram channel reading** (MTProto user client, read-only) | monitoring joined channels against selectors; hacktivist claims | Dark Web Collection, Hacktivism, Indicator Pivoting | Intel 471 instant messages, Flashpoint, Cybersixgill (the Telegram integration is a bot: it sends, it cannot read channels) |
| **Dark-web / identity vendors**: KELA, DarkOwl, Hudson Rock, Have I Been Pwned | underground forums and markets, stealer logs, breach exposure | Dark Web Collection, Infostealers | Intel 471, Flashpoint, Cybersixgill, SOCRadar, Recorded Future identity |
| **TAXII 2.1 client** | pushing to / polling an ISAC or partner TAXII server | Intelligence Sharing | OpenCTI data sharing (publishes from OpenCTI only), MISP |
| **MITRE D3FEND** and **MITRE CAPEC** | countermeasure and attack-pattern lookups | risk-communications and financial-services prompts (D3FEND), critic QA audit (CAPEC) | `web_fetch` on the MITRE sites |
| **Atomic Red Team** catalog, **SigmaHQ** rules | emulation test numbers, existing detections | adversary emulation plan, detection validation | OpenAEV payloads, Recorded Future detection rules, existing SIEM rules |
| **Fraud / transaction monitoring** (e.g. NICE Actimize, Feedzai) | mule accounts, payment anomalies, thresholds | fraud-cyber assessment and impact brief (BFSI) | pasted material, Company Context |
| **Third-party risk / security ratings** (e.g. BitSight, SecurityScorecard) | vendor inventory and ratings | third-party risk assessment (BFSI) | ServiceNow, Shodan exposure of the provider, Company Context |
| **Blockchain analytics** (e.g. Chainalysis, TRM) | wallet tracing | DPRK Cyber Operations | none |

### Extensions of existing integrations

| Integration | Missing capability | Used by |
|---|---|---|
| OpenCTI | import a STIX 2.1 bundle as an import job | STIX Bundle Authoring, IOC Export (today: rebuild in a draft, or attach with `upload_opencti_file`) |
| MISP | STIX 2 upload and STIX 2 export of a search | STIX Bundle Authoring, Threat Intel Sources |
| CrowdStrike | Intel MITRE ATT&CK mapping per actor | Threat Intel Sources (today: MITRE ATT&CK, `get_gti_threat_mitre_tree`, OpenCTI) |
| GreyNoise | IP similarity | Threat Intel Sources (today: `query_greynoise_gnql` on shared tags) |
| Google SecOps | YARA-L rule verification without creating a rule | detection rules in YARA-L |
| Platform | rendering an ATT&CK Navigator layer; a rule-linting tool (sigma-cli / pySigma, YARA compile) in the sandbox | MITRE ATT&CK Reference, Navigator layer prompt, detection prompts (today: layer JSON with `generate_file`, structural lint in `code_interpreter`) |

Trade-policy data (WTO, USITC) for the tariffs prompt needs no new integration:
an OpenAPI integration covers it.
