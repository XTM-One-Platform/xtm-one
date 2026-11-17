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

import { useSkills } from "@/hooks/api/useSkillApi";
import { useFormatter } from "@/i18n";
import { PLACEBO_TARGET_LABELS } from "@/lib/placebo";
import type { PlaceboTarget } from "@/types";

/** A target's label, a single skill by its name: `Persona`, `Skill · Triage`. */
export function usePlaceboTargetLabel(target: PlaceboTarget | null | undefined): string {
  const { t_i18n } = useFormatter();
  const { data: skills = [] } = useSkills({ scope: "all" });
  if (!target) return "";
  if (target.kind !== "skill") return t_i18n(PLACEBO_TARGET_LABELS[target.kind] ?? target.kind);
  const name = skills.find((skill) => skill.id === target.skill_id)?.name ?? target.skill_id ?? "";
  return `${t_i18n(PLACEBO_TARGET_LABELS.skill)} · ${name}`;
}
