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

import { useState, useCallback } from "react";
import { Link, useLocation, useParams, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { useTick } from "@/hooks/useTick";
import {
  Play, Trash2, Zap, Clock, FlaskConical,
  Power, PowerOff, LayoutDashboard, Terminal, Activity, MessageSquare, Scan,
} from "lucide-react";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { Spinner } from "@filigran/design-system";
import { Badge } from "@/components/ui/badge";
import { EmergencyStopBadge } from "@/components/ui/emergency-stop-badge";
import { isEmergencyStopped } from "@/lib/emergency-stop";
import { Button } from "@/components/ui/button";
import { DetailPageSkeleton } from "@/components/ui/detail-page-skeleton";
import RouteNotFound from "@/pages/RouteNotFound";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { ReadOnlyBadge } from "@/components/ui/read-only-badge";
import { CompanyManagedBanner } from "@/components/ui/company-managed-banner";
import { SharedBanner } from "@/components/ui/shared-banner";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { EntityHeaderMoreActions, EntityHeaderWideOnly, EntityPageHeader } from "@/components/ui/entity-page-header";
import type { OverflowMenuItem } from "@/components/ui/overflow-menu";
import { ManualTriggerDialog } from "@/components/ui/manual-trigger-dialog";
import { useTabParam } from "@/hooks/useTabParam";
import { useAgent } from "@/hooks/api/useAgentApi";
import {
  useAssignment, useUpdateAssignment, useDeleteAssignment, useTriggerAssignment,
  useAssignmentRuns,
} from "@/hooks/api/useAssignmentApi";
import { useAuth } from "@/hooks/useAuth";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { canManageEntity, cn, formatCompactNumber, isEntityShared } from "@/lib/utils";
import { assignmentTracesPath } from "@/lib/trace-links";
import { triggerTile } from "@/lib/ctem-signal-trigger";
import { useFormatter } from "@/i18n";
import { useAiActionDisabled, useAiDisabledReason } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import {
  TRIGGER_ICONS, TRIGGER_LABELS, describeCron, REFETCH_INTERVAL_SLOW,
} from "@/lib/constants";
import { OverviewTab } from "./assignment-detail/OverviewTab";
import { assignmentEditRights } from "./assignment-detail/edit-rights";
import { PausedBanner } from "./assignment-detail/PausedBanner";
import { AssignmentStatusBadge } from "./assignment-detail/AssignmentStatus";
import { assignmentDisplayStatus, assignmentStatusTint } from "./assignment-detail/assignment-status";
import { PromptTab } from "./assignment-detail/PromptTab";
import { ChannelsTab } from "./assignment-detail/ChannelsTab";
import { ActivityTab } from "./assignment-detail/ActivityTab";

// ---------------------------------------------------------------------------
// Tab definitions
// ---------------------------------------------------------------------------
type Tab = "overview" | "prompt" | "channels" | "activity";

const TABS: { key: Tab; label: string; icon: React.ElementType }[] = [
  { key: "overview", label: "Overview", icon: LayoutDashboard },
  { key: "prompt", label: "Prompt & Bindings", icon: Terminal },
  { key: "channels", label: "Channels & Bots", icon: MessageSquare },
  { key: "activity", label: "Activity", icon: Activity },
];

// ---------------------------------------------------------------------------
// AssignmentDetail — tabbed detail page
// ---------------------------------------------------------------------------
export default function AssignmentDetail() {
  const { t_i18n } = useFormatter();
  useTick(REFETCH_INTERVAL_SLOW);
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  // The page that opened it may name its own way back (Administration > Objects sends
  // "Back to Objects"); the agent stays the default (D1 of the #12074 retest, #12070).
  const locState = location.state as { backTo?: string; backLabel?: string } | null;
  const { toast } = useToast();
  const [tab] = useTabParam<Tab>("overview");
  const [triggerDialogOpen, setTriggerDialogOpen] = useState(false);

  const qc = useQueryClient();
  const { data: assignment, isLoading } = useAssignment(id || "", { notFoundIsOrdinary: true });
  // The runs are read once the assignment answered: an unknown id 404s on both reads, and the runs'
  // 404 thrown to the section boundary would beat Page not found below (#797 S4).
  const { data: runs } = useAssignmentRuns(assignment ? assignment.id : "", 10);

  const update = useUpdateAssignment();
  const remove = useDeleteAssignment();
  const triggerWf = useTriggerAssignment();
  /* An assignment always runs an agent, so Run always needs AI. */
  const aiOff = useAiActionDisabled();
  const aiOffReason = useAiDisabledReason() ?? undefined;
  const [ConsentEl, confirmSharing] = useCredentialSharingConfirm();

  const { data: agent } = useAgent(assignment?.agent_id ?? "");
  const { user, isAdmin } = useAuth();
  const isXtmOne = useIsXtmOne();

  const refetchAssignment = useCallback(
    () => qc.invalidateQueries({ queryKey: ["assignment", id] }),
    [qc, id],
  );

  // The agent the back link names is read with the assignment: the skeleton's link is the agent's
  // parent list, as on the not-found state, with the loaded label so the link keeps its width.
  if (isLoading)
    return (
      <DetailPageSkeleton
        backTo={locState?.backTo ?? "/agents"}
        backLabel={locState?.backLabel ?? "Back to Agent"}
        tabs={TABS.length}
        descriptionLines={1}
        actions={2}
      />
    );
  // An unknown id (or one you cannot read) is an address no page answers (#797 S4).
  if (!assignment) return <RouteNotFound />;

  const hasManageRights = agent ? canManageEntity(agent, user?.id, isAdmin) : false;
  const { canManage, canToggle, canChooseRunAs, canRunNow } = assignmentEditRights({
    agentIsBuiltin: !!agent?.is_builtin,
    isXtmOne,
    hasManageRights,
    isAdmin,
    builtinAdminOptIn: !!assignment.builtin_admin_opt_in,
  });
  // Resuming a webhook assignment answers whoever holds its URL again.
  const setEnabled = async (enabled: boolean) => {
    if (enabled && !(await confirmSharing({ entity_type: "assignment", entity_id: assignment.id, change: { enabled: true } }, { confirmLabel: t_i18n("Resume") }))) return;
    update.mutate({ id: assignment.id, enabled }, { onSuccess: refetchAssignment });
  };
  const isShared = isEntityShared(agent, user?.id);
  const deleteAssignment = async () => {
    if (await confirmSharing({ entity_type: "assignment", entity_id: assignment.id, change: { delete: true } }, { mode: "delete", fallback: { title: "Delete assignment?", description: "This will permanently delete this assignment and all its run history." } })) remove.mutate(assignment.id, { onSuccess: () => navigate(`/agents/${assignment.agent_id}`) });
  };
  // Below 1280 px View Traces and Delete are rows of the "More
  // actions" menu; Run Now and the pause toggle stay in the row.
  const moreActions: OverflowMenuItem[] = [
    ...(isAdmin
      ? [{ id: "traces", label: t_i18n("View Traces"), icon: Scan, onSelect: () => navigate(assignmentTracesPath(assignment.id)) }]
      : []),
    ...(canToggle && canManage
      ? [{ id: "delete", label: t_i18n("Delete assignment"), icon: Trash2, destructive: true, separatorBefore: true, onSelect: () => void deleteAssignment(), disabled: aiOff, disabledReason: aiOff ? aiOffReason : undefined }]
      : []),
  ];

  // A CTEM signal trigger reads as its own tile (#1409), as the Trigger card does.
  const tile = triggerTile(assignment.trigger_type, assignment.trigger_config);
  const TriggerIcon = TRIGGER_ICONS[tile] ?? Zap;
  const cronExpr = assignment.trigger_type === "schedule" ? assignment.trigger_config.cron : null;
  // The header icon follows the badge: an ended platform trigger is never tinted as active.
  const tint = assignmentStatusTint(assignmentDisplayStatus(assignment));

  return (
    <div className="space-y-6">
      <EntityPageHeader
        backTo={locState?.backTo ?? `/agents/${assignment.agent_id}`}
        backLabel={locState?.backLabel ?? "Back to Agent"}
        icon={<TriggerIcon className={cn("h-7 w-7", tint.icon)} aria-hidden />}
        tileClassName={tint.tile}
        title={assignment.name}
        wrapTitle
        description={assignment.description}
        badges={
          <>
                {isEmergencyStopped(assignment) ? (
                  <EmergencyStopBadge details={assignment.paused_details} />
                ) : (
                  <AssignmentStatusBadge assignment={assignment} />
                )}
                <Badge variant="outline">{t_i18n(TRIGGER_LABELS[tile] ?? assignment.trigger_type)}</Badge>
                {cronExpr && <Badge variant="outline">{describeCron(cronExpr)}</Badge>}
                {assignment.delay && <Badge variant="outline"><Clock className="h-2.5 w-2.5 mr-0.5" />{t_i18n("{delay} delay", { delay: assignment.delay })}</Badge>}
                {assignment.dry_run && <Badge variant="warning"><FlaskConical className="h-2.5 w-2.5 mr-0.5" />{t_i18n("Dry run")}</Badge>}
                <span className="content-compact text-default-secondary">{t_i18n("{count, plural, one {{shown} run} other {{shown} runs}}", { count: assignment.execution_count, shown: formatCompactNumber(assignment.execution_count) })}</span>
          </>
        }
        actions={
          <>
            {/* The Trace Explorer is admin-only (AdminRoute): no dead link for
                anyone else. */}
            {isAdmin && (
              <EntityHeaderWideOnly>
              <Tip content={t_i18n("Investigate every run of this assignment in the Trace Explorer")}>
                <Button asChild variant="secondary" size="sm" className="gap-1.5">
                  <Link to={assignmentTracesPath(assignment.id)}>
                    <Scan className="h-3.5 w-3.5" />
                    {t_i18n("View Traces")}
                  </Link>
                </Button>
              </Tip>
              </EntityHeaderWideOnly>
            )}
            {canRunNow && assignment.trigger_type !== "integration_event" && assignment.trigger_type !== "webhook" && (() => {
              // Refused for everyone while the owner is deactivated.
              if (assignment.paused_by_owner_deactivation) {
                return (
                  <Tip content={t_i18n("Paused: the owner's account is deactivated")} wrap>
                    <Button size="sm" className="gap-1.5 bg-accent text-negative-primary hover:bg-accent/90" disabled>
                      <Play className="h-3.5 w-3.5" />
                      {t_i18n("Run Now")}
                    </Button>
                  </Tip>
                );
              }
              const hasRunning = runs?.some((r) => r.status === "running" || r.status === "pending");
              const hasParams = assignment.manual_trigger_parameters && assignment.manual_trigger_parameters.length > 0;
              return (
                <AiDisabledTip>
                <Button
                  size="sm"
                  className="gap-1.5 bg-accent text-negative-primary hover:bg-accent/90"
                  disabled={triggerWf.isPending || hasRunning || aiOff}
                  aria-busy={triggerWf.isPending || hasRunning || undefined}
                  onClick={() => {
                    if (hasParams) {
                      setTriggerDialogOpen(true);
                    } else {
                      triggerWf.mutate({ assignmentId: assignment.id }, { onSuccess: () => toast({ title: t_i18n("Triggered"), variant: "success" }) });
                    }
                  }}
                >
                  {(triggerWf.isPending || hasRunning) && <Spinner size="sm" tone="inherit" />}
                  {!(triggerWf.isPending || hasRunning) && <Play className="h-3.5 w-3.5" />}
                  {hasRunning ? t_i18n("Running...") : t_i18n("Run Now")}
                </Button>
                </AiDisabledTip>
              );
            })()}
            {canToggle && (
              <>
                <AiDisabledTip>
                <Tip content={isEmergencyStopped(assignment) ? t_i18n("Paused by the emergency stop: lift the stop to resume it.") : assignment.enabled ? t_i18n("Pause") : t_i18n("Resume")} wrap={isEmergencyStopped(assignment)}>
                  <Button variant="ghost" size="sm" className="h-8 w-8 p-0" disabled={isEmergencyStopped(assignment) || aiOff} aria-label={assignment.enabled ? t_i18n("Pause") : t_i18n("Resume")} onClick={() => void setEnabled(!assignment.enabled)}>
                    {assignment.enabled ? <Power className="h-4 w-4" /> : <PowerOff className="h-4 w-4" />}
                  </Button>
                </Tip>
                </AiDisabledTip>
                {canManage && (
                <EntityHeaderWideOnly>
                <Button aria-label={t_i18n("Delete assignment")} variant="ghost" size="sm" className="h-8 w-8 p-0 text-feedback-error-primary" onClick={() => void deleteAssignment()} disabled={aiOff}>
                  <Trash2 className="h-4 w-4" />
                </Button>
                </EntityHeaderWideOnly>
                )}
              </>
            )}
            {!canManage && (agent?.is_company_managed || isShared) && (
              <ReadOnlyBadge />
            )}
            {/* The More actions trigger is the last control of the row, as on
                every other entity page. */}
            <EntityHeaderMoreActions items={moreActions} />
          </>
        }
      />

      {/* Why is this paused? — renders nothing when enabled. */}
      <PausedBanner
        assignment={assignment}
        onResume={canToggle && !isEmergencyStopped(assignment) ? () => void setEnabled(true) : undefined}
        resuming={update.isPending}
      />

      {/* Company-managed read-only banner */}
      {!canManage && agent?.is_company_managed && (
        <CompanyManagedBanner entityLabel="assignment" />
      )}
      {isShared && <SharedBanner entityLabel="assignment" ownerName={agent?.owner_name} readOnly={!hasManageRights} />}

      {/* ── Tabs ── */}
      <DetailTabs tabs={TABS} active={tab} />

      {/* ── Tab content ── */}
      {tab === "overview" && (
        <OverviewTab
          assignment={assignment}
          agent={agent}
          canManage={canManage}
          canChooseRunAs={canChooseRunAs}
          onUpdate={refetchAssignment}
        />
      )}
      {tab === "prompt" && (
        <PromptTab
          assignment={assignment}
          agent={agent}
          canManage={canManage}
          onUpdate={refetchAssignment}
        />
      )}
      {tab === "channels" && (
        <ChannelsTab
          assignment={assignment}
          agent={agent}
          canManage={canManage}
          onUpdate={refetchAssignment}
        />
      )}
      {tab === "activity" && (
        <ActivityTab assignmentId={assignment.id} />
      )}
      {ConsentEl}
      {assignment.manual_trigger_parameters && assignment.manual_trigger_parameters.length > 0 && (
        <ManualTriggerDialog
          open={triggerDialogOpen}
          onOpenChange={setTriggerDialogOpen}
          parameters={assignment.manual_trigger_parameters}
          assignmentName={assignment.name}
          isPending={triggerWf.isPending}
          onTrigger={(payload) => {
            triggerWf.mutate(
              { assignmentId: assignment.id, payload },
              {
                onSuccess: () => {
                  toast({ title: t_i18n("Triggered"), variant: "success" });
                  setTriggerDialogOpen(false);
                },
              },
            );
          }}
        />
      )}
    </div>
  );
}
