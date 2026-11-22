// SPDX-License-Identifier: Apache-2.0

/**
 * The validation of a case in words (CTEM-12-UI): the validation references
 * the case holds (`ctem_case_loop.record_validation`: provider, result, and
 * since CTEM-12 `safe_mode` and `run_order`) and gate A
 * (`ctem.validation.non_readonly`, CTEM-34), read from the case's own `gate`
 * events. Pure: the case page draws these, nothing here reads the network.
 */
import type { CtemCaseSummary } from "@/hooks/api/useCtemCasesApi";

/** Gate A: an OpenAEV inject that is not read-only (`builtin_fleet_docs` CTEM catalog). */
export const VALIDATION_GATE = "ctem.validation.non_readonly";

/** The work item a waiting gate A opens (the key the CTEM orchestrator personas give it). */
export function validationDecisionKey(caseId: string): string {
  return `ctem:validation:${caseId}`;
}

type Validation = NonNullable<CtemCaseSummary["validations"]>[number];

/** How a validation ran, in words. @i18n-keys */
export const SAFE_MODE_LABEL = {
  safe: "Safe mode: read-only checks only",
  unsafe: "Not read-only: a test that changes the target ran",
} as const;

/** How many validation references the card lists, newest first. */
const VALIDATIONS_SHOWN = 3;

/** The newest validation references, newest first (the store keeps them newest last). */
export function latestValidations(validations: readonly Validation[] | undefined): Validation[] {
  return [...(validations ?? [])].reverse().slice(0, VALIDATIONS_SHOWN);
}

/** The safe-mode word of one reference, or null when the module did not say. */
export function safeModeOf(v: Pick<Validation, "safe_mode">): keyof typeof SAFE_MODE_LABEL | null {
  if (v.safe_mode === true) return "safe";
  if (v.safe_mode === false) return "unsafe";
  return null;
}

/**
 * Why gate A passed automatically: "safe mode" when the newest reference ran
 * read-only (nothing could change the target, so nothing waits), else the
 * fleet's level or pin. Null when the gate did not pass automatically.
 */
export function autoPassReason(
  outcome: string | null | undefined,
  newest: Pick<Validation, "safe_mode"> | undefined,
): "safe_mode" | "fleet_runs" | null {
  if (outcome !== "passed_automatically") return null;
  return newest?.safe_mode === true ? "safe_mode" : "fleet_runs";
}
