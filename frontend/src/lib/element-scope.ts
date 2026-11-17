/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * Whether a Scope(s) selection is a boundary or guidance,
 * mirrored from `backend/app/services/element_scope.py`:
 *
 * - a selection is enforced on the agent's tools only when its element type
 *   can be enforced (`ENFORCEABLE_ELEMENT_SCOPES`) and is listed in the
 *   entity's `integration_element_enforcement` - the "Enforce as a boundary"
 *   switch next to it. Everything else is prompt guidance, the default;
 * - a flag counts only while its type is selected, and the saved form keeps
 *   only those flags (`normalizeScopeEnforcement`);
 * - on an assignment the stricter of the agent and the assignment wins: a type
 *   the agent enforces is a boundary for every assignment of that agent.
 *
 * Kept free of `@/` imports so `node --test` can load it.
 */
import type { IntegrationElement } from "@/types";

/**
 * Element-binding keys a provider can hold to a selection. Keep in step with
 * the provider guards' `*_SCOPE_KEY` constants under
 * `backend/app/integrations/providers` (a backend test pins the two together).
 */
export const ENFORCEABLE_ELEMENT_SCOPES: ReadonlySet<string> = new Set(["github:repository", "slack:slack_channel", "microsoft:teams_channel", "hubspot:crm_object_type"]);

type ElementBindings = Record<string, IntegrationElement[]> | null | undefined;
type ScopeEnforcement = readonly string[] | null | undefined;

export function isElementScopeEnforceable(scopeKey: string): boolean {
  return ENFORCEABLE_ELEMENT_SCOPES.has(scopeKey);
}

/** Whether `bindings` selects at least one element of `scopeKey`. */
export function hasScopeSelection(bindings: ElementBindings, scopeKey: string): boolean {
  return (bindings?.[scopeKey]?.length ?? 0) > 0;
}

/** Whether `enforcement` makes the selection of `scopeKey` in `bindings` a boundary. */
export function isScopeEnforced(scopeKey: string, bindings: ElementBindings, enforcement: ScopeEnforcement): boolean {
  return isElementScopeEnforceable(scopeKey) && hasScopeSelection(bindings, scopeKey) && (enforcement ?? []).includes(scopeKey);
}

/** The saved form of a choice: the flags of selected, enforceable types, sorted; `null` when none. */
export function normalizeScopeEnforcement(enforcement: ScopeEnforcement, bindings: ElementBindings): string[] | null {
  const kept = [...new Set(enforcement ?? [])].filter((scopeKey) => isScopeEnforced(scopeKey, bindings, enforcement)).sort();
  return kept.length > 0 ? kept : null;
}

/** `enforcement` with `scopeKey` switched on or off. */
export function withScopeEnforcement(enforcement: ScopeEnforcement, scopeKey: string, enforced: boolean): string[] {
  const rest = (enforcement ?? []).filter((key) => key !== scopeKey);
  return enforced ? [...rest, scopeKey] : rest;
}
