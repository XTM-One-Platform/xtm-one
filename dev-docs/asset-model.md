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

Source code (#1233, 22 Nov 2026): a 13th category, `source_code` "Source code" (a code repository, pipeline or
package the teams build and ship), with 5 subcategories: `code_repository` (a Git repository; its SBOM is attached
to it), `ci_cd_pipeline`, `package_registry`, `software_package` (a library or artifact the organization publishes)
and `code_other`. XTM One counts 13 categories and 85 subcategories. Not assets: a third-party dependency (it is the
software of the repository, section 9), and a GitHub organization or GitLab instance (`saas_application/saas_tenant`,
a host or a web application); container images, registries and IaC templates keep their category. The value is added
in OpenCRQ, OpenAEV and OpenCTI by their own pull requests: an import reads `SOURCE_CODE` as `source_code` as soon
as a product sends it, and `manage_openaev_asset` sends `GENERIC_ASSET` with no subcategory when the connected
OpenAEV refuses `SOURCE_CODE`, the exact category staying on the XTM One row. Nothing fails and nothing is lost.
The fallback runs only when the update itself sets `SOURCE_CODE` on an endpoint not already on it and OpenAEV
answers a 400 or 422 naming the category; any other error (a 5xx, a timeout, another field refused) is
answered as it is, so an endpoint is never downgraded by an unrelated failure.
The OpenAEV write tools read before they write (xtm-one #1702): `manage_openaev_asset`, `manage_openaev_asset_group`
and `manage_openaev_inject` `update` read the stored object and send it back with only the asked fields changed,
since OpenAEV's PUT replaces the whole object. An explicit `clear` list on the asset `update` unsets a field (`url`
alone on `bulk_update`), and `get_openaev_asset` prints every field the write sets (category, subcategory,
criticality, internet-facing, URL, external reference), so a write can be read back.
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

The same endpoint carries its identity and OS for the asset store: `endpoint_platform` becomes `os_family`
through the Microsoft sources' `os_family` helper (Windows, macOS, Linux: the names Intune and Defender give, so
one machine reads one family whatever its source; `Unknown` and `Generic` give none), `asset_mac_addresses`
becomes the strong `mac` key and `asset_ips` the weak `ip` key. A MAC of a virtual interface (Docker bridge,
VMware, VirtualBox, Hyper-V, QEMU/KVM, Parallels, Xen) names no machine and is dropped by the normaliser. A row
with no hostname takes its name as hostname, so an OpenAEV endpoint named `lapt-0002` and the Intune device
`LAPT-0002` are one asset.

## 3. Criticality

| OpenAEV `asset_criticality` | OpenCRQ `criticality` | OpenCTI | XTM One asset `criticality` |
| --- | --- | --- | --- |
| UNKNOWN, LOW, MEDIUM, HIGH, VERY_HIGH | `unknown`, `low`, `medium`, `high`, `very_high` (declared, or derived by a rule: `reported_criticality_rule`) | none | `unknown`, `low`, `medium`, `high`, `very_high` |

The primary assets of the profile keep their own four-level scale (`low` .. `critical`), the business view;
an asset's criticality is the technical one and maps to OpenAEV as is. When an asset is pushed to a
primary-asset-driven tool, `critical` reads as `very_high`.

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

**Outbound to OpenAEV (#1982 S1).** An outbound `asset_groups` sync to `openaev` makes OpenAEV follow the groups
XTM One owns (typed, CSV, API, agent rows; a group an OpenAEV sync wrote is skipped):

| XTM One `asset_groups` | OpenAEV `AssetGroupInput` |
| --- | --- |
| `name` | `asset_group_name` |
| `description` | `asset_group_description` |
| members (`group` field and `membership_rule`) | `asset_group_assets` (`PUT /api/asset_groups/{id}/assets`): the OpenAEV asset id of each member, the member's own `external_id` when an OpenAEV sync wrote it, else the OpenAEV record of the same name; a member with no OpenAEV counterpart is reported (`unlinked_members`), never created as an endpoint |
| (not mastered) | `asset_group_tags`, `asset_group_dynamic_filter`: read before the write and sent back as OpenAEV holds them, since its PUT replaces the whole object (#1702) |

Only the groups the sync created (`outbound_ids`, keyed on the group name) are updated or deleted; a group leaving
the sync's filters (the import step's keys read on the XTM One row: `tags`, `search`, `source`, and on assets
`category`, `subcategory`, `criticality`, `internet_facing`, `environment`, `platform`) is deleted from OpenAEV only
when this sync created it. The same filters select the AI target assets an outbound `assets` sync pushes (section 7).

## 6. Tags, environment, external id, platform

| Concept | OpenAEV | OpenCRQ | OpenCTI | XTM One |
| --- | --- | --- | --- | --- |
| Tags | `asset_tags` (tag entities) | `supporting_asset_tags` with their origin (`tag_sources`: a person, OpenAEV, XTM One) | `objectLabel` | `tags` (names) |
| Environment | none (a tag by convention) | none | none | `environment` (production, staging, development, test, other); pushed to OpenAEV as the tag `env:<value>` |
| External id | `asset_external_reference` | `external_reference`, and per source `source_asset_id` | `externalReferences` | `external_id`, with `source` naming the origin |
| Tech stack | none on the asset (Endpoint: `endpoint_platform`, `endpoint_arch`, `endpoint_is_eol`) | endpoint facts: `operating_system`, `os_version`, `platform`, `arch`, `is_eol` | none | `products` (product names of the organization's software) |
| Network | `asset_hostname`, `asset_ips`, `asset_mac_addresses`, `asset_seen_ip`, `asset_url` | endpoint facts: `hostname`, `ip_addresses`, `mac_addresses`, `seen_ip`; `url` | none | none (the profile names assets, it is not an inventory) |
| Cloud | `asset_cloud_provider`, `asset_cloud_native_type`, `asset_cloud_region` | `cloud_provider`, `cloud_native_type`, `cloud_region` | none | none |

## 7. AI targets

Since #1501 an AI target is an asset of category `ai_target`, as in OpenAEV: subcategory `llm_model`,
`ai_agent`, `mcp_server` or `rag_pipeline`, with the `ai_target_provider`, `ai_target_endpoint`,
`ai_target_model` and `ai_target_modality` fields on `AssetEntry`. It lives in `assets_exposure`, merged and
identified like any asset; the profile has no AI list, no field, category, import section or sync scope of its own
(#1844, no alias).

- **Stored profiles**: the Alembic data migration `r1844noaisys01` turned every remaining profile `ai_systems`
  row into an `ai_target` asset (kind `agent` -> `ai_agent`, `model_api` -> `llm_model`, `assistant` and
  `embedded` keep no subcategory and carry the tag `ai_kind:<kind>`; exposure `public` -> internet facing, every
  exposure kept as the tag `exposure:<value>`; `openaev_asset_id` becomes source `openaev` with that
  `external_id`, so the OpenAEV record merges instead of duplicating), then dropped the key, with its syncs,
  drafts and locks.
- **Sync with OpenAEV**: the assets import reads `/api/ai_targets/search` beside the endpoints; the outbound
  assets sync creates and updates XTM One-owned AI target assets through `/api/ai_targets`, deletes only what it
  created, and never writes a row an OpenAEV inbound sync owns.
- **Agents** read them with `get_organization_context(category="assets_exposure", query="category:ai_target")`;
  the fleet prerequisite is `profile:ai_targets`.
- **CTEM > Inventory**: no AI tab; the Assets tab filtered on category `ai_target`.

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

- Identity: strong keys in a configurable order (`DEFAULT_KEY_ORDER`: serial, cloud resource id, repository,
  device id, MAC, FQDN, hostname), each normalised (placeholder serials and null MACs dropped; a hostname drops apostrophes of
  every kind and spells blanks and underscores as hyphens, so the device name `Amani’s MacBook Pro` an endpoint
  manager sends and the hostname `Amanis-MacBook-Pro` macOS derives from it are one key). An IP address is weak: it never
  merges, it is answered as a possible match. No merge when one source holds two ids, when a higher-ranked key
  differs on both sides, or when an administrator split the pair; an administrator's merge wins over the keys.
  The answer does not depend on the order of the records and says why (`merged on serial`).
- Code repositories (#1554): a row of category `source_code` and subcategory `code_repository` is keyed
  `repository`, `host/owner/repo` (GitLab `host/group/subgroup/project`) read from its URL, else from a name shaped
  `owner/repo` (read on github.com, the only host guessed, and only for two segments), lower-cased with `.git`,
  scheme, `www.` and web sub-pages dropped. OpenAEV's `OpenCTI-Platform/opencti` and GitHub's repository are one
  asset (`merged on repository`); a row of any other category never carries the key, and two repositories with
  different numeric ids (`cloud_resource_id`, ranked above) never merge on the path.
- Fields: each field keeps every source's value; the value shown follows a per-field precedence of source classes
  (`DEFAULT_PRECEDENCE`: endpoint manager and EDR for OS and software, OpenCRQ for criticality, OpenAEV for
  exposure), customisable; `unknown` never wins over a known value; local fields come from hand sources only.
- Software: one entry per vendor, product and version across sources, with first and last seen; an unknown
  version is `unknown`, never empty.
- Endpoint sources (`organization_context_microsoft`): Microsoft Intune (Graph v1.0 `managedDevices`, and beta
  `managedDevices/{id}/detectedApps`: v1.0 has no such relationship and answers 400; permission `DeviceManagementManagedDevices.Read.All`) and Microsoft Defender for Endpoint
  (`/api/machines` and `SoftwareInventoryByMachine`, `Machine.Read.All` and `Software.Read.All`) feed the
  `assets` section like any import source, scheduled through the section syncs. A device row carries its
  identity keys (serial, Entra device id, hostname, FQDN, MAC, IP), its OS and its last-seen date; its software
  is stored as a document anchored on that row (`replace_endpoint_software`, `document_key` the source, replaced
  at every run, removed with the device on a complete read), so it ranks by the source's class: `intune` is
  `mdm`, `defender` is `edr` (`BUILTIN_SOURCE_CLASSES`). Reads are paged, a 429 or 503 is waited out with its
  `Retry-After` (bounded), and a refused permission is answered with the permission to grant. A software read
  that fails while devices sync is said in the sync's run status (`software_unread`, `software_error`), never a
  silent zero. A Defender
  read without the software permission still syncs the machines.
- Software: built from asset software (Software entities, section 9.3), one item per vendor and product with the
  versions seen and the assets running each; typed SaaS and cloud rows stay, without versions. The CPE comes
  from an administrator's override (kept across syncs), else a source that reported it, else a dictionary
  lookup with its confidence, else `unresolved`: never guessed.
- Sections: asset groups (external reference, then name) and the name-keyed sections (tech stack on CPE then
  vendor and product, primary assets, PIRs, tracked organizations) use the same matching.
- Settings: the profile part `asset_identity` (`AssetIdentity`, an `EXPOSURE_FIELDS` part, never dumped into the
  always-on block) holds the organization's `key_order` (strong keys, most reliable first; a key left out never
  merges; empty: `DEFAULT_KEY_ORDER`) and `precedence` (field to sources or source classes in order, `*` any
  other; a field left out keeps `DEFAULT_PRECEDENCE`). Saved with the profile (administrators,
  `PUT /organization-context`); every rebuild of the store reads it (`organization_asset_store.identity_settings`;
  a stored value that no longer parses keeps the defaults). `GET /organization-context/assets/identity` (any
  signed-in user) answers the saved settings, the defaults and the effective order and precedence, for the
  Sources panel.

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
- `POST /api/v1/organization-context/assets/lookup` `{keys: [...]}` (at most 200): the cross-product pivot of
  OpenCTI, OpenAEV and OpenCRQ (contract v3, the XTM One emblem). Each key is read as an FQDN and its hostname, a
  hostname, a MAC or an IP address (never a serial or a cloud id), normalised as the identity engine compares them,
  and matched against every source record of the primary organization, then the asset's name. A key of the form
  `<source>:<external id>` (`openaev:<asset id>`) is a source reference: it is matched exactly on the source and
  external id of the records and, when found, wins over every weak reading of the same key, so a product that
  imported the same OpenAEV asset finds it whatever its name or addresses. The answer holds one
  item per key found: `{key, id, name, category, criticality, internet_facing, owner, matches, url}`, `url` the
  asset's page `/ctem/inventory/assets/{id}`, `matches` the number of assets holding a weak key (the first by name
  answers). The platforms call it with the user's own cross-platform token, so a user reads what the inventory list
  answers him.
- The OpenAEV assets import reads `/api/endpoints/search` (endpoints with hostname, IPs and MACs), then
  `/api/assets/search` for the rest of the inventory (web applications, cloud resources, SaaS, network devices
  created as generic assets; AI targets keep their own read). A `platform` filter names endpoints alone and skips
  the second read.

### 9.2 Writes through the store, merge and split (S3)

- **Every save refreshes the store.** Since #1543 `organization_context.save_version` turns the asset and
  group lists of the saved profile into row writes (`organization_asset_records.apply_profile_lists`), each
  re-matching its own identity cluster, so a sync run, an import, the form and a proposal leave the store
  current in the same transaction (the agent row tools write the records directly, section 9.2bis). A store
  never built is built on its first read (`ensure_built`).
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

### 9.2bis Asset records as the source of truth (#1543)

`organization_asset_records` (one row per record per source, unique `(organization, source, external_id)`),
`organization_asset_identifiers`, `organization_asset_groups`, `organization_asset_group_members`,
`organization_primary_asset_assets` and `organization_asset_changes` (migration `r1543assetrecords01`) hold
the assets and groups; `services/organization_asset_records.py` is their only writer. A write re-matches the
identity cluster of the rows it touches (`identity_closure`, `refresh_clusters`), never the whole store, and
appends one row per changed record to the change log. `save_version` turns an old client's `assets_exposure`
and `asset_groups` into row writes (`apply_profile_lists`). Row API, administrators, audited
(`entity_type organization_context`):

- `POST /api/v1/organization-context/assets` (an `AssetEntry` row): 201 with the record (`id`, `asset_id`,
  `row_version`, audit columns); 422 names the bad field or a group that does not exist.
- `GET /api/v1/organization-context/assets/records/{id}` (any signed-in user): the record and its `row_version`.
- `PATCH /api/v1/organization-context/assets/records/{id}` (the fields to change; `If-Match: <row_version>`
  optional): the record; 409 on a stale `If-Match`, 422 when a hand write changes a field a sync owns (#1367).
- `DELETE /api/v1/organization-context/assets/records/{id}` (`If-Match` optional): 204.
- `POST /api/v1/organization-context/assets/bulk-delete` `{ids}` (at most 1,000): `{removed}`.
- `POST /api/v1/organization-context/assets/records/batch` `{rows}` (at most 10,000, matched on
  `(source, external_id)`): `{created, updated, removed, unchanged}`; a row equal to its record is not written.
- `POST`, `PATCH /{id}`, `DELETE /{id}` on `/api/v1/organization-context/asset-groups`: the same for groups; a
  deleted group's records stay and lose the group.
- `GET /api/v1/organization-context/asset-groups/{id}/members?limit=&after_name=&after_id=` (any signed-in
  user): one keyset page of the group's member records by name, each with `via` (who put it there: `record`,
  `manual` or the group's source), the group itself (`member_count` comes from the list) and `next`.
- `GET /api/v1/organization-context/asset-groups/{id}` (any signed-in user): the group's row with
  `member_count` (distinct assets), `unresolved_members` and `members_synced_at`; 404 for an unknown id.
- `GET /api/v1/organization-context/assets?group=<id>` (repeatable) keeps the canonical assets one of whose
  records is a member of one of the groups (a value that is not an id keeps none); `GET .../assets/facets`
  answers `group` as `{value: id, label: name, count}`; `GET .../assets/{id}` lists the asset's `groups`. The
  group page `/ctem/inventory/asset-groups/:id` reads its members through that filter, paged on the server.
- `PUT /api/v1/organization-context/primary-assets/{name}/links` `{names}`: the records and groups a primary asset
  covers; 404 for an unknown primary asset, 422 naming what matches nothing.
- The profile API never writes a row once the records are imported (#1752): `PUT /api/v1/organization-context`
  and `POST .../versions/{v}/restore` ignore `assets_exposure`, `asset_groups` and `ai_systems` (the stored
  version's lists are kept, so `save_version` applies no row write and the synced-row lock never judges a
  profile save), and name what they ignored in `ignored_row_fields`; a restore naming only row fields is a 422
  (the rows keep their own history, restored from `/changes/restore`). Links of primary assets and asset groups
  are judged against the records. `GET`, `PATCH`, `PUT` and the restore answer profile fields only, never the
  `assets_exposure` or `asset_groups` list: `row_counts` (`{assets_exposure, asset_groups}`) names how many
  there are, and a `PUT` that leaves a row list out keeps the stored one (before the import too). The payload
  size does not grow with the inventory. Ignore, not 422: an older client or agent keeps saving its profile
  fields during a rollout. A sync holding the organization's row lock never blocks or refuses a profile save
  (`test_organization_context_save_during_sync_postgres.py`).
- `GET /api/v1/organization-context/changes?entity_id=&since=&before_id=&limit=`: the change log, newest first;
  `POST /api/v1/organization-context/changes/restore` `{change_ids}`: a delete comes back, an update gets its
  `before` fields, a create goes.
- `PATCH /api/v1/organization-context` `{fields, based_on_version}` (administrators, #1738): the next profile
  version with those top-level fields set, every other field as stored; 422 for `assets_exposure`,
  `asset_groups` or an unknown field, 409 on a stale base. The settings cards that edit non-row fields (owned
  domains, address ranges, outside lens) use it, so no asset row travels with them. `PUT` and the version
  restore judge the synced-row lock (#1367) against the profile `GET` answered (rows from the records), never
  the stored version JSON: a sync that moved `last_seen` since no longer refuses a save that did not touch it.

Group memberships kept in sync (#1556): a group source answers each group's members (OpenAEV: static and
dynamic, a dynamic group stored as its resolved members), and `organization_asset_records.sync_group_members`
sets the memberships that source owns to that answer at every import or sync run: added and removed, a hand
(`manual`) membership and a record's own `group` (`record`) left as they are. A member binds to the record of
the member source with that id, so it lands on the merged asset whatever source created it; one no record holds
is counted on the group (`unresolved_members`, "not in the inventory"), never dropped silently.
`members_synced_at` is the last whole read. A group absent from the answer keeps its memberships.

The other group sources (`organization_context_groups`, #1556) answer members in their own asset ids, so
they bind to the asset records the same source wrote: Microsoft Intune (Entra security groups holding Intune
devices, `deviceId` mapped to the managed device through `azureADDeviceId`; user groups left out; device
categories not used, one hand-set category per device being coarser than the groups Intune targets; Graph
`GroupMember.Read.All`, `Device.Read.All`, `DeviceManagementManagedDevices.Read.All`), Tanium (Gateway
`computerGroups`, members by `endpoints(filter: {memberOf})`), CrowdStrike Falcon (host groups, members from
`host-group-members`; Host groups: Read) and ServiceNow (business services `cmdb_ci_service`, members the
hardware CIs a `cmdb_rel_ci` names as their children). A group whose member list failed or passed 20,000 is
answered without members and keeps the stored ones.

The agent row tools write the records too: `upsert_organization_rows` and `remove_organization_rows` on
`assets` or `asset_groups` plan against the records the call addresses (`organization_context_rows.
plan_record_upsert` / `plan_record_remove`, keyed on `(source, external_id)`, a field the source does not send
kept from the record) and write through `organization_asset_records.write_rows` / `remove_rows` with origin
`agent:<source>`: no profile version, no 5,000 cap, each changed row in the change log with the agent as actor,
one audit entry per call. A row of another source with the same name is added beside it (the store merges
them); a row of the same source with the name of another record is refused. Tech stack rows
stay on the profile path until they move to records (#1514, the Software entity).

Imports and syncs of `assets` and `asset_groups` are planned on the records too
(`organization_context_import.preview_import` -> `plan_record_import`, the merge rules of `_merge_sourced`:
local fields a person set are kept, a new row named like another record of the source is skipped, a sync removes
only the rows its own `sync_id` wrote, a truncated or failed read removes nothing) and applied by
`apply_record_import` in batches of `MAX_BATCH` with origin `sync:<id>` or `import`. The stored rows are read by
ref (`rows_by_ref`) and the gone rows by three indexed columns (`source_refs`): no profile version, no 5,000 cap,
only the changed rows logged and re-matched. The sync result says `rows_written` instead of `version`; the
one-off import (`POST /organization-context/import/apply`) and the agent's import tool answer `applied`
without a version. The other sections keep the profile plan.

### 9.3 Software of the organization

The software the assets run is read as Software entities (`organization_software_store`, #1495): one
Software per vendor and product (keys case-folded), its versions with the number of assets running each, its
installations, dependencies and sources, and the CPE with its confidence (`resolve_cpe`: an administrator's
override, kept across syncs; then a CPE a source reported; else `unresolved`). The derived tech stack read
(`derive_tech_stack`, `tech_stack_inventory`, `GET /organization-context/inventory/tech-stack`) is retired:
the Software tab and `GET /organization-context/software` replace it, and `GET /assets/by-software` lists the
assets running a product.
- `get_organization_context(category="software")` answers the organization's Software entities (synced and
  hand-typed, with versions, CPE and asset count) once the store holds one, and the profile rows before.
  The name `tech_stack` is retired with no alias (Sam, 24 Nov 2026): the category, the rows section, the
  import and sync section and the outbound pairing are all `software`. `s1495swprofile01` turned each profile
  tech stack row into a hand-typed Software (a hand CPE kept as the override) and renamed stored category
  selections; `s1495swsection01` renames the saved syncs' section and any selection still naming it.
  `s1495swfield01` renamed the profile field itself to `software`; `s1495products01` renames the product list
  each asset and asset group carries (`assets_exposure[].products`, `asset_groups[].products`, the inventory
  store's `fields.products`, the CSV column `products`) and the scope gate's `reconcile.products`.
- `upsert_organization_rows(section="software")` writes Software entities, not profile rows:
  a row whose vendor and product a software already carries is reported `unchanged` and never overwritten;
  a new one becomes a hand-typed Software (`create_software`, its typed version stored as a version). Rows are
  never removed by source (`remove_organization_rows` refuses the section). Before the organization has a
  profile (no asset store yet), the rows land in the profile as before.

### 9.4 SBOM import

An SBOM (CycloneDX JSON or SPDX JSON) is one more source of an asset's software: it describes one
application or one image running on the asset. `services/sbom_parser.py` (pure) reads it: CycloneDX nested
`components` give the bundling parent, SPDX `CONTAINS` and `CONTAINED_BY` relationships give it, SPDX
`externalRefs` give CPE and PURL; a missing, blank or `NOASSERTION` version is `unknown`, never an empty string;
over 10,000 components (or 20 MB) the file is refused, never cut.

`organization_asset_sboms` keeps each document, anchored on a source record of the asset (its first record by
source and external id), not on the asset row: every rebuild of the store (a newer profile, a merge or a split)
adds its components, as source `sbom`, to whichever asset holds that record, through `merge_software` like any
other source (precedence, first and last seen, sources). One row per document (`document_key`: CycloneDX
`metadata.component`, SPDX `name`, else the file name): the same document again replaces its components,
another document adds to them, and components a newer SBOM no longer lists stay with the documents that list
them. `organization_asset_software.bundled_in` keeps the bundling component (`ingress-nginx-controller 1.15.1`).
The tech stack inventory (9.3) counts SBOM software like any other.

- `POST /api/v1/organization-context/assets/{asset_id}/sbom` (administrators, multipart `file`, audited): 201
  with the format, the document, the component count, the unknown versions, the bundled ones and whether it
  replaced an earlier import; 404 on an unknown asset; 422 on a file refused. The upload in the UI comes with the
  asset sources panel.

### 9.5 Software as an entity (#1495)

Software is a first-class object of the store, aligned with the STIX 2.1 Software SCO (name, vendor,
CPE, plus `x_opencti_product`) so a later pivot to OpenCTI converts it field for field. Three tables,
kept across the rebuilds of the store (their ids are stable), filled by `organization_software_store.sync_software`
at the end of every rebuild:

- `organization_software`: one per organization, vendor and product (case-folded keys). Type (`application`,
  `operating_system`, `library`, `firmware`, `container_image`, `saas`), product-level CPE 2.3 (version `*`) and
  its state (`override`, `reported`, `high`, `medium`, `low`, `unresolved`, the `resolve_cpe` rule: never guessed),
  the administrator's `cpe_override`, product PURL (no `@version`), sources, first and last seen. `origin`
  `synced`: a source reports it, read-only to people; `manual`: typed by an administrator.
- `organization_software_versions`: one per software and version, `unknown` when no source gives it (never empty),
  with the version's CPE (reported, else the software's with the version set) and PURL; `eol` when known.
- `organization_software_dependencies`: a version that `depends_on` another (CycloneDX `dependencies`, SPDX
  `DEPENDS_ON` / `DEPENDENCY_OF`) or is `bundled_in` it (CycloneDX nesting, SPDX `CONTAINS` / `CONTAINED_BY`);
  scope when the SBOM gives one. A label resolves only within the asset's own software. Rebuilt whole with the
  installations, so an SBOM removed takes its edges with it.

`organization_asset_software` is the installation (a version on an asset, any category: host, container image,
code repository) and names its `software_version_id`; one store, no copy. A synced software nothing installs any
more is removed with its versions; a manual one stays. A store built before this change is linked by the next
read (`ensure_built` finds an installation without a version and rebuilds once).

Reads (any signed-in user):

- `GET /api/v1/organization-context/software?q=&type=&vendor=&source=&origin=&cpe_state=&unknown_version=&offset=&limit=`:
  one item per software with `version_count`, `asset_count` (distinct assets), `has_unknown_version`, `editable`.
  `type`, `origin` and `cpe_state` repeat for several values (any of them), as the Software tab's filters send them.
- `GET /api/v1/organization-context/software/{id}`: the software, its versions with asset counts, its
  installations (paginated), `depends_on` and `used_by` (one level, each edge with its version and kind).
- `GET /api/v1/organization-context/software/versions/{id}/assets?through_dependencies=`: the assets where a
  version is present, directly (`via` empty) or through an installed version that depends on it, transitively
  (`via` names it; the walk stops at 5,000 versions).

- `GET /api/v1/organization-context/software/export.csv` (same filters): the list as CSV, at most 50,000 rows,
  a cell starting with `=`, `+`, `-`, `@`, tab or CR prefixed with `'` so a spreadsheet never runs it.

Writes (administrators, audited `entity_type organization_context`, `entity_name software`; 404 unknown,
409 a vendor and product that exist, 422 refused):

- `POST /api/v1/organization-context/software` `{vendor, product, name, type, description, cpe, purl}`: a
  hand-typed software (`origin manual`); `cpe` is a CPE 2.3 string kept as its override (`cpe_state override`).
- `PATCH /api/v1/organization-context/software/{id}` `{name, type, description, purl, cpe_override}`: a synced
  software takes `cpe_override` only (any other field is 422: its name, type and versions come from its
  sources); the override is kept across syncs and an empty one lets the sources decide again.
- `DELETE /api/v1/organization-context/software/{id}`: 204; a hand-typed software and its hand attachments
  (422 for a synced one, which goes when no source reports it).
- `POST /api/v1/organization-context/software/{id}/installations` `{asset_id, version}`: attach a hand-typed
  software to an asset (`unknown` when no version). The attachment is a component of the asset's hand
  document, `organization_asset_sboms` format `manual` anchored on the asset's first source record like an
  SBOM, so it follows the asset through every rebuild and merge and shows as source `manual`. The same
  version again changes nothing. A synced software is attached by its sources only (422).
- `DELETE /api/v1/organization-context/software/{id}/installations/{asset_id}?version=`: remove the hand
  attachment (every version when `version` is empty); 404 when the asset holds none.

UI (slice 2): CTEM > Inventory has a Software tab (`/ctem/inventory/software`, the old `tech-stack` tab redirects
there) listing every software with its type, asset and version counts, CPE or its state, and sources, filtered on
type, origin and CPE state; each row opens `/ctem/inventory/software/{id}`: the identity and CPE card, versions with
asset counts (`unknown` named), the assets it is installed on (server paged), depends on and used by. Each row of
an asset page's Software section carries `software_id` and opens that page.

Next slice: the `tech_stack` profile rows migrated to manual software
and the concept retired, every reader moved to the Software entity, the CVE matcher of the CTEM case source
(`ctem_cases_source.read_open_issues`, `ctem_metrics_collector._profile_tech_stack`) included (3). Platform
events for software writes are not raised yet: no subscriber needs them before slice 3.

## 10. OpenCRQ alignment

OpenCRQ-Platform/opencrq#3152 closed the three divergences: `security_platform` and its 10 subcategories are
accepted (section 1), an asset carries groups and tags (sections 5 and 6), and an owner sent as a name is resolved
to a tenant member (section 4). The OpenCRQ tools of XTM One (`create_opencrq_supporting_asset`,
`update_opencrq_supporting_asset`) send all of them.

### Primary assets and supporting assets

OpenCRQ holds two layers. A **primary asset** is a business asset (a service, a process, a dataset) rated on four
security needs (availability, integrity, confidentiality, traceability); the profile lists them as `primary_assets`
(the name before primary assets). A **supporting asset** is the IT underneath (a server, an application, a
repository): an inventory asset, never a primary asset.

- Import (`organization_context_import._read_opencrq`): the primary-asset section reads OpenCRQ primary assets
  (`primaryAssets.getPrimaryAssets` per organization), criticality = the highest rated need, `min_criticality`
  empty by default (every primary asset). Rows an older build imported from supporting assets are superseded at
  the next sync (`ReadResult.superseded`); a row a person typed is kept.
- Outbound: XTM One no longer pushes primary assets to OpenCRQ supporting assets; OpenCRQ is the source.
- Links (`organization_context_opencrq_links`): the OpenCRQ primary-asset sync, option `link_supporting_assets`
  in its filters (on unless `"false"`), imports the OpenCRQ `support_to_primary` links onto
  `organization_primary_asset_links`: `source` (`manual`, `opencrq`, `rule`), `external_id` (the OpenCRQ link id),
  `roles` (OpenCRQ `link-roles.ts`: stores, processes, reads, transmits, administers, monitors,
  supports_availability, performs, unknown), `inherited_needs` (need to `full` / `lower` / `none`, the strongest of
  the roles; `unknown` inherits everything), `via` (the supporting asset a `depends_on` chain goes through, written
  by the signals slice), `removed`. The supporting asset is the inventory record of source `opencrq` with the same
  id, else the single record of the same name; none or several are reported (`not_in_inventory`, `ambiguous`),
  never created. A manual link is never touched; a synced link a person removes through
  `PUT /primary-assets/{name}/links` becomes a tombstone the sync never adds back; a link OpenCRQ dropped is deleted
  at the next sync. The sync's run result carries the link summary under `links`. Migration `v1591pallinks01`.
- Inventory source (Sam, 24 Nov 2026): the `primary_assets` section is in the `inventory` scope of
  `organization_context_sync.SECTION_SCOPES` (and `organization_context_sources.FIELD_SCOPES`), so
  `GET /organization-context/syncs?scope=inventory` and `/sources?scope=inventory` list the OpenCRQ primary-asset
  sync next to the asset sources, on the same scheduler (no second one). Nothing is pre-provisioned (Sam,
  24 Nov 2026): an administrator adds the OpenCRQ primary-asset source from Inventory > Sources like any other
  source, and the Primary assets card names no source. Migration `n1883nodefsync01` deleted the daily source
  older builds saved by themselves (create audit `details.default`) when no person had edited it since; the rows
  it had imported stayed, as an administrator's delete in its default `leave` mode keeps them. Manual rows and
  manual links still win over every run.
  Rows a deleted sync left (`leave`, the migration above included) keep their platform source and are listed in
  Sources and syncs as that platform with no sync (#1940): its schedule cell reads "Rows from an earlier sync,
  none scheduled" and its menu offers "Set up a sync" (the import preselected on that platform and the section the
  rows feed; the new sync takes the rows back, since `plan_rows` and the import count an unstamped row of the same
  source as the sync's) and "Remove or hand over the rows" (`POST /organization-context/sources/{source}/rows`).
  The table offers that purge or release for every source but rows entered by hand; `plan_rows` refusing the
  hand source stays the one authority.
