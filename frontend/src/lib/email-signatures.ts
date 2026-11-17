// SPDX-License-Identifier: Apache-2.0

/**
 * Email signature settings: an integration's own setting, and an
 * agent's override per binding (`Agent.integration_signatures`).
 */
import type { AgentEmailSignatures, IntegrationSignatureOverride, SignatureMode } from "@/types";

/** The select value of "no signature integration chosen": picked automatically, or inherited. */
export const NO_SIGNATURE_INTEGRATION_CHOSEN = "__none__";

export const SIGNATURE_MODES = ["inherit", "on", "off"] as const satisfies readonly SignatureMode[];

/**
 * The signature integrations a picker offers: those the viewer may choose, and
 * the one already chosen when they may not (named as the server resolved it),
 * so the picker still shows what is set.
 */
export function signatureOptions(
  available: AgentEmailSignatures["signature_integrations"],
  chosenId: string | null,
  chosenName: string | null,
  unavailableLabel: string,
): { value: string; label: string }[] {
  const options = available.map((option) => ({ value: option.id, label: option.name }));
  if (chosenId && !available.some((option) => option.id === chosenId)) {
    options.push({ value: chosenId, label: chosenName ?? unavailableLabel });
  }
  return options;
}

/**
 * The agent's overrides after one binding changes. The other entries are kept
 * as they are (including bindings the viewer does not see); an entry that only
 * inherits is dropped, and no entry left is `null`.
 */
export function nextIntegrationSignatures(
  current: Record<string, IntegrationSignatureOverride> | null | undefined,
  integrationId: string,
  change: { signature_mode?: SignatureMode; signature_integration_id?: string | null },
): Record<string, IntegrationSignatureOverride> | null {
  const next: Record<string, IntegrationSignatureOverride> = { ...(current ?? {}) };
  const before = next[integrationId];
  const mode = change.signature_mode ?? before?.signature_mode ?? "inherit";
  const chosen =
    change.signature_integration_id === undefined
      ? before?.signature_integration_id
      : change.signature_integration_id ?? undefined;
  if (mode === "inherit" && !chosen) {
    delete next[integrationId];
  } else {
    next[integrationId] = chosen ? { signature_mode: mode, signature_integration_id: chosen } : { signature_mode: mode };
  }
  return Object.keys(next).length > 0 ? next : null;
}
