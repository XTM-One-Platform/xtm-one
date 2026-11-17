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
 * The "Duplicate skill" dialog's pure parts (`ForkSkillDialog`, #9174): the
 * form, the request it sends to `POST /skills/{id}/fork`, what a refusal says,
 * the toast after a fork and the provenance line a forked skill shows. Kept
 * out of the component so they are tested without rendering.
 */
import type { SkillForkRequest, SkillForkResult } from "@/hooks/api/useSkillApi";
import type { AgentSkill } from "@/types";

export interface ForkSkillForm {
  name: string;
  /** `null`: no fleet is re-bound, the copy is used by nobody yet. */
  repointFleetId: string | null;
}

type Translate = (text: string, values?: Record<string, string | number>) => string;

/** The copy's default name, in the reader's language ("Copie de" in fr, never the English "Copy of"). */
export function defaultForkSkillName(source: Pick<AgentSkill, "name">, t: Translate): string {
  return t("Copy of {name}", { name: source.name });
}

export function initialForkSkillForm(source: Pick<AgentSkill, "name">, t: Translate): ForkSkillForm {
  return { name: defaultForkSkillName(source, t), repointFleetId: null };
}

/**
 * The request the dialog sends. An emptied name sends the localized default,
 * so the copy is never named by the server's English `Copy of` fallback.
 */
export function toForkSkillRequest(form: ForkSkillForm, defaultName: string): SkillForkRequest {
  const name = form.name.trim();
  return { name: name || defaultName, repoint_fleet_id: form.repointFleetId };
}

/** What the dialog shows for a refused fork: a localized message, and the API's words as a detail. */
export function describeSkillForkRefusal(
  error: unknown,
  t: (text: string) => string,
): { message: string; detail: string | null } {
  const raw = error instanceof Error ? error.message : "";
  const status = (error as { status?: number } | null)?.status;
  if (status === 403) return { message: t("You are not allowed to duplicate this skill."), detail: raw || null };
  if (status === 400) return { message: t("This skill has no content yet: refresh it, then duplicate it."), detail: raw || null };
  return { message: t("The skill was not duplicated."), detail: raw || null };
}

/** The toast lines after a fork: what was made, the agents re-bound, and what was left as it was. */
export function skillForkSummary(
  result: SkillForkResult,
  t: (text: string, values?: Record<string, string | number>) => string,
): { title: string; lines: string[]; variant: "success" | "info" } {
  const lines = [t("{name} is ready to customize.", { name: result.skill.name })];
  const repointed = result.repointed_agent_ids.length;
  if (repointed > 0) {
    lines.push(t("Agents of the fleet now using the copy: {count}.", { count: repointed }));
  }
  if (result.warnings.length > 0) lines.push(...result.warnings);
  return { title: t("Skill duplicated"), lines, variant: result.warnings.length > 0 ? "info" : "success" };
}

/** Whether a skill is a fork, and of what: drives the provenance line on the skill page. */
export function skillForkProvenance(
  skill: Pick<AgentSkill, "forked_from">,
): { name: string; isBuiltin: boolean; sourceId: string; version: number | null } | null {
  const from = skill.forked_from;
  if (!from || typeof from !== "object" || !from.skill_id) return null;
  return {
    name: from.name,
    isBuiltin: !!from.is_builtin,
    sourceId: from.skill_id,
    version: typeof from.version_number === "number" ? from.version_number : null,
  };
}
