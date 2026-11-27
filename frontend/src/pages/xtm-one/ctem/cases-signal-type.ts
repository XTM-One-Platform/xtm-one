// SPDX-License-Identifier: Apache-2.0

/**
 * The Signal type filter of the CTEM cases list (#2556): the trigger paths of
 * the signals behind a case (`signal_types` of a list row, the 12 paths of
 * `app/models/ctem_signal.py` TRIGGER_PATHS), and "No signal" for a case whose
 * reader sees no signal of it (a split or a legacy case), so those stay reachable.
 */

/** The trigger paths, in the order of the CTEM trigger model (path 1 first). */
export const CASE_SIGNAL_TYPES = [
  "new_vulnerability",
  "relevant_threat",
  "code_flaw",
  "proven_exploitable",
  "new_exposure",
  "stale_asset",
  "surface_drift",
  "mitigation_regression",
  "control_decay",
  "credential_leak",
  "third_party_event",
  "blind_spot",
] as const;

type CaseSignalType = (typeof CASE_SIGNAL_TYPES)[number];

/** The filter value of a case with no visible signal (the API's `signal_type=none`). */
export const NO_SIGNAL = "none";

/** Filter option labels, the ones `ctem_signals.PATH_CATALOG` names. @i18n-keys */
const CASE_SIGNAL_TYPE_LABEL = {
  new_vulnerability: "New vulnerability on a running version",
  relevant_threat: "Relevant new threat or actor",
  code_flaw: "Code or dependency flaw before production",
  proven_exploitable: "Proven exploitable to a primary asset",
  new_exposure: "New exposure",
  stale_asset: "Stale or orphaned asset",
  surface_drift: "Attack-surface drift",
  mitigation_regression: "Mitigation regression",
  control_decay: "Control or evidence decay",
  credential_leak: "Credential or secret leak",
  third_party_event: "Third-party or sub-processor event",
  blind_spot: "Blind spot or coverage gap",
} as const satisfies Record<CaseSignalType, string>;

/** The Signal type values of a case: its trigger paths, or "none" when its reader sees no signal. */
export function caseSignalTypes(row: { signal_types?: readonly string[] | null }): string[] {
  const types = row.signal_types ?? [];
  return types.length > 0 ? [...types] : [NO_SIGNAL];
}

/**
 * The options of the filter: the known paths in the trigger model's order, a path
 * the server names that this build does not know (a newer backend) by the server's
 * label, then "No signal".
 */
export function caseSignalTypeOptions(
  translate: (key: string) => string,
  serverLabels: Record<string, string> | undefined,
  noSignalLabel: string,
): { value: string; label: string }[] {
  const known = CASE_SIGNAL_TYPES.map((path) => ({ value: path, label: translate(CASE_SIGNAL_TYPE_LABEL[path]) }));
  const extra = Object.entries(serverLabels ?? {})
    .filter(([path]) => !(CASE_SIGNAL_TYPES as readonly string[]).includes(path))
    .map(([value, label]) => ({ value, label }));
  return [...known, ...extra, { value: NO_SIGNAL, label: noSignalLabel }];
}
