# FIMI Attribution Review

VIGINUM separates imputation (technically linking observables, campaigns or a set to an information manipulation set) from attribution (a political choice to name an actor); in OpenCTI both are the relationship `attributed-to`. Use it to decide how to record what a document says about who is behind an operation, and to audit the `attributed-to` relationships already in the graph.

## When to use

- A document names, suspects or merely hints at an actor, a government or a service behind an intrusion set (IMS) or a campaign, and you must decide what to write.
- The analyst asks "is this attribution justified?", "who attributed RRN to whom?" or wants the attributions of a set, campaign or actor cleaned up.
- Before a report moves to `ANALYZED`.

## Procedure

### A. Recording the attribution a document states

1. **Locate every attribution statement.** Read the report (`get_opencti_container_full` if it is already in OpenCTI) or the source text. For each statement note: who states it (VIGINUM itself, a government, a platform or vendor, the press), about which set or campaign, towards which actor, and on what basis (technical elements, public declaration, or nothing). Doctrine passages: `search_knowledge` (`knowledge_base` = `VIGINUM FIMI Doctrine`, query "Modéliser l'imputation et l'attribution").
2. **Classify each statement** (OpenCTI doctrine, §3.2.1, figures F5 and F6):

| Case | What the document gives | Graph |
|---|---|---|
| 1. Technical evidence | elements linking the set to the actor | create `attributed-to` |
| 2. Public attribution statement, substantiated or not | a government, platform or vendor attributes | create `attributed-to`, to keep the trace of who attributed, when, with which marking |
| 3. Passing mention | "likely run by the Russian government" with no support | actor shown in the graph, **no** `attributed-to` |

   Doctrine example: CopyCop with "Acteurs russes" unlinked is case 3 (F5); CopyCop `attributed-to` Acteurs russes is case 1 or 2 (F6). If you hesitate between 2 and 3, ask whether a named party made a statement of attribution; if nobody did, it is 3.
3. **Always show the origin actor.** For presumed state activity the graph must contain the actor standing for the origin, even in case 3 (for example the meta-actor "Acteurs russes"), so the document is found from that actor's page. `search_opencti` for it first; create it only after confirmation.
4. **Choose the ends.** Allowed: Campaign `attributed-to` Intrusion Set, Campaign `attributed-to` Threat Actor, Intrusion Set `attributed-to` Threat Actor. Prefer Campaign → Intrusion Set when a set exists. Actor-to-actor links are `cooperates-with` or `part-of`, never `attributed-to`; an observable or a Channel is linked to the campaign, not attributed to an actor.
5. **Date, confidence, marking, author.** Start of observation = publication date of the document stating the attribution (not the date of the events, not today); **no end date** (OpenCTI fills both with the publication date by default: delete the end). Author = the document's author, marking = the document's marking (doctrine §4.5, §4.6.2). Put the attributing party and the date of its statement in the description, for example "Meta publicly attributed RRN to two Russian companies on 15 December 2022 (VIGINUM RRN report, June 2023)". Translate VIGINUM's wording into the confidence value the platform uses and say how you mapped it.
6. **Check what exists.** `list_opencti_relationships` on the set, with `relationship_type` = `attributed-to`. If the same author already states the same attribution, do not duplicate; a different document or author gets its own relationship, since the point is to find who attributed what and when.
7. **Present the plan** (objects, relationships with direction, dates, confidence, marking, and the case for each) and wait for confirmation. Then `create_opencti_draft` / `use_opencti_draft`, `create_opencti_relationship` (leave `stop_time` empty), `add_opencti_to_container`, and read it back with `get_opencti_relationship` to confirm there is no end date. A case 3 mention may go in a note (`create_opencti_note`) if the analyst wants the trace.

### B. Auditing an existing graph

8. **List.** `search_opencti` for the intrusion set, campaign or actor, then `list_opencti_relationships` with `relationship_type` = `attributed-to`, once from the entity and once from each of its campaigns; `get_opencti_relationship` for each.
9. **Test each relationship:** (a) direction and types in the list of step 4; (b) no end date (a start equal to the end is the signature of the untouched default); (c) start = publication date of a report that states it; (d) author, marking, confidence filled; (e) a report that contains it actually justifies it: find the reports with `search_opencti` and `get_opencti_container_full`, then reread the passage and classify it as in step 2; if the container listing does not show the relationship, say support is unverified; (f) the origin actor appears in the graph; (g) two sets the sources keep apart were not merged.
10. **Report** a table: relationship, direction, dates, author and marking, supporting report, verdict (OK, unsupported, malformed, unverified) and a proposed correction. Correction options: edit a date with `update_opencti_relationship_field` (`start_time`, `stop_time`; clear the end date in the interface if the field cannot be emptied), recreate a wrongly directed relationship in a draft, or downgrade an unsupported one to "actor shown, no relationship". This agent cannot delete a relationship: name the ones to remove. Change nothing before the analyst confirms.

## How VIGINUM words it

| Passage | Wording | Reading |
|---|---|---|
| Storm-1516 (MOI note, January 2026) | "imputé par le gouvernement américain au renseignement militaire russe et à un think tank moscovite" | case 2: a government's statement |
| CopyCop, same note and Storm-1516 report | "imputé à ... John Mark DOUGAN"; VIGINUM "est en mesure de confirmer" the links; "implication avérée" | case 1: VIGINUM's own technical work (shared IP addresses, figure F12) |
| Lakhta | "non formellement imputé mais lié historiquement à la galaxie PRIGOJINE" | no formal attribution: no `attributed-to` |
| Lakhta accounts, Storm-1516 and GlobUs reports | "liés avec une confiance élevée" / "niveau de confiance modéré" | imputation of assets to a set: goes in confidence, not an actor link |
| Storm-1516, KHOROCHENKY | "accusé publiquement", but VIGINUM "n'est pas en mesure de confirmer" | case 2 at most, with VIGINUM's reservation in the description; ask the analyst |
| RRN report, June 2023 | Meta "a publiquement attribué la campagne" to Struktura and Social Design Agency; shared director and 21 of 24 clients are VIGINUM's corroboration of the link between the two companies | case 2; start = date of the capitalised report |
| Portal Kombat 2, February 2024 | TigerWeb's role "démontré" (source code, footers, same AS); the Inforos link is only "l'hypothèse ... n'est pas à exclure" | first case 1, second not an attribution |
| African Initiative, June 2025 | "très probablement contrôlé par l'État", AI-Freak "probablement conduit par un sous-traitant", "potentiellement" | hedged assessments: case 3 unless the analyst decides otherwise |
| GlobUs, October 2026 | "considère comme probable", "estime probable" that it was used by Russian services; the network "serait lié" to Lakhta | case 3: actor shown, no relationship |

VIGINUM's own usage: campaigns are "imputées" to a set, a set is "attribué techniquement" to an actor (MOI definitions, §2.2).

## Checks before answering or writing

- [ ] Each statement is classified 1, 2 or 3, with the quoted wording and the report name.
- [ ] The origin actor is in the graph, including in case 3.
- [ ] Direction and entity types are from the canonical list; no actor-to-actor `attributed-to`.
- [ ] Start = publication date of the stating document; no end date; author, marking and confidence set.
- [ ] The description names who attributed and when.
- [ ] No duplicate of an existing relationship from the same author.
- [ ] Audit verdicts rest on a passage actually read; else say unverified.
- [ ] Nothing written without confirmation, only through a draft.

## Pitfalls

- Turning "likely" into a relationship: the Storm-1516 and GlobUs reports are full of hedged assessments that remain case 3.
- Treating an asset imputation ("lié avec une confiance élevée au projet Lakhta") as a set-to-actor attribution.
- Merging sets to simplify the graph: VIGINUM keeps Storm-1516, CopyCop and Lakhta apart.
- Dating the attribution at the date of the events or of the actor's first activity, instead of the document date.
- Leaving the default end date.
- Capitalising a third party's statement as VIGINUM's own: the author is the document's, the attributing party goes in the description.
- The doctrine's examples are fictitious or adjusted, not VIGINUM's capitalisation.

## Related skills

- `fimi-report-capitalisation`
- `fimi-ims-assessment`
- `fimi-threat-brief`
