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
 * The "source changed since you forked it" notice's pure parts
 * (`FleetSourceChangesNotice`, #9174): whether to show it, and one line per
 * object that moved, from `GET /fleets/{id}/source-changes`. Kept out of the
 * component so they are tested without rendering.
 *
 * The endpoint names a member by its agent slug and a connected agent or fleet
 * by its id: the lines read the display names the page already loads
 * (`sourceChangeNames`), never an id, and fall back to the slug for a member
 * whose agent the reader cannot see.
 */
import type { FleetSourceChange, FleetSourceChanges } from "@/hooks/api/useFleetApi";

type Translate = (text: string, values?: Record<string, string | number>) => string;

/** The English label of each structural field the diff names, read by `t_i18n(FIELD_LABELS[key])`. @i18n-keys */
const FIELD_LABELS = {
  name: "Name",
  description: "Description",
  skills: "Skills",
  accepted_handover_types: "Accepted handovers",
  orchestrator: "Orchestrator",
  role_label: "Role",
  parent: "Reports to",
  exposed: "Exposure",
  persona: "Persona",
  skill_ids: "Skills",
  handovers: "Handovers",
  instruction: "Collaboration instruction",
  inclusion: "Shared conversation",
} as const;

const LABEL_OF: Readonly<Record<string, string>> = FIELD_LABELS;

/** The display names the lines read: member agents by slug, connected agents and fleets by id. */
export interface SourceChangeNames {
  members: ReadonlyMap<string, string>;
  connected: ReadonlyMap<string, { kind: "agent" | "fleet"; name: string }>;
}

const NO_NAMES: SourceChangeNames = { members: new Map(), connected: new Map() };

/** The names the page already loads: its agents list and its fleets list. */
export function sourceChangeNames(
  agents: ReadonlyArray<{ id: string; slug: string | null; name: string }> | undefined,
  fleets: ReadonlyArray<{ id: string; name: string }> | undefined,
): SourceChangeNames {
  const members = new Map<string, string>();
  const connected = new Map<string, { kind: "agent" | "fleet"; name: string }>();
  for (const a of agents ?? []) {
    if (a.slug && a.name) members.set(a.slug, a.name);
    if (a.name) connected.set(a.id, { kind: "agent", name: a.name });
  }
  for (const f of fleets ?? []) if (f.name) connected.set(f.id, { kind: "fleet", name: f.name });
  return { members, connected };
}

/** The source changed and is still there: the structural diff applies. */
export function sourceChanged(found: FleetSourceChanges): boolean {
  return found.is_fork && found.source_exists && found.changed === true;
}

/**
 * The seeded fleet that took over a retired built-in source (CTEM-31), on a
 * fork only: a fork of a fleet folded into the generic CTEM loop names it.
 */
export function sourceSuccessor(found: FleetSourceChanges): NonNullable<FleetSourceChanges["successor"]> | null {
  return found.is_fork && found.successor ? found.successor : null;
}

/** Show the notice on a fork whose existing source changed, or whose built-in source was retired into a successor. */
export function showSourceChanges(found: FleetSourceChanges | undefined): found is FleetSourceChanges {
  return !!found && (sourceChanged(found) || sourceSuccessor(found) !== null);
}

/**
 * The source's name as it reads now: the fork keeps the name it had at fork
 * time, and a renamed source sends its current name on the card change.
 */
export function currentSourceName(found: Pick<FleetSourceChanges, "changes">, keptName: string): string {
  const card = found.changes.find((c) => c.object === "fleet" && c.fields.includes("name"));
  return card?.name || keptName;
}

/** The field names of a change in the reader's language, deduplicated (two fields share "Skills"). */
export function changedFieldLabels(change: Pick<FleetSourceChange, "fields">, t: Translate): string[] {
  const labels = change.fields.map((f) => t(LABEL_OF[f] ?? f));
  return [...new Set(labels)];
}

/** One sentence per change: what moved, without the fields (the caller lists them). */
export function describeSourceChange(change: FleetSourceChange, t: Translate, names: SourceChangeNames = NO_NAMES): string {
  if (change.object === "fleet") return t("The fleet's card changed");
  if (change.object === "member") {
    const name = names.members.get(change.name) ?? change.name;
    if (change.action === "added") return t("Member {name} added", { name });
    if (change.action === "removed") return t("Member {name} removed", { name });
    return t("Member {name} changed", { name });
  }
  const target = names.connected.get(change.name);
  if (!target) {
    // An id the reader cannot resolve is never printed.
    if (change.action === "added") return t("A connected agent or fleet you cannot open was added");
    if (change.action === "removed") return t("A connected agent or fleet you cannot open was removed");
    return t("A connected agent or fleet you cannot open changed");
  }
  const name = target.name;
  if (target.kind === "fleet") {
    if (change.action === "added") return t("Connected fleet {name} added", { name });
    if (change.action === "removed") return t("Connected fleet {name} removed", { name });
    return t("Connected fleet {name} changed", { name });
  }
  if (change.action === "added") return t("Connected agent {name} added", { name });
  if (change.action === "removed") return t("Connected agent {name} removed", { name });
  return t("Connected agent {name} changed", { name });
}
