// SPDX-License-Identifier: Apache-2.0

import type { AvailableDecisionModels, DecisionListingStatus, LLMProvider } from "@/types";

/**
 * Why a provider that lists its decision models offered none. Not
 * "empty": most OpenAI-compatible providers serve none, so it is no news. @i18n-keys
 */
const DECISION_LISTING_NOTES = {
  incomplete: "{provider} did not finish checking which of its models serve decisions: open the picker again in a moment, and if this persists, the server log has the cause.",
  failed: "Listing the decision models of {provider} failed: the server log has the cause.",
  unavailable: "{provider} is not available to you, so it is not asked for its decision models.",
} as const satisfies Record<Exclude<DecisionListingStatus, "listed" | "empty">, string>;

/** Each provider whose decision listing offered no model, with why. */
export function decisionListingNotes(providers: LLMProvider[] | null | undefined, listing: AvailableDecisionModels | undefined) {
  const status = new Map(listing?.listings.map((l) => [l.provider_id, l.status]));
  return (providers ?? []).flatMap((p) => {
    const s = status.get(p.id);
    return s && s !== "listed" && s !== "empty" ? [{ provider: p, note: DECISION_LISTING_NOTES[s] }] : [];
  });
}
