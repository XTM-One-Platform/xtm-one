# Organization profile

The organization profile describes your organization once, for every agent: what it does, where it operates, what it must protect, what it runs, how much risk it accepts and where findings become tickets. Agents read it before they rank, scope or estimate, so that a vulnerability, a threat or an exposure is weighed against your business rather than in general.

Administrators fill it in `Settings` > `Organization`. There is one profile per platform.

## How agents use it

Every agent has a read-only tool, `get_organization_context`, that returns the profile, in chat and in assignment runs alike. An agent that belongs to a fleet also starts each run with the profile in its context.

By default, every agent also starts each chat and each run with a short block: the organization's name, sector, regions, size and risk appetite, then one line per category of the profile with its size and the call that reads it. The categories are the long lists of the profile:

| Category | In the prompt | Read with the tool |
|---|---|---|
| Identity (name, sector, regions, size, risk appetite, currency) | Always, in the short block | The whole profile |
| `primary_assets`, `software`, `shipped_products`, `pirs`, `assets_exposure`, `tracked_organizations` | Their size, in the short block; their rows only when preloaded | `category`, filtered by `query`, paged by `offset` and `limit` (up to 200 rows a page), so a list of any length is read whole. `assets_exposure` reads the merged assets of **CTEM > Inventory** (one row per real asset, with its sources and merged records) and its `query` also takes `field:value` terms on `category`, `subcategory`, `criticality`, `internet_facing`, `os_family`, `source` and `group` |
| `prioritization_policy` | Its number of rules, in the short block; the rules only when preloaded | One row per threshold you set, per exclusion and per accepted risk |
| `enterprise_knowledge` | Its number of linked knowledge bases, never the content | Without `query`, the linked knowledge bases; with `query`, the passages that answer it, only from the knowledge bases the person the agent acts for can read |

A vulnerability on a product named like one of your imported GitHub or GitLab repositories (a CPE product part or a plain name, case, `-` and `_` ignored) resolves to that repository in CTEM without a `shipped_products` row; forks are left out.

`crown_jewels` and `tech_stack`, the former names of `primary_assets` and `software`, are still read for one release; a saved selection naming them was renamed on upgrade.

The short block stays the same size whatever the length of the lists. An agent asked about a vulnerability reads the software inventory page by page, or narrows it to a vendor or a product with `query`.

You choose at three levels, the most specific one winning:

- **Settings > Organization > Organization context in agents**: `Use organization context in every agent`, on by default, and `Preloaded organization categories`, none by default, are the defaults for every agent and assignment.
- **An agent's Configuration > General**: `Organization context` is `Inherit` (follow the platform), `Always` or `Off`; `Preloaded organization categories` is `Inherit` or a list of its own.
- **An assignment's parameters**: the same two fields, `Inherit` following the agent, then the platform.

A preloaded category is loaded after the short block, capped, with the call that reads the rest. Each form shows the value in effect and where it comes from. `Off` on an agent or an assignment also removes the tool; the platform switch removes the short block only. A fleet run keeps its fuller profile block and does not repeat the short block. Every built-in agent inherits the platform default; the vulnerability and remediation agents of the exposure management fleets preload the software inventory, the crown jewels, the assets and exposure, and the prioritization policy.

Agents cite the fields they used. For a field you have not filled, they name it in their output instead of guessing it: "the crown jewels are not set", for example. The profile is treated as information typed by a person, never as instructions to the agent.

The profile also feeds other features:

- An OpenCTI trigger can take its filters from the profile's intelligence requirements, regions and sector. See [React to OpenCTI events](../usage/xtm-suite/opencti.md#react-to-opencti-events).
- The primary assets stay in line with OpenCRQ through a scheduled Inventory source (see [Primary assets and OpenCRQ](#primary-assets-and-opencrq) below).

## Shared context and permissions

The organization profile is one shared context for the whole platform. Every import, sync and agent write shows the same notice, **Shared organization context**:

- Data is read from the source platform with the permissions of the person who sets up the import or sync. A sync keeps reading with the permissions of the administrator who saved it: the import dialog says **Reads as** or **Runs as** with that person, the **Runs as** column of **Settings > Organization > Syncs** names them, and editing a sync saved by someone else says whose permissions it runs with now and that saving it makes it run with yours (**Runs as** the owner, then **Saving makes it run as** you).
- The source platform's access rules, including organization segregation and markings, are not carried over.
- Once imported, the organization context is visible to every user and every agent of this XTM One platform.

This applies to the organization context only. Agent integrations, dashboards and every other feature keep each user's own permissions.

A CSV upload and a row typed by hand read no platform, so they carry the shared-visibility sentence only. The agent tools of the **Organization context** group (`update_organization_context`, `upsert_organization_rows`, `run_organization_import`, `create_organization_sync`, `run_organization_sync`, `propose_organization_context_change`) state the same in their description, and their previews and results carry the notice, so the agent tells the person before it applies.

## Let an agent edit the profile

Reading the profile needs nothing. Changing it is the **Organization Profile Editing** built-in tool group (`organization_context`), off by default and marked administrator-only in an agent's built-in tools:

- `update_organization_context` saves field changes at once, on your profile or a tracked organization's: the whole new value of each field with its basis. Each value is validated like this form. The change is a new saved version authored by the administrator the agent acts for, the agent's name and the basis kept per field, so **Saved versions** below can roll it back. Each save writes an audit log entry.
- `propose_organization_context_change` records a proposal an administrator accepts or rejects here instead.
- `list_organization_proposals` and `decide_organization_proposal` read the proposals and accept or reject a pending one, as the review on this page does: `accept` names the fields or organizations kept, an empty list rejects it (a proposal already applied with `update_organization_context`, for example). A decided proposal is refused, and each decision writes an audit log entry.
- `list_organization_versions` lists the saved versions of the profile, newest first, with their source, author and crown jewel count; with `version` it reads that version's whole profile. `restore_organization_version` saves the next version with some fields (or every field) taken from a past one, with the same checks as the restore on this page, and answers the rows it drops; each restore writes an audit log entry.
- `update_organization_context` and `restore_organization_version` take `based_on_version`, the version the agent read: when another version was saved since, nothing is saved and the agent is told to read the profile again.
- `reconcile_organization_with_opencrq` runs the crown-jewel reconcile with OpenCRQ of this page: `apply` false (the default) answers the plan, `apply` true saves a version of the profile. Nothing is written to OpenCRQ: the assets read are its primary assets.
- `upsert_organization_rows` adds or updates rows of one section without resending the list. Assets and asset groups are keyed on the source the rows come from and their `external_id` there; a row a person wrote (source `manual`, `csv` or `api`) is never updated or removed, and a field the source does not send (owner, environment, group) keeps the value a person set. Software products are added when their name is new and never overwritten. Rows are validated like the CSV import; a bad row is refused with its reason and the others go on. At most 500 rows per call.
- `remove_organization_rows` removes the assets or asset groups a source synced, by `external_id` (a device retired from the source).
- `run_organization_import` runs the import of [Draft the profile from your platforms](#draft-the-profile-from-your-platforms) for one section and one platform, with the same rules.
- `list_organization_syncs`, `create_organization_sync` and `run_organization_sync` read, add and run now the scheduled syncs of **Settings > Organization > Syncs** (see [Keep a section in sync](#keep-a-section-in-sync)). A sync an agent creates is saved with the administrator the agent acts for as its owner: its runs read the platform with that administrator's token, exactly as a sync saved in the dialog. The agent previews the import with `run_organization_import` first; `create_organization_sync` refuses what the scheduler could not run (an unknown section, a platform that cannot feed it, an unknown time zone) and a second **Sync all** schedule.

The row and import tools preview by default: they answer what would be added, updated, left unchanged, removed or refused, with the reasons, and save nothing. With `apply` set to true they save a new version, authored by the administrator the agent acts for, with the agent and the counts kept as the basis.

Only an administrator can add the group to an agent. The tools are offered only on a turn that acts for an administrator: the verified caller in chat, the run's acting user in an assignment. A non-administrator talking to the same agent does not get them, and a direct call is refused ("only an administrator can change the organization profile"); an identity the platform cannot resolve is refused too. The built-in CTEM Assistant and the Platform Health orchestrator keep their proposal tool without the group.

## Fill the profile with an agent

An agent bound to the integration (read-only is enough) and to **Organization Profile Editing** fills a section from any platform the integration reaches: an endpoint manager, an EDR, a cloud inventory, a CMDB.

**Intune.** Bind the agent to the Microsoft Intune integration and ask:

```text
List every managed device in Intune with intune_list_devices (page through all of them),
then upsert them into the assets with upsert_organization_rows, source intune,
rows_format intune_device. Show me the preview first; apply only when I say so.
```

With `rows_format` `intune_device` the platform maps each device itself: the Intune device id is the `external_id`, the operating system gives the category (Windows, macOS and Linux devices are workstations, laptops when Intune says so; iOS and Android devices are smartphones, iPads tablets), and the operating system and compliance state become tags (`os:windows`, `compliance:noncompliant`). The primary user's name becomes the owner only when you pass `include_owner` true. With `intune_detected_app`, detected apps are reduced to the security products among them (Defender, CrowdStrike, SentinelOne and the like), added to the software inventory.

**On a schedule.** To keep the assets in sync, give the same agent an assignment with a schedule trigger (for example `0 6 * * *`, every day at 06:00) and a prompt such as:

```text
Read every managed device from Intune. Upsert them into the assets with
upsert_organization_rows, source intune, rows_format intune_device, apply true.
Then remove with remove_organization_rows the intune assets whose device id
Intune no longer lists. Report the counts and every refused row with its reason.
```

The run acts as the assignment's user, who must be an administrator for the tools to be offered. Each run saves at most one version per call, visible in **Saved versions** below.

**A platform sync instead of an assignment.** For a platform the import reads (OpenCTI, OpenAEV, OpenCRQ), the agent can schedule the platform sync itself, with no model call at each run:

```text
Preview the OpenCTI import of the assets with run_organization_import. If it looks right,
keep it in sync every day at 06:00 Paris time with create_organization_sync, then
run it once now with run_organization_sync and report the counts.
```

**In one click.** On **Settings > Organization**, **Draft > From a connected integration** opens a dialog: pick a connected integration, turn on **Keep the assets in sync every day** if you want the scheduled assignment, then **Open the chat**. The chat opens with the agent and its preview instruction ready to send; nothing is saved until you confirm in the chat. The dialog calls `POST /api/v1/organization-context/fill-agent`, which, with an `integration_id`, creates that agent for you: bound to the integration read-only and to **Organization Profile Editing**, its persona and its first prompt written for the platform (Intune devices are mapped on the server, any other platform's devices are mapped by the agent with their own id as `external_id`). With `schedule` true it also creates the assignment **Keep my assets in sync from <integration>** (`cron` `0 6 * * *` by default, `timezone` optional). A second call for the same integration reuses the agent and the assignment. The call creates nothing when AI is off or unlicensed.

## Sources

At the top of **Settings > Organization**, the `Sources` panel says who feeds the profile before the tabs say what it holds. It lists each connected platform (OpenCTI, OpenAEV, OpenCRQ) with its number of syncs, then the rows typed by hand, imported from CSV, written through the API or kept by an agent. Each source shows one chip per section it fills (crown jewels, software inventory, assets, asset groups) with its number of rows; a chip opens that section's tab, filtered on the source where its table has a `Source` filter. `Add a sync` opens the import stepper: pick a section and a platform, preview the rows, then turn on `Keep in sync`. With no source yet, the panel says where to start.

## Edit the profile

Select `Edit`, change what you need, then `Save`. `Cancel` drops your changes.

Above the form, a box lists the fields not filled yet: the same list agents report. The fields are:

- `Organization name`, `Organization size` (a band of employees), `Sector` and `Sub-sector`.
- `Regions of operation`: two-letter country codes, separated by commas (`FR, DE, US`).
- `Annual loss threshold` and `Approval threshold`: your risk appetite, as amounts.
- `Currency`: a three-letter currency code (`EUR`), and `Loaded hourly rate`: the cost of an hour of your team's work.
- `Ticketing integration` and `Ticketing project`: where CTEM cases become tickets (for example `jira` and `SEC`). The target counts as filled only when both are set. Both sit at the top of the **CTEM case sync** card, before the rules that say how a case opens, updates and closes its ticket.

The **Priority intelligence requirements (PIRs)** are not on this page: pick them on **CTEM > Inventory > Exposure policy > Prioritization policy**, next to the two PIR score thresholds (`PIR score minimum: vulnerabilities` and `PIR score minimum: threat activity`). The list is picked from the PIRs of your connected OpenCTI: it filters the two PIR playbooks and raises the context score of a threat or a vulnerability flagged in one. Select `Edit` on the card, change the list, then `Save`.

A value that is not valid (a region that is not a country code, an amount that is not a number) is refused, and the message names the field.

### Pause and resume CTEM case opening

The **Case opening** switch sits on the header of **CTEM > Cases** and of **CTEM > Signals > Sources**, for administrators. It is on by default.

- **Off**: a confirmed signal opens no case. It stays confirmed and waits; the Cases list says how many signals are waiting. Case deadlines, announcements and the stall reminder keep running, and a case can still be split by hand.
- **On again**: every waiting signal opens (or joins) its case at once, without waiting for the next scheduled pass, and the Cases list fills while you watch. A message says how many signals are being opened.

Other users see a **Case opening paused** badge while it is off. Every change is in the audit log.

## Crown jewels and business services

The crown jewels are the assets that matter most to the business. Without them, agents cannot rank findings by business impact. In edit mode, `Add a crown jewel` adds a row with:

- the crown jewel's name;
- the `Business service` it supports;
- its `Owner`;
- its `Criticality`: `Low`, `Medium`, `High` (the default) or `Critical`.

A link icon marks a crown jewel linked to an OpenCRQ asset.

## Primary assets and OpenCRQ

OpenCRQ rates the same business assets as primary assets. With OpenCRQ connected as a company-managed integration, the primary assets are an Inventory source: `CTEM` > `Inventory` > `Sources` lists the OpenCRQ primary-asset sync next to the asset sources, with the same controls (enabled, schedule, last run with its counts, next run, `Run now`, errors). It is on and daily by default, and it links the supporting assets to their primary assets unless turned off.

- A primary asset you add or edit by hand, and a link you add or remove by hand, always win over the sync.
- Turning the source off stops the schedule; the rows it already wrote stay.

The primary assets card names the source with the OpenCRQ icon and the time of its last sync. It has no OpenCRQ button: run or configure the sync from `Sources`. OpenCRQ is read with the access of the administrator who last saved the sync. See [XTM One in OpenCRQ](../usage/xtm-suite/opencrq.md).

## Software

The software inventory lists the products you run, so that agents can match vulnerabilities to them. `Add a product` adds a row with the `Product`, its `Vendor` and, optionally, its `CPE`: the standard product identifier that vulnerability records use, which makes the match exact.

When your assets carry their installed software (from a sync or an import), the software inventory agents read is built from it: one entry per product, with each version seen and the number of assets running it, and the sources that report it. The rows you typed stay: a product no asset reports (a SaaS application, a cloud service) keeps its own entry, and a CPE you typed wins over the one a source reports. `GET /api/v1/organization-context/software` answers the same list (the CTEM > Inventory > Software tab), and `GET /api/v1/organization-context/assets/by-software?product=` lists the assets running a product.

### Software from an SBOM

An SBOM (software bill of materials) lists what an application or a container image is made of. An administrator can import one on an asset with `POST /api/v1/organization-context/assets/{asset_id}/sbom` (a CycloneDX JSON or SPDX JSON file in the `file` field): its components join the asset's software with the source `sbom`, with their CPE and package URL when the SBOM gives them, and with the component that bundles them (the NGINX inside an ingress controller image, for example). A component without a version is listed as `unknown`. Importing the same SBOM again replaces what it gave; another SBOM on the same asset adds to it. The SBOM stays with the asset when a sync merges it with another record. A file over 10,000 components is refused: split it per application or image.

### How one asset is matched across sources

The same device read from two sources (an endpoint manager and OpenAEV, for example) is one asset when they share a strong identifier: hardware serial, cloud resource id, code repository path, managed device id, MAC address, FQDN or hostname, in that order by default. A code repository is matched on its path (`github.com/owner/repo`, read from its URL or from a name such as `Owner/Repo`, whatever the case or a trailing `.git`), so the repository OpenAEV lists and the one GitHub or GitLab sends are one asset; two repositories with different GitHub or GitLab ids are never merged on the path. An IP address alone never merges two assets. Each field shows the value of the source that ranks first for it (by default the endpoint manager or EDR for the operating system and software, OpenCRQ for criticality, OpenAEV for exposure), and every source's value is kept. An administrator can change the order of the identifiers, leave one out, or change the source order of a field in the profile's `asset_identity` part; `GET /api/v1/organization-context/assets/identity` shows the saved settings, the defaults and what applies.

## AI targets are assets

The AI your organization runs or exposes is an asset of category `AI target`, as in OpenAEV, with its subcategory (`LLM model`, `AI agent`, `MCP server` or `RAG pipeline`), provider, endpoint, model and modality. It is listed, filtered and synced in CTEM > Inventory > Assets like any other asset, and AI security work reads it from there: an agent reads them with the `assets_exposure` category and the query `category:ai_target`. The profile has no separate AI list: entries saved in it before were turned into AI target assets, linked to their OpenAEV record when they had one.

## Saved versions

Each save is a new version: nothing is overwritten. The page shows the current version and when it was saved. When two administrators save at the same moment, the second save is refused: reload the page and save again.

Scripts list the saved versions, newest first, with `GET /api/v1/organization-context/versions` (administrators only). Any signed-in user can read the current profile with `GET /api/v1/organization-context`. The profile carries its own fields only: assets and asset groups are read and written with `/api/v1/organization-context/assets` and `/api/v1/organization-context/asset-groups`, and the profile answers how many there are in `row_counts`. A save or a restore never changes an asset row, and a row list sent with it is ignored and named in `ignored_row_fields`. Restoring a version brings back profile fields only: assets keep their own history in CTEM > Inventory.

## Draft the profile from your platforms

XTM One can propose a profile from what your connected platforms already hold:

- OpenCTI: your organization's name, its first sector and your priority intelligence requirements;
- OpenAEV: your security platforms (EDR, SIEM and others), as software inventory entries;
- OpenCRQ: the supporting assets rated high or very high, as crown jewels linked to their asset.

Only empty fields are filled: what a person typed always wins. Each filled field names the platform it came from, and a platform that is not connected or cannot be read is reported instead of failing the draft. Each platform is read with your own access. No model is involved.

The draft is a proposal, never a save. In **Settings > Organization**, open **Draft > From the platforms**: the form opens on the proposed values, each one named with its platform, and nothing is saved until you select **Save**. A platform that could not be read is named with a link to **Settings > Integrations**. Scripts use `POST /api/v1/organization-context/draft`.

## Import one section from a platform

**Settings > Organization > Import from the platforms** imports one section of the profile from one connected platform, with no model call, in five steps:

1. **What to import**: assets, asset groups, software inventory, crown jewels or PIRs. AI targets come in with the assets (category `AI target`).
2. **Source**: the platforms that hold this section, shown with their logo. A platform that is not connected or not readable with your account is shown greyed, with the reason.
3. **Filters**: each value is picked from the platform itself, never typed.
4. **Mapping**: each profile field with the platform attribute it reads, its fallback, and an example taken from the platform. The fields the platform does not provide are listed: rows keep what you typed for them.
5. **Preview**: the platform's own count, then the rows that would be new, updated, unchanged or skipped. Nothing is saved before **Import**. When the platform cannot be read, the preview says so with the reason: the permission the integration lacks (for example the CrowdStrike Falcon *Host groups: Read* scope), an integration that is not connected, an HTTP status or a timeout. A platform's own error text is never shown.

| Section | Platform | Filters |
|---|---|---|
| Assets | OpenCTI | labels, markings, author, creator, category, subcategory, criticality, exposure, environment, platform, created on or after |
| Assets | OpenAEV | asset groups, tags, category, subcategory, criticality, platform |
| Asset groups | OpenAEV | tags, name contains |
| Asset groups | Microsoft Intune (Entra device groups), Tanium (computer groups), CrowdStrike Falcon (host groups), ServiceNow (business services) | name contains |
| Software | OpenAEV | security platform type |
| Crown jewels | OpenCRQ | lowest criticality, tenant |
| PIRs | OpenCTI | the PIRs to import |

An asset group read from a platform brings its members, kept in sync at every run: an asset leaving the group in the platform leaves it here, a member added by hand stays. A member the inventory does not hold yet is counted as not in the inventory. Intune groups are the Entra ID security groups that hold Intune devices (user groups are left out); the Intune integration then needs the Graph permissions `GroupMember.Read.All` and `Device.Read.All` besides `DeviceManagementManagedDevices.Read.All`. CrowdStrike needs the Host groups: Read scope. ServiceNow groups are the business services, their members the hardware CIs related to them in the CMDB. Import the assets of the same platform first: a group binds its members to the assets that platform brought.

OpenCTI Systems carry a category, a criticality, an exposure, an environment, an owner and a platform from the OpenCTI release that ships OpenCTI-Platform/opencti#19943; the import reads them. On an older OpenCTI those filters are hidden and every System takes the type you pick on the filters step, which is otherwise used only for Systems with no category. The owner falls back to the System's author; the platform and the markings are kept as `platform:` and `marking:` tags.

Every row of every list section (crown jewels, software inventory, assets, asset groups) records where it comes from: its **source** (`manual` when typed, `csv`, `api`, `agent:<agent id>`, or the platform: `opencti`, `openaev`, `opencrq`), its id in that platform (`external_id`) and the sync that wrote it (`sync_id`). A crown jewel saved with only its OpenCRQ link reads as a row of that platform.

An import can run again with the same or other filters. A row a previous import from the same platform wrote is updated, never duplicated, and the fields a person added that the platform does not send (a crown jewel's business service and linked assets, an owner the platform leaves empty) are kept; a row typed by hand, uploaded as CSV or written through the API is never overwritten. Rows a previous import wrote that the platform no longer returns with these filters are listed as **no longer in** the platform: they are kept unless you switch on their removal. Each import is recorded with the platform, the filters and the time of the read. Scripts use `GET /api/v1/organization-context/import/options`, `POST /api/v1/organization-context/import/preview` and `POST /api/v1/organization-context/import/apply` (`remove_gone` removes the rows no longer in the platform).

An asset is one real thing whatever the number of platforms that report it. When a platform sends an asset named like a row another source wrote (typed by hand, or read from another platform), the import adds it as a second record of that asset instead of skipping it, and the asset list shows it once, with each platform it comes from and, for every field, which source set the value. Records are joined on their serial number, cloud resource id, code repository path, device id, MAC address, fully qualified name or host name; an IP address alone never joins two records. An administrator can merge records the keys did not join, or keep apart two records they joined (`POST /api/v1/organization-context/assets/decisions`, removed with `DELETE` on the same path and the decision id); both are kept across syncs and written to the audit log. A row typed by hand needs an external id before it can be merged or kept apart, because without one it is known only by its position in the list.

## Keep a section in sync

An import can also be saved as a **sync** that runs again on a schedule, so the profile follows the platform: on the import's preview, switch on **Keep in sync** and pick the schedule, then select **Import and keep in sync** (or **Save the sync** when nothing is new today). Only the sections the import feeds can be synced: assets, asset groups, software inventory, crown jewels and PIRs. The name, sector, regions, size, currency, risk appetite and the other typed fields are never synced.

A sync keeps the source, the filters, the batch type, the choice for the rows that left the platform (removed by default: a deletion upstream deletes the row in XTM One; switch it off to keep them), its exclusion list (`excluded_external_ids`: the platform's rows it never imports, a stored copy included) and a schedule:

- **Hourly**, at a minute of the hour.
- **Daily**, at a time in a time zone.
- **Weekly**, on a day at a time in a time zone.

A section can carry several syncs, for example one per source or one per filter set. **Sync all** is one more schedule that runs every enabled section sync in the order they were saved. **Run now** runs a sync at once without moving its next scheduled run.

**Settings > Organization > Syncs** lists them: the section, the source, the filters, the schedule, the last run (when, rows added, updated and removed, or its error on hover), the next run, the status (**Enabled**, **Paused** with its reason on hover, **Disabled**) and the enabled switch, with **Run now**, **Edit** (schedule and switch) and **Delete** on each row. **Add Sync all** appears once a section sync exists. Deleting a sync stops its schedule; the rows it imported stay in the profile.

Each run follows the import rules:

- A run saves a new profile version only when something changed. The version names the sync and the administrator who saved it.
- A row typed by hand, uploaded as CSV or written through the API is never overwritten.
- A run reads with the platform token of the administrator who saved the sync. When that administrator is no longer active or no longer an administrator, or the platform token is missing or revoked, the sync pauses and shows the reason. Saving it again resumes it with your own token.
- Two runs of one sync never overlap.
- A run whose read was cut (more rows than one run reads) or failed removes nothing.
- The rows a run writes carry the sync's id; a row whose id is on the exclusion list is left out and its stored copy removed. The last run counts the excluded rows.
- Every run that changed the profile or failed, and every change to a sync, is written to the audit log. `GET /api/v1/organization-context/syncs/{id}/history` answers the last of these events for one sync (when, who, the trigger, the counts, the error).

### Delete a sync and its rows

Deleting a sync asks what becomes of the rows it wrote (`DELETE /api/v1/organization-context/syncs/{id}?rows=`):

- **leave** (the default): the rows stay as they are.
- **release**: the rows stay and become editable by hand. The saved version records where they came from ("formerly synced from" the platform).
- **purge**: only the rows this sync wrote are removed. A row typed by hand, or written by another source, is kept, even when it has the same name.

`GET /api/v1/organization-context/syncs/{id}/rows-preview?mode=purge` counts and names the rows first, and the rows of other sources with the same name that are kept. Release and purge save a new profile version and are written to the audit log.

### Sources

`GET /api/v1/organization-context/sources?scope=profile|inventory` lists every source of rows: its kind (a platform sync, an agent or partner writing through the API, a CSV import, the API, or entered by hand), its name (OpenAEV, Microsoft Intune, the agent's name), the agent that last wrote its rows when a saved version names it, its rows per section and its syncs. The rows of an agent, a partner or a CSV import can be purged or released the same way: `GET /api/v1/organization-context/sources/{source}/rows-preview?mode=` then `POST /api/v1/organization-context/sources/{source}/rows` with `{"mode": "purge"}` or `{"mode": "release"}`. Rows entered by hand are never purged.

### Sync direction

Each sync has one direction and one master, never a free two-way merge:

- **Inbound** (the default, and every sync saved before directions existed): the platform is the master. Its rows are read-only in XTM One and follow the platform.
- **Outbound**: the XTM One list (typed, CSV, API or maintained by an agent) is the master. The platform rows this sync created are created, updated and deleted to match it. A platform row the sync did not create is never touched, except the criticality of a crown jewel already linked to it, which follows the profile.

An outbound run writes with the token of the administrator who saved the sync, like an inbound run, and every run is written to the audit log. A change of direction starts clean: the new direction does not take over the rows the previous one created.

| Section | Platform | Directions |
|---|---|---|
| Crown jewels | OpenCRQ (supporting assets) | Inbound, Outbound |
| Assets | OpenAEV | Inbound (outbound planned) |
| Asset groups | OpenAEV | Inbound (outbound planned) |
| PIRs | OpenCTI | Inbound (outbound planned) |
| Assets | OpenCTI | Inbound: Systems are owned by OpenCTI connectors |
| Assets of category AI target | OpenAEV (AI targets) | Inbound, Outbound: XTM One-owned AI target assets are created and updated in OpenAEV |
| Software | OpenAEV | Inbound: the software inventory is read from OpenAEV agents |

### What each platform feeds

The source x section x direction matrix, read from the platform APIs the syncs call:

| Platform | Section | What a run reads | Directions |
|---|---|---|---|
| OpenAEV | Assets | Endpoints (`POST /api/endpoints/search`): name, category and subcategory, criticality, exposure, and since #1134 S4 the freshness: **last seen** (`asset_last_seen`) and **status** (`asset_status`: active, inactive, agentless, or stale when unseen past the tenant's threshold) | Inbound |
| OpenAEV | Asset groups | Asset groups (`POST /api/asset_groups/search`) | Inbound |
| OpenAEV | Assets of category AI target | AI targets (`POST /api/ai_targets/search`) | Inbound, Outbound |
| OpenAEV | Software | Security platforms (`POST /api/security_platforms/search`) | Inbound |
| OpenCTI | Assets | Systems, with the OpenAEV taxonomy when OpenCTI carries it | Inbound |
| OpenCTI | PIRs | Priority intelligence requirements | Inbound |
| OpenCRQ | Crown jewels | Supporting assets at or above the minimum criticality | Inbound, Outbound |

The freshness of an asset is the source's: it is read-only in XTM One and follows each run. When several sources describe one asset, the most recently seen record wins a field the precedence leaves tied. Not synced yet: an OpenAEV per-asset software inventory (OpenAEV exposes no software list on an endpoint today), OpenCTI organizations into tracked organizations (linked one by one from the tracked organization), and OpenCRQ risk tolerances (no profile field holds them).

In **Syncs**, the **Direction** column shows each sync's master, and **Edit** sets it: **Outbound** is offered only where the platform can be written, otherwise it is greyed out with the reason. Changing the direction shows that the new direction starts clean before you save.

`GET /api/v1/organization-context/syncs/directions` lists, per section and platform, the directions a sync may take and why outbound is not offered.

The syncs run on the platform's scheduler, with no model call, AI on or off. The API is `GET`, `POST /api/v1/organization-context/syncs`, `PUT` and `DELETE /syncs/{id}`, and `POST /syncs/{id}/run`; `direction` is `inbound` or `outbound`.

## Draft the profile with AI

**Draft > With AI** proposes values from what you give it, all optional:

- a description of the organization (what it does, where, its main systems), up to 4,000 characters;
- documents, such as an annual report, an asset or CMDB export or a supplier list: 5 files, 10 MB each, used for this draft only and not stored;
- your public website: one page and at most 4 links of the same site, fetched only when you give it;
- the facts **From the platforms** reads, for each connected platform you keep ticked.

One model call is made when you select **Draft**, never when the page loads, and it counts on your quota like a chat turn.

The result is a review, never a save. Each proposed field shows its value, its source and a confidence; hover the confidence to read the basis. A field that already holds a value is marked **Replaces your value** and shows the current value. An amount the model inferred is marked **Check this**. Accept or reject each row, or select **Accept all**, which leaves conflicts, **Check this** rows and low confidence rows for you to review. Proposals equal to the current value are collapsed under **Unchanged**, and the fields nothing supports are listed under **Not supported**: they stay empty.

**Save accepted** writes one new version with the source "AI draft"; the version keeps, for each accepted field, its source and basis.

Without an Enterprise licence, or when AI is switched off, **With AI** shows a lock and opens the licence options; **From the platforms** stays available and calls no model.

## Draft tracked organizations with AI

In **Settings > Organization > Tracked organizations**, **Draft with AI** proposes each organization's details from its name alone: sector, regions, website, relationship, criticality and the matching OpenCTI organization. It needs agentic AI; without it the button opens the trial, license or LLM setup instead.

1. Type the names, one per line, or choose a supplier or customer file (CSV or XLSX, 2 MB at most). The first column is read as names and a header row is skipped.
2. Choose the relationship to use when the model cannot place a name.
3. Read the count line, then select **Draft**. A run drafts at most 50 names and uses one agentic request of your quota per 10 names. Above 50 names, the dialog says how many are left for the next run.

The proposals open on their own review page. Nothing is created yet.

- Accept or reject each row from its actions, or select rows and use **Accept selected** or **Reject selected**.
- When several OpenCTI organizations match a name, pick one in the row. A row with **No match** is created without a link.
- A name you already track is shown as **Already tracked**. It is never created twice and costs no request.
- When a batch fails, its names are listed with **Retry**. The other batches are kept.
- **Create accepted** creates the accepted organizations with monitoring off. **Discard draft** creates nothing.

## Next step

[Users and groups](users-and-groups.md) explains how to manage who uses the platform.
