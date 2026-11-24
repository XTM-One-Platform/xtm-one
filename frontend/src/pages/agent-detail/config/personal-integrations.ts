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

import type { Integration } from "@/types";

/** The owner's choice stored on the agent (`Agent.integration_lending`). */
export interface IntegrationLending {
  integration_ids: string[];
  set_by: string;
  set_at: string;
}

/**
 * The agent's personal integrations its people would not get otherwise:
 * bound by the agent, not company-managed and shared with none of the
 * agent's groups or people (one shared with them is already theirs). An agent
 * shared with no group and no person has none.
 */
export function unsharedPersonalIntegrations(
  bindings: Record<string, string[]> | null,
  integrations: Integration[] | undefined,
  sharedGroupIds: string[] | null,
  sharedUserIds: string[] | null = null,
): Integration[] {
  if (!isShared(sharedGroupIds, sharedUserIds) || !bindings || !integrations) return [];
  const groups = sharedGroupIds ?? [];
  const people = sharedUserIds ?? [];
  const boundIds = new Set(Object.values(bindings).flat());
  return integrations.filter(
    (integ) =>
      boundIds.has(integ.id) &&
      !integ.is_company_managed &&
      !(integ.shared_group_ids ?? []).some((gid) => groups.includes(gid)) &&
      !(integ.shared_user_ids ?? []).some((uid) => people.includes(uid)),
  );
}

function isShared(sharedGroupIds: string[] | null, sharedUserIds: string[] | null): boolean {
  return !!sharedGroupIds?.length || !!sharedUserIds?.length;
}

/** What the banner needs of a personal integration. */
export type PersonalIntegration = Pick<Integration, "id" | "name" | "provider">;

/**
 * The banner's integrations: the agent's own answer
 * (`Agent.shared_personal_integrations`, sent to anyone who may edit it) when
 * present, since an editor who is not the owner cannot read the owner's
 * personal rows; else computed from the rows the viewer can read.
 */
export function bannerPersonalIntegrations(
  bindings: Record<string, string[]> | null,
  integrations: Integration[] | undefined,
  sharedGroupIds: string[] | null,
  sharedUserIds: string[] | null,
  fromAgent: PersonalIntegration[] | null | undefined,
): PersonalIntegration[] {
  if (!isShared(sharedGroupIds, sharedUserIds)) return [];
  if (fromAgent) return fromAgent;
  return unsharedPersonalIntegrations(bindings, integrations, sharedGroupIds, sharedUserIds);
}

/**
 * Lent when the owner chose to lend every one of them: binding a new
 * personal integration brings the full banner back until the owner chooses
 * again (the backend drops an unbound id from the lending by itself).
 */
export function lendsAll(lending: IntegrationLending | null | undefined, ids: string[]): boolean {
  if (!lending?.integration_ids?.length || ids.length === 0) return false;
  const lent = new Set(lending.integration_ids);
  return ids.every((id) => lent.has(id));
}
