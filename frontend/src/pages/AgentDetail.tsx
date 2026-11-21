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

import { useParams, useNavigate, useLocation } from "react-router-dom";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { useTabParam } from "@/hooks/useTabParam";
import { useTick } from "@/hooks/useTick";
import {
  Bot,
  MessageSquare,
  Trash2,
  Power,
  PowerOff,
  MessageSquareOff,
  CopyPlus,
  Download,
  User,
  Hash,
} from "lucide-react";
import { useState } from "react";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";
import { ConfirmDeleteDialog } from "@/components/ui/confirm-delete-dialog";
import { AgentExportDialog } from "@/components/ui/agent-export-dialog";
import { AgenticGateDialog, AgenticGateDialogControlled } from "@/components/ui/license-gate-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { IconButton, Spinner } from "@filigran/design-system";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { SharedBadge } from "@/components/ui/shared-badge";
import { ReadOnlyBadge } from "@/components/ui/read-only-badge";
import { MissingVariablesBanner } from "@/components/ui/missing-variables-banner";
import { TIP_WRAP_FOCUS_RING, Tip } from "@/components/ui/tooltip";
import { DetailPageSkeleton } from "@/components/ui/detail-page-skeleton";
import { useToast } from "@/components/ui/toast";
import {
  useAgent, useUpdateAgent, useDeleteAgent, useDuplicateAgent,
  useDownloadAgentExport, useAgentTemplates,
} from "@/hooks/api/useAgentApi";
import { useAssignments, useActiveAgentIds } from "@/hooks/api/useAssignmentApi";
import { useGeneralAssistantConfig } from "@/hooks/api/useSettingsApi";
import { useAuth } from "@/hooks/useAuth";
import { canManageEntity, canViewAgentChats, cn, isEntityShared } from "@/lib/utils";
import { canStandInForGeneralAssistant } from "@/lib/general-assistant-stand-in";
import type { AgentExportFormat } from "@/types";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { EntityHeaderMoreActions, EntityHeaderWideOnly, EntityPageHeader } from "@/components/ui/entity-page-header";
import type { OverflowMenuItem } from "@/components/ui/overflow-menu";
import { ICON_MAP, REFETCH_INTERVAL_SLOW } from "@/lib/constants";
import { useAgenticAIEnabled, useIsXtmOne } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { OverviewTab } from "./agent-detail/OverviewTab";
import { ConfigTab } from "./agent-detail/ConfigTab";
import { BudgetTab } from "./agent-detail/BudgetTab";
import { AgentRoiSection } from "@/components/roi/AgentRoiSection";
import { MemoryTab } from "./agent-detail/MemoryTab";
import { ChatsTab } from "./agent-detail/ChatsTab";
import { DelegationsTab } from "./agent-detail/DelegationsTab";
import { ActivityTab } from "./agent-detail/ActivityTab";
import { RoiRecordsList } from "@/components/roi/RoiRecordsList";
import { ROI_RECORDS_SEGMENT } from "@/lib/roi-records";
import { EvaluationTab } from "./agent-detail/EvaluationTab";
import { VersioningTab } from "./agent-detail/VersioningTab";
import { ObservabilityTab } from "./agent-detail/ObservabilityTab";
import { FeedbackTab } from "./agent-detail/FeedbackTab";
import { agentDetailTabs, isAgentTab, type AgentTab } from "./agent-detail/tabs";
import RouteNotFound from "@/pages/RouteNotFound";
import { AgentSuccessorDialog } from "./agent-detail/AgentSuccessorDialog";
import { useAiActionDisabled, useAiDisabledReason } from "@/hooks/useAiDisabled";

// ---------------------------------------------------------------------------
// Main Agent Detail Page
// ---------------------------------------------------------------------------
type Tab = AgentTab;

export default function AgentDetail() {
  const aiOff = useAiActionDisabled();
  const aiOffReason = useAiDisabledReason();
  const agenticEnabled = useAgenticAIEnabled();
  const { t_i18n } = useFormatter();
  useTick(REFETCH_INTERVAL_SLOW); // keep relative timestamps fresh
  const { id, tab: tabSegment, section } = useParams<{ id: string; tab?: string; section?: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const { toast } = useToast();

  // Contextual back navigation — if we came from the agentic flow graph, go back there
  const locState = location.state as { backTo?: string; backLabel?: string } | null;
  const backTo = locState?.backTo ?? "/agents";
  const backLabel = locState?.backLabel ?? "Back to Agents";
  const { data: agent, isLoading } = useAgent(id || "", { notFoundIsOrdinary: true });
  const { user, isAdmin } = useAuth();

  const isXtmOne = useIsXtmOne();
  const evaluation = useFeatureFlag("EVALUATION");
  const update = useUpdateAgent();
  const remove = useDeleteAgent();
  const duplicate = useDuplicateAgent();
  const exportAgent = useDownloadAgentExport();
  const { data: assignments } = useAssignments(id);
  const { data: templates } = useAgentTemplates();
  const { data: activeAgentIdList } = useActiveAgentIds();
  const { data: gaConfig } = useGeneralAssistantConfig();
  // Answering in the General Assistant's place: a link it stopped
  // qualifying for keeps the toggle, so a disabled agent can be enabled again.
  const isLinkedGA = agent ? gaConfig?.linked_agent_id === agent.id && canStandInForGeneralAssistant(agent) : false;
  const isAgentRunning = agent ? (activeAgentIdList ?? []).includes(agent.id) : false;
  const [tab] = useTabParam<Tab>("overview");
  const [ConsentEl, confirmSharing, checkingSharing] = useCredentialSharingConfirm();
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [successorDialogOpen, setSuccessorDialogOpen] = useState(false);
  const [successorId, setSuccessorId] = useState<string | null>(null);
  const [exportDialogOpen, setExportDialogOpen] = useState(false);
  const [duplicateGateOpen, setDuplicateGateOpen] = useState(false);

  // A `:tab` segment that names no agent tab (the former `budget` included) is
  // not found, before the agent header renders: no redirect (#797).
  if (!isAgentTab(tabSegment)) return <RouteNotFound />;
  if (isLoading) return <DetailPageSkeleton backTo={backTo} backLabel={backLabel} tabs={6} descriptionLines={1} actions={3} />;
  // An unknown id (or one you cannot read) is an address no page answers: the
  // full Page not found, as for any unknown path (#797 S4).
  if (!agent) return <RouteNotFound />;

  const Icon = ICON_MAP[agent.icon] ?? Bot;
  // In XTM One mode, builtin agents are always read-only (even for admins).
  // Otherwise the server's tier-aware `can_manage` decides.
  const canManage = (agent.is_builtin && isXtmOne)
    ? false
    : canManageEntity(agent, user?.id, isAdmin);
  // A built-in's CONNECTED INTEGRATIONS are configurable by an administrator,
  // unlike the rest of it. They are the deployment's own accounts rather than
  // part of the agent's seeded definition, so the read-only rule — which exists
  // because the startup resync rewrites that definition — does not apply to
  // them. A2A exposure rides on this, being an integration binding itself.
  // A separate gate rather than a loosening of `canManage`, which must stay
  // false for a built-in everywhere else.
  // A Platform Health fleet agent (`read_only_reason`, #101 S1b) takes no write
  // at all, administrators included: neither carve-out below applies to it.
  const healthLocked = Boolean(agent.read_only_reason);
  const canManageIntegrations = healthLocked ? false : (agent.is_builtin && isXtmOne) ? isAdmin : canManage;
  const canViewFeedback = isAdmin || canManage;
  // Memory management gate. Company-managed agents (incl. seeded built-ins,
  // which are always company-managed and otherwise config read-only) mirror the
  // backend `_can_manage_memory` rule: admins only. Keying on `is_company_managed`
  // (not `canManage`) avoids showing a non-admin owner controls the backend would
  // 403. Private agents keep the existing `canManage` UI gating, which is
  // intentionally stricter than the backend's owner check.
  const canManageMemory = healthLocked ? false : agent.is_company_managed ? isAdmin : canManage;
  const isShared = isEntityShared(agent, user?.id);
  const isOwnedByMe = Boolean(user?.id) && agent.user_id === user?.id;

  const handleDeleteSuccess = () => { toast({ title: t_i18n("Agent deleted"), variant: "success" }); navigate("/agents"); };
  const deleteAgent = (successorAgentId: string | null) =>
    remove.mutate(
      { id: agent.id, successorAgentId },
      {
        onSuccess: handleDeleteSuccess,
        onError: (err) => toast({ title: t_i18n("The agent was not deleted"), description: err.message, variant: "error" }),
      },
    );
  // Which agent continues its conversations is asked first; an agent
  // of one's own is then deleted once the credential preview agrees, a shared
  // one once its name is typed.
  const confirmDeletion = async (successorAgentId: string | null) => {
    if (!isOwnedByMe) {
      setSuccessorId(successorAgentId);
      setDeleteDialogOpen(true);
      return;
    }
    if (await confirmSharing({ entity_type: "agent", entity_id: agent.id, change: { delete: true } }, { mode: "delete" }))
      deleteAgent(successorAgentId);
  };

  const handleExport = (format: AgentExportFormat) => {
    exportAgent.mutate(
      { id: agent.id, name: agent.name, format },
      {
        onSuccess: ({ filename }) => {
          setExportDialogOpen(false);
          toast({
            title: t_i18n("Agent exported"),
            description: t_i18n("Downloaded {filename}.", { filename }),
            variant: "success",
          });
        },
        onError: (err) => {
          toast({ title: t_i18n("Export failed"), description: err.message, variant: "error" });
        },
      },
    );
  };

  const duplicateAgent = () =>
    duplicate.mutate(agent.id, {
      onSuccess: (newAgent) => {
        const warnings = newAgent.warnings ?? [];
        toast({
          title: t_i18n("Agent duplicated"),
          description: [
            t_i18n("\"{name}\" is ready to customize.", { name: newAgent.name }),
            ...(warnings.length > 0
              ? [t_i18n("Some bindings of the original agent were not copied:"), ...warnings]
              : []),
          ].join(" "),
          variant: warnings.length > 0 ? "info" : "success",
        });
        navigate(`/agents/${newAgent.id}`);
      },
    });
  const canDelete = canManage && !isLinkedGA;
  // Below 1280 px Duplicate, Export and Delete are rows of the
  // "More actions" menu, with the gates and disabled states of the buttons.
  // Platform Health refuses a duplicate with 403: no Duplicate on it.
  const moreActions: OverflowMenuItem[] = [
    ...(healthLocked ? [] : [{
      id: "duplicate",
      label: t_i18n("Duplicate"),
      icon: CopyPlus,
      disabled: duplicate.isPending || aiOff,
      disabledReason: aiOff ? aiOffReason ?? undefined : undefined,
      onSelect: () => (agenticEnabled ? duplicateAgent() : setDuplicateGateOpen(true)),
    }]),
    {
      id: "export",
      label: t_i18n("Export"),
      icon: Download,
      disabled: exportAgent.isPending,
      onSelect: () => setExportDialogOpen(true),
    },
    ...(canDelete
      ? [{
          id: "delete",
          label: t_i18n("Delete agent"),
          icon: Trash2,
          destructive: true,
          separatorBefore: true,
          disabled: aiOff || checkingSharing || remove.isPending,
          disabledReason: aiOff ? aiOffReason ?? undefined : undefined,
          onSelect: () => setSuccessorDialogOpen(true),
        }]
      : []),
  ];

  // The chats routes' own rule, served with the agent; without it, only an
  // admin or the owner.
  const canViewChats = canViewAgentChats(agent, user?.id, isAdmin);

  const tabs = agentDetailTabs({ canManageMemory, canViewChats, canViewFeedback, evaluation, isAdmin });

  return (
    <div className="space-y-6">
      <EntityPageHeader
        backTo={backTo}
        backLabel={backLabel}
        icon={Icon}
        muted={!agent.enabled}
        title={agent.name}
        description={agent.description}
        // One meta row at 1024 and 1440 (#10215, UX R2 A4 on #10287): the badges keep
        // their width, the slug takes the shrink first and the owner keeps its name up to 12rem
        // (UX R1 B1 on #10553); both truncate, their full text in the shared Tip.
        metaOneLine
        badges={
                <>
                  {isAgentRunning && agent.enabled ? (
                    <Badge variant="cyan" className="shrink-0 gap-1">
                      <Spinner size="sm" tone="inherit" />
                      {t_i18n("Running")}
                    </Badge>
                  ) : (
                    <Badge variant={agent.enabled ? "success" : "outline"} className="shrink-0">{agent.enabled ? t_i18n("Active") : t_i18n("Disabled")}</Badge>
                  )}
                  {agent.is_company_managed && <span className="inline-flex shrink-0"><CompanyManagedBadge size="default" /></span>}
                  {isShared && <span className="inline-flex shrink-0"><SharedBadge size="default" ownerName={agent.owner_name} /></span>}
                  {(healthLocked || (agent.is_company_managed && !isAdmin) || (isShared && !canManage)) && (
                    <span className="inline-flex shrink-0">
                      <ReadOnlyBadge tooltip={healthLocked ? t_i18n("Platform Health is built in and read-only.") : undefined} />
                    </span>
                  )}
                  {agent.template_slug && <Badge variant="outline" className="shrink-0">{t_i18n("From: {name}", { name: templates?.find((t) => t.slug === agent.template_slug)?.name ?? agent.template_slug })}</Badge>}
                </>
        }
        meta={
              <>
                <Tip content={agent.owner_email ? t_i18n("Owner: {name} ({email})", { name: agent.owner_name ?? "—", email: agent.owner_email }) : t_i18n("Owner: {name}", { name: agent.owner_name ?? "—" })}>
                  <span tabIndex={0} className={cn("inline-flex min-w-0 max-w-[12rem] shrink-0 items-center gap-1.5", TIP_WRAP_FOCUS_RING)} data-testid="agent-header-owner">
                    <User className="h-3.5 w-3.5 shrink-0" aria-hidden />
                    <span className="truncate">
                      <span className="font-medium text-default-secondary">{agent.owner_name ?? "—"}</span>
                      {agent.owner_email && (
                        <span className="text-default-secondary"> ({agent.owner_email})</span>
                      )}
                    </span>
                  </span>
                </Tip>
                {agent.slug && (
                  <Tip content={t_i18n("Agent slug: {slug}", { slug: `agent:${agent.slug}` })}>
                    <span tabIndex={0} className={cn("inline-flex min-w-0 shrink-[4] items-center gap-1.5 font-mono-plex", TIP_WRAP_FOCUS_RING)} data-testid="agent-header-slug">
                      <Hash className="h-3.5 w-3.5 shrink-0" aria-hidden />
                      <span className="truncate">agent:{agent.slug}</span>
                    </span>
                  </Tip>
                )}
              </>
        }
        actions={
            <>
              {/* Secondary actions first, the filled "Chat with Agent" at the
                  right end of the labelled ones, the icon-only toggles last
                  (design-system doc, "Page-title actions"). Below xl
                  the secondary ones fold into More actions. */}
              <EntityHeaderWideOnly>
              {!healthLocked && (
              <AgenticGateDialog feature="Duplicating agents">
                <Tip content={t_i18n("Create a private copy of this agent")}>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="gap-1.5 text-default-secondary hover:text-default-primary"
                    disabled={duplicate.isPending || aiOff}
                    aria-busy={duplicate.isPending || undefined}
                    onClick={duplicateAgent}
                  >
                    {duplicate.isPending && <Spinner size="sm" tone="inherit" />}
                    {!duplicate.isPending && <CopyPlus className="h-3.5 w-3.5" />} {t_i18n("Duplicate")}
                  </Button>
                </Tip>
              </AgenticGateDialog>
              )}
              <Tip content={t_i18n("Export agent as a bundle or an AgentSchema document")}>
                <Button
                  variant="ghost"
                  size="sm"
                  className="gap-1.5 text-default-secondary hover:text-default-primary"
                  disabled={exportAgent.isPending}
                  aria-busy={exportAgent.isPending || undefined}
                  onClick={() => setExportDialogOpen(true)}
                >
                  {exportAgent.isPending && <Spinner size="sm" tone="inherit" />}
                  {!exportAgent.isPending && <Download className="h-3.5 w-3.5" />} {t_i18n("Export")}
                </Button>
              </Tip>
              </EntityHeaderWideOnly>
              <Tip content={aiOff
                ? t_i18n("AI is disabled on this platform, so this agent cannot answer.")
                : agent.disable_chat
                  ? t_i18n("This agent is designed for assignments, not direct chat. You can still try if you know what you're doing.")
                  : undefined}>
                <Button
                  size="sm"
                  className={cn(
                    "gap-1.5",
                    agent.disable_chat
                      ? "bg-elevation-hover text-default-secondary hover:bg-elevation-highlight"
                      : "bg-accent text-negative-primary hover:bg-accent/90",
                  )}
                  disabled={aiOff}
                  onClick={() => navigate(`/chat?agent=${agent.id}`)}
                >
                  {agent.disable_chat ? <MessageSquareOff className="h-3.5 w-3.5" /> : <MessageSquare className="h-3.5 w-3.5" />} {t_i18n("Chat with Agent")}
                </Button>
              </Tip>
              {(canManage || isAdmin) && !isLinkedGA && !healthLocked && (
                <Tip content={agent.enabled ? t_i18n("Disable") : t_i18n("Enable")}>
                  <IconButton
                    variant="default"
                    priority="tertiary"
                    aria-label={agent.enabled ? t_i18n("Disable agent") : t_i18n("Enable agent")}
                    icon={agent.enabled ? <Power className="h-4 w-4" /> : <PowerOff className="h-4 w-4" />}
                    className="h-8 w-8"
                    disabled={aiOff}
                    onClick={() => update.mutate({ id: agent.id, enabled: !agent.enabled })}
                  />
                </Tip>
              )}
              {canDelete && (
                <EntityHeaderWideOnly>
                  <IconButton
                    variant="destructive"
                    priority="tertiary"
                    aria-label={t_i18n("Delete agent")}
                    icon={checkingSharing || remove.isPending ? <Spinner size="sm" tone="inherit" label={checkingSharing ? t_i18n("Checking...") : t_i18n("Loading...")} /> : <Trash2 className="h-4 w-4" />}
                    aria-busy={checkingSharing || remove.isPending || undefined}
                    className="h-8 w-8"
                    disabled={aiOff || checkingSharing || remove.isPending}
                    onClick={() => setSuccessorDialogOpen(true)}
                  />
                </EntityHeaderWideOnly>
              )}
              <EntityHeaderMoreActions items={moreActions} />
              <AgenticGateDialogControlled open={duplicateGateOpen} onOpenChange={setDuplicateGateOpen} feature="Duplicating agents" />
            </>
        }
      />

      <MissingVariablesBanner agentId={agent.id} />

      {/* Tabs */}
      <DetailTabs tabs={tabs} active={tab} />

      {/* Tab content */}
      {tab === "overview" && <OverviewTab agent={agent} assignments={assignments} canManage={canManage} />}
      {tab === "config" && (
        <ConfigTab
          agent={agent}
          canManage={canManage}
          canManageIntegrations={canManageIntegrations}
        />
      )}
      {tab === "value" && (
        <div className="space-y-6">
          {/* What the agent returns first (#9807, moved from Overview by #11037), then what one run may spend. */}
          <AgentRoiSection agentId={agent.id} canManage={canManage} />
          <BudgetTab agent={agent} canManage={canManage} />
        </div>
      )}
      {tab === "versioning" && <VersioningTab agent={agent} canManage={canManage} isAdmin={isAdmin} />}
      {tab === "memory" && <MemoryTab agent={agent} canManage={canManageMemory} />}
      {tab === "chats" && canViewChats && <ChatsTab agent={agent} canManage={canManage} />}
      {tab === "delegations" && <DelegationsTab agent={agent} />}
      {tab === "evaluation" && evaluation && <EvaluationTab agent={agent} canManage={canManage} />}
      {tab === "observability" && canViewChats && <ObservabilityTab agent={agent} />}
      {tab === "feedback" && canViewFeedback && <FeedbackTab agent={agent} />}
      {/* The ROI records the review-rate figure opens (#10570), runs and work items alike. */}
      {tab === "activity" &&
        (section === ROI_RECORDS_SEGMENT ? <RoiRecordsList scope="agent" id={agent.id} /> : <ActivityTab agent={agent} />)}
      {ConsentEl}
      <AgentSuccessorDialog
        agent={agent}
        open={successorDialogOpen}
        onOpenChange={setSuccessorDialogOpen}
        confirmLabel={isOwnedByMe ? t_i18n("Delete") : t_i18n("Continue")}
        onConfirm={(successorAgentId) => void confirmDeletion(successorAgentId)}
      />
      <ConfirmDeleteDialog
        open={deleteDialogOpen}
        onOpenChange={setDeleteDialogOpen}
        name={agent.name}
        title={t_i18n("Delete Shared Agent")}
        description={t_i18n("You are about to delete the agent \"{name}\". This agent may be used by other team members.", { name: agent.name })}
        onConfirm={() => deleteAgent(successorId)}
      />
      {exportDialogOpen && (
        <AgentExportDialog
          agentId={agent.id}
          isExporting={exportAgent.isPending}
          onExport={handleExport}
          onClose={() => setExportDialogOpen(false)}
        />
      )}
    </div>
  );
}
