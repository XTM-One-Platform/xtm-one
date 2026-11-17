// SPDX-License-Identifier: Apache-2.0
/**
 * The tactic's readable name from its kill chain phase name (initial-access: Initial access).
 * ATT&CK tactic names are proper names, shown as the data carries them; the order of
 * the rows is the kill chain order the backend reads from OpenCTI (no tactic list here).
 */
export function tacticLabel(phase: string): string {
  const words = phase.replace(/-/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : phase;
}
