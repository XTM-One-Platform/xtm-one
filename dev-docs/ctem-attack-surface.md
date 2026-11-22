# CTEM outside lens: the attack surface pass (CTEM-26)

The first version of the outside lens. It reads what the Internet reaches of the
organization through an attack-surface provider XTM One already connects, and
records each difference as a CTEM signal. The spine (reconcile, scope gate,
cases: `ctem-signals.md`, `ctem-scope-gate.md`) decides what each signal becomes.
No model call and no human gate on this path. The native OpenAEV capability
(asset freshness and reachability assertions) is CTEM-27 and will feed the same
paths.

## 1. Profile

| Field | Meaning |
|---|---|
| `owned_domains` | The DNS and email domains the organization owns (shared with CTEM-29). |
| `address_ranges` | The public address ranges it owns, as CIDR blocks (normalised; a bare address is its /32 or /128; anything else is refused at save). |
| `attack_surface.enabled` | Opt-in, off by default. |
| `attack_surface.integration` | The id of the provider integration the pass reads (Shodan today). |
| `attack_surface.cadence_hours` | Hours between two passes (24 by default). |
| `attack_surface.stale_after_days` | A host unseen for this many days is a stale asset (14 by default). |

These are exposure fields: read by the pass, never dumped into the always-on
profile block, not contract fields. Per-asset expectations stay out of the
profile: `assets_exposure[].internet_facing` is the only declaration;
reachability assertions belong to OpenAEV (CTEM-27).

## 2. The pass

`services/ctem_attack_surface.py`, called by the autonomous tick after the case
sweep, at most once per cadence window (Redis slot `ctem:attack-surface:pass`). The slot is kept only by
a pass that committed: a skipped pass (no credential yet) or a failed provider read gives it back, so the
next tick retries.

1. Read: the provider reader (`ShodanSurfaceReader`) lists the DNS records of
   each owned domain and the banners of each owned domain and range, one
   observation per public host (address, ports that answer, certificate names,
   DNS target, whether the name resolves).
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
window retries. A missing or unsupported integration skips the pass with its
reason in the log.
