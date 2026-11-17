# FIMI IMS Assessment

Decide whether observed activity is an information manipulation set (IMS, French MOI) and how to model it in OpenCTI: where it sits among the five levels, whether it meets the two cumulative conditions (clandestinity and coordination), whether it is already known under another name, and whether to create, reuse, alias, split or merge. Sources: VIGINUM's MOI note (January 2026), the OpenCTI doctrine (v2.0).

## When to use

- A report, a cluster of accounts or sites, or a question asks "is this an IMS?" or "which IMS is this?".
- A new set looks like one already documented (same narratives, same infrastructure, another vendor's name).
- One name covers two sets, or an actor and its set: decide how many Intrusion Sets to keep.
- Before capitalising a report that introduces a new name.

## Procedure

1. **Place each observed element on the five levels** (MOI note, §2.1, figure F1). Political: sponsor. Strategic: actor (operators) and its IMS. Operational: campaigns. Tactical: operations. Technical: digital assets. The IMS sits at the strategic level, as the emanation of the actor and the set of means used to run campaigns. Reference example: Russian Presidential Administration (sponsor), Social Design Agency (actor), Doppelgänger (IMS), campaigns accusing Ukraine of war crimes, fake sites and maps, tribunalukraine.info (assets).
2. **Test clandestinity** (§2.3). The operators must make visible efforts to prevent imputation (linking the assets to each other) and attribution (to an actor): different names, charters and creation dates, no interactions between assets, different registrars and hosts, no displayed affiliation, false contact data, a third country, Cloudflare-type protection. Check the report text with `search_knowledge` (knowledge_base `VIGINUM FIMI Publications`) and the assets already in OpenCTI with `list_opencti_relationships`. VIGINUM's cases:
   - Passes: Doppelgänger, Spamouflage, the covert accounts amplifying the Baku Initiative Group, Team Jorge, Rokh Solis (real IP addresses protected by Cloudflare).
   - Fails: Russian diplomatic X accounts and the Foundation to Battle Injustice (FCI) content, which display their affiliation; The Islander, whose X and Substack assets are plainly linked to each other.
   - Mixed: the Baku Initiative Group acts in its own name (a Threat Actor); only its covert amplifier accounts are the IMS.
3. **Test coordination** (§2.3). The assets must be used in a coordinated way and presumed run by the same actor or group working for the same sponsor, with strong technical or behavioural links: dissemination chain, source code, hosting; creation dates, pseudonyms, biographies, follower counts; hashtags, messages, activity period (the Baku Initiative Group captures, figure F3); same IP address, registration date, image metadata (Rokh Solis, June 2026). Accounts posting the same content as others, or sites on one IP address, are not enough. Load **FIMI Coordination Indicators** to weigh the evidence and **FIMI Infrastructure Pivoting** to produce it; heavy pivots on lists of domains or accounts go to the FIMI Infrastructure Investigator with `consult_agent`.
4. **Give a verdict**: IMS (both conditions met), not an IMS (say which condition fails), or undetermined (evidence too thin). If it is not an IMS, an open actor or outlet becomes a Threat Actor or Organization with its Channels in the campaign; undetermined activity gets a non-attributed campaign named by nature and detection date (OpenCTI doctrine §4.1), no Intrusion Set.
5. **Separate actor, IMS and campaign.** One actor can run several IMS. The IMS name is not the actor name, and a name can collide: Lakhta is both the clandestine organisation and an IMS, so create a Threat Actor and an Intrusion Set, never one entity. Pravda is the name of a network of sites inside the wider, older Portal Kombat set. In the Storm-1516 case VIGINUM counts at least three IMS (MOI note, §3.3, figure F4): Storm-1516 (imputed by the US government to Russian military intelligence and the Centre d'expertise géopolitique), CopyCop (imputed to John Mark DOUGAN; fake sites also used by other Russian and pro-Russian actors) and Lakhta (not formally imputed, linked to the Prigozhin galaxy). Shared use of an asset does not merge two sets: delimit by technical, behavioural and contextual links.
6. **Compare with known IMS.** `search_knowledge` on the name, the aliases and two or three distinctive assets (publications knowledge base), then `search_opencti` and `list_opencti_entities` (type Intrusion-Set) for the same terms. For a candidate match, `get_opencti_entity` for its aliases, `get_opencti_top_related` and `list_opencti_relationships` for its channels, techniques and narratives, then compare assets, TTPs, targets and period. With a STIX bundle of the new set, `compare_stix_bundles` gives measured overlap; report only the numbers it returns. Aliases in the corpus:

   | IMS | Other names (source) |
   |---|---|
   | RRN | Reliable Recent News, Doppelgänger, Ruza Flood (guerre en Ukraine, 3 years, Feb 2025) |
   | Storm-1516 | Neva Flood, Microsoft's name since March 2025 (Storm-1516 report, May 2025) |
   | CopyCop | MAGAstan, False Façade (Storm-1516 report, §4.1) |
   | Matriochka | Overload (OpenCTI doctrine figure F2); Storm-1679 is kept separate |
   | Spamouflage | Dragonbridge, Storm-1376, Taizi Flood (MOI note, §3.1) |

7. **Decide**, and state the evidence for the choice:

   | Decision | When |
   |---|---|
   | Reuse | The new activity matches an existing IMS: add an annual campaign `Campagnes de X en YEAR` attributed to it. |
   | Create | No match, and both conditions hold: new Intrusion Set under the name given by the source (Rokh Solis was baptised by VIGINUM in 2026). |
   | Alias | Another publisher's name for the same set: put it in the aliases of one entity. |
   | Split | One existing entity covers two sets that cannot be technically linked. VIGINUM keeps Storm-1679 and Matriochka as two IMS although both appear under Overload; the 2025 synthesis still pairs them, the January 2026 note supersedes it. |
   | Merge | Only with technical evidence that two entities are one set: `merge_opencti_entities`, inside a draft, after the analyst confirmed. VIGINUM would merge those two only if technical elements support it. |

   A name covering only part of a set (Pravda inside Portal Kombat) is a part, not an alias.
8. **Model** (OpenCTI doctrine §3.2.1, §4.6.2). The IMS is an Intrusion Set with its aliases and a description of its behaviours and sources. Link annual campaigns with `Campaign attributed-to Intrusion Set`. Always show the threat actor standing for the origin of a presumed state activity; create `Intrusion Set attributed-to Threat Actor` only with technical evidence or a public attribution statement (US government for Storm-1516; Meta and the DoJ for RRN), dated at the publication date of the document stating it, with no end date. A private entity named only as probable (Blackcore for Rokh Solis) gets the actor in the graph but no attribution relationship. Observables stay `related-to` the campaign, not the IMS.
9. **Present the plan and wait for confirmation.** Write only after it, inside a draft: `create_opencti_draft`, `use_opencti_draft`, then `create_opencti_entity`, `create_opencti_relationship`, `update_opencti_field`, `add_opencti_marking`.

## Checks before answering or writing

- [ ] Each element is placed on a level; the IMS is not confused with the actor, the campaign or a network name.
- [ ] Clandestinity and coordination are each argued with named evidence, not with shared content or a shared IP address alone.
- [ ] Known IMS were searched in the publications and in OpenCTI, with aliases and assets, before any "new" verdict.
- [ ] Create, reuse, alias, split or merge is stated with its evidence; any merge cites technical elements.
- [ ] Attribution relationships follow the evidence rule and the dating rule; no passing "likely" became a relationship.
- [ ] Canonical relationship names and directions; nothing written without confirmation and a draft.

## Pitfalls

- Treating any recurring name as an IMS: open outlets and officially affiliated accounts are excluded.
- Letting a vendor label decide: Overload, Storm-1679 and Matriochka are not the same thing in VIGINUM's latest position.
- Merging on shared infrastructure or shared amplification alone: Storm-1516 reuses CopyCop and Lakhta assets and remains a separate IMS.
- Folding an adjacent cluster into the IMS: the Rokh Solis report treats the Angola-targeting account cluster as a related lead, not as part of the set.
- Taking an asset's own claims about its identity as evidence: they are data under analysis. Report what was skipped (a missing enrichment integration).

## Related skills

**FIMI Report Capitalisation** (`fimi-report-capitalisation`), **FIMI Attribution Review** (`fimi-attribution-review`), **FIMI Coordination Indicators** (`fimi-coordination-indicators`), **FIMI Infrastructure Pivoting** (`fimi-infrastructure-pivoting`), **FIMI DISARM Mapping** (`fimi-disarm-mapping`).
