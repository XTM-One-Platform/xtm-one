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

/** Helpers for resources a packaged skill carries onto an agent.
 *
 * A skill package's `scripts/` become Custom Tools and its `references/` become
 * Knowledge Base documents at import time. The backend attaches those entities
 * to any agent the skill is attached to, and detaches them with it — see
 * `backend/app/services/skill_package_bindings.py`. These helpers let the agent
 * config surface them as skill-owned instead of letting them appear (and
 * disappear) unexplained.
 */

import type { SkillPackageBindings } from "@/types";

/** The `t_i18n` of `useFormatter()` — passed in because these are plain
 *  functions, not hooks, and the agent Configuration tab is translated. */
type Translate = (id: string, values?: Record<string, string | number>) => string;

type PackageResourceOwners = {
  /** KB id → names of the attached skills that carry it. */
  knowledgeBases: Record<string, string[]>;
  /** Custom tool id → names of the attached skills that carry it. */
  customTools: Record<string, string[]>;
};

function index(
  bindings: SkillPackageBindings[] | undefined,
  keep: (skillId: string) => boolean,
): PackageResourceOwners {
  const owners: PackageResourceOwners = { knowledgeBases: {}, customTools: {} };
  for (const entry of bindings ?? []) {
    if (!keep(entry.skill_id)) continue;
    for (const id of entry.knowledge_base_ids) {
      (owners.knowledgeBases[id] ??= []).push(entry.skill_name);
    }
    for (const id of entry.custom_tool_ids) {
      (owners.customTools[id] ??= []).push(entry.skill_name);
    }
  }
  return owners;
}

/** Index the entities carried by the skills currently attached to an agent. */
export function packageResourceOwners(
  bindings: SkillPackageBindings[] | undefined,
  attachedSkillIds: string[] | null | undefined,
): PackageResourceOwners {
  const attached = new Set(attachedSkillIds ?? []);
  return index(bindings, (skillId) => attached.has(skillId));
}

/** Index every packaged entity and the skills that carry it, attached or not.
 *
 * Such an entity cannot be attached to an agent by hand — the backend unbinds
 * anything no attached skill carries — so the UI points at the skill instead.
 */
export function packageResourceCarriers(
  bindings: SkillPackageBindings[] | undefined,
): PackageResourceOwners {
  return index(bindings, () => true);
}

/** "1 KB · 2 tools" — what a skill brings along, or null when it brings nothing. */
export function packageResourceSummary(
  entry: SkillPackageBindings | undefined,
  t: Translate,
): string | null {
  if (!entry) return null;
  const parts: string[] = [];
  const kbCount = entry.knowledge_base_ids.length;
  const toolCount = entry.custom_tool_ids.length;
  if (kbCount) parts.push(t("{count} KB", { count: kbCount }));
  if (toolCount) {
    parts.push(
      toolCount > 1 ? t("{count} tools", { count: toolCount }) : t("{count} tool", { count: toolCount }),
    );
  }
  return parts.length ? parts.join(" · ") : null;
}

/** Tooltip text explaining why a binding is present and not removable by hand. */
export function packageOwnerHint(
  skillNames: string[] | undefined,
  t: Translate,
): string | undefined {
  if (!skillNames?.length) return undefined;
  return t(
    "Attached with the skill {skills}: it ships this resource in its package. Detach the skill to remove it.",
    { skills: skillNames.join(", ") },
  );
}

/** Tooltip text for a packaged resource that no attached skill carries.
 *
 * Worded to hold whether or not the resource is currently bound: an agent keeps
 * it exactly while the carrying skill is attached, so a bound one without its
 * skill is on its way out and an unbound one cannot be added by hand.
 */
export function packageCarrierHint(
  skillNames: string[] | undefined,
  t: Translate,
): string | undefined {
  if (!skillNames?.length) return undefined;
  return t(
    "Ships with the skill {skills}: this agent keeps it only while that skill is attached. Attach or detach the skill, not the resource.",
    { skills: skillNames.join(", ") },
  );
}
