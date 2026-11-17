// SPDX-License-Identifier: Apache-2.0

// D3-3 of the CTEM open-source program (#9459, `dev-docs/ctem-open-source.md`
// section 7): the rule-based "what to do next" of the Recommended actions card.
// When no briefing authored the next steps (Community Edition, AI off, no
// model, or before the first briefing), these rows are the deterministic
// equivalent, read from the posture already collected. No model, no endpoint.

import type { CtemPosture } from "@/types";

/** One deterministic next action, before the card gives it an icon and a CTA. */
export type NextAction =
  /** A stage whose source product is connected, yet no fact reaches it. */
  | { kind: "stage_no_data"; stage: string; label: string; products: string[] }
  /** An OpenAEV expectation type with failed or partial results. */
  | { kind: "expectation"; type: string; failed: number };

/** The deterministic next actions of a posture, most urgent first: failed
 *  expectations by count, then the stages with a connected source and no data
 *  in lifecycle order. Missing products are not here: the blind spot rows
 *  ("Connect {name}") already carry them. */
export function deterministicNextActions(
  posture: Pick<CtemPosture, "stages" | "products"> | null | undefined,
): NextAction[] {
  if (!posture) return [];
  const actions: NextAction[] = [];

  // Expectations are read only while OpenAEV is connected: a stored snapshot
  // must never show real failures next to a Connect action.
  if (posture.products.openaev?.present) {
    const buckets = posture.stages.find((s) => s.key === "validation")?.viz.expectation_buckets ?? {};
    const failures = Object.entries(buckets)
      .map(([type, b]) => ({ type, failed: (b?.FAILED ?? 0) + (b?.PARTIAL ?? 0) }))
      .filter((f) => f.failed > 0)
      .sort((a, b) => b.failed - a.failed || a.type.localeCompare(b.type));
    for (const f of failures) actions.push({ kind: "expectation", ...f });
  }

  for (const stage of posture.stages) {
    if (stage.covered || stage.products_present.length === 0) continue;
    actions.push({ kind: "stage_no_data", stage: stage.key, label: stage.label, products: stage.products_present });
  }
  return actions;
}

/** Card priorities of the rule-based rows (#9459). The Recommended actions card
 *  sorts every row by priority and keeps six: these rows rank under the KEV row
 *  (95) and above the detection coverage gap (80), the failed expectations
 *  first. Row k of a kind sits at base - 4k/(k+1): always decreasing, never 4
 *  or more under its base, so no number of rows of one kind reaches the next
 *  band. */
export const NEXT_ACTION_PRIORITY = { expectation: 90, stage_no_data: 85 } as const;

/** The actions with their card priority, in the order given. */
export function rankNextActions(actions: NextAction[]): Array<NextAction & { priority: number }> {
  const seen = { expectation: 0, stage_no_data: 0 };
  return actions.map((a) => {
    const k = seen[a.kind]++;
    return { ...a, priority: NEXT_ACTION_PRIORITY[a.kind] - (4 * k) / (k + 1) };
  });
}

/** The reason line of a failed expectation type: what failed and what to do.
 *  An unknown (future) OpenAEV type gets the generic sentence. */
export const EXPECTATION_REASONS: Record<string, string> = {
  PREVENTION: "Attacks the defenses did not block - tune prevention controls",
  DETECTION: "Attacks no tool detected - tune detection rules",
  VULNERABILITY: "Assets found vulnerable in simulations - patch or mitigate",
  HUMAN_RESPONSE: "Simulated attacks the team did not report - train and rehearse",
};
export const EXPECTATION_REASON_FALLBACK = "Simulated attacks with failed expectations - review the results";
