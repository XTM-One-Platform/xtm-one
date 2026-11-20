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
 * A workspace: where a team works. Its header is the fleet page's
 * (`EntityPageHeader`), with the fleet it hosts as a chip.
 *
 * Tabs, in the URL (`/tasks/workspaces/:id/:tab?`):
 * - Overview: what it gives its agents (`KnowledgePanel`);
 * - Team, only when it hosts a fleet: the fleet's members (`WorkspaceTeam`);
 * - Operations: work items, shared state and worksets filed here or produced
 *   by its fleet (`WorkspaceOperations`, `?workspace=` server-side);
 * - Conversations: the reader's own conversations filed here. Starting a
 *   conversation here files it in the workspace from its first message.
 */
import { WORKSPACES_PATH } from "@/lib/task-routes";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { FolderKanban, MessageSquarePlus, MessagesSquare } from "lucide-react";

import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { CompanyManagedBadge } from "@/components/ui/company-managed-badge";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { NotFoundState } from "@/components/ui/not-found-state";
import { ReadOnlyBadge } from "@/components/ui/read-only-badge";
import { SharedBadge } from "@/components/ui/shared-badge";
import { PageSkeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast";
import { KnowledgePanel } from "@/components/workspaces/knowledge/KnowledgePanel";
import { WorkspaceActionsMenu } from "@/components/workspaces/WorkspaceActionsMenu";
import { WorkspaceHostedFleets } from "@/components/workspaces/WorkspaceHostedFleets";
import { WorkspaceOperations } from "@/components/workspaces/WorkspaceOperations";
import { WorkspaceTeam } from "@/components/workspaces/WorkspaceTeam";
import { useCreateConversation } from "@/hooks/api/useChatApi";
import { useWorkspace, useWorkspaceConversations } from "@/hooks/api/useWorkspaceApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useAuth } from "@/hooks/useAuth";
import { useTabParam } from "@/hooks/useTabParam";
import { useFormatter } from "@/i18n";
import { workspaceReachesOthers } from "@/lib/workspace-sharing";
import { formatShortDateTime, isEntityShared } from "@/lib/utils";
import { resolveWorkspaceTab, workspaceTabs, type WorkspaceTab } from "@/lib/workspace-tabs";
import { WORKSPACE_CONVERSATIONS_SHOWN } from "@/hooks/api/useWorkspaceApi";

export default function WorkspaceProject() {
  const { workspaceId = "" } = useParams<{ workspaceId: string }>();
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const location = useLocation();
  // Opened from a fleet's chip, the back link returns to the fleet.
  const back = location.state as { backTo?: string; backLabel?: string } | null;
  const { toast } = useToast();
  const { user } = useAuth();
  const workspace = useWorkspace(workspaceId);
  // Read once the workspace is known: an unknown id shows NotFoundState.
  const conversations = useWorkspaceConversations(workspace.data ? workspaceId : null);
  const create = useCreateConversation();
  const aiOff = useAiActionDisabled();
  const [requestedTab] = useTabParam<WorkspaceTab>("overview");

  if (workspace.isLoading) return <PageSkeleton />;
  if (!workspace.data) {
    return (
      <NotFoundState
        icon={FolderKanban}
        title="Workspace not found"
        description="It may have been deleted, or it is not shared with you."
        actionLabel="Back to Workspaces"
        onAction={() => navigate(WORKSPACES_PATH)}
        backTo={WORKSPACES_PATH}
        backLabel="Back to Workspaces"
      />
    );
  }
  const ws = workspace.data;

  const startConversation = async () => {
    try {
      const conv = await create.mutateAsync({ workspace_id: ws.id });
      navigate(`/chat?c=${conv.id}`);
    } catch (err) {
      toast({
        title: t_i18n("The conversation could not be started"),
        description: err instanceof Error && err.message ? err.message : undefined,
        variant: "error",
      });
    }
  };

  const hostedFleet = (ws.hosted_fleets ?? [])[0];
  const tabs = workspaceTabs(!!hostedFleet);
  const tab = resolveWorkspaceTab(requestedTab, !!hostedFleet);
  const shared = isEntityShared(ws, user?.id);
  const badges =
    ws.is_company_managed ||
    shared ||
    workspaceReachesOthers(ws, user?.id) ||
    !ws.can_manage ||
    ws.is_archived ? (
      <>
        {/* The Workspaces tab's badges for the same workspace. */}
        {ws.is_company_managed && <CompanyManagedBadge size="default" />}
        {shared ? (
          <SharedBadge size="default" ownerName={ws.owner_name} />
        ) : (
          !ws.is_company_managed && workspaceReachesOthers(ws, user?.id) && <SharedBadge size="default" />
        )}
        {!ws.is_company_managed && !ws.can_manage && <ReadOnlyBadge />}
        {ws.is_archived && <Badge variant="warning">{t_i18n("Archived")}</Badge>}
      </>
    ) : undefined;

  return (
    <div className="space-y-6">
      <EntityPageHeader
        backTo={back?.backTo ?? WORKSPACES_PATH}
        backLabel={back?.backLabel ?? "Back to Workspaces"}
        icon={FolderKanban}
        title={ws.name}
        hintLabel={t_i18n("What a workspace is")}
        hint={
          hostedFleet
            ? t_i18n(
                "This is where the fleet {name} works: its agents' work lands here, and every agent here gets its instructions, sources, knowledge bases and skills.",
                { name: hostedFleet.name },
              )
            : t_i18n(
                "A workspace is where people and agents work together: everything filed here shares its instructions, sources, knowledge bases and skills.",
              )
        }
        description={ws.description}
        badges={badges}
        links={<WorkspaceHostedFleets workspace={ws} />}
        actions={
          <>
            {ws.can_manage && !ws.is_archived && (
              <AiDisabledTip>
                <Button className="gap-1.5" disabled={create.isPending || aiOff} onClick={startConversation}>
                  <MessageSquarePlus className="h-4 w-4" />
                  {t_i18n("New conversation")}
                </Button>
              </AiDisabledTip>
            )}
            <WorkspaceActionsMenu workspace={ws} openWorkspace={false} />
          </>
        }
      />

      <DetailTabs tabs={tabs} active={tab} />

      {tab === "overview" && (
        <section aria-label={t_i18n("Workspace knowledge")}>
          <KnowledgePanel workspaceId={ws.id} />
        </section>
      )}
      {tab === "team" && hostedFleet && <WorkspaceTeam workspaceId={ws.id} fleetId={hostedFleet.id} />}
      {tab === "operations" && <WorkspaceOperations workspaceId={ws.id} fleetName={hostedFleet?.name} />}
      {tab === "conversations" && (
        /* The person's own conversations: what others filed here is theirs,
           searchable by the agents, never listed. */
        <section aria-label={t_i18n("Your conversations")} className="max-w-3xl space-y-2">
          <h2 className="flex items-center gap-2 content-base font-semibold">
            <MessagesSquare className="h-4 w-4 text-accent" />
            {t_i18n("Your conversations")}
          </h2>
          {(conversations.data ?? []).length === 0 ? (
            <Card className="border-dashed">
              <CardContent className="py-8 text-center content-compact text-default-secondary">
                {t_i18n("None of your conversations is filed here yet.")}
              </CardContent>
            </Card>
          ) : (
            (conversations.data ?? []).map((conv) => (
              <button
                key={conv.id}
                type="button"
                onClick={() => navigate(`/chat?c=${conv.id}`)}
                className="flex w-full items-center justify-between gap-3 rounded-sm border border-elevation-subtle px-3 py-2 text-left transition-colors hover:bg-elevation-highlight"
              >
                <span className="truncate content-compact font-medium text-default-primary">
                  {conv.title || t_i18n("New conversation")}
                </span>
                <span className="shrink-0 content-caption text-default-secondary">
                  {formatShortDateTime(conv.updated_at)}
                </span>
              </button>
            ))
          )}
          {(conversations.data ?? []).length >= WORKSPACE_CONVERSATIONS_SHOWN && (
            <p className="content-caption text-default-secondary">
              {t_i18n("Your {count} most recent conversations here.", {
                count: WORKSPACE_CONVERSATIONS_SHOWN,
              })}
            </p>
          )}
        </section>
      )}
    </div>
  );
}
