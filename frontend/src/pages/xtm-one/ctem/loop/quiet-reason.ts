// SPDX-License-Identifier: Apache-2.0

/**
 * Why an empty outside path raised nothing (#2634): the attack-surface pass sends a reason code and
 * its data (`quiet_reason` of `GET /ctem-signals/paths`, `ctem_attack_surface.quiet_reasons`); this
 * words it in the reader's language, the pass date in the locale format (UX round 1 on #2654, B1, B2).
 */
import type { CtemSignalQuietReason } from "@/hooks/api/useCtemSignalsApi";

type Translate = (key: string, values?: Record<string, string | number>) => string;

/** The sentence of each code; a code this build does not know reads as none (no raw code on screen). */
const QUIET_REASON_TEXT: Record<string, string> = {
  resync_queued: "Pass queued after the resync: it runs once every other source has replayed",
  pass_off: "No pass has run yet: the attack surface pass is turned off (Attack surface, in the organization profile)",
  no_scope: "No pass has run yet: the organization profile declares no owned domains and no address ranges",
  no_provider: "No pass has run yet: the organization profile declares no attack surface provider",
  provider_unreadable: "No pass has run yet: the declared attack surface provider cannot be read",
  no_pass_completed: "No pass has completed yet: a pass whose provider read fails records nothing and is retried later",
  empty_read: "The last pass ({at}) read no host: the provider answered nothing for {scopes}",
  rearmed:
    "{count, plural, one {# host re-armed by the signal purge waits for the next pass that sees it (last pass {at})} other {# hosts re-armed by the signal purge wait for the next pass that sees them (last pass {at})}}",
  baseline: "The first pass ({at}) set the baseline: it raises no drift",
  withheld:
    "{count, plural, one {The last pass ({at}) withheld # new host: a partial read, or known to the provider before the previous pass} other {The last pass ({at}) withheld # new hosts: a partial read, or known to the provider before the previous pass}}",
  no_undeclared_host: "The last pass ({at}) found no undeclared host and no new port",
  nothing_new: "The last pass ({at}) found nothing new on this path",
};

const EMPTY_SCOPES_TEXT = "{reason}; the provider answered nothing for {scopes}";

/** The reason in words, or undefined when there is none or its code is unknown. */
export function quietReasonText(
  reason: CtemSignalQuietReason | undefined | null,
  t_i18n: Translate,
  formatDate: (iso: string) => string,
): string | undefined {
  const text = reason ? QUIET_REASON_TEXT[reason.code] : undefined;
  if (!reason || !text) return undefined;
  const values = {
    at: reason.at ? formatDate(reason.at) : "",
    count: reason.count ?? 0,
    scopes: (reason.scopes ?? []).join(", "),
  };
  const sentence = t_i18n(text, values);
  const empty = reason.empty_scopes ?? [];
  return empty.length ? t_i18n(EMPTY_SCOPES_TEXT, { reason: sentence, scopes: empty.join(", ") }) : sentence;
}
