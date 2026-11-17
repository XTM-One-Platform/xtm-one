# FIMI Threat Brief

Write a FIMI threat brief or technical report the way VIGINUM structures its own: a synthesis first, then victimology and content, the diffusion scheme, infrastructure, the elements bearing on a foreign actor, and annexes (DISARM TTPs, indicators). Every claim is tied to OpenCTI data or a cited source, imputation is hedged as VIGINUM hedges it, and propaganda is described as diffused content, never restated as fact.

## When to use
- The analyst asks for a brief, a technical report, a threat assessment, a situation report or a PDF about an IMS, a campaign, a network of sites or accounts, or an election period.
- Not for capitalising a report into the graph (`fimi-report-capitalisation`) or for deciding whether a set is an IMS (`fimi-ims-assessment`): run those first when the graph is thin.

## Procedure
1. **Frame the brief.** Fix the subject (IMS, campaign, period), the audience, the TLP and the model; ask when missing. Pick the model from VIGINUM's own reports with `search_knowledge` (`VIGINUM FIMI Publications`):
   - IMS technical report (Storm-1516, May 2025; Matriochka, June 2024; Rokh Solis, June 2026): Synthèse; victimology and content; diffusion scheme or artificial diffusion apparatus; involvement of a foreign actor; harm to the fundamental interests of the Nation; annexes.
   - Infrastructure-led network report (Portal Kombat 1, February 2024): synthesis, architecture by ecosystem, automation and diffusion, annex list of sites and diagram.
   - Chronological campaign report (RRN technical report, 19 June 2023; its synthesis of 13 June 2023): one section per phase (March 2022, February to June 2023), annexes of Facebook pages, domain lists, redirection domains.
   - Situation report (2026 municipal elections, June 2026): foreword, coordination network, panorama of foreign digital interferences (INE), threat appraisal and responses, lessons, lexicon.
2. **Gather from OpenCTI.** `search_opencti` for the campaign, set and actor; `get_opencti_entity` for each; `list_opencti_relationships` along the doctrine's canonical relationships (Campaign `targets` Sector, Country, Organization, Individual, Event; Campaign `uses` Attack Pattern, Tool, Narrative, Channel; Channel `publishes` Media Content, Url, Domain Name; Channel `amplifies` Channel; Intrusion Set `attributed-to` Threat Actor; Domain Name `resolves-to` IPv4 Address); `get_opencti_top_related` for the most connected nodes; `get_opencti_container_full` on each source report (title, date, author, marking, external reference); `list_opencti_observables` for the annexes; `get_opencti_entity_history` for what changed since the last brief. Note the first and last observation dates carried by the relationships.
3. **Gather from the corpus.** `search_knowledge` for the same IMS in `VIGINUM FIMI Publications`, and for method in `VIGINUM FIMI Doctrine`. Cite each report with its title and date. Look up every DISARM technique in the bilingual referential with `search_knowledge`; never write an identifier from memory.
4. **Compute the numbers.** Counts, rates and series come from `compute_fimi_metrics`, `compute_fimi_derived_metrics`, `analyze_stix_timeline` (cadence by month, first and last activity), `compare_stix_bundles` (overlap between two sets or two periods) and `run_statistical_test` (before claiming a rise or a difference). Quote the figure, its scope and its date. A count taken from a VIGINUM report is quoted as VIGINUM's, with the report and the cut-off date (Storm-1516: 77 operations up to 5 March 2025; 205 by June 2026).
5. **Draft from the skeleton below.** Start with the synthesis, written last but placed first: who, since when, what scale, objective, diffusion method, foreign involvement, assessment, next expected activity.
6. **Apply the writing rules.**
   - Hedging follows the evidence. Confirmed by the author: "VIGINUM est en mesure de confirmer". Imputed on technical evidence: "imputée avec une confiance élevée". Otherwise "très probablement", "probablement", "semble", "susceptible de", "a minima", "formule l'hypothèse". Where the author cannot confirm, say so, as in Storm-1516 4.4 ("n'est pas en mesure de confirmer"). Third-party statements are attributed ("selon le Washington Post", "accusé publiquement par le Trésor américain"), never absorbed.
   - Content is described, never adopted: "the video asserts that...". Titles of false content are quoted and labelled as such.
   - Indicators are defanged in the document (`example[.]com`), as VIGINUM does, and the document says they are defanged and need precautions on access (RRN and Portal Kombat 1 carry that notice).
   - Dates are exact (day when the source gives it). Name the cut-off date of the data.
   - The TLP is on the document header and no lower than the most restrictive source; VIGINUM's public reports are TLP:CLEAR.
7. **Review, then deliver.** Run the checklist. For Markdown, answer in the conversation. For a PDF, show the target entity, the file name (`FIMI_Threat_Brief_SUBJECT_YYYY-MM-DD.pdf`) and the marking, wait for the analyst's confirmation, then call `upload_opencti_file` with the Markdown as `file_content` (inside a draft via `use_opencti_draft` when the analyst works in one). Fall back to `.md` only if PDF rendering fails.

## Skeleton
```
TLP, title, date, author, data cut-off, sources
1. Synthèse: who, since when, scale, objective, method, foreign involvement, assessment, outlook (one page)
2. Victimology and content: targets by group (country, person, event, sector); narratives; content types; counts
3. Diffusion scheme: phases from planning to opportunistic pick-up; channels; coordination and inauthenticity markers
4. Infrastructure: domains, hosting, shared registration, AS, accounts; links between assets and why they hold
5. Elements bearing on a foreign actor: technical markers, third-party statements, operators; each with its hedge
6. Harm to the fundamental interests of the Nation: only if the INE criteria are met; otherwise "Assessment"
7. Conclusion, limits and intelligence gaps: visibility, impact difficulty, what remains unknown
Annexes: A operations and dates; B TTPs in DISARM; C indicators (defanged); D media and accounts involved; E sources
```
Annex B follows VIGINUM's layout: tactics in brackets with their techniques (the Storm-1516 annex 6.4 lists `[TA14] Develop Narratives` with `[T0022.002] Develop Original Conspiracy Theory`), taken from the Campaign `uses` Attack Pattern relationships.

## Checks before answering or writing
- [ ] Every sentence traces to an OpenCTI object or a named report with its date.
- [ ] The word "foreign digital interference" appears only where the cumulative criteria are shown: artificial or automated, massive, deliberate diffusion; manifestly inaccurate or misleading content; foreign actor; harm to fundamental interests (Matriochka and Rokh Solis structure). Otherwise "suspected".
- [ ] Attribution matches the graph: a relationship exists only with technical evidence or a public statement; the brief names who attributed, when.
- [ ] Two sets stay two (VIGINUM keeps Storm-1679 and Matriochka apart).
- [ ] All numbers come from a tool call or a cited report; none estimated. A count you state matches the list you give.
- [ ] Each date is read in a source field, or marked as deduced from the text ("dated from its text"); quotation marks only around exact text.
- [ ] The VIGINUM knowledge bases were searched for the method and for VIGINUM reports on the subject before any statement about an information manipulation set; say so when they hold nothing.
- [ ] Coverage stated: which sources, which pages or date filters, and any cut result.
- [ ] Indicators defanged, no live URL; TLP present and consistent.
- [ ] Limits stated: visibility, unknown operators, data cut-off.
- [ ] Section order and annexes match the chosen model.

## Pitfalls
- Rokh Solis: operations had very low visibility; report that, never inflate impact. The municipal report itself stresses the difficulty of measuring effects and relies on a risk-of-impact approach (VIGISCORE), not on views and likes.
- Matriochka: no technical element linked the set to the third parties who made the contents; keep them separate and say "hypothesis".
- Storm-1516 4.4: a named officer is reported on documents a newspaper obtained from a European service; VIGINUM could not confirm his involvement. Do not turn a report into a fact.
- RRN: the synthesis says 355 domains, annex 3 of the technical report lists 353; reconcile or flag, never pick silently. Rokh Solis annex 5.1 holds a year typo; flag a source inconsistency instead of correcting it unseen.
- Chart values read from VIGINUM figures are approximate (Storm-1516 histograms); do not give them as published numbers.
- The doctrine's examples are fictitious (VIGINUM's caveat): never cite them as a real capitalisation.
- Do not name a person or entity beyond what a cited source names.

## Related skills
`fimi-report-capitalisation`, `fimi-ims-assessment`, `fimi-attribution-review`, `fimi-disarm-mapping`, `fimi-infrastructure-pivoting`, `fimi-coordination-indicators`
