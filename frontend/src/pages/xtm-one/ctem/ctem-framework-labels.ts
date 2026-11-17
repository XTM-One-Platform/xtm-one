// SPDX-License-Identifier: Apache-2.0
/**
 * Display labels of the kill chain names the data carries (dev-docs/ctem-stage-viz.md
 * section 8.1). Labels ONLY: no phase, no technique id and no framework list
 * lives here, every chain and its phases come from the data. An unknown name is
 * shown as written. This is the one exemption of the no-ATT&CK-constant test.
 */
const LABELS: Record<string, string> = {
  "mitre-attack": "Enterprise ATT&CK",
  "mitre-mobile-attack": "Mobile ATT&CK",
  "mitre-ics-attack": "ICS ATT&CK",
  "mitre-atlas": "ATLAS",
  disarm: "DISARM",
};

/** The readable label of a kill chain name; the name itself when unknown, "Other" when empty. */
export function frameworkLabel(name: string): string {
  if (!name) return "Other";
  return LABELS[name.toLowerCase()] ?? name;
}
