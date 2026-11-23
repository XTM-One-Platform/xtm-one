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
 * The fleet page's Members view (`/fleets/:fleetId/members`, dev-docs/ctem-fleets.md 10.5):
 * one table for every member, home and connected, with the settings each one
 * inherits from the fleet or overrides, the fleet defaults on top, and the
 * shared selection of the product (`SelectionBox` per row, `ListControlBar`
 * for select-all, the count, Clear and the bulk actions).
 *
 * - An inherited cell shows the effective value with a muted "Fleet default"
 *   caption; an overridden one shows "Overridden" and a reset button.
 * - A bulk action is one call to `POST /fleets/{id}/members/bulk` and one
 *   toast with the count and an Undo that sends the previous values back.
 * - Read-only with the reason for a reader who cannot manage the fleet, an
 *   archived fleet or a built-in one (the Members card's rights).
 *
 * - Organizations (R3): every visible row, home or connected, carries an
 *   organization scope (the primary always, plus every monitored organization
 *   or a list of tracked ones), inherited from the fleet or overridden.
 *
 * - Presets (R4): "Apply a preset" on the fleet defaults or on the selection
 *   writes the preset's values through the same routes, with the same Undo.
 *
 * - Instruction (R5): a connected row's instruction opens the shared
 *   centred `Dialog` (`MemberInstructionPanel`): the editor, the AI first draft on
 *   request, the fleet documentation prefill and, below xl, the automations.
 *
 * The hierarchy stays on the Graph view.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Boxes, Building2, Crown, Layers, Lock, MessagesSquare, PanelRight, Pencil, RotateCcw, Users } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { InfoTip } from "@/components/ui/info-tip";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ListControlBar, SelectionBox, type ListBulkAction } from "@/components/ui/list-control-bar";
import { SearchInput } from "@/components/ui/search-input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { TableSortHeader } from "@/components/ui/table-sort-header";
import { TagList } from "@/components/ui/tag-list";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useAssignments } from "@/hooks/api/useAssignmentApi";
import {
  useBulkUpdateFleetMembers,
  useFleetMemberPresets,
  useUpdateFleet,
  type Fleet,
  type FleetContextInclusion,
  type FleetMember,
  type FleetMemberPreset,
  type FleetMemberSetting,
  type FleetMembersBulkSet,
  type FleetOrganizationScope,
} from "@/hooks/api/useFleetApi";
import { useOrganizations, type Organization } from "@/hooks/api/useOrganizationsApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

import { CONTEXT_INCLUSION_FIELD, CONTEXT_INCLUSION_SHORT_LABELS, CONTEXT_INCLUSIONS } from "./context-inclusion";
import { BUILTIN_MEMBERS_REASON, BUILTIN_REASON, fleetEditRights } from "./fleet-edition";
import { fleetRefusalMessage } from "./fleet-refusals";
import {
  MEMBER_SETTING_LABELS,
  MEMBERS_ROLE_FILTERS,
  MEMBERS_STATE_FILTERS,
  PRIMARY_SCOPE,
  defaultImpact,
  firstLine,
  isConfigurable,
  isOverridden,
  isVisible,
  membersTableRows,
  presetCalls,
  presetDefaultsChange,
  resetCalls,
  sameScope,
  scopeTags,
  toggleSelection,
  undoBulkCalls,
  RESET_SETTING_LABELS,
  membersCounts,
  readMembersSectionOpen,
  readMembersTableState,
  settleAll,
  visibleMembersRows,
  writeMembersSectionOpen,
  writeMembersTableState,
  type MembersRoleFilter,
  type MembersSortKey,
  type MembersStateFilter,
  type MembersTableState,
} from "./members-view";
import { ApplyPresetDialog } from "./ApplyPresetDialog";
import { DEFAULTS_INSTRUCTION_SUMMARY, FLEET_DEFAULTS_OPEN_BY_DEFAULT, instructionSummaryKey } from "./capability-slots-table";
import { FleetCapabilitySlotsCard } from "./FleetCapabilitySlotsCard";
import { FleetFoldHeader } from "./FleetFoldHeader";
import { MemberInstructionPanel } from "./MemberInstructionPanel";
import { OrganizationScopeDialog } from "./OrganizationScopeDialog";

/** Until R6, the setting is recorded and shown only (R-D2 A). @i18n-keys */
const NOTES = { notAppliedYet: "Recorded for the fleet; applied to consults in a later release" } as const;
const NOT_APPLIED_YET = NOTES.notAppliedYet;
/** What the organization scope is, in the help line of the fleet default. @i18n-keys */
const SCOPE_NOTES = { help: "The organizations a member reasons about; the platform's own organization is always in" } as const;
const SCOPE_HELP = SCOPE_NOTES.help;
/** One help line under each fleet default, at 12 px (Forms rule, UX round 3 B3). @i18n-keys */
const DEFAULTS_HELP = {
  context: "How much of the conversation a connected member receives when the fleet calls it",
  instruction: "How the fleet works with its connected members: when to call them and what to ask",
} as const;
/** The 12 px help line under a form field (frontend-design-system.md, Forms), never content-caption. */
const FIELD_HELP_CLASS = "content-compact text-default-secondary";
/** One label row for the three fleet defaults: the Label's own 14 px type (no size class, Forms rule), same 16 px line with or without an info bubble. */
const DEFAULTS_LABEL_CLASS = "flex h-4 items-center gap-1 text-default-secondary";
/** The Members section (defaults, toolbar, table) opens folded, as the fleet defaults did since #1128 (#1387). */
const MEMBERS_SECTION_OPEN_BY_DEFAULT = FLEET_DEFAULTS_OPEN_BY_DEFAULT;
/** A toast with an Undo stays 10 s, paused on hover and focus. */
const UNDO_TOAST_MS = 10_000;

export function FleetMembersView({
  fleet,
  members,
  loading,
  onOpenAgent,
}: {
  fleet: Fleet;
  members: FleetMember[];
  loading: boolean;
  onOpenAgent: (agentId: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  // The Members card's rights: reader, archived and built-in fleets read with the reason.
  const { members: rights } = fleetEditRights(fleet);
  const canEdit = rights.canEdit;
  // On a built-in fleet the capability slots above the table stay the customer's (UX r1 B1 on #994).
  const reason = rights.reason === BUILTIN_REASON ? BUILTIN_MEMBERS_REASON : rights.reason;

  // Search, filters and sort live in the URL, so a link reopens the same view (Sam's standards 3).
  const [searchParams, setSearchParams] = useSearchParams();
  const tableState = readMembersTableState(searchParams);
  const setTableState = (patch: Partial<MembersTableState>) =>
    setSearchParams((params) => writeMembersTableState(params, { ...readMembersTableState(params), ...patch }), { replace: true });
  const onSort = (key: string) => {
    const sort = key as MembersSortKey;
    // A first click sorts ascending, a second descending, a third goes back to the table order.
    if (tableState.sort !== sort) setTableState({ sort, direction: "asc" });
    else if (tableState.direction === "asc") setTableState({ direction: "desc" });
    else setTableState({ sort: null, direction: "asc" });
  };
  const { data: organizations = [] } = useOrganizations();
  const scopeText = (member: FleetMember) =>
    scopeTags(member.organization_scope, organizations)
      .map((tag) => (tag.translate ? t_i18n(tag.text) : tag.text))
      .join(", ");

  // Derived on each render; the React Compiler memoizes them.
  const allRows = membersTableRows(members);
  const rows = visibleMembersRows(allRows, tableState, scopeText);
  const filtered = tableState.query.trim() !== "" || tableState.role !== "all" || tableState.state !== "all";
  // Every visible row takes an organization scope, so every visible row is selectable.
  const selectableIds = canEdit ? rows.filter(isVisible).map((m) => m.id) : [];
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [anchor, setAnchor] = useState<string | null>(null);
  // A selection only ever holds rows that are still selectable.
  const selection = new Set([...selected].filter((id) => selectableIds.includes(id)));
  const [contextDialog, setContextDialog] = useState(false);
  // The fleet defaults form holds an unsaved edit: the Members section stays open over it (#1387).
  const [defaultsDirty, setDefaultsDirty] = useState(false);
  const [contextValue, setContextValue] = useState<FleetContextInclusion>("summary");
  // The scope dialog: the selection (`bulk`) or one row (its member).
  const [scopeTarget, setScopeTarget] = useState<{ kind: "bulk" } | { kind: "row"; member: FleetMember } | null>(null);
  // The preset dialog: the fleet defaults or the selection.
  const [presetTarget, setPresetTarget] = useState<"defaults" | "selection" | null>(null);
  // The member whose instruction panel is open (R5), by id so the panel follows the stored row.
  const [instructionTarget, setInstructionTarget] = useState<string | null>(null);
  const instructionMember = instructionTarget ? members.find((m) => m.id === instructionTarget) ?? null : null;
  const { data: presets = [], isLoading: presetsLoading } = useFleetMemberPresets({ enabled: canEdit });

  const bulk = useBulkUpdateFleetMembers(fleet.id);
  const updateFleet = useUpdateFleet(fleet.id);
  const { data: assignments } = useAssignments();
  const enabledByAgent = useMemo(() => {
    const counts = new Map<string, number>();
    for (const a of assignments ?? []) if (a.enabled) counts.set(a.agent_id, (counts.get(a.agent_id) ?? 0) + 1);
    return counts;
  }, [assignments]);

  const clear = useCallback(() => {
    setSelected(new Set());
    setAnchor(null);
  }, []);

  const toggle = (id: string, range: boolean) => {
    setSelected(toggleSelection(selection, selectableIds, id, anchor, range));
    setAnchor(id);
  };

  // `title` arrives translated: each call site passes a literal t_i18n("...")
  // so the i18n extractor and validate:i18n see the key.
  const refused = (title: string) => (err: unknown) =>
    toast({ title, description: fleetRefusalMessage(err, t_i18n), variant: "error" });

  /** One bulk call, one toast with the count and an Undo restoring the previous values. */
  const applyBulk = (targets: FleetMember[], set: FleetMembersBulkSet, title: string) => {
    if (targets.length === 0) return;
    const settings = Object.keys(set) as FleetMemberSetting[];
    const undo = undoBulkCalls(targets, settings);
    bulk.mutate(
      { memberIds: targets.map((m) => m.id), set },
      {
        onSuccess: (updated) =>
          toast({
            title: t_i18n(title, { count: updated.length }),
            variant: "success",
            durationMs: UNDO_TOAST_MS,
            action: {
              label: t_i18n("Undo"),
              // One call per group of previous states, one toast once all of them settled.
              onClick: () => {
                void settleAll(undo, (call) => bulk.mutateAsync(call)).then((outcome) =>
                  outcome
                    ? refused(t_i18n("The change was not undone"))(outcome.failed)
                    : toast({ title: t_i18n("Change undone"), variant: "success" }),
                );
              },
            },
          }),
        onError: refused(t_i18n("The members were not updated")),
      },
    );
  };

  const selectedRows = rows.filter((m) => selection.has(m.id));
  const selectionHasHome = selectedRows.some((m) => m.role === "member");

  /**
   * Several bulk calls as one action (one per kind of row): one toast with
   * `title` (translated at the call site, so the extractor sees the key) once
   * all settled, one Undo restoring the previous values.
   */
  const applyCalls = (targets: FleetMember[], calls: { memberIds: string[]; set: FleetMembersBulkSet }[], title: string) => {
    if (calls.length === 0) return;
    const undo = calls.flatMap((call) =>
      undoBulkCalls(
        targets.filter((m) => call.memberIds.includes(m.id)),
        Object.keys(call.set) as FleetMemberSetting[],
      ),
    );
    void settleAll(calls, (call) => bulk.mutateAsync(call)).then((outcome) => {
      if (outcome) {
        refused(t_i18n("The members were not updated"))(outcome.failed);
        return;
      }
      toast({
        title,
        variant: "success",
        durationMs: UNDO_TOAST_MS,
        action: {
          label: t_i18n("Undo"),
          onClick: () => {
            void settleAll(undo, (call) => bulk.mutateAsync(call)).then((back) =>
              back
                ? refused(t_i18n("The change was not undone"))(back.failed)
                : toast({ title: t_i18n("Change undone"), variant: "success" }),
            );
          },
        },
      });
    });
  };

  /** Reset: one call per kind of row (a home row resets its scope alone), one toast, one Undo. */
  const resetSelection = () =>
    applyCalls(
      selectedRows,
      resetCalls(selectedRows),
      t_i18n("{count, plural, one {# member reset to the fleet defaults} other {# members reset to the fleet defaults}}", { count: selectedRows.length }),
    );

  const bulkActions: ListBulkAction[] = [
    {
      id: "conversation",
      // Short labels keep the bar on one row at 1024 with the three actions (UX round 1, B2).
      label: t_i18n("Conversation..."),
      icon: MessagesSquare,
      onSelect: () => setContextDialog(true),
      disabled: selectionHasHome,
      disabledReason: t_i18n("Home members are called inside the fleet: select connected rows only"),
      busy: bulk.isPending,
    },
    {
      id: "organizations",
      label: t_i18n("Organizations..."),
      icon: Building2,
      onSelect: () => setScopeTarget({ kind: "bulk" }),
      busy: bulk.isPending,
    },
    {
      id: "preset",
      label: t_i18n("Preset..."),
      icon: Layers,
      onSelect: () => setPresetTarget("selection"),
      busy: bulk.isPending,
    },
    {
      id: "reset",
      label: t_i18n("Reset to fleet defaults"),
      // Icon alone, its label as accessible name and tooltip: "Rétablir les défauts" and "Restablecer todo"
      // moved the action to a second row at 1024 (UX round 2 of #9422, B1).
      iconOnly: true,
      icon: RotateCcw,
      onSelect: resetSelection,
      disabled: !selectedRows.some((m) => (m.overridden ?? []).length > 0),
      disabledReason: t_i18n("No selected member overrides a fleet default"),
      busy: bulk.isPending,
    },
  ];

  /** A preset on the selection (bulk calls, one per kind of row) or on the fleet defaults (one fleet update); both undo. */
  const applyPreset = (preset: FleetMemberPreset) => {
    const target = presetTarget;
    setPresetTarget(null);
    if (target === "selection") {
      applyCalls(
        selectedRows,
        presetCalls(selectedRows, preset.values),
        t_i18n("{count, plural, one {Preset applied to # member} other {Preset applied to # members}}", { count: selectedRows.length }),
      );
      return;
    }
    if (target === "defaults") {
      const { patch, previous } = presetDefaultsChange(fleet.member_defaults, preset.values);
      saveDefaults(patch, previous);
    }
  };

  /** One fleet update of the member defaults, one toast with an Undo sending the previous values back. */
  const saveDefaults = (patch: FleetMembersBulkSet, previous: FleetMembersBulkSet) =>
    updateFleet.mutate(
      { member_defaults: patch },
      {
        onSuccess: () =>
          toast({
            title: t_i18n("Fleet default saved"),
            variant: "success",
            durationMs: UNDO_TOAST_MS,
            action: {
              label: t_i18n("Undo"),
              onClick: () => updateFleet.mutate({ member_defaults: previous }, { onError: refused(t_i18n("The change was not undone")) }),
            },
          }),
        onError: refused(t_i18n("The fleet default was not saved")),
      },
    );

  const applyScope = (scope: FleetOrganizationScope) => {
    const target = scopeTarget;
    setScopeTarget(null);
    if (!target) return;
    const targets = target.kind === "row" ? [target.member] : selectedRows;
    applyBulk(targets, { organization_scope: scope }, "{count, plural, one {Organizations set for # member} other {Organizations set for # members}}");
  };

  const resetOne = (member: FleetMember, setting: FleetMemberSetting) =>
    applyBulk([member], { [setting]: null }, "{count, plural, one {# member reset to the fleet defaults} other {# members reset to the fleet defaults}}");

  /** The panel's save: an empty text inherits the fleet default, like a reset (`null`). */
  const saveInstruction = (member: FleetMember, value: string | null) => {
    setInstructionTarget(null);
    const next = value === null || value.trim() === "" ? null : value;
    applyBulk(
      [member],
      { collaboration_instruction: next },
      next === null
        ? "{count, plural, one {# member reset to the fleet defaults} other {# members reset to the fleet defaults}}"
        : "{count, plural, one {Instruction set for # member} other {Instruction set for # members}}",
    );
  };

  const setOne = (member: FleetMember, value: FleetContextInclusion) =>
    applyBulk([member], { context_inclusion: value }, "{count, plural, one {Conversation shared set for # member} other {Conversation shared set for # members}}");

  if (loading) {
    return (
      <Card data-testid="fleet-members-view-loading">
        <CardContent className="space-y-3 p-4">
          <Skeleton className="h-16 w-full" />
          {Array.from({ length: 5 }, (_, i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </CardContent>
      </Card>
    );
  }

  const hasConnected = allRows.some((m) => m.role === "connected");
  const hasRows = rows.length > 0;
  // Folded by default (#1387); a link with a members search, filter or sort (or mopen=1) opens it.
  const membersPinned = defaultsDirty || selection.size > 0;
  const membersOpen = readMembersSectionOpen(searchParams, MEMBERS_SECTION_OPEN_BY_DEFAULT) || membersPinned;
  const setMembersOpen = (open: boolean) =>
    setSearchParams((params) => writeMembersSectionOpen(params, open, MEMBERS_SECTION_OPEN_BY_DEFAULT), { replace: true });

  return (
    <div className="space-y-4" data-testid="fleet-members-view">
      {!canEdit && (
        <p role="note" className="flex items-start gap-2 content-caption text-default-secondary">
          <Lock className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
          {t_i18n(reason)}
        </p>
      )}

      {/* Capability slots (#914): bindings are the customer layer, open on a built-in fleet too. */}
      <FleetCapabilitySlotsCard fleet={fleet} />

      {/* One fold for the whole members part (#1387): fleet defaults, toolbar, selection bar and table;
          the shared fleet section header (UX r1 B1 of #1393). */}
      <Card data-testid="fleet-members-section" data-open={membersOpen ? "true" : "false"}>
        <CardContent className="p-4">
          <FleetFoldHeader
            icon="users"
            title={t_i18n("Members")}
            summary={!membersOpen && <MembersSectionSummary fleet={fleet} members={members} organizations={organizations} />}
            fold={{
              open: membersOpen,
              onToggle: () => setMembersOpen(!membersOpen),
              controls: "fleet-members-section-body",
              // An unsaved defaults edit or a selection keeps the section open (#1128 guard, #1387).
              disabled: membersPinned,
              testId: "fleet-members-toggle",
            }}
          />
        </CardContent>
      </Card>

      {membersOpen && (
      <div id="fleet-members-section-body" className="space-y-4">
      <FleetDefaultsBar
        fleet={fleet}
        members={members}
        organizations={organizations}
        canEdit={canEdit}
        saving={updateFleet.isPending}
        onSave={saveDefaults}
        onApplyPreset={() => setPresetTarget("defaults")}
        onDirtyChange={setDefaultsDirty}
      />

      {allRows.length > 0 && (
        <div className="flex flex-wrap items-center gap-2" data-testid="fleet-members-toolbar">
          <SearchInput
            toolbar
            placeholder={t_i18n("Search members...")}
            value={tableState.query}
            onChange={(query) => setTableState({ query })}
          />
          <Select value={tableState.role} onValueChange={(role) => setTableState({ role: role as MembersRoleFilter })}>
            <SelectTrigger className="h-8 w-[170px]" aria-label={t_i18n("Filter by role")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {MEMBERS_ROLE_FILTERS.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {t_i18n(option.label)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={tableState.state} onValueChange={(state) => setTableState({ state: state as MembersStateFilter })}>
            <SelectTrigger className="h-8 w-[220px]" aria-label={t_i18n("Filter by state")}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {MEMBERS_STATE_FILTERS.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {t_i18n(option.label)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {filtered && (
            <Button type="button" variant="ghost" size="sm" onClick={() => setTableState({ query: "", role: "all", state: "all" })}>
              {t_i18n("Clear filters")}
            </Button>
          )}
        </div>
      )}

      {canEdit && hasRows && (
        <ListControlBar
          page={0}
          pageSize={Math.max(rows.length, 1)}
          total={rows.length}
          onPageChange={() => {}}
          // Every member is on one page: no 1/1 pager, so the bulk actions keep one row at 1024 (UX round 2, B2).
          pager={false}
          selection={{
            hasVisibleRows: selectableIds.length > 0,
            allVisibleSelected: selectableIds.length > 0 && selection.size === selectableIds.length,
            selectedCount: selection.size,
            allMatchingSelected: false,
            totalMatching: selectableIds.length,
            canSelectAllMatching: false,
            onToggleVisible: () => (selection.size === selectableIds.length ? clear() : setSelected(new Set(selectableIds))),
            onSelectAllMatching: () => {},
            onClear: clear,
            bulkActions,
          }}
        />
      )}

      <Card>
        <CardContent className="p-0">
          {rows.length > 0 && (
          <table className="w-full table-fixed" aria-label={t_i18n("Members")}>
            <thead>
              <tr className="border-b border-elevation-subtle-soft text-left content-caption text-default-secondary">
                {canEdit && <th className="w-10 px-3 py-2" aria-label={t_i18n("Selection")} />}
                <TableSortHeader label="Member" sortKey="member" activeKey={tableState.sort} direction={tableState.direction} onSort={onSort} className="w-[200px] xl:w-[220px] px-3" />
                <TableSortHeader label="Role" sortKey="role" activeKey={tableState.sort} direction={tableState.direction} onSort={onSort} className="w-[110px] xl:w-[120px] px-3" />
                <th className="w-[170px] xl:w-[190px] px-3 py-2 font-medium">
                  <span className="inline-flex items-center gap-1">
                    {t_i18n(CONTEXT_INCLUSION_FIELD)}
                    <InfoTip content={t_i18n(NOT_APPLIED_YET)} label={t_i18n(NOT_APPLIED_YET)} />
                  </span>
                </th>
                <TableSortHeader
                  label={MEMBER_SETTING_LABELS.organization_scope}
                  sortKey="organizations"
                  activeKey={tableState.sort}
                  direction={tableState.direction}
                  onSort={onSort}
                  // Below xl the Collaboration instruction column is hidden: this column takes the room it frees (UX round 2, B1).
                  className="xl:w-[220px] px-3"
                />
                <th className="hidden xl:table-cell px-3 py-2 font-medium">{t_i18n(MEMBER_SETTING_LABELS.collaboration_instruction)}</th>
                <th className="hidden xl:table-cell w-[120px] px-3 py-2 font-medium">{t_i18n("Automations")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((member) => (
                <MemberRow
                  key={member.id}
                  member={member}
                  canEdit={canEdit}
                  selected={selection.has(member.id)}
                  organizations={organizations}
                  onEditScope={() => setScopeTarget({ kind: "row", member })}
                  enabledAutomations={member.agent_id ? enabledByAgent.get(member.agent_id) ?? 0 : 0}
                  onToggle={(range) => toggle(member.id, range)}
                  onOpenAgent={onOpenAgent}
                  onReset={(setting) => resetOne(member, setting)}
                  onSetContext={(value) => setOne(member, value)}
                  onOpenInstruction={() => setInstructionTarget(member.id)}
                />
              ))}
            </tbody>
          </table>
          )}
          {allRows.length > 0 && rows.length === 0 && (
            <EmptyState
              variant="inset"
              icon={Users}
              title="No member matches the filters"
              description="Change the search or the filters, or clear them to see every member."
            />
          )}
          {!hasConnected && !filtered && (
            <EmptyState
              variant="inset"
              icon={Boxes}
              title="No connected agent or fleet"
              description="Connect an agent or a fleet from the Graph view to set what it receives and how the fleet works with it."
            />
          )}
        </CardContent>
      </Card>
      </div>
      )}

      <MemberInstructionPanel
        fleet={fleet}
        member={instructionMember}
        canEdit={canEdit}
        saving={bulk.isPending}
        enabledAutomations={instructionMember?.agent_id ? enabledByAgent.get(instructionMember.agent_id) ?? 0 : 0}
        onClose={() => setInstructionTarget(null)}
        onSave={saveInstruction}
      />

      <OrganizationScopeDialog
        open={scopeTarget !== null}
        onOpenChange={(open) => !open && setScopeTarget(null)}
        scope={(scopeTarget?.kind === "row" ? scopeTarget.member.organization_scope : fleet.member_defaults?.organization_scope) ?? PRIMARY_SCOPE}
        organizations={organizations}
        fleetId={fleet.id}
        description={
          scopeTarget?.kind === "row"
            ? t_i18n("The organizations {name} reasons about.", { name: scopeTarget.member.name })
            : t_i18n("{count, plural, one {The organizations # selected member reasons about.} other {The organizations the # selected members reason about.}}", { count: selectedRows.length })
        }
        onApply={applyScope}
      />

      <ApplyPresetDialog
        open={presetTarget !== null}
        onOpenChange={(open) => !open && setPresetTarget(null)}
        presets={presets}
        loading={presetsLoading}
        organizations={organizations}
        description={
          presetTarget === "defaults"
            ? t_i18n("The preset's values become the fleet defaults; members that override a setting keep their value.")
            : t_i18n("{count, plural, one {The preset's values are written on # selected member; a home member takes its organizations only.} other {The preset's values are written on the # selected members; a home member takes its organizations only.}}", { count: selectedRows.length })
        }
        onApply={applyPreset}
      />

      <Dialog open={contextDialog} onOpenChange={setContextDialog}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t_i18n(CONTEXT_INCLUSION_FIELD)}</DialogTitle>
            <DialogDescription>
              {t_i18n("{count, plural, one {What # selected member receives from the calling conversation.} other {What the # selected members receive from the calling conversation.}}", { count: selectedRows.length })}
            </DialogDescription>
          </DialogHeader>
          <form
            className="space-y-4"
            onSubmit={(e) => {
              e.preventDefault();
              setContextDialog(false);
              applyBulk(selectedRows, { context_inclusion: contextValue }, "{count, plural, one {Conversation shared set for # member} other {Conversation shared set for # members}}");
            }}
          >
            <ContextSelect value={contextValue} onChange={setContextValue} className="w-full" />
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={() => setContextDialog(false)}>
                {t_i18n("Cancel")}
              </Button>
              <Button type="submit">{t_i18n("Apply")}</Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/**
 * The type scale of the context value. The design-system SelectValue sets its
 * own `text-content-base`, so the scale goes on the value itself: a class on the
 * trigger does not reach it. A members row is compact; the dialog and the
 * defaults bar keep the base scale.
 */
const CONTEXT_VALUE_SCALE_CLASS = {
  base: undefined,
  compact: "font-content-compact text-content-compact leading-content-compact tracking-content-compact",
} as const;

export function ContextSelect({
  value,
  onChange,
  disabled,
  className,
  scale = "base",
  label,
}: {
  value: FleetContextInclusion;
  onChange: (value: FleetContextInclusion) => void;
  disabled?: boolean;
  className?: string;
  /** Type scale of the value element: "compact" in a members row, "base" elsewhere. */
  scale?: keyof typeof CONTEXT_VALUE_SCALE_CLASS;
  label?: string;
}) {
  const { t_i18n } = useFormatter();
  return (
    <Select value={value} disabled={disabled} onValueChange={(next) => onChange(next as FleetContextInclusion)}>
      <SelectTrigger className={className} aria-label={label ?? t_i18n(CONTEXT_INCLUSION_FIELD)}>
        <SelectValue className={CONTEXT_VALUE_SCALE_CLASS[scale]} data-part="context-value" data-scale={scale} />
      </SelectTrigger>
      <SelectContent>
        {CONTEXT_INCLUSIONS.map((option) => (
          <SelectItem key={option.value} value={option.value}>
            {t_i18n(option.label)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

interface FleetDefaultsBarProps {
  fleet: Fleet;
  members: FleetMember[];
  organizations: readonly Organization[];
  canEdit: boolean;
  saving: boolean;
  onSave: (patch: FleetMembersBulkSet, previous: FleetMembersBulkSet) => void;
  /** Opens "Apply a preset" on the fleet defaults. */
  onApplyPreset: () => void;
  /** Told whether the form holds an unsaved edit, so the Members section stays open over it (#1387). */
  onDirtyChange?: (dirty: boolean) => void;
}

/**
 * The fleet defaults on top of the table: what a connected member inherits, and what a save rewrites.
 * The form is keyed on the stored defaults, so a change written elsewhere (a preset applied, an Undo)
 * re-reads the server values instead of keeping the edited ones as a false dirty state
 * (UX round 1 of #9422, B1).
 */
export function FleetDefaultsBar(props: FleetDefaultsBarProps) {
  // Open or folded is the Members section's (#1387): the form shows whole once the section is open.
  return <FleetDefaultsForm key={JSON.stringify(props.fleet.member_defaults ?? null)} {...props} />;
}

/** The folded Members bar's line: the member counts, then the current fleet defaults (#1133, #1387). */
function MembersSectionSummary({ fleet, members, organizations }: { fleet: Fleet; members: FleetMember[]; organizations: readonly Organization[] }) {
  const { t_i18n } = useFormatter();
  const counts = membersCounts(members);
  const context = (fleet.member_defaults?.context_inclusion ?? "none") as FleetContextInclusion;
  const scope = scopeTags(fleet.member_defaults?.organization_scope ?? PRIMARY_SCOPE, organizations)
    .map((tag) => (tag.translate ? t_i18n(tag.text) : tag.text))
    .join(", ");
  return (
    <span className="min-w-0 content-compact text-default-secondary" data-testid="fleet-members-summary">
      {/* One ICU message for both counts, so each locale joins them its own way (UX r1 A1 of #1393). */}
      {t_i18n(
        "{home, plural, =0 {No home member} one {# home member} other {# home members}}, {connected, plural, =0 {no connected member} one {# connected} other {# connected}}",
        counts,
      )}
      {" · "}
      <span data-testid="fleet-defaults-summary">
        {t_i18n("Context: {value}", { value: t_i18n(CONTEXT_INCLUSION_SHORT_LABELS[context]) })}
        {" · "}
        {t_i18n("Scope: {value}", { value: scope })}
        {" · "}
        {t_i18n(DEFAULTS_INSTRUCTION_SUMMARY[instructionSummaryKey(fleet.member_defaults?.collaboration_instruction ?? "")])}
      </span>
    </span>
  );
}

function FleetDefaultsForm({
  fleet,
  members,
  organizations,
  canEdit,
  saving,
  onSave,
  onApplyPreset,
  onDirtyChange,
}: FleetDefaultsBarProps) {
  const { t_i18n } = useFormatter();
  const [scopeDialog, setScopeDialog] = useState(false);
  const currentScope = fleet.member_defaults?.organization_scope ?? PRIMARY_SCOPE;
  const scopeImpact = defaultImpact(members, "organization_scope");
  const current = {
    context_inclusion: (fleet.member_defaults?.context_inclusion ?? "none") as FleetContextInclusion,
    collaboration_instruction: fleet.member_defaults?.collaboration_instruction ?? "",
  };
  const [context, setContext] = useState<FleetContextInclusion>(current.context_inclusion);
  const [instruction, setInstruction] = useState(current.collaboration_instruction);
  const contextImpact = defaultImpact(members, "context_inclusion");
  const instructionImpact = defaultImpact(members, "collaboration_instruction");
  const dirty: FleetMembersBulkSet = {};
  if (context !== current.context_inclusion) dirty.context_inclusion = context;
  if (instruction !== current.collaboration_instruction) dirty.collaboration_instruction = instruction;
  const changed = Object.keys(dirty) as (keyof typeof current)[];
  const impact = changed.includes("context_inclusion") ? contextImpact : instructionImpact;
  // An edit in progress keeps the Members section open, so a fold never hides unsaved values (#1128, #1387).
  const isDirty = changed.length > 0;
  useEffect(() => {
    onDirtyChange?.(isDirty);
  }, [isDirty, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);

  return (
    <Card data-testid="fleet-defaults-bar">
      <CardContent className="space-y-3 p-4">
        {/* The shared fleet section header (UX r1 B1 of #1393); no fold of its own inside the Members section.
            The description wraps under the title when the row is short, never cut (UX round 2 of #9422, B2). */}
        <FleetFoldHeader
          icon="sliders-horizontal"
          title={t_i18n("Fleet defaults")}
          summary={<span className="min-w-0 content-compact text-default-secondary">{t_i18n("What a member inherits while it does not override the setting")}</span>}
          action={
            canEdit && (
              <Button type="button" variant="outline" size="sm" className="gap-1.5" disabled={saving} onClick={onApplyPreset}>
                <Layers className="h-3.5 w-3.5" aria-hidden />
                {t_i18n("Apply a preset")}
              </Button>
            )
          }
        />
        <div id="fleet-defaults-form" className="space-y-3">
        <div className="grid gap-3 md:grid-cols-[220px_minmax(0,1fr)_minmax(0,260px)]">
          <div className="space-y-1.5">
            <Label className={DEFAULTS_LABEL_CLASS}>
              {t_i18n(CONTEXT_INCLUSION_FIELD)}
              <InfoTip content={t_i18n(NOT_APPLIED_YET)} label={t_i18n(NOT_APPLIED_YET)} />
            </Label>
            <ContextSelect value={context} onChange={setContext} disabled={!canEdit} className="w-full" label={t_i18n("Fleet default: {field}", { field: t_i18n(CONTEXT_INCLUSION_FIELD) })} />
            <p className={FIELD_HELP_CLASS}>{t_i18n(DEFAULTS_HELP.context)}</p>
          </div>
          <div className="min-w-0 space-y-1.5">
            <Label htmlFor="fleet-default-instruction" className={DEFAULTS_LABEL_CLASS}>
              {t_i18n(MEMBER_SETTING_LABELS.collaboration_instruction)}
            </Label>
            <Input
              id="fleet-default-instruction"
              aria-label={t_i18n(MEMBER_SETTING_LABELS.collaboration_instruction)}
              aria-describedby="fleet-default-instruction-help"
              value={instruction}
              maxLength={4000}
              disabled={!canEdit}
              placeholder={t_i18n("When and how the fleet works with its connected members (optional)")}
              onChange={(e) => setInstruction(e.target.value)}
            />
            <p id="fleet-default-instruction-help" className={FIELD_HELP_CLASS}>
              {t_i18n(DEFAULTS_HELP.instruction)}
            </p>
          </div>
          <div className="min-w-0 space-y-1.5" data-testid="fleet-default-organizations">
            <Label className={DEFAULTS_LABEL_CLASS}>{t_i18n(MEMBER_SETTING_LABELS.organization_scope)}</Label>
            {/* The control area is the 36 px of the two fields beside it, so the three read as one row. */}
            <span className="flex h-9 min-w-0 items-center gap-1">
              <ScopeTags scope={currentScope} organizations={organizations} />
              {canEdit && (
                <Tip content={t_i18n("Fleet default: {field}", { field: t_i18n(MEMBER_SETTING_LABELS.organization_scope) })}>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="ml-auto h-8 w-8 shrink-0"
                    aria-label={t_i18n("Fleet default: {field}", { field: t_i18n(MEMBER_SETTING_LABELS.organization_scope) })}
                    disabled={saving}
                    onClick={() => setScopeDialog(true)}
                  >
                    <Pencil className="h-3.5 w-3.5" aria-hidden />
                  </Button>
                </Tip>
              )}
            </span>
            <p className={FIELD_HELP_CLASS}>{t_i18n(SCOPE_HELP)}</p>
          </div>
        </div>
        <OrganizationScopeDialog
          open={scopeDialog}
          onOpenChange={setScopeDialog}
          scope={currentScope}
          organizations={organizations}
          fleetId={fleet.id}
          description={t_i18n("{applies, plural, one {Applies to # member} other {Applies to # members}}, {keep, plural, one {# keeps its override} other {# keep their override}}", scopeImpact)}
          onApply={(scope) => {
            setScopeDialog(false);
            if (!sameScope(scope, currentScope)) onSave({ organization_scope: scope }, { organization_scope: currentScope });
          }}
        />
        {canEdit && changed.length > 0 && (
          <div className="flex flex-wrap items-center justify-end gap-2">
            <span className="content-caption text-default-secondary" data-testid="fleet-defaults-impact">
              {t_i18n("{applies, plural, one {Applies to # member} other {Applies to # members}}, {keep, plural, one {# keeps its override} other {# keep their override}}", impact)}
            </span>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => {
                setContext(current.context_inclusion);
                setInstruction(current.collaboration_instruction);
              }}
            >
              {t_i18n("Cancel")}
            </Button>
            <Button
              type="button"
              size="sm"
              disabled={saving}
              onClick={() => {
                const previous: FleetMembersBulkSet = {};
                for (const field of changed) previous[field] = current[field] as never;
                onSave(dirty, previous);
              }}
            >
              {t_i18n("Save defaults")}
            </Button>
          </div>
        )}
        </div>
      </CardContent>
    </Card>
  );
}

/** A scope as tags: the monitored set as one tag, else the primary first, then each listed organization. */
function ScopeTags({ scope, organizations }: { scope: FleetOrganizationScope | undefined; organizations: readonly Organization[] }) {
  const { t_i18n } = useFormatter();
  const tags = scopeTags(scope, organizations).map((tag) => (tag.translate ? t_i18n(tag.text) : tag.text));
  // Two badges at most and never a cut name: the second folds into "+N" (its tooltip names it) when both do not fit whole;
  // the primary stays shown (UX round 1 and round 2, B1).
  return <TagList tags={tags} max={2} badgeMin="whole" className="min-w-0" />;
}

function SettingCaption({
  overridden,
  setting,
  canEdit,
  onReset,
}: {
  overridden: boolean;
  setting: FleetMemberSetting;
  canEdit: boolean;
  onReset: () => void;
}) {
  const { t_i18n } = useFormatter();
  const resetLabel = t_i18n(RESET_SETTING_LABELS[setting]);
  if (!overridden) return <span className="block truncate content-caption text-default-secondary">{t_i18n("Fleet default")}</span>;
  return (
    <span className="flex items-center gap-1 content-caption text-default-secondary">
      <span className="truncate">{t_i18n("Overridden")}</span>
      {canEdit && (
        <Tip content={resetLabel}>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="h-7 w-7 shrink-0"
            aria-label={resetLabel}
            onClick={onReset}
          >
            <RotateCcw className="h-3.5 w-3.5" aria-hidden />
          </Button>
        </Tip>
      )}
    </span>
  );
}

function MemberRow({
  member,
  canEdit,
  selected,
  organizations,
  onEditScope,
  enabledAutomations,
  onToggle,
  onOpenAgent,
  onReset,
  onSetContext,
  onOpenInstruction,
}: {
  member: FleetMember;
  canEdit: boolean;
  selected: boolean;
  organizations: readonly Organization[];
  onEditScope: () => void;
  enabledAutomations: number;
  onToggle: (range: boolean) => void;
  onOpenAgent: (agentId: string) => void;
  onReset: (setting: FleetMemberSetting) => void;
  onSetContext: (value: FleetContextInclusion) => void;
  /** Opens the row's instruction panel (R5). */
  onOpenInstruction: () => void;
}) {
  const { t_i18n } = useFormatter();
  const configurable = isConfigurable(member);
  const visible = isVisible(member);
  const hidden = !visible;
  const name = hidden ? t_i18n("An agent not shared with you") : member.name;
  const role = member.is_orchestrator ? "Orchestrator" : member.role === "connected" ? "Connected" : "Home member";
  const contextLabel = t_i18n(MEMBER_SETTING_LABELS.context_inclusion);
  const scopeLabel = t_i18n(MEMBER_SETTING_LABELS.organization_scope);
  const instruction = firstLine(member.collaboration_instruction);
  const instructionOverridden = configurable && isOverridden(member, "collaboration_instruction");
  const openLabel = t_i18n("Open {name} settings", { name });

  return (
    <tr
      className={cn("border-b border-elevation-subtle-soft last:border-b-0 align-top", selected && "bg-elevation-highlight/[0.06]")}
      data-testid="fleet-members-row"
    >
      {canEdit && (
        <td className="px-3 py-2">
          {visible ? (
            <span
              onClickCapture={(e) => {
                if (e.shiftKey) {
                  e.stopPropagation();
                  e.preventDefault();
                  onToggle(true);
                }
              }}
              onKeyDownCapture={(e) => {
                if (e.key === " " && e.shiftKey) {
                  e.preventDefault();
                  e.stopPropagation();
                  onToggle(true);
                }
              }}
            >
              <SelectionBox checked={selected} onToggle={() => onToggle(false)} label={t_i18n("Select {name}", { name })} />
            </span>
          ) : null}
        </td>
      )}
      <td className="min-w-0 px-3 py-2">
        <span className="flex min-w-0 items-center gap-1 content-compact font-medium">
          {member.is_orchestrator && <Crown className="h-3 w-3 shrink-0 text-accent" aria-label={t_i18n("Orchestrator")} />}
          {member.target_fleet_id && <Boxes className="h-3 w-3 shrink-0" aria-label={t_i18n("Fleet")} />}
          {member.agent_id && !hidden ? (
            <Tip content={name}>
              <button
                type="button"
                className="min-w-0 truncate rounded-sm text-left hover:underline outline-none focus-visible:ring-2 focus-visible:ring-focus"
                onClick={() => onOpenAgent(member.agent_id as string)}
              >
                {name}
              </button>
            </Tip>
          ) : (
            <span className={cn("min-w-0 truncate", hidden && "text-default-secondary")} title={name}>
              {name}
            </span>
          )}
          {configurable && (
            // Below xl the instruction and automations columns fold into the row's panel (10.5, 1024 table).
            <Tip content={openLabel}>
              <Button type="button" variant="ghost" size="icon" className="ml-auto h-7 w-7 shrink-0 xl:hidden" aria-label={openLabel} onClick={onOpenInstruction}>
                <PanelRight className="h-3.5 w-3.5" aria-hidden />
              </Button>
            </Tip>
          )}
        </span>
        {instructionOverridden && (
          <span className="block truncate content-caption text-default-secondary xl:hidden">{t_i18n("Instruction overridden")}</span>
        )}
      </td>
      <td className="px-3 py-2 content-compact">
        <span className="block truncate">{t_i18n(role)}</span>
      </td>
      <td className="min-w-0 px-3 py-2">
        {configurable ? (
          <>
            {canEdit ? (
              <ContextSelect
                value={member.context_inclusion}
                onChange={onSetContext}
                // -mt-[5px]: the 28 px trigger centres its 18 px line (5 px above it), the
                // static cells of the row start at the top; this puts both lines on one baseline.
                className="-mt-[5px] h-7 w-full"
                scale="compact"
                label={t_i18n("{field} for {name}", { field: contextLabel, name })}
              />
            ) : (
              <span className="block truncate content-compact">{t_i18n(CONTEXT_INCLUSION_SHORT_LABELS[member.context_inclusion])}</span>
            )}
            <SettingCaption overridden={isOverridden(member, "context_inclusion")} setting="context_inclusion" canEdit={canEdit} onReset={() => onReset("context_inclusion")} />
          </>
        ) : (
          <span className="block truncate content-compact text-default-secondary">{hidden ? null : t_i18n("Not applicable")}</span>
        )}
      </td>
      <td className="min-w-0 px-3 py-2" data-testid="fleet-members-organizations">
        {visible ? (
          <>
            <span className="flex min-w-0 items-center gap-1">
              <ScopeTags scope={member.organization_scope} organizations={organizations} />
              {canEdit && (
                <Tip content={t_i18n("{field} for {name}", { field: scopeLabel, name })}>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="ml-auto h-7 w-7 shrink-0"
                    aria-label={t_i18n("{field} for {name}", { field: scopeLabel, name })}
                    onClick={onEditScope}
                  >
                    <Pencil className="h-3.5 w-3.5" aria-hidden />
                  </Button>
                </Tip>
              )}
            </span>
            <SettingCaption overridden={isOverridden(member, "organization_scope")} setting="organization_scope" canEdit={canEdit} onReset={() => onReset("organization_scope")} />
          </>
        ) : null}
      </td>
      <td className="hidden xl:table-cell min-w-0 px-3 py-2">
        {configurable ? (
          <>
            <Tip content={member.collaboration_instruction || t_i18n("No instruction")}>
              {/* The cell shows the first line; a click opens the editor in the row's panel (10.5, B2). */}
              <button
                type="button"
                aria-label={t_i18n("{field} for {name}", { field: t_i18n(MEMBER_SETTING_LABELS.collaboration_instruction), name })}
                className="block w-full truncate rounded-sm text-left content-compact hover:underline outline-none focus-visible:ring-2 focus-visible:ring-focus"
                onClick={onOpenInstruction}
              >
                {instruction || <span className="text-default-secondary">{t_i18n("No instruction")}</span>}
              </button>
            </Tip>
            <SettingCaption overridden={instructionOverridden} setting="collaboration_instruction" canEdit={canEdit} onReset={() => onReset("collaboration_instruction")} />
          </>
        ) : (
          <span className="block truncate content-compact text-default-secondary">{hidden ? null : t_i18n("Not applicable")}</span>
        )}
      </td>
      <td className="hidden xl:table-cell px-3 py-2 content-compact">
        {enabledAutomations > 0 ? (
          <span className="block truncate">{t_i18n("{count, plural, one {# enabled} other {# enabled}}", { count: enabledAutomations })}</span>
        ) : (
          <span className="block truncate text-default-secondary">{t_i18n("None")}</span>
        )}
      </td>
    </tr>
  );
}
