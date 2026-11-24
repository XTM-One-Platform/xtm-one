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
 * The pure part of the fleet's Members view (dev-docs/ctem-fleets.md 10.5):
 * which rows the table lists, which ones a selection may hold, the range a
 * Shift+click selects, what a fleet default rewrites, and the Undo of a bulk
 * change (the previous values, grouped so each group is one bulk call).
 */
import type {
  FleetContextInclusion,
  FleetMember,
  FleetMemberPreset,
  FleetMemberSetting,
  FleetMembersBulkSet,
  FleetOrganizationScope,
} from "@/hooks/api/useFleetApi";
import type { Organization } from "@/hooks/api/useOrganizationsApi";

/** The column name of each setting, through `t_i18n`. @i18n-keys */
export const MEMBER_SETTING_LABELS = {
  context_inclusion: "Conversation shared",
  collaboration_instruction: "Collaboration instruction",
  organization_scope: "Organizations",
} as const satisfies Record<FleetMemberSetting, string>;

/**
 * The name of the per-member reset button of each setting, one whole sentence
 * per setting so the field reads lower case inside it in every language.
 * Through `t_i18n`. @i18n-keys
 */
export const RESET_SETTING_LABELS = {
  context_inclusion: "Reset conversation shared to the fleet default",
  collaboration_instruction: "Reset collaboration instruction to the fleet default",
  organization_scope: "Reset organizations to the fleet default",
} as const satisfies Record<FleetMemberSetting, string>;

/** The settings that belong to a connected row only; the organization scope applies to every member. */
const CONNECTED_ONLY_SETTINGS: readonly FleetMemberSetting[] = ["context_inclusion", "collaboration_instruction"];

/** The words of a scope, through `t_i18n` (dev-docs/ctem-fleets.md 10.2). @i18n-keys */
export const ORGANIZATION_SCOPE_WORDS = {
  primary: "Own organization",
  monitored: "All monitored organizations",
  unknown: "An organization not found",
} as const;

export const PRIMARY_SCOPE: FleetOrganizationScope = { mode: "primary" };

/** The rows of the table: the home members (orchestrator first), then the connected ones. */
export function membersTableRows(members: readonly FleetMember[]): FleetMember[] {
  const home = members
    .filter((m) => m.role === "member")
    .sort((a, b) => Number(b.is_orchestrator) - Number(a.is_orchestrator) || a.name.localeCompare(b.name));
  const connected = members.filter((m) => m.role === "connected").sort((a, b) => a.name.localeCompare(b.name));
  return [...home, ...connected];
}

/**
 * Whether a row carries the member settings: a connected row the reader can
 * see. A home member reads "Not applicable" (it is called inside the fleet);
 * a row not shared with the reader is listed without selection or edit.
 */
export function isConfigurable(member: FleetMember): boolean {
  return member.role === "connected" && isVisible(member);
}

/** A row the reader can see: a home member or a connected one. Its organization scope is editable. */
export function isVisible(member: FleetMember): boolean {
  return member.name !== "" || !!member.target_fleet_id;
}

/** Whether a row takes `setting`: the organization scope on every visible row, the others on connected rows. */
export function takesSetting(member: FleetMember, setting: FleetMemberSetting): boolean {
  return CONNECTED_ONLY_SETTINGS.includes(setting) ? isConfigurable(member) : isVisible(member);
}

/**
 * The tags of a scope, in the order the editor lists them: the primary first,
 * then each listed organization by name (an id no longer listed reads "An
 * organization not found"). The monitored scope is one tag, "All monitored
 * organizations": the primary is always in it (the editor says so), and a
 * second tag would fold it into "+N", so its meaning would show on no screen
 * (UX round 3, B1). The words come back
 * untranslated: the caller passes each through `t_i18n` except the names.
 */
export function scopeTags(
  scope: FleetOrganizationScope | undefined,
  organizations: readonly Organization[],
): { text: string; translate: boolean }[] {
  const primary = organizations.find((o) => o.role === "primary");
  const head = { text: primary?.name || ORGANIZATION_SCOPE_WORDS.primary, translate: !primary?.name };
  if (!scope || scope.mode === "primary") return [head];
  if (scope.mode === "monitored") return [{ text: ORGANIZATION_SCOPE_WORDS.monitored, translate: true }];
  const byId = new Map(organizations.map((o) => [o.id, o]));
  return [
    head,
    ...scope.organization_ids
      .filter((id) => byId.get(id)?.role !== "primary")
      .map((id) => {
        const org = byId.get(id);
        return org ? { text: org.name, translate: false } : { text: ORGANIZATION_SCOPE_WORDS.unknown, translate: true };
      }),
  ];
}

/** The tracked organizations a `list` scope may name: active ones, by name. */
export function trackedOrganizations(organizations: readonly Organization[]): Organization[] {
  return organizations.filter((o) => o.role === "tracked" && !o.archived_at);
}

/**
 * The tracked organizations a scope editor's run estimate reads: the monitored
 * ones for "All monitored organizations", else the picked ones still tracked.
 * The primary is never estimated (its own triggers are not capped).
 */
export function scopeEstimateIds(
  monitored: boolean,
  picked: readonly string[],
  organizations: readonly Organization[],
): string[] {
  const tracked = trackedOrganizations(organizations);
  if (monitored) return tracked.filter((o) => o.monitoring === "on").map((o) => o.id);
  const known = new Set(tracked.map((o) => o.id));
  return [...new Set(picked)].filter((id) => known.has(id));
}

/**
 * What the estimate line of a scope editor says, from the API's answer (no
 * platform sentence shown as is). A tracked organization is not estimated
 * when the API gives a note or any serving assignment's count failed (no
 * OpenCTI identity linked, a query error): only read figures are summed.
 * `unserved` is true when no member trigger serves any picked organization,
 * so the line explains the zero instead of showing a bare "About 0 runs".
 * `read` counts the tracked organizations whose figure was read (served, no
 * note, no failed count): with none read the runs are unknown, not zero.
 */
export function scopeEstimateSummary(estimate: {
  organizations: readonly {
    role: string;
    expected_runs_per_month: number;
    note: string | null;
    assignments: readonly { error: string | null }[];
  }[];
}): { runs: number; read: number; unread: number; unserved: boolean } {
  const tracked = estimate.organizations.filter((o) => o.role === "tracked");
  const isUnread = (o: (typeof tracked)[number]) => !!o.note || o.assignments.some((a) => !!a.error);
  return {
    runs: tracked.reduce((sum, o) => sum + (isUnread(o) ? 0 : o.expected_runs_per_month), 0),
    read: tracked.filter((o) => !isUnread(o) && o.assignments.length > 0).length,
    unread: tracked.filter(isUnread).length,
    unserved: tracked.length > 0 && tracked.every((o) => !o.note && o.assignments.length === 0),
  };
}

/**
 * The scope an editor answers from its picks: "All monitored" wins over a
 * list; no tracked organization picked is the primary alone.
 */
export function scopeFromPicks(monitored: boolean, organizationIds: readonly string[]): FleetOrganizationScope {
  if (monitored) return { mode: "monitored" };
  const ids = [...new Set(organizationIds)];
  return ids.length > 0 ? { mode: "list", organization_ids: ids } : { mode: "primary" };
}

/** Two scopes are the same setting (a list compares as a set). */
export function sameScope(a: FleetOrganizationScope | undefined, b: FleetOrganizationScope | undefined): boolean {
  const left = a ?? PRIMARY_SCOPE;
  const right = b ?? PRIMARY_SCOPE;
  if (left.mode !== right.mode) return false;
  if (left.mode !== "list" || right.mode !== "list") return true;
  const ids = new Set(left.organization_ids);
  return ids.size === new Set(right.organization_ids).size && right.organization_ids.every((id) => ids.has(id));
}

/**
 * The bulk calls of "Reset to fleet defaults" on `rows`: every setting the
 * row takes goes back to the fleet default, so a home row resets its scope
 * alone and a connected row all three (one call per kind of row).
 */
export function resetCalls(rows: readonly FleetMember[]): { memberIds: string[]; set: FleetMembersBulkSet }[] {
  const home = rows.filter((m) => m.role === "member" && isVisible(m)).map((m) => m.id);
  const connected = rows.filter(isConfigurable).map((m) => m.id);
  const calls: { memberIds: string[]; set: FleetMembersBulkSet }[] = [];
  if (connected.length > 0) {
    calls.push({ memberIds: connected, set: { context_inclusion: null, collaboration_instruction: null, organization_scope: null } });
  }
  if (home.length > 0) calls.push({ memberIds: home, set: { organization_scope: null } });
  return calls;
}

export function isOverridden(member: FleetMember, setting: FleetMemberSetting): boolean {
  return (member.overridden ?? []).includes(setting);
}

/**
 * The selection after a click on `id`: a plain click toggles the row and
 * becomes the anchor; a Shift+click (or Shift+Space) sets every selectable row
 * between the anchor and `id` to the clicked row's new state.
 */
export function toggleSelection(
  selected: ReadonlySet<string>,
  orderedIds: readonly string[],
  id: string,
  anchor: string | null,
  range: boolean,
): Set<string> {
  const next = new Set(selected);
  const select = !selected.has(id);
  const from = anchor === null ? -1 : orderedIds.indexOf(anchor);
  const to = orderedIds.indexOf(id);
  if (!range || from < 0 || to < 0) {
    if (select) next.add(id);
    else next.delete(id);
    return next;
  }
  const [lo, hi] = from <= to ? [from, to] : [to, from];
  for (const rowId of orderedIds.slice(lo, hi + 1)) {
    if (select) next.add(rowId);
    else next.delete(rowId);
  }
  return next;
}

/** What saving a fleet default rewrites: the rows that inherit it and the ones that keep their override. */
export function defaultImpact(members: readonly FleetMember[], setting: FleetMemberSetting): { applies: number; keep: number } {
  const rows = members.filter((m) => takesSetting(m, setting));
  const keep = rows.filter((m) => isOverridden(m, setting)).length;
  return { applies: rows.length - keep, keep };
}

/**
 * The Undo of a bulk change of `settings` on `rows`: one bulk call per group
 * of rows that had the same previous state (an inherited field goes back to
 * `null`, an overridden one to its own value), so the Undo restores exactly
 * what was there, override markers included.
 */
export function undoBulkCalls(
  rows: readonly FleetMember[],
  settings: readonly FleetMemberSetting[],
): { memberIds: string[]; set: FleetMembersBulkSet }[] {
  const groups = new Map<string, { memberIds: string[]; set: FleetMembersBulkSet }>();
  for (const row of rows) {
    const set: FleetMembersBulkSet = {};
    for (const setting of settings) {
      if (!isOverridden(row, setting)) set[setting] = null;
      else if (setting === "context_inclusion") set.context_inclusion = row.context_inclusion as FleetContextInclusion;
      else if (setting === "organization_scope") set.organization_scope = row.organization_scope ?? PRIMARY_SCOPE;
      else set.collaboration_instruction = row.collaboration_instruction;
    }
    const key = JSON.stringify(set);
    const group = groups.get(key) ?? { memberIds: [], set };
    group.memberIds.push(row.id);
    groups.set(key, group);
  }
  return [...groups.values()];
}

/**
 * Runs every call of a multi-call action (the Undo of a bulk change) and
 * reports once all of them settled: `null` when every call landed, else the
 * first refusal, so the caller shows one toast, never one per call.
 */
export async function settleAll<T>(calls: readonly T[], run: (call: T) => Promise<unknown>): Promise<{ failed: unknown } | null> {
  const results = await Promise.allSettled(calls.map((call) => run(call)));
  const refused = results.find((result): result is PromiseRejectedResult => result.status === "rejected");
  return refused ? { failed: refused.reason } : null;
}

/** The role filter of the table: every row, the orchestrator, the other home members, or the connected rows. */
export type MembersRoleFilter = "all" | "orchestrator" | "member" | "connected";
/** The setting-state filter: every row, the rows that override a fleet default, or the rows that inherit them all. */
export type MembersStateFilter = "all" | "overridden" | "default";
/** The sortable columns; no sort keeps the table order (home members, orchestrator first, then connected). */
export type MembersSortKey = "member" | "role" | "organizations";

/** The label of each role filter, through `t_i18n`. @i18n-keys */
const MEMBERS_ROLE_LABELS = {
  all: "All roles",
  orchestrator: "Orchestrator",
  member: "Home member",
  connected: "Connected",
} as const satisfies Record<MembersRoleFilter, string>;
/** The label of each setting-state filter, through `t_i18n`. @i18n-keys */
const MEMBERS_STATE_LABELS = {
  // Short enough for the 220 px trigger at 1024 in every locale (UX round 2, B5).
  all: "Default or overridden",
  overridden: "Overridden",
  default: "Fleet default",
} as const satisfies Record<MembersStateFilter, string>;

/** The options of each filter, in menu order. */
export const MEMBERS_ROLE_FILTERS = (Object.keys(MEMBERS_ROLE_LABELS) as MembersRoleFilter[]).map((value) => ({ value, label: MEMBERS_ROLE_LABELS[value] }));
export const MEMBERS_STATE_FILTERS = (Object.keys(MEMBERS_STATE_LABELS) as MembersStateFilter[]).map((value) => ({ value, label: MEMBERS_STATE_LABELS[value] }));

/** The table state carried in the URL (`mq`, `mrole`, `mstate`, `msort`, `mdir`), so a link reopens the same view. */
export interface MembersTableState {
  query: string;
  role: MembersRoleFilter;
  state: MembersStateFilter;
  sort: MembersSortKey | null;
  direction: "asc" | "desc";
}

function oneOf<T extends string>(value: string | null, allowed: readonly T[], fallback: T): T {
  return allowed.includes(value as T) ? (value as T) : fallback;
}

/** The table state read from the URL; an unknown value falls back to the default. */
export function readMembersTableState(params: URLSearchParams): MembersTableState {
  const sort = params.get("msort");
  return {
    query: params.get("mq") ?? "",
    role: oneOf<MembersRoleFilter>(params.get("mrole"), MEMBERS_ROLE_FILTERS.map((f) => f.value), "all"),
    state: oneOf<MembersStateFilter>(params.get("mstate"), MEMBERS_STATE_FILTERS.map((f) => f.value), "all"),
    sort: sort === "member" || sort === "role" || sort === "organizations" ? sort : null,
    direction: params.get("mdir") === "desc" ? "desc" : "asc",
  };
}

/** The URL parameters of the table state: a link carrying one of them opens the Members section (#1387). */
const MEMBERS_TABLE_PARAMS = ["mq", "mrole", "mstate", "msort", "mdir"] as const;
/** The open or folded Members section in the URL: `mopen=1` open, `mopen=0` folded over a table state. */
const MEMBERS_OPEN_PARAM = "mopen";

/**
 * Whether the URL opens the Members section (fleet defaults, toolbar and table, #1387): `mopen=1`, or a
 * members search, filter or sort in the link (a deep link keeps working) unless `mopen=0` folds it.
 * Without either, `openByDefault` decides.
 */
export function readMembersSectionOpen(params: URLSearchParams, openByDefault: boolean): boolean {
  const flag = params.get(MEMBERS_OPEN_PARAM);
  if (flag === "1") return true;
  if (flag === "0") return false;
  return openByDefault || MEMBERS_TABLE_PARAMS.some((key) => params.has(key));
}

/**
 * The URL after Show or Hide: Show writes `mopen=1` (clearing the filters later keeps the section open);
 * Hide removes it, or writes `mopen=0` when a table state left in the link would reopen it.
 */
export function writeMembersSectionOpen(params: URLSearchParams, open: boolean, openByDefault: boolean): URLSearchParams {
  const next = new URLSearchParams(params);
  next.delete(MEMBERS_OPEN_PARAM);
  if (open) next.set(MEMBERS_OPEN_PARAM, "1");
  else if (readMembersSectionOpen(next, openByDefault)) next.set(MEMBERS_OPEN_PARAM, "0");
  return next;
}

/** The home (orchestrator included) and connected counts of the folded Members bar. */
export function membersCounts(members: readonly FleetMember[]): { home: number; connected: number } {
  const rows = membersTableRows(members);
  return { home: rows.filter((m) => m.role === "member").length, connected: rows.filter((m) => m.role === "connected").length };
}

/** The URL after a change of the table state: a default value leaves its parameter out; every other parameter is kept. */
export function writeMembersTableState(params: URLSearchParams, state: MembersTableState): URLSearchParams {
  const next = new URLSearchParams(params);
  const put = (key: string, value: string, fallback: string) => (value && value !== fallback ? next.set(key, value) : next.delete(key));
  put("mq", state.query, "");
  put("mrole", state.role, "all");
  put("mstate", state.state, "all");
  put("msort", state.sort ?? "", "");
  put("mdir", state.sort ? state.direction : "asc", "asc");
  return next;
}

function roleOf(member: FleetMember): Exclude<MembersRoleFilter, "all"> {
  if (member.is_orchestrator) return "orchestrator";
  return member.role === "connected" ? "connected" : "member";
}

const ROLE_ORDER: Record<Exclude<MembersRoleFilter, "all">, number> = { orchestrator: 0, member: 1, connected: 2 };

/**
 * The rows the table shows: the search on the member name (case-insensitive),
 * the role filter and the setting-state filter, then the sort. A row hidden
 * from the reader never matches a search (its name is not shown); with no sort
 * the rows keep their order. `scopeText` gives the text a row's scope sorts by.
 */
export function visibleMembersRows(
  rows: readonly FleetMember[],
  state: MembersTableState,
  scopeText: (member: FleetMember) => string,
): FleetMember[] {
  const query = state.query.trim().toLocaleLowerCase();
  const kept = rows.filter((m) => {
    if (query && !(isVisible(m) && m.name.toLocaleLowerCase().includes(query))) return false;
    if (state.role !== "all" && roleOf(m) !== state.role) return false;
    const overridden = (m.overridden ?? []).length > 0;
    if (state.state === "overridden" && !overridden) return false;
    if (state.state === "default" && overridden) return false;
    return true;
  });
  if (!state.sort) return kept;
  const sign = state.direction === "desc" ? -1 : 1;
  const key = state.sort;
  return kept
    .map((m, index) => ({ m, index }))
    .sort((a, b) => {
      const order =
        key === "member"
          ? a.m.name.localeCompare(b.m.name)
          : key === "role"
            ? ROLE_ORDER[roleOf(a.m)] - ROLE_ORDER[roleOf(b.m)]
            : scopeText(a.m).localeCompare(scopeText(b.m));
      return order * sign || a.index - b.index;
    })
    .map(({ m }) => m);
}

/**
 * The names of the built-in presets (`builtin:<key>`, dev-docs/ctem-fleets.md
 * 10.6), through `t_i18n`: the server answers them in English. A company
 * preset's name is shown as an administrator typed it. @i18n-keys
 */
export const BUILTIN_PRESET_NAMES = {
  "builtin:own-summary": "Own organization, a summary",
  "builtin:monitored-nothing": "All monitored organizations, nothing",
  "builtin:own-full": "Own organization, the full conversation",
} as const;

/** A preset's name as the picker shows it: `translate` says whether it goes through `t_i18n`. */
export function presetName(preset: Pick<FleetMemberPreset, "id" | "name" | "builtin">): { text: string; translate: boolean } {
  const builtin = preset.builtin ? BUILTIN_PRESET_NAMES[preset.id as keyof typeof BUILTIN_PRESET_NAMES] : undefined;
  return builtin ? { text: builtin, translate: true } : { text: preset.name, translate: false };
}

/** The settings a preset writes, in the column order of the table. */
export function presetSettings(values: FleetMembersBulkSet): FleetMemberSetting[] {
  return (["context_inclusion", "organization_scope", "collaboration_instruction"] as const).filter((s) => values[s] != null);
}

/**
 * The bulk calls of "Apply a preset" on `rows`: a connected row takes every
 * value the preset sets, a home row its organization scope alone (the other
 * two settings are not a home member's); a kind of row the preset writes
 * nothing to gets no call.
 */
export function presetCalls(rows: readonly FleetMember[], values: FleetMembersBulkSet): { memberIds: string[]; set: FleetMembersBulkSet }[] {
  const settings = presetSettings(values);
  const pick = (keep: (s: FleetMemberSetting) => boolean) => {
    const set: FleetMembersBulkSet = {};
    for (const s of settings.filter(keep)) (set as Record<string, unknown>)[s] = values[s];
    return set;
  };
  const calls: { memberIds: string[]; set: FleetMembersBulkSet }[] = [];
  const connected = rows.filter(isConfigurable).map((m) => m.id);
  const home = rows.filter((m) => m.role === "member" && isVisible(m)).map((m) => m.id);
  const connectedSet = pick(() => true);
  const homeSet = pick((s) => !CONNECTED_ONLY_SETTINGS.includes(s));
  if (connected.length > 0 && Object.keys(connectedSet).length > 0) calls.push({ memberIds: connected, set: connectedSet });
  if (home.length > 0 && Object.keys(homeSet).length > 0) calls.push({ memberIds: home, set: homeSet });
  return calls;
}

/**
 * A preset applied to the fleet defaults: the patch of `PATCH /fleets/{id}`
 * (`member_defaults`, the fields the preset sets) and the previous values of
 * the same fields, which the Undo sends back.
 */
export function presetDefaultsChange(
  defaults: { [K in FleetMemberSetting]?: FleetMembersBulkSet[K] } | undefined,
  values: FleetMembersBulkSet,
): { patch: FleetMembersBulkSet; previous: FleetMembersBulkSet } {
  const patch: FleetMembersBulkSet = {};
  const previous: FleetMembersBulkSet = {};
  for (const s of presetSettings(values)) {
    (patch as Record<string, unknown>)[s] = values[s];
    if (s === "context_inclusion") previous.context_inclusion = (defaults?.context_inclusion ?? "none") as FleetContextInclusion;
    else if (s === "organization_scope") previous.organization_scope = defaults?.organization_scope ?? PRIMARY_SCOPE;
    else previous.collaboration_instruction = defaults?.collaboration_instruction ?? "";
  }
  return { patch, previous };
}

/** The longest preset name the server takes (`PRESET_NAME_MAX_LENGTH`). */
export const PRESET_NAME_MAX_LENGTH = 120;

/**
 * The draft of the company preset editor (Settings > Fleets > Member presets):
 * which settings the preset writes and their value. A setting left out is not
 * written when the preset is applied.
 */
export interface PresetDraft {
  name: string;
  sets: Record<FleetMemberSetting, boolean>;
  context_inclusion: FleetContextInclusion;
  collaboration_instruction: string;
  organization_scope: FleetOrganizationScope;
}

/** The draft of a new preset, or of an existing one to edit. */
export function presetDraft(preset?: Pick<FleetMemberPreset, "name" | "values">): PresetDraft {
  const values = preset?.values ?? {};
  return {
    name: preset?.name ?? "",
    sets: {
      context_inclusion: values.context_inclusion != null,
      collaboration_instruction: values.collaboration_instruction != null,
      organization_scope: values.organization_scope != null,
    },
    context_inclusion: values.context_inclusion ?? "summary",
    collaboration_instruction: values.collaboration_instruction ?? "",
    organization_scope: values.organization_scope ?? PRIMARY_SCOPE,
  };
}

/** The values a draft saves: the settings it writes, nothing else. */
export function presetDraftValues(draft: PresetDraft): FleetMembersBulkSet {
  const values: FleetMembersBulkSet = {};
  if (draft.sets.context_inclusion) values.context_inclusion = draft.context_inclusion;
  if (draft.sets.organization_scope) values.organization_scope = draft.organization_scope;
  if (draft.sets.collaboration_instruction) values.collaboration_instruction = draft.collaboration_instruction;
  return values;
}

/** Why a preset draft cannot be saved, in English, read through `t_i18n`. @i18n-keys */
const PRESET_DRAFT_ERRORS = {
  noName: "Give the preset a name",
  takenName: "A preset with this name exists",
  noSetting: "Choose at least one setting the preset writes",
} as const;

/**
 * Why a draft cannot be saved, untranslated (the caller passes it through
 * `t_i18n`), or `null`: a name, unique among the presets without regard to
 * case and not a built-in's, and at least one setting.
 */
export function presetDraftError(
  draft: PresetDraft,
  presets: readonly Pick<FleetMemberPreset, "id" | "name">[],
  editingId?: string,
): { field: "name" | "settings"; message: string } | null {
  const name = draft.name.trim().toLowerCase();
  if (!name) return { field: "name", message: PRESET_DRAFT_ERRORS.noName };
  const builtinNames = Object.values(BUILTIN_PRESET_NAMES).map((n) => n.toLowerCase());
  if (builtinNames.includes(name) || presets.some((p) => p.id !== editingId && p.name.trim().toLowerCase() === name)) {
    return { field: "name", message: PRESET_DRAFT_ERRORS.takenName };
  }
  if (presetSettings(presetDraftValues(draft)).length === 0) return { field: "settings", message: PRESET_DRAFT_ERRORS.noSetting };
  return null;
}

/** The first line of an instruction, what its cell shows. */
export function firstLine(text: string): string {
  return text.split(/\r?\n/, 1)[0] ?? "";
}
