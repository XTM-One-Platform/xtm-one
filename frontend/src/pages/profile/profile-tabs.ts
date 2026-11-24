// SPDX-License-Identifier: Apache-2.0

/**
 * The tabs of the profile page, `/profile/<tab>`. A segment that names no tab
 * (the former `runners`, moved to `/desktop`, included) is not found: no
 * redirect, no empty profile (#797, #822).
 */

export type ProfileTab =
  | "overview"
  | "approvals"
  | "channels"
  | "phones"
  | "agent-accounts"
  | "memory"
  | "quotas"
  | "api-keys"
  | "mcp";

const PROFILE_TABS: readonly ProfileTab[] = [
  "overview", "approvals", "channels", "phones", "agent-accounts", "memory", "quotas", "api-keys", "mcp",
];

/** Whether a `/profile/:tab` segment is a tab; bare `/profile` is (the overview). */
export function isProfileTab(segment: string | undefined): boolean {
  return segment === undefined || (PROFILE_TABS as readonly string[]).includes(segment);
}
