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
 * The General Assistant's skill choice: the company-managed
 * skills it can carry, the built-in ones it carries while no choice is made
 * (`skill_ids: null`, the skills tagged `slug:<one of default_skill_slugs>`),
 * and the comparisons the Configure panel's Save and the chip row use.
 */
import { useSkills } from "@/hooks/api/useSkillApi";
import type { AgentSkill, GeneralAssistantConfig } from "@/types";

export interface GeneralAssistantSkillCatalog {
  /** The company-managed, enabled skills an admin can attach. */
  offered: AgentSkill[];
  /** The ids of the built-in skills it carries while no choice is made. */
  defaults: string[];
  isLoading: boolean;
}

/** The skills offered to the General Assistant and its built-in ones. */
export function useGeneralAssistantSkillCatalog(gaConfig: GeneralAssistantConfig | null | undefined): GeneralAssistantSkillCatalog {
  const { data: skills, isLoading } = useSkills();
  const offered = (skills ?? []).filter((skill) => skill.is_company_managed && skill.enabled);
  const builtIn = new Set((gaConfig?.default_skill_slugs ?? []).map((slug) => `slug:${slug}`));
  const defaults = offered
    .filter((skill) => skill.tags.includes("builtin") && skill.tags.some((tag) => builtIn.has(tag)))
    .map((skill) => skill.id);
  return { offered, defaults, isLoading };
}

/** The skills carried for a choice: the admin's, or the built-in ones while it is `null`. */
export function carriedSkillIds(choice: string[] | null | undefined, defaults: string[]): string[] {
  return choice ?? defaults;
}

/** Whether two choices are the same (order aside); `null` only equals `null`. */
export function sameSkillChoice(a: string[] | null | undefined, b: string[] | null | undefined): boolean {
  if (a == null || b == null) return (a ?? null) === (b ?? null);
  return a.length === b.length && a.every((id) => b.includes(id));
}
