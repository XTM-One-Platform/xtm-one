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
 * The "Duplicate fleet" dialog's pure parts (`ForkFleetDialog`): the form, the
 * request it sends to `POST /fleets/{id}/fork`, the default name, what a
 * refusal says and the provenance line a forked fleet shows. Kept out of the
 * component so they are tested without rendering.
 */
import type { Fleet, FleetForkMembers, FleetForkRequest, FleetForkResult } from "@/hooks/api/useFleetApi";

export interface ForkFleetForm {
  name: string;
  members: FleetForkMembers;
  /** `null`: a new workspace named after the copy. */
  workspaceId: string | null;
}

type Translate = (text: string, values?: Record<string, string | number>) => string;

/** The copy's default name, in the reader's language: "Copy of {name}". */
function defaultForkName(source: Pick<Fleet, "name">, t: Translate): string {
  return t("Copy of {name}", { name: source.name });
}

export function initialForkForm(source: Pick<Fleet, "name">, t: Translate): ForkFleetForm {
  return { name: defaultForkName(source, t), members: "copy", workspaceId: null };
}

/** The request the dialog sends: an empty name lets the server pick `Copy of` the source's name. */
export function toForkRequest(form: ForkFleetForm): FleetForkRequest {
  const name = form.name.trim();
  return { name: name || null, workspace_id: form.workspaceId, members: form.members };
}

/** What the dialog shows for a refused fork: a localized message, and the API's words as a detail. */
export function describeForkRefusal(
  error: unknown,
  t: (text: string) => string,
): { message: string; detail: string | null } {
  const raw = error instanceof Error ? error.message : "";
  const status = (error as { status?: number } | null)?.status;
  if (status === 403) return { message: t("You are not allowed to duplicate this fleet."), detail: raw || null };
  if (status === 400) return { message: t("This fleet has no orchestrator: copy every member, or set its orchestrator first."), detail: raw || null };
  return { message: t("The fleet was not duplicated."), detail: raw || null };
}

/** The toast lines after a fork: what was copied, that nothing runs, and what was left out. */
export function forkSummary(
  result: FleetForkResult,
  t: (text: string, values?: Record<string, string | number>) => string,
): { title: string; lines: string[]; variant: "success" | "info" } {
  const lines = [
    t("\"{name}\" is ready to customize.", { name: result.fleet.name }),
    t("{count, plural, =0 {No automation was copied.} one {# automation was copied, switched off.} other {# automations were copied, switched off.}}", {
      count: result.assignments_copied,
    }),
  ];
  if (result.warnings.length > 0) lines.push(t("Some parts of the original fleet were not copied:"), ...result.warnings);
  return { title: t("Fleet duplicated"), lines, variant: result.warnings.length > 0 ? "info" : "success" };
}

/** Whether a fleet is a fork, and of what: drives the provenance line on the fleet page. */
export function forkProvenance(fleet: Pick<Fleet, "forked_from">): { name: string; isBuiltin: boolean; sourceId: string; forkedAt: string } | null {
  const from = fleet.forked_from;
  if (!from || typeof from !== "object" || !from.fleet_id) return null;
  return { name: from.name, isBuiltin: !!from.is_builtin, sourceId: from.fleet_id, forkedAt: from.forked_at };
}
