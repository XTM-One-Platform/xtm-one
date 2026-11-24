# CTEM marking and segregation parity (21 Nov 2026, updated 22 Nov 2026)

Question: does a CTEM object (a threat, an asset, a scenario, a case) stay
hidden from a user that the source product hides from them, whichever of
OpenCTI, OpenAEV, OpenCRQ or XTM One shows it? Sam's target (21 Nov 2026):
the three modules and XTM One apply the same marking and segregation rules,
a marking is the same marking in every product, and OpenCRQ carries markings
natively.

Read on the `main` of each repository on 21 Nov 2026, OpenCRQ on draft PR
OpenCRQ-Platform/opencrq#33 (head `8acf5a65`); the OpenCRQ and XTM One
columns re-read on 22 Nov 2026 on OpenCRQ `main` `824eaf06` and XTM One
`main` `e23fba89`, once every gap below had merged. Each cell is **verified**
(file and line or symbol read) or **inferred** (from a design document or
from the code around it, not exercised). XTM One's own audit is
[`ctem-data-segregation-audit.md`](ctem-data-segregation-audit.md).

## Matrix

| Dimension | OpenCTI | OpenAEV (feature-flagged, ADR-009) | OpenCRQ | XTM One |
|---|---|---|---|---|
| Tenant / org isolation | One platform per deployment; no tenant. Verified | Tenant v2 statement inspector (ADR-002), `TxCtx`. Verified (ADR-009 s.4, `ScopeDimension`) | Tenant policies on every table (`module/src/security/policies/tenant-policies.ts`). Verified | Not multi-tenant; never crosses a module tenant: one JWT per user, the module resolves the tenant (audit s.5). Inferred on the module side |
| OpenCTI organization segregation | `granted_refs` (organizations), `authorized_members` (restricted access, bypasses org sharing), applied when `platform_organization` is set and the user is outside it: `buildDataRestrictions` (`database/engine-query-builder.ts` l.253-275), `checkUserFilterStoreElements` (`utils/access.ts` l.889-913). The STIX export carries both in the OpenCTI extension (`stix-2-1-converter.ts` l.196, l.202). Verified | No such concept (no OpenCTI organizations). Inferred | Mirrored: `admittedBody` reads `authorized_members` and `granted_refs_ids` from the OpenCTI extension of the mirrored body; a reader outside the platform organization reads an object of a restricted type only when it is shared with one of their organizations or is their own individual; a reader OpenCTI did not answer for reads no member-restricted or organization-shared object; `BYPASS` passes, markings still applied (`marking-policies.ts`, `readableMirroredThreat`, opencrq #36). Verified (**G3 closed**) | Inherits OpenCTI's rule on live reads (per-user JWT). Inferred |
| Marking definitions: source and id | `Marking-Definition` entities, STIX `standard_id`; `definition_type` + `x_opencti_order`. Verified (`user-domain.ts` l.519-540) | `marking_definitions` per tenant, local `marking_definition_id`, type, definition, color, order, and since OpenAEV-Platform/openaev#9631 (squash `5fd974c4`, 22 Nov 2026) `marking_definition_stix_id`: OpenCTI's `standard_id`, derived the OpenCTI way (static TLP 2.0 ids, else UUIDv5 on type and definition), backfilled, unique per tenant, kept on an OpenCTI-format import (`MarkingDefinitionStixIds`, `V6_20261121210000000__Add_marking_definition_stix_id`). Verified | Native: `marking_definitions` per tenant under OpenCTI `standard_id`s (TLP 2.0 seeded, opencrq #43), managed by API (#49), synced from the tenant's OpenCTI at consumer start and on the stream (#51), OpenAEV definitions mirrored under their STIX id by the connector (#59). `marking_refs` on supporting assets, risk scenarios (#47), findings (#53) and threat analyses (#33). Verified (**G2 closed**) | None of its own. Verified (audit) |
| Ordinal scale | Per `definition_type`: holding a marking grants every lower `x_opencti_order` of that type (`computeAvailableMarkings`). Verified | Per type, highest granted order expanded downwards in Java; a type with no grant contributes nothing (`MarkingScopeResolver.resolve` l.64-80). Verified | Mirrored rows: OpenCTI's `me.allowed_marking`, already expanded (`reader-markings.ts`). Native and imported rows: per `definition_type`, a held marking clears every marking of its type at or below its `order` (`clearedMarkings`, `native-marking-policies.ts`). Verified | Delegated to each module. Inferred |
| Clearance source | Groups (`allowed_marking` from the user's groups). Verified (`getUserAndGlobalMarkings` l.560-587) | Groups, not roles (`groups_markings` join `users_groups`, `MarkingClearanceCacheManager` l.61-67). Verified | Mirrored rows: the reader's OpenCTI groups, asked of OpenCTI as the reader (`READER_MARKINGS_QUERY`). Native and imported rows: OpenCRQ groups (`group_marking_definitions` joined on `group_users`, `clearedMarkings`). Verified (**G2 closed**) | The user's identity in each module. Verified (audit s.1) |
| Multi-marking rule | AND: every marking of the object held (`isMarkingAllowed` l.881-887). Verified | AND: `row <@ clearance` (`is_marking_set_allowed`, `V6_20260921090000000`). Verified | AND: `marking_refs <@ clearance` for mirrored, imported and native rows (`readableThreat`, `nativeMarkingPolicy`). Verified | Inherited. Inferred |
| Unmarked rule | Visible to all, a user with no marking included (`engine-query-builder.ts` l.207-209). Verified | Visible to all; `NULL` = unmarked (`COALESCE`); "fail closed means see less, not see nothing" (ADR-009 s.4 `MarkingCtx`). Verified | Empty array visible to all, a reader with no clearance included: a reader OpenCTI did not answer for holds `[]` and reads the unmarked mirrored rows (`originMarkingScope`, `markingArray`). `NULL` on a connector row = markings unknown = hidden (stated, a pre-0005 row). Verified (**D1 closed** in #33) | Inherited. Inferred |
| Over-clearance direct read | Not found (the object is filtered out of the load). Inferred | 404, never 403 (ADR-009 s.4 governance). Inferred (decision; enforcement by the inspector) | The policy filters the row, so a detail read answers not found (`threats-router.ts` l.468-469; `nativeMarkingPolicy` doc comment). Inferred for every detail route | Cases: absent and 404 for a viewer who sees no member (audit s.2a). Verified |
| Assign only what you hold | `validateMarking` refuses a marking outside `allowed_marking` (`access.ts` l.1110-1120); sharing capped by `max_shareable_marking` (`checkUserCanShareMarkings`). Verified | Decision (ADR-009 s.4); enforcement not found by code search. Inferred | `nativeMarkingPolicy.create` refuses a marking the creator does not hold; a group cannot be cleared for another tenant's marking (#49). Verified (**G2 closed**) | N/A (writes go to the module as the user, audit s.4) |
| Bypass / background scope | `BYPASS` capability and the first admin see all; internal users skip checks (`engine-query-builder.ts` l.196). Verified | Background jobs see all; first admin holds the whole tenant scale; `BYPASS` and `user_admin` do not; connector and agent accounts hold every marking through their group (ADR-009 s.4, `MarkingScopeResolver` l.60-62). Verified | Jobs, job runs, connectors and internal reads are not narrowed (`originMarkingScope`, `nativeClearanceReader`); the super administrator reads every native row. Verified | Unattended module calls use the service-account rule (audit s.3b); CTEM fleets F1/F2/F4 read with one identity: leak, medium, decision owed (audit s.3a). Verified |
| Derived / aggregate data | Aggregations run through the same search restrictions (`buildDataRestrictions` used by the query builder, l.1620). Inferred | Out of scope, stated: expectations and native aggregates can surface hidden assets; Elasticsearch reads not covered (ADR-009 s.5). Verified | Scores blend every row, stated limit (#33 body; jobs not narrowed). Findings follow their asset's clearance (`supportingAssetMarkingPolicy`, #53). Verified | Per-viewer computation on visible issues (audit s.2a). Verified |
| Cache and eviction | Session user cache. Inferred | 5 min TTL and eviction on every reduction (`evictForUser`, `evictAll`, cache l.35-37, l.148-196). Verified | Mirrored rows: 60 s TTL, no eviction (OpenCRQ cannot observe a reduction in OpenCTI), stated limit. Native rows: no cache, the clearance is read in the statement (`clearedMarkings`). Verified | 5 min per-user view cache (audit s.2a). Verified |
| OpenAEV data in OpenCRQ | - | - | The connector translates `asset_markings` to STIX ids, stores them per source (`supporting_asset_sources.reported_marking_refs`) and projects the union on the asset; an OpenAEV id it cannot resolve fails closed (#59). Verified (**G4 closed**) | - |
| XTM One with OpenAEV marking on | - | - | - | **G5**: per-user reads verified in code (posture, stages, cases, agent tools and the MCP server mint a per-user JWT). Finding **F1, closed**: the evidence-health pass reads OpenCRQ with the integration owner's credential and named assets in system signals every administrator reads; it now names an asset only when OpenCRQ states it and its scenario unmarked (xtm-one #1242, squash `e23fba89`; opencrq #61, squash `94050cfa`). Live two-user test: owed on ctem.one |

## One marking vocabulary

A marking is identified by its STIX `standard_id` everywhere: OpenCTI's id is
the one OpenCRQ already stores, and the one an OpenAEV marking must carry to
be the same marking.

- **Gap G1, closed** by OpenAEV-Platform/openaev#9631 (closes openaev #9628,
  squash `5fd974c4`, 22 Nov 2026): a nullable `marking_definition_stix_id`,
  unique per tenant, backfilled by migration `V6_20261121210000000`, set on
  create and on the default seed, followed on a rename when derived, exposed
  in `MarkingDefinitionOutput`, exported and imported in OpenCTI format
  (`GET` and `POST .../marking_definitions/stix`, the #9361 scope) so a
  marking imported from OpenCTI keeps its `standard_id`. Additive, no change
  to `marking_ids` semantics: zero breaking change.

## Gaps and plan

| # | Gap | Repository | Slice |
|---|---|---|---|
| D1 | Unknown or empty OpenCTI clearance hides unmarked mirrored rows | opencrq | Closed: #33 |
| G1 | OpenAEV markings carry no STIX id | openaev | Closed: openaev #9631 (squash `5fd974c4`) |
| G2 | OpenCRQ has no native markings, no group clearance, no assign check | opencrq | Closed: #43, #47, #49, #51, #53, #55 |
| G3 | OpenCTI organization segregation not mirrored in OpenCRQ | opencrq | Closed: #36 |
| G4 | OpenAEV connector data unmarked in OpenCRQ | opencrq | Closed: #59 |
| G5 | XTM One never tested against OpenAEV marking | xtm-one | Code read done; F1 closed (xtm-one #1242, opencrq #61). Live two-user, two-organization retest on ctem.one owed: cases, counterparts, graph, search, agent tool reads, notifications |

Stated limits kept on purpose: derived scores and aggregates (OpenAEV ADR-009
s.5, OpenCRQ #33), a cache that cannot see a reduction made in another
product (OpenCRQ 60 s), the CTEM fleets' shared identity (audit s.3a), a
marking an imported STIX file names that the tenant does not define does not
restrict the row (OpenCRQ #55).

## Tests owed in each repository touched

A user with the marking sees, a user without does not (list, detail 404,
search, export; aggregates stated); an object restricted to one OpenCTI
organization is hidden from another; an unknown identity sees no marked
object.
