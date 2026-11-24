// SPDX-License-Identifier: Apache-2.0

/**
 * The validation of a case in words (CTEM-12-UI): the validation references
 * the case holds (`ctem_case_loop.record_validation`: provider, result,
 * `run_order`) and gate A (`ctem.validation.non_readonly`, CTEM-34), read from
 * the case's own `gate` events. The platform never classifies an inject
 * (#1810): an old reference that carries `safe_mode` is stored as written and
 * not rendered. Pure: the case page draws these, nothing here reads the network.
 */
import type { CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";

/** Gate A: the decision to launch an OpenAEV validation (`builtin_fleet_docs` CTEM catalog). */
export const VALIDATION_GATE = "ctem.validation.non_readonly";

/** The work item a waiting gate A opens (the key the CTEM orchestrator personas give it). */
export function validationDecisionKey(caseId: string): string {
  return `ctem:validation:${caseId}`;
}

type Validation = NonNullable<CtemCaseSummary["validations"]>[number];

/** How many validation references the card lists, newest first. */
const VALIDATIONS_SHOWN = 3;

/** The newest validation references, newest first (the store keeps them newest last). */
export function latestValidations(validations: readonly Validation[] | undefined): Validation[] {
  return [...(validations ?? [])].reverse().slice(0, VALIDATIONS_SHOWN);
}

/** The part of a decision work item the chosen label is read from. */
export interface AnsweredDecision {
  payload?: Record<string, unknown> | null;
  ask?: { options?: readonly { key: string; label: string }[] | null } | null;
}

function sameKey(a: unknown, key: string): boolean {
  return typeof a === "string" && a.toLowerCase() === key.toLowerCase();
}

/**
 * The label of the option a person chose on gate A (UX rounds 1 and 2 B1:
 * never the key alone). An answered item has no ask any more: the answer
 * endpoint clears it and stores the choice in `payload.answer`
 * ({option, label}), so that is read first; the ask's options are a second
 * source for an item still carrying them. Null when the item or the option is
 * not visible to the viewer: the card then names the key.
 */
export function chosenOptionLabel(
  item: AnsweredDecision | null | undefined,
  key: string | null | undefined,
): string | null {
  if (!key || !item) return null;
  const answer = item.payload?.answer as { option?: unknown; label?: unknown } | undefined;
  if (answer && sameKey(answer.option, key) && typeof answer.label === "string" && answer.label.trim()) {
    return answer.label.trim();
  }
  const label = item.ask?.options?.find((o) => sameKey(o.key, key))?.label?.trim();
  return label || null;
}

/**
 * Why gate A passed automatically: the fleet's level or pin runs it without a
 * person (#1810: never a class of the inject). Null when it did not pass
 * automatically.
 */
export function autoPassReason(outcome: string | null | undefined): "fleet_runs" | null {
  return outcome === "passed_automatically" ? "fleet_runs" : null;
}
