# FIMI DISARM Mapping

Map the behaviours observed in an information operation to DISARM techniques and record them in OpenCTI as Attack Patterns used by the annual Campaign. Method (OpenCTI doctrine §3.2.2 and §4.6.1): describe the behaviour first, look the technique up in VIGINUM's bilingual referential (DISARM v1.3), never take an identifier from memory.

## When to use
- The analyst asks which DISARM techniques describe a behaviour, a campaign or a report.
- A report is being capitalised and the TTP list (annex 3.1 of the Matriochka report, June 2024; annex 6.4 of the Storm-1516 report, May 2025) must become `uses` links.
- Existing `Campaign uses Attack Pattern` links need review (wrong identifier, missing dates, duplicate pattern).

## Procedure
1. **Fix the frame.** Identify the annual campaign (`search_opencti` on `Campagnes de RRN en 2023` style names; a non-attributed campaign if none). Read what is already mapped: `list_opencti_relationships` from the campaign, keeping `uses` towards Attack Pattern. Attack Patterns hang from the Campaign, not from the Intrusion Set: inference exposes them on the set (doctrine §2.2).
2. **Write each behaviour as an observable action**, without any technique word: who or what asset, did what, to what, on which platform, when, and the source passage. Example (Matriochka, §2.1): "a first group of X accounts posts a fake news-article image; 30 to 45 minutes later a second group quotes that post in reply to media posts". One sentence per behaviour; intent goes on a separate line.
3. **Find the phase.** Walk the 16 tactics in the referential's order: TA01 Plan Strategy, TA02 Plan Objectives, TA13 Target Audience Analysis, TA14 Develop Narratives, TA06 Develop Content, TA15 Establish Social Assets, TA16 Establish Legitimacy, TA05 Microtarget, TA07 Select Channels and Affordances, TA08 Conduct Pump Priming, TA09 Deliver Content, TA17 Maximize Exposure, TA18 Drive Online Harms, TA10 Drive Offline Activity, TA11 Persist in the Information Environment, TA12 Assess Effectiveness. Goal (TA01-02), preparation (TA13 to TA07) or action in the field (TA08-TA11)? Both VIGINUM annexes list nothing under TA05, TA10, TA12 or TA18: do not force a technique into a phase the evidence does not reach.
4. **Look up candidates.** `search_knowledge` with `knowledge_base` = `VIGINUM FIMI Doctrine`, once in French and once in English ("typosquatting nom de domaine", "Spoof Parody Account Site", "comptes jetables", "Create Anonymous Accounts"). Read the entry, its parent and its siblings (French and English titles and descriptions) and compare the description with your sentence, not the title with your vocabulary.
5. **Choose the most specific supported entry.** Take the sub-technique only if the evidence covers it, the parent otherwise. One behaviour may carry several techniques in different tactics (a fake news site is T0013 in TA15 and T0098.001 in TA16). When capitalising a VIGINUM annex, reproduce its list as published (it lists a parent and its sub-techniques together, for example T0090 and T0090.001); when mapping your own observations, prefer the sub-technique alone.
6. **Verify every identifier** by retrieving its heading from the referential in this session. Known quirks, to report and not to "fix": the French column writes `T0105` for T0105.001 and `T0132.00` for T0132.001 (the referential takes the English identifier); T0114.001 appears twice (Social media, Traditional Media); T0084.003, T0084.004 and T0049.005 lack a French title or translation.
7. **Find the existing Attack Pattern before creating one.** `search_opencti` on the identifier (T0013) and on the English title (Create Inauthentic Websites); `get_opencti_entity` on each hit to see how the identifier is carried. Reuse the hit matching the identifier; report a duplicate rather than merging it. Only if nothing matches, plan an Attack Pattern named `[T0013] Create Inauthentic Websites` (English title and description from the referential).
8. **Date the `uses` link** per the doctrine's four cases (§4.6.1): start and end observed, both dates; start observed and activity ongoing, start only; only an end observed, end only; no date in the document, none. The attribution rule (start = publication date, no end) does not apply. For a month-only date the doctrine is silent: propose the first of the month, say so, let the analyst decide. Each link also carries the document's author, marking and a description citing the passage.
9. **Present the plan, then wait.** One table: behaviour, passage, tactic, technique, existing pattern or "to create", dates, confidence. Then, only after the analyst confirms: `create_opencti_draft`, `use_opencti_draft`, `create_opencti_entity` (missing Attack Patterns), `create_opencti_relationship` (Campaign `uses` Attack Pattern, with dates), `update_opencti_relationship_field` to fix dates, `add_opencti_marking`, and `add_opencti_to_container` to attach the links to the Report. Never write outside a draft.

## Worked examples (VIGINUM's own lists)
| Source | Behaviour (passage) | Technique (listed by the annex; the pairing is a reading to confirm) |
|---|---|---|
| Matriochka, annex 3.1 | seeders post fakes, quoters reply under the targets' posts (§2.1) | T0115 Post Content, T0116 Comment or Reply on Content, in TA09 |
| Matriochka, annex 3.1 | accounts without biography, with generated photos, probably bought (§2.1); emails to media (§2.1) | T0090.001 Create Anonymous Accounts; T0128.004 Launder Information Assets, hedged: the report says "possibly bought"; T0112 Email |
| Matriochka, annex 3.1 | fakes impersonating media, officials, organisations | T0099 family; T0099.003 to T0099.006 are not in the referential (see Pitfalls) |
| Storm-1516, annex 6.4 | CopyCop fake news sites, more than 290 (§3.1.3) | T0013, T0098.001 (create), T0049.007 (sites amplify narratives) |
| Storm-1516, annex 6.4 | fake whistleblowers on disposable accounts since August 2023 (§3.1.1) | T0090.001, T0097 Create Personas |
| Storm-1516, annex 6.4 | AI-generated video and voice since February 2024 at least (§2.3.1) | T0087.001, T0088.001: dated from 2024-02, start only |
| RRN, June 2023 synthesis | typosquatted names of national media and government sites | the referential's T0099.002 describes typosquatting and cites RRN/Doppelgänger; the RRN report itself cites no identifier: say it is your mapping |
| OpenCTI doctrine | Portal Kombat 2022 and 2023: [T0013] and [T0098] (figure F1); T0049.001, T0097, T0002 (§4.7.1) | illustrative, as VIGINUM warns |

## Checks before answering or writing
- Each behaviour has a source passage and a sentence free of technique vocabulary.
- Each identifier was retrieved from the referential in this session, or is flagged as absent from it.
- Description matches the behaviour; sub-technique only if supported.
- No technique stands for a target, narrative or channel (those are other entities).
- Existing pattern searched; no duplicate planned.
- Dates follow the four cases; author and marking filled; relationship direction is Campaign `uses` Attack Pattern.
- The analyst confirmed the table before any draft.

## Pitfalls
- **Versions drift.** The referential is DISARM v1.3. The reports use newer entries: Matriochka and Storm-1516 list T0099.003 to T0099.006 under "T0099 Impersonate Existing Entity" (the referential has T0099 Prepare Assets Impersonating Legitimate Entities with only .001 and .002); Storm-1516 titles T0097.001 "Produce Evidence for Persona" (referential: Backstop Personas); the doctrine's figure F7 cites [T0143.001] Authentic Persona, absent from the referential. When capitalising such an annex, keep the published identifier and title and write in the description that the referential does not list it; when mapping your own observation, stay within the referential.
- **"Blanchiment" is not T0128.004 by default.** Storm-1516 launders content through sponsored articles in foreign media (§3.2); the referential defines T0128.004 as acquiring control of legitimate accounts or channels. Compare the definition, then tell the analyst when the annex's choice and the narrative description differ.
- **Shared techniques link nothing.** Matriochka and Storm-1516 share many identifiers (T0090.001, T0128.001, T0128.004, T0099.003, T0084.002) yet remain distinct sets; techniques are no imputation evidence.
- **Planning techniques rest on the report's judgement** (T0074 is in both annexes): cite the passage stating the objective.
- Never infer an identifier from a name, and never trust the French column's identifier over the English one.

## Related skills
- `fimi-report-capitalisation`: the full report-to-graph procedure that calls this step.
- `fimi-ims-assessment`: whether the behaviours form an information manipulation set.
- `fimi-attribution-review`: attribution relationships and their dating.
- `fimi-coordination-indicators`: the evidence behind coordinated behaviours.
- `fimi-threat-brief`: presenting the profile.
