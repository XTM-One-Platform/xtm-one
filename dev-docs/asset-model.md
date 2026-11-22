# Asset data model across the XTM Platform

Status: reference, #11781 (14 Nov 2026). Read from the live code of each product on that date:

| Product | Source read |
| --- | --- |
| OpenAEV (the reference) | `openaev-model/.../database/model/Asset.java`, `AssetCategory.java`, `AssetSubCategory.java`, `AssetCriticality.java`, `AssetGroup.java`, `Endpoint.java`; migration `V5_31__Add_asset_categorization` (#6452) |
| OpenCRQ | `module/src/modules/supporting-assets/schema.ts` (`supporting_assets`, `supporting_asset_sources`, endpoint facts) and `constants.ts` |
| OpenCTI | `opencti-graphql/src/modules/attributes/stixDomainObject-registrationAttributes.ts` (`System`, an Identity) |
| XTM One | `backend/app/services/organization_context.py` (`AssetEntry`, `AssetGroupEntry`, `AiSystem`) |

XTM One keeps its organization profile in OpenAEV's vocabulary, lower-cased as OpenCRQ already stores it, so an
asset moves between the three without a lookup table beyond the case.

## 1. Type: category and subcategory

| | OpenAEV | OpenCRQ | OpenCTI | XTM One |
| --- | --- | --- | --- | --- |
| Field | `asset_category`, `asset_subcategory` (enums, upper case) | `category_id`, `subcategory_id` (text, lower case) | none (`System` only; `identity_class` is free) | `category`, `subcategory` |
| Categories | 12: HOST, CONTAINER_WORKLOAD, CLOUD_RESOURCE, WEB_APPLICATION, NETWORK_DEVICE, MOBILE_DEVICE, IOT_OT_DEVICE, IDENTITY, SAAS_APPLICATION, AI_TARGET, SECURITY_PLATFORM, GENERIC_ASSET | 12: the same, lower case (`security_platform` rows are kept out of every CRQ reading: no technique, no family gap) | none | the 12 of OpenAEV, lower case |
| Subcategories | 80, each bound to one category | 80: OpenAEV's, lower case | none | the 80 of OpenAEV; a subcategory must belong to the row's category |
| Unclassified | GENERIC_ASSET | `generic_asset` (the to-classify bucket) | n/a | `generic_asset` (the default) |

Conversion: XTM One to OpenAEV is `upper()`; OpenAEV to XTM One is `lower()`. XTM One to OpenCRQ is identity,
`security_platform` and its subcategories included (OpenCRQ-Platform/opencrq#3152).
OpenCTI to XTM One: a `System` is `generic_asset` until a person classifies it.

`ASSET_TAXONOMY` in `organization_context.py` is the one copy in XTM One; the backend validates every row against it.

## 2. Exposure

| OpenAEV `asset_internet_facing` | OpenCRQ `exposure` | OpenCTI | XTM One `internet_facing` |
| --- | --- | --- | --- |
| `true` | `internet_facing` | none | `yes` |
| `false` | `internal` | none | `no` |
| `null` | `unknown` | none | `unknown` |

XTM One keeps the three-valued yes / no / unknown: it says "unknown" where OpenAEV has a null, which the
prioritization policy (`internet_exposure_required`) must tell apart from "no".

### 2.1 Reachability: the rule of the Exposed layer

The Exposed layer of the CTEM prioritization funnel keeps a vulnerability when it sits on a reachable
asset. `backend/app/services/asset_reachability.py` (`asset_reachability`) is the one implementation; the
rule is normative:

1. Exposed removes a vulnerability only when every asset it sits on is proven unreachable. In doubt it stays in.
2. A user endpoint is always reachable, whatever `internet_facing` says: category `host` with subcategory
   `workstation`, `laptop` or `thin_client`, and every `mobile_device` (`smartphone`, `tablet`); a legacy
   `endpoint` row is read as `host` / `workstation`. Users open email and browse the web on them: phishing,
   the browser and removable media reach them, and local privilege escalation is the second step of most
   intrusions.
3. `internet_facing` `yes` is reachable; `unknown` stays in, never filtered.
4. Only `internet_facing` `no` on an asset that is not a user endpoint (a server, a virtual machine, a network
   or OT device...) is unreachable, and only when nothing else marks it reachable: an asset group with
   `internet_facing` `yes` it belongs to makes it reachable.
5. "Internet-facing only" is the opt-in policy `internet_exposure_required` (off by default and in the
   recommended policy), never the meaning of Exposed. Turning it on drops the findings on user endpoints too.

| Asset | `internet_facing` | Verdict | Reason |
| --- | --- | --- | --- |
| host / laptop, workstation, thin_client; mobile_device | any | reachable | `endpoint` |
| any other | `yes` | reachable | `internet_facing_yes` |
| any other, in a group marked `yes` | `no` or `unknown` | reachable | `group_internet_facing` |
| any other | `unknown` | unknown (kept) | `unknown` |
| any other | `no` | unreachable | `proven_unreachable` |

The layer is live when the counted vulnerabilities can be joined to the profile assets (an asset reference
matches a row on `source` and `external_id`, else on its name). The join (`_exposed_join` in
`ctem_metrics_collector.py`) reads OpenAEV's CVE findings (`/api/findings/search`, `finding_type` `cve`,
at most 2,000): each names the endpoints a CVE was found on. A CVE whose every endpoint is proven unreachable
(`unreachable_keys`) is counted once in the last live layer through OpenCTI (its name under that layer's
filter) and taken off; every other vulnerability of the layer stays in. When the join cannot be made
(OpenCTI or OpenAEV not connected, findings past the cap, a tailored layer too wide to carry a filter), the
layer is `not_available` with the reason `assets_not_linked` and the reachability split of the assets read
(`reachable`, `unreachable`, `unknown`, `endpoints`); it is `not_configured`, with the "Add your assets and
exposure" call to action, only when `assets_exposure` is empty.

An OpenAEV endpoint imported without a subcategory of its own takes the one its `endpoint_platform` names
(`PLATFORM_TAXONOMY` in `organization_context_import.py`): Windows and MacOS are `host` / `workstation` (user
endpoints, rule 2), Linux is `host` / `server`, iOS and Android are `mobile_device` / `smartphone`, Container
is `container_workload` / `container`; OpenAEV's own subcategory wins, and `internet_facing` keeps OpenAEV's
value.

## 3. Criticality

| OpenAEV `asset_criticality` | OpenCRQ `criticality` | OpenCTI | XTM One asset `criticality` |
| --- | --- | --- | --- |
| UNKNOWN, LOW, MEDIUM, HIGH, VERY_HIGH | `unknown`, `low`, `medium`, `high`, `very_high` (declared, or derived by a rule: `reported_criticality_rule`) | none | `unknown`, `low`, `medium`, `high`, `very_high` |

The crown jewels of the profile keep their own four-level scale (`low` .. `critical`), the business view;
an asset's criticality is the technical one and maps to OpenAEV as is. When an asset is pushed to a
crown-jewel-driven tool, `critical` reads as `very_high`.

## 4. Owner

| OpenAEV | OpenCRQ | OpenCTI | XTM One |
| --- | --- | --- | --- |
| `asset_linked_person` (text) | `owner_id` (a platform user) and `linked_person` (text) | `createdBy` / `objectAssignee` (references) | `owner` (text) |

Conversion: XTM One `owner` to OpenAEV `asset_linked_person`. To OpenCRQ it is sent as `ownerName`: OpenCRQ resolves it
to one tenant member (email, then display name, exact and case-insensitive) and sets `owner_id` with
`owner_source = resolved`; no match or several matches leave `owner_id` unchanged, an owner a person set is never
replaced, and the text is kept in `linked_person`. The create and update tools say which of `resolved`, `unresolved`,
`ambiguous` or `kept_manual` OpenCRQ applied.

## 5. Groups

| OpenAEV | OpenCRQ | OpenCTI | XTM One |
| --- | --- | --- | --- |
| `AssetGroup`: its own entity (`asset_group_name`, `_description`, `_external_reference`, tags), static members (`asset_group_assets`) or dynamic (`asset_group_dynamic_filter`) | asset groups per tenant (name, external reference), static members recording their source; a push sends `groups` (name, optional external reference) and replaces only its own memberships | none | `asset_groups`: its own list (name, description, environment, exposure, owner, tech stack, tags, source, external id); an asset joins one through `group` |

Before #11781 a group was a row of `assets_exposure` typed `asset_group`. A stored profile is upgraded on read:
each such row moves to `asset_groups` with every field it had; a nested `group` (OpenAEV has no nested groups) is
kept as the tag `group:<name>`. Dynamic groups are not modelled in XTM One: a sync from OpenAEV reads their members
as resolved at sync time.

## 6. Tags, environment, external id, platform

| Concept | OpenAEV | OpenCRQ | OpenCTI | XTM One |
| --- | --- | --- | --- | --- |
| Tags | `asset_tags` (tag entities) | `supporting_asset_tags` with their origin (`tag_sources`: a person, OpenAEV, XTM One) | `objectLabel` | `tags` (names) |
| Environment | none (a tag by convention) | none | none | `environment` (production, staging, development, test, other); pushed to OpenAEV as the tag `env:<value>` |
| External id | `asset_external_reference` | `external_reference`, and per source `source_asset_id` | `externalReferences` | `external_id`, with `source` naming the origin |
| Tech stack | none on the asset (Endpoint: `endpoint_platform`, `endpoint_arch`, `endpoint_is_eol`) | endpoint facts: `operating_system`, `os_version`, `platform`, `arch`, `is_eol` | none | `tech_stack` (product names of the profile's tech stack) |
| Network | `asset_hostname`, `asset_ips`, `asset_mac_addresses`, `asset_seen_ip`, `asset_url` | endpoint facts: `hostname`, `ip_addresses`, `mac_addresses`, `seen_ip`; `url` | none | none (the profile names assets, it is not an inventory) |
| Cloud | `asset_cloud_provider`, `asset_cloud_native_type`, `asset_cloud_region` | `cloud_provider`, `cloud_native_type`, `cloud_region` | none | none |

## 7. AI systems

`AiSystem` (`name`, `kind`, `owner`, `exposure` internal / partner / public, `openaev_asset_id`) is the inventory of
AI systems the organization runs. In OpenAEV the same object is an asset of category `ai_target`
(`llm_model`, `ai_agent`, `mcp_server`, `rag_pipeline`) with its `ai_target_*` fields; `openaev_asset_id` links
the two. It stays a list of its own in the profile (it carries the AI governance fields), and an AI system that
is also part of the attack surface is an asset of category `ai_target` too.

## 8. OpenCTI Systems

OpenCTI models an asset as `System` (an Identity). Since OpenCTI-Platform/opencti#19943 a System carries the
OpenAEV taxonomy: `x_opencti_system_category` (vocabulary `system_category_ov`), `x_opencti_system_subcategory`
(`system_subcategory_ov`, its category named in each entry's description), `x_opencti_criticality`,
`x_opencti_internet_facing` (yes, no, unknown), `x_opencti_environment`, `x_opencti_owner` and
`x_opencti_platform`. It still has no group, no network or cloud facts and no endpoint facts.

The import of Settings > Organization (`backend/app/services/organization_context_import.py`,
`opencti_system`) maps a System as follows:

| Profile field | OpenCTI attribute | Fallback |
|---|---|---|
| name | `name` | |
| category, subcategory | `x_opencti_system_category`, `x_opencti_system_subcategory` (a subcategory alone gives its category; one of another category is dropped) | the batch type the administrator picks |
| criticality | `x_opencti_criticality` | `unknown` |
| internet_facing | `x_opencti_internet_facing` | `unknown` |
| environment | `x_opencti_environment` (a value outside the profile's list becomes `other`) | empty |
| owner | `x_opencti_owner` | `createdBy.name` |
| tags | `objectLabel`, plus `platform:<x_opencti_platform>` and `marking:<definition>` per marking | |
| external_id | `id` | |

An OpenCTI without these attributes (checked by introspection of the `System` type) is read with the base
query, its taxonomy filters are dropped, and every System takes the batch type with exposure and
criticality `unknown`.

Re-import is keyed on source and `external_id`: a row this source wrote is updated, a row typed by hand
(`manual`, `csv`, `api`) is never overwritten, and a row this source wrote that the read no longer returns
is reported as gone, kept unless the administrator removes it. A truncated or failed read removes nothing.

## 9. One thing across sources (#1178)

`backend/app/services/organization_asset_identity.py` is the pure rule (no I/O, no model call) that turns the
records several sources hold on one thing into one canonical thing; the store of the next slices persists it.

- Identity: strong keys in a configurable order (`DEFAULT_KEY_ORDER`: serial, cloud resource id, device id, MAC,
  FQDN, hostname), each normalised (placeholder serials and null MACs dropped). An IP address is weak: it never
  merges, it is answered as a possible match. No merge when one source holds two ids, when a higher-ranked key
  differs on both sides, or when an administrator split the pair; an administrator's merge wins over the keys.
  The answer does not depend on the order of the records and says why (`merged on serial`).
- Fields: each field keeps every source's value; the value shown follows a per-field precedence of source classes
  (`DEFAULT_PRECEDENCE`: endpoint manager and EDR for OS and software, OpenCRQ for criticality, OpenAEV for
  exposure), customisable; `unknown` never wins over a known value; local fields come from hand sources only.
- Software: one entry per vendor, product and version across sources, with first and last seen; an unknown
  version is `unknown`, never empty.
- Tech stack: derived from asset software (`derive_tech_stack`), one item per vendor and product with the
  versions seen and the assets running each; typed SaaS and cloud rows stay, without versions. The CPE comes
  from an administrator's override (kept across syncs), else a source that reported it, else a dictionary
  lookup with its confidence, else `unresolved`: never guessed.
- Sections: asset groups (external reference, then name) and the name-keyed sections (tech stack on CPE then
  vendor and product, crown jewels, AI systems, PIRs, tracked organizations) use the same matching.

### 9.1 The store (S2)

`backend/app/models/organization_asset.py` keeps the engine's answer per organization, so the asset list is no
longer bound by the profile JSON (5,000 rows) and an asset can carry hundreds of software entries:

| Table | One row per | Unique | Index |
|---|---|---|---|
| `organization_assets` | canonical asset: name shown, `fields` (value, source, every source's value), `merged_on`, `possible_matches`, `profile_version` | `(organization_id, canonical_key)` | `(organization_id, name_key)` for the list and its search |
| `organization_asset_sources` | source record: source, its id there, identifiers, fields, last seen | `(organization_id, source, external_id)` | `asset_id` |
| `organization_asset_software` | vendor, product, version on an asset (`unknown` when no source knows it), CPE, PURL, install date, first and last seen, sources | `(asset_id, vendor_key, product_key, version)` | `(organization_id, product_key, version)` for "assets of a software" |

The migration (`o1178assets01`) creates the tables and backfills nothing. `organization_asset_store.ensure_built`
builds an organization's store from its current profile on the first read and rebuilds it when a newer profile
version is saved (one advisory lock per organization, so two first reads build once). An asset whose canonical key
is unchanged keeps its id across rebuilds, so an id read from the API stays valid after an unrelated save. The
profile stays the source of the rows and the store is its canonical view (S3: refreshed at every save, 9.2).

Read API (any signed-in user, primary organization, every list paginated, `limit` at most 200):

- `GET /api/v1/organization-context/assets?q=&offset=&limit=`: canonical assets by name, each with its sources and
  software count.
- `GET /api/v1/organization-context/assets/{id}?software_offset=&software_limit=`: one asset, each field with the
  source that set it and every source's value, the merge explanation, its records and a page of its software.
- `GET /api/v1/organization-context/assets/by-software?product=&vendor=&version=`: the assets running a software,
  with the version each runs.

### 9.2 Writes through the store, merge and split (S3)

- **Every save refreshes the store.** `organization_context.save_version` calls
  `organization_asset_store.refresh_after_save` in the same transaction: a sync run, an import, the form, the
  agent row tools (`upsert_organization_rows`, `remove_organization_rows`) and a proposal all rebuild the store
  with the rows they saved. A refresh failure is logged and never fails the save; the next read rebuilds
  (`ensure_built`).
- **One asset, several records.** In the `assets` section only (`MULTI_SOURCE_SECTIONS` of
  `organization_context_import` and `organization_context_rows`), a row whose name matches a row of another
  source is added beside it as a second record instead of being skipped (import, sync) or refused (agent
  upsert). The other row is never overwritten: an agent still cannot change a platform-sourced row, and a row of
  its own source is still keyed on `source` + `external_id`. The store then joins the records on their
  identifiers (section 9). Asset groups and the name-keyed sections keep one row per name.
- **Merge and split.** `organization_asset_decisions` (migration `o1178assetdecisions01`) keeps an
  administrator's `merge` (two to 50 records that are one asset) or `split` (two records that are never one
  asset), applied by every rebuild through `identity.Decisions`. API, administrators, audited
  (`entity_type organization_context`, `entity_name asset_decision`):
  - `GET /api/v1/organization-context/assets/decisions` (any signed-in user): the decisions, newest first.
  - `POST /api/v1/organization-context/assets/decisions` `{kind, records: [{source, external_id}], reason}`: 201
    with the decision; 422 for a record the store does not hold, a wrong record count, a decision already
    recorded, or a record addressed by its position (`row-<index>`, a row typed by hand with no `external_id`:
    the address moves when a row above it is removed, so the row gets an `external_id` first).
  - `DELETE /api/v1/organization-context/assets/decisions/{id}`: 204; the keys decide again.

## 10. OpenCRQ alignment

OpenCRQ-Platform/opencrq#3152 closed the three divergences: `security_platform` and its 10 subcategories are
accepted (section 1), an asset carries groups and tags (sections 5 and 6), and an owner sent as a name is resolved
to a tenant member (section 4). The OpenCRQ tools of XTM One (`create_opencrq_supporting_asset`,
`update_opencrq_supporting_asset`) send all of them.
