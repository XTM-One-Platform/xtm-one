# 🎯 OpenAEV Connector

This page explains what the OpenCRQ connector pulls from an OpenAEV instance, what the OpenAEV side
must provide for it to work, how often each part runs, and when what it reported is considered
stale. It also covers the two places where it departs from a plain "call a documented endpoint"
integration. See [Configuration](configuration.md#openaev-adversary-exposure-validation) for the
per-tenant `CONNECTORS__OPENAEV__<tenant-uuid>__URL`/`CONNECTORS__OPENAEV__<tenant-uuid>__TOKEN`
variables.

## What the connector syncs

- **The whole asset inventory** — every asset OpenAEV knows, whatever its category and whether or
  not a simulation has ever targeted it. Observations enrich an asset; they never create one.
  Security platforms (`SECURITY_PLATFORM`) are not synced: OpenAEV's `/assets/search` excludes them
  server-side. One reaching OpenCRQ anyway lands as `security_platform` with its subcategory (EDR,
  SIEM and so on), never fused with a host nor counted as a loss-bearing target.
- **Generic attributes** on every asset — name, category, subcategory, criticality, internet
  exposure, external reference — and its tags, read through OpenAEV's own tag vocabulary. A tag's
  identity in OpenCRQ is its name within the tenant, so a tag renamed on OpenAEV's side follows
  rather than duplicating — and can split a tag two sources share. See
  [Asset consolidation](asset-consolidation.md#tags).
- **Network properties**, for endpoint-typed assets only — hostname, IP and MAC addresses, seen
  IP, platform, architecture, end-of-life flag, plus the agent-reported identifiers that link an
  asset back to a specific EDR agent.
- **AI target connection properties**, for `AI_TARGET` assets — provider, endpoint URL, model,
  modality, system prompt. See below for the two fields that are read off the wire and then never
  used again.
- **Cloud attributes** — provider, native type, region — read alongside the network properties
  from the same call, and shown in their own *Cloud* card exactly as OpenAEV shows them.
- **Attack observations** — for each asset, scenario and MITRE technique, the latest verified
  prevention or detection verdict, one per security platform type that reported on it (EDR, SIEM…).
  Only simulations that are `FINISHED` count. The link to the simulation and to the inject behind
  the verdict is kept.
- **CVE observations** — the CVEs OpenAEV found on each asset.
- **The description, URL and linked person of generic assets** (web applications, identities, SaaS
  applications, and any machine-family category stored as a generic asset). OpenAEV serves them for
  these assets one call at a time, so they are fetched on a budget, outside the inventory (see
  [A run and its phases](#a-run-and-its-phases)). Endpoints and AI targets get theirs from the
  inventory's batch calls. The linked person is stored as a name, not as an OpenAEV user id.
- **The posture score** is not synced. It is read from OpenAEV when an asset's page opens: the
  validated (`SUCCESS`/`FAILED`) expectation counts OpenAEV's own dashboard shows for the asset,
  summed over every OpenAEV source the asset has. The score, the band and the wording shown in
  OpenCRQ are computed from those counters, not copied from OpenAEV, and the counters themselves
  are not stored.

## What OpenAEV must provide

Everything the connector reads about verdicts and CVEs comes from OpenAEV's bulk snapshot export.

- **An OpenAEV version that ships the bulk snapshot export**, with its **`BULK_SNAPSHOT_EXPORT`
  preview feature enabled** (`openaev.enabled-dev-features=BULK_SNAPSHOT_EXPORT` on the OpenAEV
  side). Without it the connector reads exercises and findings instead, see
  [OpenAEV without the snapshot export](#openaev-without-the-snapshot-export).
- **The service account's role needs two capabilities**:
  - **`ACCESS_SNAPSHOT_OBSERVATION`** (*Access observation snapshots*), which guards the two
    snapshot routes.
  - **`ACCESS_ASSESSMENT`** (*Access assessment*), so that the account sees the observations of
    every scenario and simulation of the OpenAEV tenant. Without it, the account only sees the
    scenarios and simulations it was explicitly granted: with none, the attack stream is **empty,
    with no error to say why**.
- **`ACCESS_FINDINGS` is not needed** when the snapshot export is on: the connector does not call
  the findings search.
- The token also needs read access to the inventory routes: `/api/me/tenants`, `/assets/search`,
  `/endpoints/find`, `/ai_targets/search`, `/tags`, `/users/find`, `/assets/{id}`, and
  `/dashboards/adhoc/series` for the posture. It calls no exercise, scenario, expectation or
  inject route.

A sync against an OpenAEV whose account lacks the snapshot capability **fails and says so**,
without retrying:

| OpenAEV answers | The run fails with a message starting with |
|---|---|
| `403` on a snapshot route | `OpenAEV refused the snapshot request: the account lacks the ACCESS_SNAPSHOT_OBSERVATION capability (403)` |

A `404` is not a failure: it means `BULK_SNAPSHOT_EXPORT` is off, and the run reads exercises and
findings instead. The connector asks the attack snapshot route once before anything else, so a
refused run has read and written nothing.

## A run and its phases

The connector is one job per OpenCRQ tenant, scheduled hourly by default (the schedule can be
changed from the connector's page, and **Sync now** runs the same job). Every run goes through the
same phases, and each decides on its own whether it is due:

| Phase | When it runs | OpenAEV calls |
|---|---|---|
| **Inventory** | On the first run, then when the last completed inventory is older than 24 hours. Also once in a run where an observation names an asset the inventory does not know. | Tags, asset search, endpoint batches, AI target search, linked-person lookup |
| **Attack observations** | Every run, from the cursor saved by the previous one: only what changed since. | Snapshot attack stream |
| **CVE observations** | Every run, from its own cursor. | Snapshot vulnerability stream |
| **Full reconciliation** | Per stream and per OpenAEV tenant, on the first run and then when the last completed one is older than 7 days. It restarts the stream from the beginning. | The same two streams, without a cursor |
| **Retirement** | After the observation phase of each OpenAEV tenant. | None |
| **Descriptions of generic assets** | Every run, within a budget of 500 calls shared by all OpenAEV tenants, for assets not fetched in the last 24 hours. | `GET /assets/{id}` |

A run that is stopped before it reaches the end of a stream (the 30-minute run limit, an OpenAEV
error) keeps the cursor of the last page it wrote and resumes from there at the next run; a full
reconciliation interrupted the same way is resumed, not restarted. OpenAEV delivers each document at
least once, and every write tolerates seeing one twice.

### The inventory

Per OpenAEV tenant, before anything else:

1. List the tag vocabulary (`GET /tags`) and upsert it into OpenCRQ's own vocabulary.
2. Page through the unified inventory (`POST /assets/search`) for every non-excluded asset's
   generic attributes and tags.
3. Batch-fetch the **endpoint rows** found in step 2 through `POST /endpoints/find` for their
   network and cloud properties, their URL and their description.
4. Page through the AI target search (`POST /ai_targets/search`) for the AI target connection
   properties and their description.
5. Resolve the linked person of the assets in one `POST /users/find` call.
6. Upsert every asset — generic attributes and, where a detail call carried one, its property
   block — in batches, then its tag links, building the OpenAEV asset id to OpenCRQ asset map every
   later phase resolves against.

Step 3 routes on OpenAEV's `asset_type` discriminator, **never on `asset_category`**.
`POST /endpoints/find` is backed by the endpoint repository, so it answers for `endpoint` rows and
silently omits every other one. A `CLOUD_RESOURCE` created as a generic asset carries a
machine-family category on an `asset` row: routing it on its category would have it come back from
nothing, on every run, with no error to show for it.

Nothing is written before step 6 on purpose. The matcher that consolidates an asset with what
another connector already reported runs once, when a `(connector, source asset id)` pair is first
written, and never again for that pair: an asset upserted from its inventory row alone would be
fixed on an asset of its own before the hostname and the agent reference that identify it had even
been fetched, and could never be fused with the Defender device behind it.

An asset that did not change in OpenAEV is **not rewritten** in OpenCRQ, and neither are its
properties, sources or tag links, so nothing downstream sees it as modified. The exception is an
endpoint whose agents were seen again: the last-seen date they report counts as a change.

When the inventory is not due, the observation phases resolve their assets against the ones already
on file. An observation naming an asset that is not on file triggers the inventory once in that run
(if it has not run) and the page is read again; an asset still unknown after that is skipped, **and
the cursor still moves on**. Its observation is picked up again when OpenAEV next updates it, or at
the next full reconciliation.

The inventory is checkpointed as one unit across the OpenAEV tenants: a job that resumes mid-run
does not repeat it, but a crash partway through it restarts it from the first tenant.

### The observations

Both streams are read page by page (1 000 documents per page). Each page is written in a fixed
order — a local copy of the attack documents, the evidence, then the cursor — so that a crash
between two writes redelivers the page rather than losing it.

- **Attack observations** become one finding evidence per asset, scenario, technique, expectation
  type (prevention or detection) and security platform type. A platform type counts as exposed
  unless it is among those that succeeded; when OpenAEV names no reporting platform type, a
  `SUCCESS` verdict reads as not exposed and anything else, `PARTIAL` included, as exposed. The
  reason reads like `Not detected by EDR in simulation <name>`. A technique OpenCRQ's MITRE catalog
  does not hold is skipped.
- **A document whose simulation is not `FINISHED`** (scheduled, running, paused, canceled) writes
  nothing: the previous verdict stays in place until the replay finishes, when OpenAEV delivers the
  document again.
- **CVE observations** become one evidence per asset and CVE, with the reason
  `Reported by OpenAEV in simulation <name>`. Only documents whose type is `CVE` and whose value is
  a well-formed CVE identifier (trimmed and upper-cased) are kept. OpenAEV carries no CVSS score
  or exploit information, so the CVE is created without them.

### The descriptions

The description phase runs after the observations and the retirement of each OpenAEV tenant, since
it is the least urgent work of the run. It fetches the generic assets never fetched first, then the
ones fetched longest ago, up to 500 calls per run (8 at a time). Each OpenAEV tenant gets an even
share of the calls the run has left, and what one leaves unspent goes to the next. An asset OpenAEV no longer knows
(`404`) is skipped for this run; any other failure of one asset is logged and does not fail the
run. If OpenAEV answers none of a batch of 100, the phase stops for that OpenAEV tenant. A field
OpenAEV emptied is emptied in OpenCRQ too, and the inventory carries back the stored value rather
than blanking it.

An asset fetched less than 24 hours ago is not a candidate, so a small estate is not fetched again
on every run. An estate with *N* such assets therefore takes `N / 500` runs to describe all of them
the first time, and refreshes each description once every `N / 500` runs after that, or once a day
when that is longer.

## What goes stale, and when

OpenAEV's differential streams carry no deletion, so what disappears from OpenAEV is inferred. The
evidence the connector no longer stands behind is dismissed with the reason
**`No longer reported by OpenAEV`**; it comes back by itself if OpenAEV reports it again.

| What OpenAEV did | When the evidence goes stale |
|---|---|
| A newer finished simulation of the same scenario no longer plays a technique on an asset | At the run that receives the newer simulation's documents, even when the older document arrives first, and whenever a full reconciliation delivers the older one again. |
| A document that is rewritten no longer carries a security platform type it carried before | Same run, and the type stays dismissed when other techniques of the same asset and scenario change later. |
| An asset, a scenario or a technique was deleted | At the next **full reconciliation**, and only if it ended with OpenAEV reporting its snapshot as ready (see below). |
| A replay of the scenario is still running | Never: the previous verdict stands until the replay finishes. |
| A CVE is no longer reported on an asset | See below. |

For attack observations, the connector keeps a minimal local copy of the documents it received (one
row per OpenAEV document) to know which ones are still current. A document is **superseded** when
another document for the same asset and scenario comes from a finished simulation that started
later. This is what lets a verdict that OpenAEV updates on its own — a late answer from a
collector, a manual validation — leave the asset's other techniques untouched. A document is
**deleted** when a full reconciliation that ended ready did not see it again. Only the current
documents of an asset and scenario write evidence, so an old document that a full reconciliation
delivers again brings nothing back. The copy also records the security platform types each
document's evidence was written with: a type the document no longer carries is the one that goes
stale.

Several OpenAEV assets can be folded onto one OpenCRQ asset; the documents of all of them are judged
together, so a technique that only one of them reports stays applicable when a run delivers the
documents of another.

What a run read is reconciled into evidence once the stream has been read, and the stream remembers
the start of the oldest run that is not reconciled yet. A run that fails before then leaves that
mark in place, and the next run reconciles from it; a full reconciliation that reached the end of
its stream is only recorded as completed once its retirement is done.

**A full reconciliation only deletes anything if OpenAEV says its snapshot is ready** at the end of
the walk. Otherwise nothing is removed, the attempt is recorded, and it is retried a day later,
until a walk ends ready. A full reconciliation of one OpenAEV tenant that ends ready only deletes
that tenant's documents from the local copy, but it judges the attack evidence of the whole
connector, assets of other OpenAEV tenants included. One precaution holds in every run: the attack
evidence of an OpenCRQ asset is only judged when every OpenAEV tenant behind it has a complete
copy, that is the tenant being reconciled or one whose own full reconciliation has ended ready at
least once, since the documents of all its OpenAEV assets are judged together. After a reset of the connector, after a run on the fallback path or on
the first run after an upgrade, the copy of the tenants not read yet is empty or half read, and the
evidence of their assets is left alone until they are complete. An asset whose OpenAEV tenant is not
known yet, which is the case until the first inventory has registered the assets, waits for every
OpenAEV tenant of the run to be complete.

The connector also keeps the list of the OpenAEV assets each inventory reported. An inventory that
ran to the end, listed at least one asset, truncated no list and listed as many assets as every page
reported dismisses the CVEs reported for the assets of its tenant that OpenAEV no longer lists, since
OpenAEV deletes an asset's vulnerability documents with it, then forgets those assets. An inventory
during which OpenAEV's count of assets changed forgets nothing: an asset added or deleted mid-read
shifts the pages, and an asset the shift skipped would be taken for deleted. The warning
`OpenAEV asset inventory changed while it was read` says so.

**CVEs** have no local copy, and follow a coarser rule. A CVE source is keyed and scoped by the id
OpenAEV gives the asset it was reported for, so OpenAEV assets folded onto one OpenCRQ asset, from one
OpenAEV tenant or from several, each keep their own source: what one of them stops reporting is
retired without touching what another still reports, and a CVE stays applicable while any of them
reports it. In a run that is not part of a full reconciliation, an OpenAEV asset that received at
least one CVE observation has its other CVEs, those not delivered since the stream's last
reconciliation, dismissed; they come back as soon as OpenAEV delivers them again, at the latest at the
next full reconciliation. An asset that received none is left alone. While a full reconciliation of
the CVE stream is in progress, nothing is dismissed; once it ends ready, every CVE of the OpenAEV
tenant's assets that the walk did not deliver is. OpenAEV itself never removes a CVE observation
when the finding behind it is deleted: such a CVE stays reported.

## What changed from the previous connector

If you are upgrading, expect these differences:

- **No 7-day window.** The first run is a full backfill, so verdicts and CVEs older than a week now
  arrive, each with its own verification date.
- **Simulations without a scenario no longer produce an attack verdict.** OpenAEV's attack stream
  is carried by scenarios.
- **Only `CVE` findings are read.** A `vulnerability`-typed finding whose value names a CVE is no
  longer picked up: the injectors that emit that type never name one.
- **The evidence reason no longer names the inject.** The link to the inject is kept.
- **The history keeps one transition per run.** When a scenario is replayed several times between
  two runs, the evidence history only shows the latest state.
- **The posture is read live** when an asset's page opens, instead of being synced and stored.
- **Descriptions of generic assets arrive over several runs**, not all at once.
- **The token needs `ACCESS_SNAPSHOT_OBSERVATION` and `ACCESS_ASSESSMENT`,** and no longer needs
  `ACCESS_FINDINGS`.

## An agent's identity comes from two calls at once

OpenAEV serves the same agent in two shapes, and neither shape carries the whole identity.

The unified inventory maps through a DTO that **embeds** the agent's executor, so it states the
executor's type — but it never carries `agent_external_reference`. The endpoint detail call returns
the raw entity, which does carry that reference but reduces the executor relation to its **bare id
string**. OpenCRQ needs both halves: the executor type is what tells it the agent is a Microsoft
Defender one, and the reference is the Defender device id that lets the same machine reported by
both connectors resolve to a single asset. The two agent lists are therefore joined on `agent_id`.

The practical consequence for anyone extending this connector: an agent field read from only one of
the two calls will be silently empty for half its values, and modelling `agent_executor` as an
object alone makes the whole enrichment phase fail rather than degrade.

## The posture score is not a public contract

Reading the posture replays the ad-hoc widget query the OpenAEV UI itself builds
(`POST /dashboards/adhoc/series`) rather than calling a documented, versioned endpoint — it is the
one part of this connector without a stable contract, isolated in a single client method for
exactly that reason.

The read happens when an asset's page opens, for the asset's OpenAEV sources only, with a
10-second timeout. When the asset has no OpenAEV source, the tenant has no OpenAEV connection,
OpenAEV is slow, unreachable or without the dashboard engine, or has never measured the asset, the
page shows no posture tile instead of an error. The sync never depends on it.

## What never leaves OpenAEV

`ai_target_token` (a credential) and `ai_target_configuration` (free-form, and may itself carry a
credential) are present on OpenAEV's raw payload — on **every** asset it returns from the detail
calls, endpoints included, not only on AI targets. Neither is ever read into an OpenCRQ type,
mapped, or persisted.

What guarantees that is worth knowing before changing it: the detail schemas are strict, so unknown
fields are dropped at the parse boundary. These are the schemas that parse the endpoint and AI target
raw entities, plus the `GET /assets/{id}` overview, whose DTO leaves the credential out today. The
inventory, agent, observation, tag and user schemas are deliberately permissive, to let OpenAEV add a
field without breaking a sync. Relaxing the detail schemas the same way would make the credential
survive parsing and travel through the handler unread — which holds only until something logs the
object.

## What a run costs, and where it stops

A run costs what changed since the previous one, plus the inventory once a day and the weekly
reconciliation:

- **A run where nothing changed** makes one call per snapshot stream and per OpenAEV tenant, writes
  no evidence and rewrites no asset, plus up to 500 description calls while the estate has generic
  assets not fetched in the last 24 hours.
- **The inventory** is a full pull, once a day: it grows with the size of the estate, not with what
  changed. It costs one call per page of 100 assets, one per 100 endpoints and one per page of 100
  AI targets.
- **The weekly full reconciliation** re-reads every observation OpenAEV holds, per stream and per
  OpenAEV tenant: its cost follows the number of documents (assets × techniques for the attack
  stream), not the number of simulations ever run.
- **The first run** does all of it: the inventory and both full reconciliations, and may need
  several runs to complete on a large estate (a run is stopped after 30 minutes and resumes where
  it stopped).
- **A hard pagination cap of 1 000 pages of 100** per listing call (asset search, AI target
  search). Past 100 000 assets the inventory is truncated; the truncation is logged at warn level
  (`… truncated: pagination reached max pages`) and the run otherwise completes normally. The
  snapshot streams have no such cap, but OpenAEV does not expose a differential asset feed, so the
  cap on the inventory remains.

An observation on an asset the inventory did not return is **dropped**, not ingested against an
asset created on the spot. That is the intent — observations enrich the inventory, they do not
define it — and it is normally invisible, since an asset created in OpenAEV between two inventories
is picked up by the inventory a run triggers itself. It stops being invisible above the pagination
cap: a truncated inventory also loses every observation sitting on the assets it did not return.

## Resetting the connector

**Reset state**, in the connector's menu, makes the next run start over as on a first install: it
rebuilds the inventory, forgets the cursors, the local copy of the attack documents and the registry
of assets, and re-reads both streams from the beginning as a full reconciliation. Nothing already
imported is removed. The first-run costs above apply again. It is offered only to a member who holds
`MANAGE_SECURITY`, as for every connector.

## Connecting a tenant

- Set `CONNECTORS__OPENAEV__<tenant-uuid>__URL` and `CONNECTORS__OPENAEV__<tenant-uuid>__TOKEN`
  (see [Configuration](configuration.md#openaev-adversary-exposure-validation)).
- On the OpenAEV side, enable the `BULK_SNAPSHOT_EXPORT` preview feature, and give the token's
  role the `ACCESS_SNAPSHOT_OBSERVATION` and `ACCESS_ASSESSMENT` capabilities, as above.
- Link a service account to the connector from the Integrations page, then run it once and check
  its run for the errors listed above.

## OpenAEV without the snapshot export

An OpenAEV whose `BULK_SNAPSHOT_EXPORT` preview feature is off answers `404` on both snapshot
routes. The connector then reads what it read before the snapshot streams existed: the finished
exercises and the CVE findings.

**How the mode is chosen.** At the start of every run, before the inventory, the connector asks the
attack snapshot route of the first OpenAEV tenant for a single observation (`page_size: 1`, a
read-only query). A `2xx` runs the snapshot mode described above. A `404` runs this one, for every
OpenAEV tenant of the run, since the feature is an instance-wide switch. A `403` fails the run as
described above, with no fallback. Any other failure is retried like any other call. There is no
setting: the choice is made again at each run, so enabling the feature on OpenAEV takes effect at
the next run.

**The account's rights.** The token reads `/exercises/search`, `/exercises/{id}`,
`/exercises/{id}/expectations`, `/scenarios/{id}`, `/injects/{id}` and `/findings/search`, next to
the inventory routes. The role needs **`ACCESS_FINDINGS`**; the two snapshot capabilities are not
used. A findings search that is refused for one OpenAEV tenant does not stop the exercises: the run
completes, but `last_reported_at` stays where it was, so the next run reads the same window again.

**What a run does.**

- The inventory on every run, not once a day.
- The CVE findings of each OpenAEV tenant, `CVE` findings and `Vulnerability` findings whose name
  names a CVE (a name that does not is ignored, as is one whose status says `not vulnerable`).
- The finished exercises changed since the last completed run, **never more than 7 days back**, the
  oldest first. An exercise finished more than 7 days before the first run is not read. The reason of
  an attack evidence names the simulation and the inject title.
- For each exercise, the techniques its scenario no longer reports on an asset it visited are
  retired (`No longer reported by OpenAEV`), under that scenario.
- The descriptions of generic assets, within the same budget as the snapshot mode, at the end of the
  run.

The posture is read live in both modes.

**Switching modes.** The evidence both modes write lands on the same keys, so a switch in either
direction creates no duplicate. Each run of this mode forgets the stream cursors and the local copy
of the attack documents, and keeps the asset registry. The first run of the snapshot mode after it
therefore reads both streams from the beginning, as a full reconciliation, and retires what only the
exercises and findings carried.

**CVEs carried over from the previous connector.** The previous connector keyed a CVE by the OpenCRQ
asset, not by the OpenAEV asset that reported it, so the upgrade copies each such CVE onto every
OpenAEV asset folded onto that OpenCRQ asset. In this mode, on an asset folded from several OpenAEV
assets, a copy keeps the CVE applicable after the OpenAEV asset that reported it stops, for as long
as the asset it was copied onto reports no CVE at all. The first full reconciliation of the CVE
stream that ends ready in the snapshot mode dismisses it.

**Which mode ran.** The run's progress starts with `OpenAEV snapshot export is off, reading
exercises and findings`, followed by `Syncing OpenAEV CVE findings`, `Listing exercises changed
since …` and `Processing exercise N of M`; the snapshot mode reports `Reading OpenAEV attack
observations of tenant …` instead. The backend logs, at warn level, `OpenAEV answers 404 on the
snapshot routes, its BULK_SNAPSHOT_EXPORT preview feature is off: reading exercises and findings
instead`.
