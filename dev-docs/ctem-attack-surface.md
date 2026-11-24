# CTEM outside lens: the attack surface pass (CTEM-26)

The first version of the outside lens. It reads what the Internet reaches of the
organization through an attack-surface provider XTM One already connects, and
records each difference as a CTEM signal. The spine (reconcile, scope gate,
cases: `ctem-signals.md`, `ctem-scope-gate.md`) decides what each signal becomes.
No model call and no human gate on this path. The native OpenAEV capability
(asset freshness and reachability assertions, CTEM-27) feeds the same paths
(section 2ter).

## 1. Profile

| Field | Meaning |
|---|---|
| `owned_domains` | The DNS and email domains the organization owns (shared with CTEM-29). |
| `address_ranges` | The public address ranges it owns, as CIDR blocks (normalised; a bare address is its /32 or /128; anything else is refused at save). |
| `attack_surface.enabled` | Opt-in, off by default. |
| `attack_surface.integration` | The id of a provider integration the pass reads (section 2bis lists the providers). |
| `attack_surface.integrations` | More provider integrations read in the same pass (up to 10), merged per host. |
| `attack_surface.cadence_hours` | Hours between two passes (24 by default). |
| `attack_surface.stale_after_days` | A host unseen for this many days is a stale asset (14 by default). |

These are exposure fields: read by the pass, never dumped into the always-on
profile block, not contract fields. Per-asset expectations stay out of the
profile: `assets_exposure[].internet_facing` is the only declaration;
reachability assertions belong to OpenAEV (CTEM-27).

Settings > Organization > Exposure edits them in the card "Owned domains and
outside lens" (`OrganizationOutsideLensCard.tsx`, #1140 slice 2; administrators,
like the PUT): the domains and ranges are normalised in the page exactly as the
backend does (`normalizeDomain`, `normalizeAddressRange` in
`frontend/src/lib/organization-profile.ts`), a refused range blocks Save. The
provider picker lists the connected integrations whose provider is in
`SUPPORTED_PROVIDERS`, read from `GET /api/v1/organization-context/attack-surface/providers`,
so a new reader shows up there with no frontend change. Every other profile
save carries the four fields whole (`toForm` / `fromForm`).

Agents set them like any profile field: `update_organization_context`
(administrator, a new version at once) and `propose_organization_context_change`
(a pending proposal an administrator accepts field by field) accept the three
paths `owned_domains`, `address_ranges` and `attack_surface` on the primary
profile (`PROPOSABLE_EXPOSURE_PATHS` in
`services/organization_context_proposals.py`); the profile model validates them
(an invalid CIDR is refused with its reason). A tracked organization has none.

## 2. The pass

`services/ctem_attack_surface.py`, called by the autonomous tick after the case
sweep, at most once per cadence window (Redis slot `ctem:attack-surface:pass`). The slot is kept only by
a pass that committed: a skipped pass (no credential yet) or a failed provider read gives it back, so the
next tick retries.

1. Read: each declared provider's reader (section 2bis) gives one observation
   per public host (address, ports that answer, certificate names, DNS target,
   whether the name resolves); with several providers, `merge_observations`
   joins them per host (ports and certificate names joined, a host resolves when
   any provider saw it resolve) and the evidence names the providers that saw it.
   Every declared reader must answer: one failed read fails the pass, because a
   partial view would age the hosts only that provider sees into path 6.
2. Diff, in code (`diff`, a pure function): against the previous pass
   (`ctem_surface_observations`, one row per organization and host) and the
   profile.
3. Record: each difference is a signal of the outside lens, subject kind
   `asset`, recorded by `system` through `record_signal`, then the scope gate
   runs on it (`evaluate_after_record`).
4. Store: the observations become the new baseline; each host keeps the signal
   keys already raised for it, so two identical passes record nothing new.

| Path | Fires when |
|---|---|
| 5 `new_exposure` | A declared asset with `internet_facing: no` answers; a declared asset answers on a port the previous pass did not see. |
| 6 `stale_asset` | A listed name that no DNS record of any type backs (a name with only MX, TXT or NS records is not dangling); a name of an owned domain points outside the owned ranges; a host an earlier pass saw is unseen for `stale_after_days`. |
| 7 `surface_drift` | A host neither declared nor seen by an earlier pass, read completely by this pass and the previous one (section 2quater; the reconcile leaves it `to_validate`); a new port on an undeclared host. |

The first pass sets the baseline: it raises paths 5 and 6 (they compare with
the profile) and no path 7. A port that closes, a name that resolves again or a
host seen again clears its key, so the same change raises a signal again the
next time it happens.

## 2bis. Providers

`READERS` maps an integration provider to its reader; `NOT_SUPPORTED` names the
connected providers that cannot fill an observation, with the reason. A
provider is added only when its API fills the observation from a response
shape the tests record, never approximated.

| Provider | Reader | What it reads | Limits |
|---|---|---|---|
| Shodan (`shodan`) | `ShodanSurfaceReader` | `GET /dns/domain/{domain}` paged on `more` (records and listed subdomains), `GET /shodan/host/search` on `hostname:` and `net:` paged on `total` (banners: address, port, transport, product, certificate CN) | Up to 5 DNS pages and 3 search pages per query (section 2quater); a query at its bound is a truncated read. |
| Google Threat Intelligence ASM (`gti`) | `GtiAsmSurfaceReader` | `GET /asm/projects`, then per project `GET /asm/search/entities/type:Intrigue::Entity::IpAddress` (in-scope IP entities: `summary.ports.tcp/udp`, `summary.resolutions`), kept when a resolution is under an owned domain or the address is in an owned range | Up to 5 projects and 10 pages of 100 per pass. ASM lists no unresolved name: path 6 "resolves to nothing" comes from Shodan only. Needs the ASM module on the license. |
| SecurityScorecard | not supported | | Scores and findings per issue type, no host inventory with open ports. |
| SOCRadar | not supported | | The ASM digital asset list (`GET /company/{id}/asm`) carries names, source and monitoring state, no address and no open ports. |
| DomainTools | not supported | | DNS and WHOIS history, no open ports; the registrar's view, not the reachable surface. |
| Rapid7 | not supported | | InsightVM assets are the inside scan of managed assets, not the outside view. |

## 2quater. Read completeness and the pass summary (#1498)

A host missing from the baseline is only new when the previous pass could have
seen it. Each reader records what each query read (`ReadCoverage`: provider,
scope, kind, pages, records, `truncated` when the query hit its page bound);
`read_statuses` gives each `<provider>|<scope>` read a status: `truncated`,
`empty` (its queries answered nothing) or `complete`. The scope is the owned
domain or range queried; GTI ASM reads per project, so its one read is `*`.

Path 7 `new_host` is raised only when (`new_host_withheld`):

- a previous pass summary exists;
- every read that saw the host is `complete` in this pass and was `complete`
  in the previous one (a read missing from the previous summary counts as not
  complete);
- the provider's own dating does not put the host before the previous pass
  (`known_since`: a Shodan DNS record's `last_seen`, a banner's `timestamp`,
  an ASM entity's `first_seen`).

Any other host joins the baseline silently; the pass counts it as
`new_host_withheld`. A signal raised carries `reads` and `previous_pass_at` in
its evidence. The first pass after this change has no previous summary, so it
raises no path 7: it records the summary the next pass compares with.

Only the organization's own hosts are part of the surface (`in_scope`): a name
under an owned domain, or a host or IP inside an owned address range (a declared
asset always counts). A provider also answers other names, such as the reverse
DNS of a shared hosting IP: such a name raises nothing on any path (5, 6 or 7),
is never stored in the baseline, is counted as `out_of_scope` in the pass
summary, and is named in `shared_ip_names` in the evidence of the owned hosts on
the same IP.

Credit budget per pass (Shodan): up to `_SHODAN_DNS_PAGES` (5) pages of
`/dns/domain` and `_SHODAN_SEARCH_PAGES` (3) pages of 100 matches of each host
search, so at most 8 query credits per owned domain and 3 per range. GTI ASM
keeps its 5 projects and 10 pages of 100.

Every committed pass is stored (`ctem_surface_passes`, `models/ctem_surface_pass.py`):
time, provider label, hosts observed, the coverage list, the statuses, the counts
per path (with `recorded`, `new_host_withheld` and `out_of_scope`) and the time of the pass it
compared with. An administrator reads the latest ones with
`GET /api/v1/ctem-signals/attack-surface/passes?limit=N` (`truncated` true when
a read hit its bound), so a pass that found nothing is still visible.

Repair: `POST /api/v1/ctem-signals/attack-surface/repair-new-hosts` with
`{"dry_run": false}` (administrators; `dry_run` true by default answers the
hosts) discards the open path 7 `new_host` signals of the outside lens that carry
no `previous_pass_at` (raised before this rule), with the verdict
`false_positive` and reason code `partial_previous_read`. The hosts stay in the
baseline; a signal that opened a case is left to the case step.

## 2ter. OpenAEV feeds (CTEM-27)

OpenAEV's own outside checks reach the same paths through its provisioned
triggers, not through the pass (`services/ctem_signal_listeners.py`):

| OpenAEV event | Path | How |
|---|---|---|
| `asset_stale` (Asset update filtered on `asset_status` = `STALE`) | 6 `stale_asset`, outside | OpenAEV's freshness job raises one update when an asset stays unseen past the tenant's `asset_stale_after_days`. |
| `scenario_score_degraded` on a reachability assertion (`scenario_reachability_assertion`) | 5 `new_exposure`, outside | A target that should not answer answers. The delivery does not carry the flag: the receiver reads the scenario once with the integration owner's credential (`ctem_openaev_events.stamp_reachability_assertion`) and writes `reachability_assertion` on the event and the evidence. |
| `scenario_score_degraded` on any other scenario, or the scenario could not be read | 8 `mitigation_regression`, inside | Unchanged. |

The evidence keys of the pass (section 2) are not written by these feeds: the
loop trace shows their evidence as raw lines.

## 3. Segregation

- Provider reads are system work: the credential is the one of the declared
  integration's owner (as the CTEM ticketing sync), listed in the credential
  gate (`tests/test_integration_credential_gate.py`). The token only reads the
  outside view of the owned domains and ranges.
- The signals are recorded by `system`: only an administrator reads them
  (`ctem-signals.md`, visibility). A user who is not an administrator sees no
  outside-lens signal.
- The owned domains, the ranges and the pass settings are profile fields: only
  the profile edit right changes them.

## 4. Failures

A provider read that fails records nothing and keeps the baseline; the next
window retries. A missing or unsupported integration is skipped with its
reason in the log; the pass runs on the other declared integrations, and is
skipped when none is left.
