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
 * The fleet page's members panel, editable.
 *
 * Home members are listed as the fleet's hierarchy (orchestrator first, each
 * member under its parent) with their health; connected agents and fleets
 * follow with their collaboration instruction and the context they receive.
 *
 * Who may edit (`can_manage`: owner, `config` grantee, admin): add a home
 * member (agents they manage with no home fleet, as the "New fleet" dialog),
 * remove one, move it to another fleet, change its parent (drag a row onto
 * another, onto "Top level", or the row menu's parent picker for the
 * keyboard), set its role label, expose it to other fleets; connect an agent or a fleet, edit
 * its instruction and context, remove it. Everyone else reads the panel with
 * the reason. Every write goes through `/api/v1/fleets`; membership is never
 * an agent edit.
 */
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRightLeft, Boxes, Crown, Eye, EyeOff, GitBranch, Lock, SlidersHorizontal, Tag, Trash2, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { agentSelectOption } from "@/components/ui/entity-select-options";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import {
  useAddFleetMember,
  useFleets,
  useMoveFleetMember,
  useSetFleetExposure,
  useUpdateFleetMember,
  type Fleet,
  type FleetContextInclusion,
  type FleetMember,
  type FleetMemberHealth,
  type FleetMemberHealthStatus,
} from "@/hooks/api/useFleetApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { healthBadge, healthReasons, staleWorkItemsHref } from "@/lib/fleet-graph";
import { canManageEntity, cn, timeAgo } from "@/lib/utils";
import type { Agent } from "@/types";

import { CONTEXT_INCLUSION_FIELD, CONTEXT_INCLUSIONS } from "./context-inclusion";
import { FleetMemberCaption } from "./fleet-member-caption";
import { homeMemberCandidates } from "./new-fleet-form";
import {
  canReparent,
  connectableAgentIds,
  connectableFleetIds,
  connectedRows,
  fleetEditRights,
  memberTree,
  moveTargets,
  toggledExposure,
} from "./fleet-edition";
import { ChangeParentDialog } from "./fleet-member-actions";
import { fleetRefusalMessage } from "./fleet-refusals";
import { useRemoveFleetMemberAction } from "./useRemoveFleetMemberAction";
import { useReparentFleetMember } from "./useReparentFleetMember";

const STATUS_VARIANT: Record<FleetMemberHealthStatus, "destructive" | "warning" | "secondary" | "outline" | "success"> = {
  failing: "destructive",
  stale: "warning",
  disabled: "secondary",
  idle: "outline",
  ok: "success",
};

/** The drop target "Top level": a member dropped there has no parent. */
const ROOT_DROP = "__root__";

// "Change parent" is the dialog the graph's node menu opens too (`ChangeParentDialog`).
type MemberDialogKind = "move" | "label";
type MemberDialog = { kind: MemberDialogKind; member: FleetMember } | null;

const DIALOG_TITLE: Record<MemberDialogKind, string> = {
  move: "Move to another fleet",
  label: "Role in the fleet",
};

export function FleetMembersPanel({
  fleet,
  members,
  health,
  agents,
  onOpenAgent,
  onConfigureMembers,
}: {
  fleet: Fleet;
  members: readonly FleetMember[];
  health: readonly FleetMemberHealth[];
  agents: readonly Agent[];
  onOpenAgent: (agentId: string) => void;
  /** Opens the Members view, where each member's settings are set. */
  onConfigureMembers?: () => void;
}) {
  const { t_i18n, locale } = useFormatter();
  const { toast } = useToast();
  const { user, isAdmin } = useAuth();
  const { members: rights } = fleetEditRights(fleet);
  const canEdit = rights.canEdit;
  const addMember = useAddFleetMember(fleet.id);
  const updateMember = useUpdateFleetMember(fleet.id);
  const { confirmElement, removeMember } = useRemoveFleetMemberAction(fleet.id);
  const moveMember = useMoveFleetMember(fleet.id);
  const setExposure = useSetFleetExposure(fleet.id);
  const { data: fleets } = useFleets({ enabled: canEdit, optional: true });
  const [dragged, setDragged] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  const [dialog, setDialog] = useState<MemberDialog>(null);
  const [dialogValue, setDialogValue] = useState<string | null>(null);
  const [parentMember, setParentMember] = useState<FleetMember | null>(null);

  const healthByAgent = useMemo(() => new Map(health.map((h) => [h.agent_id, h])), [health]);
  const agentById = useMemo(() => new Map(agents.map((a) => [a.id, a])), [agents]);
  const tree = useMemo(() => memberTree(members), [members]);
  const connected = useMemo(() => connectedRows(members), [members]);
  const fleetName = useMemo(() => new Map((fleets ?? []).map((f) => [f.id, f.name])), [fleets]);
  const candidates = useMemo(
    () => homeMemberCandidates(agents, (a) => canManageEntity(a, user?.id, isAdmin)),
    [agents, user?.id, isAdmin],
  );
  const connectable = useMemo(() => {
    const ids = new Set(connectableAgentIds(agents.map((a) => a.id), members));
    return agents.filter((a) => ids.has(a.id));
  }, [agents, members]);
  const connectableFleets = useMemo(() => {
    const ids = new Set(connectableFleetIds((fleets ?? []).map((f) => f.id), fleet.id, members));
    return (fleets ?? []).filter((f) => ids.has(f.id) && !f.is_archived);
  }, [fleets, fleet.id, members]);
  const targets = useMemo(() => moveTargets(fleets ?? [], fleet.id), [fleets, fleet.id]);

  const fail = (title: string) => (err: unknown) =>
    toast({ title: t_i18n(title), description: fleetRefusalMessage(err, t_i18n), variant: "error" });

  // The one re-parent of the page: the row drag, the dialog and the graph drag.
  const reparent = useReparentFleetMember(fleet.id, members);

  const toggleExposed = (member: FleetMember) => {
    if (!member.agent_id) return;
    const next = toggledExposure(fleet.exposed_agent_ids, member.agent_id, fleet.orchestrator_agent_id);
    setExposure.mutate(next, {
      onSuccess: () =>
        toast({
          title: next.includes(member.agent_id as string) ? t_i18n("Exposed to other fleets") : t_i18n("No longer exposed"),
          variant: "success",
        }),
      onError: fail("The exposure was not updated"),
    });
  };

  // Asks first, as the graph's node menu does.
  const remove = (member: FleetMember) => void removeMember(member);

  const openDialog = (kind: MemberDialogKind, member: FleetMember) => {
    setDialog({ kind, member });
    setDialogValue(kind === "label" ? member.role_label : null);
  };

  const applyDialog = () => {
    if (!dialog) return;
    if (dialog.kind === "label") {
      const label = (dialogValue ?? "").trim();
      if (label !== dialog.member.role_label) {
        updateMember.mutate(
          { memberId: dialog.member.id, patch: { role_label: label } },
          { onSuccess: () => toast({ title: t_i18n("Role updated"), variant: "success" }), onError: fail("The role was not updated") },
        );
      }
    } else if (dialogValue) {
      moveMember.mutate(
        { member: dialog.member, targetFleetId: dialogValue },
        {
          onSuccess: () =>
            toast({ title: t_i18n("Moved to {fleet}", { fleet: fleetName.get(dialogValue) ?? "" }), variant: "success" }),
          // One transaction on the server: a refusal leaves the member where it was.
          onError: fail("The member was not moved"),
        },
      );
    }
    setDialog(null);
  };

  const rowMenu = (member: FleetMember): OverflowMenuItem[] => {
    const exposed = !!member.agent_id && fleet.exposed_agent_ids.includes(member.agent_id);
    return [
      { id: "parent", label: t_i18n("Change parent..."), icon: GitBranch, onSelect: () => setParentMember(member) },
      { id: "label", label: t_i18n("Set the role..."), icon: Tag, onSelect: () => openDialog("label", member) },
      ...(member.is_orchestrator
        ? []
        : [
            {
              id: "expose",
              label: exposed ? t_i18n("Stop exposing to other fleets") : t_i18n("Expose to other fleets"),
              icon: exposed ? EyeOff : Eye,
              onSelect: () => toggleExposed(member),
            },
          ]),
      {
        id: "move",
        label: t_i18n("Move to another fleet..."),
        icon: ArrowRightLeft,
        onSelect: () => openDialog("move", member),
        disabled: targets.length === 0,
      },
      { id: "remove", label: t_i18n("Remove from the fleet"), icon: Trash2, onSelect: () => remove(member), destructive: true, separatorBefore: true },
    ];
  };

  const onDrop = (targetId: string) => {
    const member = members.find((m) => m.id === dragged);
    setDragged(null);
    setDropTarget(null);
    if (member) reparent(member, targetId === ROOT_DROP ? null : targetId);
  };

  const dropProps = (targetId: string) =>
    canEdit && dragged && canReparent(members, dragged, targetId === ROOT_DROP ? null : targetId)
      ? {
          onDragOver: (e: React.DragEvent) => {
            e.preventDefault();
            setDropTarget(targetId);
          },
          onDragLeave: () => setDropTarget((t) => (t === targetId ? null : t)),
          onDrop: (e: React.DragEvent) => {
            e.preventDefault();
            onDrop(targetId);
          },
        }
      : {};

  const visibleHealthCount = tree.filter((r) => r.member.agent_id && agentById.has(r.member.agent_id)).length;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between gap-2 space-y-0 pb-2">
        <CardTitle className="content-base">{t_i18n("Members")}</CardTitle>
        {onConfigureMembers && (
          <Button type="button" variant="ghost" size="sm" className="h-7 gap-1 px-2" onClick={onConfigureMembers}>
            <SlidersHorizontal className="h-3.5 w-3.5" aria-hidden />
            <span className="content-compact">{t_i18n("Configure members")}</span>
          </Button>
        )}
      </CardHeader>
      <CardContent className="space-y-4 pt-0">
        {!canEdit && (
          <p role="note" className="flex items-start gap-2 content-caption text-default-secondary">
            <Lock className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
            {t_i18n(rights.reason)}
          </p>
        )}

        {tree.length === 0 ? (
          <p className="content-caption text-default-secondary">{t_i18n("No member yet")}</p>
        ) : visibleHealthCount === 0 ? (
          <p className="content-caption text-default-secondary">{t_i18n("The members of this fleet are not shared with you")}</p>
        ) : (
          <ul className="space-y-0.5" aria-label={t_i18n("Home members")}>
            {tree.map(({ member, depth }) => {
              const agentId = member.agent_id as string;
              if (!agentById.has(agentId)) return null;
              const h = healthByAgent.get(agentId);
              const exposed = fleet.exposed_agent_ids.includes(agentId);
              // The health parts of the caption; FleetMemberCaption renders the visible line and
              // its title from them, so the two never drift.
              const healthParts = [
                h && (h.last_run_at ? t_i18n("Last run {when}", { when: timeAgo(h.last_run_at, locale) }) : t_i18n("No run yet")),
                h && h.runs_in_flight > 0 && t_i18n("{count} in flight", { count: h.runs_in_flight }),
                h && h.open_work_items > 0 && t_i18n("{count, plural, one {# open item} other {# open items}}", { count: h.open_work_items }),
              ].filter((part): part is string => typeof part === "string" && part !== "");
              return (
                <li
                  key={member.id}
                  draggable={canEdit}
                  onDragStart={(e) => {
                    setDragged(member.id);
                    e.dataTransfer.effectAllowed = "move";
                  }}
                  onDragEnd={() => {
                    setDragged(null);
                    setDropTarget(null);
                  }}
                  {...dropProps(member.id)}
                  className={cn(
                    "flex items-center gap-1 rounded-sm",
                    dropTarget === member.id && "ring-2 ring-focus",
                    dragged === member.id && "opacity-50",
                  )}
                  style={{ paddingLeft: depth * 16 }}
                >
                  <button
                    type="button"
                    onClick={() => onOpenAgent(agentId)}
                    className="flex min-w-0 flex-1 items-center gap-2 rounded-sm px-2 py-1.5 text-left hover:bg-elevation-hover transition-colors outline-none focus-visible:ring-2 focus-visible:ring-focus"
                  >
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-1 truncate content-compact font-medium">
                        {member.is_orchestrator && <Crown className="h-3 w-3 shrink-0 text-accent" aria-label={t_i18n("Orchestrator")} />}
                        <span className="min-w-0 truncate" title={member.name}>{member.name}</span>
                        {exposed && !member.is_orchestrator && (
                          <Eye className="h-3 w-3 shrink-0 text-default-secondary" aria-label={t_i18n("Exposed to other fleets")} />
                        )}
                      </span>
                      {/* The name owns the first line; the role label leads the second, so a long
                          label never squeezes the name in the narrow panel. */}
                      <FleetMemberCaption roleLabel={member.role_label} parts={healthParts} />
                    </span>
                  </button>
                  {h && (() => {
                    const label = healthBadge(h);
                    const badge = (
                      <Badge variant={STATUS_VARIANT[h.status]} className="content-caption shrink-0">
                        {t_i18n(label.key, label.values)}
                      </Badge>
                    );
                    const reasons = healthReasons(h);
                    // No reason line (an OK member with only runs in flight): no tooltip, never an empty one.
                    if (reasons.length === 0) return badge;
                    // The badge sits beside the row button, not inside it, so it can be its own
                    // tab stop: Tab reaches it and its tooltip opens on focus.
                    return (
                      <Tip
                        content={
                          <span className="block space-y-0.5" data-testid="fleet-member-health-reasons">
                            {reasons.map((r) => (
                              <span key={r.key} className="block">{t_i18n(r.key, r.values)}</span>
                            ))}
                          </span>
                        }
                      >
                        {h.status === "stale" ? (
                          // Stale items: the badge opens them, filtered on this owner.
                          <Link
                            to={staleWorkItemsHref(agentId)}
                            data-testid="fleet-member-health-badge"
                            // The accessible name starts with the visible badge text (WCAG 2.5.3).
                            aria-label={t_i18n("{count, plural, one {# stale item} other {# stale items}}: open them for {name}", {
                              count: h.stale_work_items,
                              name: member.name,
                            })}
                            className="inline-flex shrink-0 rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus"
                          >
                            {badge}
                          </Link>
                        ) : (
                          <span
                            tabIndex={0}
                            data-testid="fleet-member-health-badge"
                            className="inline-flex shrink-0 rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus"
                          >
                            {badge}
                          </span>
                        )}
                      </Tip>
                    );
                  })()}
                  {canEdit && <OverflowMenu items={rowMenu(member)} label={t_i18n("Actions for {name}", { name: member.name })} />}
                </li>
              );
            })}
            {canEdit && dragged && canReparent(members, dragged, null) && (
              <li
                {...dropProps(ROOT_DROP)}
                className={cn(
                  "rounded-sm border border-dashed border-elevation-subtle px-2 py-1.5 content-caption text-default-secondary",
                  dropTarget === ROOT_DROP && "ring-2 ring-focus",
                )}
              >
                {t_i18n("Drop here: top level")}
              </li>
            )}
          </ul>
        )}

        {canEdit && (
          <SearchableSelect
            aria-label={t_i18n("Add a member")}
            options={candidates.map((a) => agentSelectOption(a))}
            value={null}
            onValueChange={(value) =>
              value &&
              addMember.mutate(
                { role: "member", agent_id: value },
                { onSuccess: () => toast({ title: t_i18n("Member added"), variant: "success" }), onError: fail("The member was not added") },
              )
            }
            placeholder={t_i18n("Add a member...")}
            emptyMessage={t_i18n("No agent without a fleet that you can manage")}
          />
        )}

        <div className="space-y-2 border-t border-elevation-subtle pt-3">
          <h3 className="content-compact font-medium">{t_i18n("Connected agents and fleets")}</h3>
          {connected.length === 0 && (
            <p className="content-caption text-default-secondary">{t_i18n("No connected agent or fleet")}</p>
          )}
          {connected.map((row) => (
            <ConnectedRow
              key={row.id}
              row={row}
              canEdit={canEdit}
              fleetName={row.target_fleet_id ? fleetName.get(row.target_fleet_id) ?? row.name : null}
              onOpenAgent={onOpenAgent}
              onSave={(patch) =>
                updateMember.mutate(
                  { memberId: row.id, patch },
                  { onSuccess: () => toast({ title: t_i18n("Connection updated"), variant: "success" }), onError: fail("The connection was not updated") },
                )
              }
              onRemove={() => remove(row)}
            />
          ))}
          {canEdit && (
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-1">
              <SearchableSelect
                aria-label={t_i18n("Connect an agent")}
                options={connectable.map((a) => agentSelectOption(a))}
                value={null}
                onValueChange={(value) =>
                  value &&
                  addMember.mutate(
                    { role: "connected", agent_id: value, collaboration_instruction: "", context_inclusion: "none" },
                    { onSuccess: () => toast({ title: t_i18n("Agent connected"), variant: "success" }), onError: fail("The agent was not connected") },
                  )
                }
                placeholder={t_i18n("Connect an agent...")}
              />
              <SearchableSelect
                aria-label={t_i18n("Connect a fleet")}
                options={connectableFleets.map((f) => ({ value: f.id, label: f.name }))}
                value={null}
                onValueChange={(value) =>
                  value &&
                  addMember.mutate(
                    { role: "connected", target_fleet_id: value, collaboration_instruction: "", context_inclusion: "none" },
                    { onSuccess: () => toast({ title: t_i18n("Fleet connected"), variant: "success" }), onError: fail("The fleet was not connected") },
                  )
                }
                placeholder={t_i18n("Connect a fleet...")}
                emptyMessage={t_i18n("No other fleet to connect")}
              />
            </div>
          )}
        </div>
      </CardContent>

      <Dialog open={!!dialog} onOpenChange={(next) => !next && setDialog(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{dialog ? t_i18n(DIALOG_TITLE[dialog.kind]) : ""}</DialogTitle>
            <DialogDescription>
              {dialog?.kind === "label"
                ? t_i18n("What {name} does in this fleet. A label only: the agent does not change.", { name: dialog.member.name })
                : t_i18n("An agent has one home fleet: it leaves this one. Its handovers and assignments do not change.")}
            </DialogDescription>
          </DialogHeader>
          <form
            className="space-y-4"
            onSubmit={(e) => {
              e.preventDefault();
              applyDialog();
            }}
          >
            <div className="space-y-1.5">
              <Label htmlFor="fleet-member-dialog-field" className="text-default-secondary">
                {dialog?.kind === "label" ? t_i18n("Role") : t_i18n("Fleet")}
              </Label>
              {dialog?.kind === "label" ? (
                <Input
                  id="fleet-member-dialog-field"
                  aria-label={t_i18n("Role")}
                  autoFocus
                  value={dialogValue ?? ""}
                  maxLength={100}
                  placeholder={t_i18n("e.g. Release manager")}
                  onChange={(e) => setDialogValue(e.target.value)}
                />
              ) : (
                <SearchableSelect
                  id="fleet-member-dialog-field"
                  aria-label={t_i18n("Fleet")}
                  options={targets.map((f) => ({ value: f.id, label: f.name }))}
                  value={dialogValue}
                  onValueChange={(value) => setDialogValue(value || null)}
                  emptyMessage={t_i18n("No other fleet you can manage")}
                />
              )}
            </div>
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={() => setDialog(null)}>
                {t_i18n("Cancel")}
              </Button>
              <Button type="submit" disabled={dialog?.kind === "move" && !dialogValue}>
                {dialog?.kind === "move" ? t_i18n("Move") : t_i18n("Save")}
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
      <ChangeParentDialog fleetId={fleet.id} members={members} member={parentMember} onClose={() => setParentMember(null)} />
      {confirmElement}
    </Card>
  );
}

function ConnectedRow({
  row,
  canEdit,
  fleetName,
  onOpenAgent,
  onSave,
  onRemove,
}: {
  row: FleetMember;
  canEdit: boolean;
  fleetName: string | null;
  onOpenAgent: (agentId: string) => void;
  onSave: (patch: { collaboration_instruction?: string; context_inclusion?: FleetContextInclusion }) => void;
  onRemove: () => void;
}) {
  const { t_i18n } = useFormatter();
  const [instruction, setInstruction] = useState(row.collaboration_instruction);
  const label = fleetName ?? (row.name || t_i18n("An agent not shared with you"));
  return (
    <div className="space-y-1.5 rounded-sm border border-elevation-subtle-soft p-2">
      <div className="flex min-w-0 items-center gap-2">
        {row.target_fleet_id ? (
          <span className="flex min-w-0 flex-1 items-center gap-1 truncate content-compact">
            <Boxes className="h-3 w-3 shrink-0" aria-label={t_i18n("Fleet")} />
            <span className="truncate" title={label}>{label}</span>
          </span>
        ) : (
          <button
            type="button"
            title={label}
            className="min-w-0 flex-1 truncate text-left content-compact hover:underline outline-none focus-visible:ring-2 focus-visible:ring-focus rounded-sm"
            onClick={() => row.agent_id && onOpenAgent(row.agent_id)}
          >
            {label}
          </button>
        )}
        {canEdit && (
          <Button type="button" variant="ghost" size="icon" className="shrink-0" aria-label={t_i18n("Remove {name}", { name: label })} onClick={onRemove}>
            <X className="h-4 w-4" />
          </Button>
        )}
      </div>
      <Select
        value={row.context_inclusion}
        disabled={!canEdit}
        onValueChange={(value) => onSave({ context_inclusion: value as FleetContextInclusion })}
      >
        <SelectTrigger className="w-full" aria-label={t_i18n(CONTEXT_INCLUSION_FIELD)}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {CONTEXT_INCLUSIONS.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {t_i18n(option.label)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Input
        aria-label={t_i18n("Collaboration instruction")}
        value={instruction}
        maxLength={4000}
        disabled={!canEdit}
        placeholder={t_i18n("When and how the fleet works with this agent (optional)")}
        onChange={(e) => setInstruction(e.target.value)}
        onBlur={() => instruction !== row.collaboration_instruction && onSave({ collaboration_instruction: instruction })}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            (e.target as HTMLInputElement).blur();
          }
        }}
      />
    </div>
  );
}
