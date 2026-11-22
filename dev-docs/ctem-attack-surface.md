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
| 7 `surface_drift` | A host neither declared nor seen by an earlier pass (the reconcile leaves it `to_validate`); a new port on an undeclared host. |

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
| Shodan (`shodan`) | `ShodanSurfaceReader` | `GET /dns/domain/{domain}` (records and listed subdomains), `GET /shodan/host/search` on `hostname:` and `net:` (banners: address, port, transport, product, certificate CN) | One query credit per domain and per range. |
| Google Threat Intelligence ASM (`gti`) | `GtiAsmSurfaceReader` | `GET /asm/projects`, then per project `GET /asm/search/entities/type:Intrigue::Entity::IpAddress` (in-scope IP entities: `summary.ports.tcp/udp`, `summary.resolutions`), kept when a resolution is under an owned domain or the address is in an owned range | Up to 5 projects and 10 pages of 100 per pass. ASM lists no unresolved name: path 6 "resolves to nothing" comes from Shodan only. Needs the ASM module on the license. |
| SecurityScorecard | not supported | | Scores and findings per issue type, no host inventory with open ports. |
| SOCRadar | not supported | | The ASM digital asset list (`GET /company/{id}/asm`) carries names, source and monitoring state, no address and no open ports. |
| DomainTools | not supported | | DNS and WHOIS history, no open ports; the registrar's view, not the reachable surface. |
| Rapid7 | not supported | | InsightVM assets are the inside scan of managed assets, not the outside view. |

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
