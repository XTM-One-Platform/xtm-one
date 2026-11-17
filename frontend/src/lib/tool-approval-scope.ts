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
 * Where tool approvals are shown, and where they are changed.
 *
 * Dependency-free so `node --test` reads it (`tool-approval-scope.test.ts`).
 * `lib/hitl.ts` re-exports it for the screens.
 */

/** Settings > Access and security > Tool approvals: the platform's rules, for admins. */
export const PLATFORM_APPROVALS_PATH = "/settings/security/approvals";
/** Profile > Tool approvals: what asks the reader, and what they always allow. */
export const PERSONAL_APPROVALS_PATH = "/profile/approvals";

/**
 * Where a tool's approval is changed: the platform rules for an administrator,
 * the reader's own approvals for everyone else. `?tool=` lands on that tool.
 */
export function toolApprovalsHref(toolKey: string | undefined, isAdmin: boolean): string {
  const base = isAdmin ? PLATFORM_APPROVALS_PATH : PERSONAL_APPROVALS_PATH;
  return toolKey ? `${base}?tool=${encodeURIComponent(toolKey)}` : base;
}

/**
 * Whether Profile > Tool approvals has a row for this tool: it holds the tools
 * that ask first and the ones the reader always allows (so an approval can be
 * taken back once the tool stopped asking).
 */
export function onPersonalList(
  entry: { tool_key: string; gates: boolean },
  approved: { has(toolKey: string): boolean },
): boolean {
  return entry.gates || approved.has(entry.tool_key);
}

/**
 * Where a link about one tool's approval goes, or null when the reader has
 * nothing to change there: an administrator always reaches the platform rule,
 * anyone else only a tool on their own list - a tool that runs without asking
 * them would land on an empty, filtered list.
 */
export function toolApprovalLink(
  entry: { tool_key: string; gates: boolean },
  isAdmin: boolean,
  approved: { has(toolKey: string): boolean },
): string | null {
  return isAdmin || onPersonalList(entry, approved) ? toolApprovalsHref(entry.tool_key, isAdmin) : null;
}

/**
 * Whether an agent's tool calls go through the approval gate: its own
 * `approve_tool_calls`, else the install default. `undefined` while that
 * default is not known, so the caller shows `unknown` rather than a guess.
 */
export function effectiveApproval(
  own: boolean | null | undefined,
  installDefault: boolean | undefined,
): boolean | undefined {
  return own ?? installDefault;
}

/** The part of `GET /tool-approvals` that says whether anything asks. */
interface ApprovalSwitches {
  tool_approvals_enabled?: boolean;
  approve_tool_calls_default?: boolean;
  approvals_in_use?: boolean;
}

/**
 * Whether a screen shows an approval state ("Asks first", "Runs without
 * asking") at all.
 *
 * For the platform: only while approvals are in use — an enabled agent or the
 * General Assistant asks, and approvals are not suspended. For one agent (its
 * own `approve_tool_calls` given): only while that agent asks, which a
 * disabled agent never does. A tool's place on the allowed list says what
 * would happen IF an agent asked; shown while nothing asks, it made an opt-in
 * feature look switched on for everyone.
 * False while the answer is not known, so nothing flashes in.
 */
export function approvalsShown(
  switches: ApprovalSwitches | undefined,
  agent?: { approveToolCalls: boolean | null | undefined; enabled?: boolean },
): boolean {
  if (!switches) return false;
  if (agent === undefined) return switches.approvals_in_use === true;
  if (switches.tool_approvals_enabled === false || agent.enabled === false) return false;
  return effectiveApproval(agent.approveToolCalls, switches.approve_tool_calls_default) === true;
}
