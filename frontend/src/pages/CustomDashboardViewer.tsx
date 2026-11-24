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
 * One custom dashboard - `/dashboards/:id`: the page of widgets an agent built
 * for this person, as it was last refreshed.
 *
 * The widgets are the agent's to write (`build_dashboard` / `edit_dashboard`);
 * the person refines them by asking the assistant, and may rearrange them here
 * (move, remove, a PATCH at the revision read). An agent may refresh the
 * dashboard meanwhile: the edit then meets a 409, and the page shows the
 * latest revision before anything else is changed.
 *
 * The dashboard can be shown fullscreen, and its definition's
 * `collapseNavigation` folds the app's navigation to its rail while it is open
 * (the person's own preference is untouched).
 */
import { useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { Check, Eye, History, House, LayoutGrid, Maximize, Minimize, Move, Send, Share2, Sparkles, Trash2 } from "lucide-react";

import { DashboardAskContext } from "@/components/custom-dashboard/ask";
import { useAskAssistant } from "@/lib/ask-assistant";
import { CustomDashboardView, type DashboardEditing } from "@/components/custom-dashboard/CustomDashboardView";
import { DashboardHistoryDialog } from "@/components/custom-dashboard/DashboardHistoryDialog";
import { DashboardExportMenu } from "@/components/custom-dashboard/DashboardExportMenu";
import { DashboardPublishDialog } from "@/components/custom-dashboard/DashboardPublishDialog";
import { DashboardShareDialog } from "@/components/custom-dashboard/DashboardShareDialog";
import { DashboardRefresh } from "@/components/custom-dashboard/DashboardRefresh";
import { useKeepInput } from "@/components/custom-dashboard/keep-input";
import { normalizeDashboard } from "@/components/custom-dashboard/spec";
import { useElementFullscreen } from "@/components/presentation/fullscreen";
import { BackButton } from "@/components/ui/back-button";
import { Button } from "@/components/ui/button";
import { ConfirmDeleteDialog } from "@/components/ui/confirm-delete-dialog";
import { NotFoundState } from "@/components/ui/not-found-state";
import { PageSkeleton } from "@/components/ui/skeleton";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import { useMe, useUpdateProfile } from "@/hooks/api/useAuthApi";
import {
  useCustomDashboard,
  useDeleteCustomDashboard,
  useQueuedDashboardEdit,
} from "@/hooks/api/useCustomDashboardApi";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import { useRailHold } from "@/hooks/useNavPrefs";
import { useFormatter } from "@/i18n";
import { formatTimeAgo } from "@/lib/relative-time";
import { cn } from "@/lib/utils";
import type { CustomDashboardOperation } from "@/types";

export default function CustomDashboardViewer() {
  const { id = "" } = useParams<{ id: string }>();
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const navigate = useNavigate();
  const location = useLocation();
  const ask = useAskAssistant();
  const { data, isLoading, isError } = useCustomDashboard(id);
  const update = useQueuedDashboardEdit(id, data?.revision);
  const remove = useDeleteCustomDashboard();
  const [editing, setEditing] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [sharing, setSharing] = useState(false);
  const [history, setHistory] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const { data: me } = useMe();
  const updateProfile = useUpdateProfile();
  const studio = useFeatureFlag("DASHBOARD_STUDIO");
  // The tab on screen: what "this tab" of a download is.
  const [tabId, setTabId] = useState<string | null>(null);
  const dashboard = useMemo(() => (data ? normalizeDashboard(data.spec) : null), [data]);
  const stage = useRef<HTMLDivElement>(null);
  const fullscreen = useElementFullscreen(stage);
  useRailHold(Boolean(dashboard?.appProperties.collapseNavigation));
  const keepInput = useKeepInput(data?.spec, Boolean(data?.can_manage), update.edit);

  if (isLoading) return <PageSkeleton cards={4} />;
  if (isError || !data || !dashboard) {
    return (
      <NotFoundState
        icon={LayoutGrid}
        title="Dashboard not found"
        description="It was deleted, or it is not shared with you."
        actionLabel="All dashboards"
        onAction={() => navigate("/dashboards")}
        backTo="/dashboards"
        backLabel={t_i18n("Back to Dashboards")}
      />
    );
  }

  const edit = (operations: CustomDashboardOperation[]) =>
    update.edit(
      operations,
      {
        onError: (error) => {
          const conflict = (error as { status?: number }).status === 409;
          toast({
            title: conflict ? t_i18n("This dashboard changed meanwhile") : t_i18n("The dashboard could not be changed"),
            description: conflict ? t_i18n("Here is its latest version. Make your change again.") : error.message,
            variant: conflict ? "info" : "error",
          });
        },
      },
    );

  const editingControls: DashboardEditing | null =
    editing && data.can_manage
      ? {
          onLayout: (operation) => edit([operation]),
          onRemove: (widget) => edit([{ op: "remove", id: widget.id }]),
          onDone: () => setEditing(false),
        }
      : null;

  const isHome = me?.home_dashboard_id === data.id;
  const toggleHome = () =>
    updateProfile.mutate(
      { home_dashboard_id: isHome ? null : data.id },
      {
        onSuccess: () =>
          toast({
            title: isHome
              ? t_i18n("Your dashboard is the standard one again")
              : t_i18n("This is now your dashboard"),
            variant: "success",
          }),
        onError: (error) =>
          toast({ title: t_i18n("Your dashboard could not be changed"), description: error.message, variant: "error" }),
      },
    );

  const refinePrompt = t_i18n('Update my dashboard "{title}" (id {id}) with the latest data.', {
    title: data.title,
    id: data.id,
  });

  return (
    <div className="flex w-full flex-col gap-6">
      <BackButton to="/dashboards" label={t_i18n("Back to Dashboards")} className="self-start" />
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex min-w-0 flex-col gap-1">
          <h1 className="flex items-center gap-2 text-3xl font-bold">
            <LayoutGrid className="h-6 w-6 shrink-0 text-accent" aria-hidden="true" />
            <span className="min-w-0 break-words">{data.title}</span>
          </h1>
          {dashboard.subtitle && <p className="content-base text-default-secondary">{dashboard.subtitle}</p>}
          <p className="content-caption text-default-secondary">
            {data.updated_at && t_i18n("Updated {when}", { when: formatTimeAgo(intl, data.updated_at) })}
            {data.assignment_run_id && (
              <>
                {" · "}
                <Link to={`/operations/executions/${data.assignment_run_id}`} className="text-accent hover:underline">
                  {t_i18n("Refreshed by a scheduled run")}
                </Link>
              </>
            )}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <DashboardRefresh dashboardId={data.id} />
          {data.can_manage && !dashboard.appProperties.hideEdit && (
            <Button
              className="gap-1.5"
              onClick={() => (studio ? navigate(`/dashboards/${data.id}/studio`) : ask(refinePrompt))}
            >
              <Sparkles className="h-4 w-4" /> {studio ? t_i18n("Open in the studio") : t_i18n("Refine with the assistant")}
            </Button>
          )}
          {/* Only a dashboard read as oneself: an admin reading another
              person's dashboard cannot make it theirs. */}
          {data.can_use_as_home && (
            <Button
              variant={isHome ? "secondary" : "outline"}
              className="gap-1.5"
              aria-pressed={isHome}
              disabled={updateProfile.isPending}
              onClick={toggleHome}
            >
              {isHome ? <Check className="h-4 w-4" /> : <House className="h-4 w-4" />}
              {isHome ? t_i18n("Your dashboard") : t_i18n("Use as my dashboard")}
            </Button>
          )}
          {data.can_manage && (
            <>
              {/* The definition's `hideEdit` keeps the page a view: no studio, no rearranging. */}
              {!dashboard.appProperties.hideEdit && (
                <Button variant="outline" className="gap-1.5" onClick={() => setEditing((on) => !on)}>
                  {editing ? <Check className="h-4 w-4" /> : <Move className="h-4 w-4" />}
                  {editing ? t_i18n("Done") : t_i18n("Rearrange")}
                </Button>
              )}
              <Button variant="outline" className="gap-1.5" onClick={() => setSharing(true)}>
                <Share2 className="h-4 w-4" /> {t_i18n("Share")}
              </Button>
            </>
          )}
          {studio && !dashboard.appProperties.hideExport && (
            <DashboardExportMenu
              dashboardId={data.id}
              title={data.title}
              tabId={tabId ?? dashboard.tabs[0]?.id ?? null}
              tabCount={dashboard.tabs.length}
              schedule={data}
            />
          )}
          {studio && !dashboard.appProperties.hideExport && (
            <Tip content={t_i18n("Publish or export")}>
              <Button variant="ghost" size="icon" aria-label={t_i18n("Publish or export")} onClick={() => setPublishing(true)}>
                <Send className="h-4 w-4" />
              </Button>
            </Tip>
          )}
          <Tip content={t_i18n("Fullscreen")}>
            <Button variant="ghost" size="icon" aria-label={t_i18n("Fullscreen")} onClick={fullscreen.enter} data-testid="dashboard.fullscreen">
              <Maximize className="h-4 w-4" />
            </Button>
          </Tip>
          <Tip content={t_i18n("History")}>
            <Button variant="ghost" size="icon" aria-label={t_i18n("Dashboard history")} onClick={() => setHistory(true)}>
              <History className="h-4 w-4" />
            </Button>
          </Tip>
          {(data.permission === "owner" || data.permission === "manage") && (
            <Tip content={t_i18n("Delete")}>
              <Button
                variant="ghost"
                size="icon"
                aria-label={t_i18n("Delete dashboard")}
                onClick={() => setConfirmingDelete(true)}
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            </Tip>
          )}
        </div>
      </div>
      {!data.can_manage && (
        <p className="flex items-center gap-2 content-compact text-default-secondary">
          <Eye className="h-4 w-4 shrink-0" aria-hidden="true" />
          {data.is_company_managed
            ? t_i18n("A company dashboard: you can view it, only admins change it.")
            : t_i18n("Shared with you to view: you cannot change it.")}
        </p>
      )}

      <DashboardAskContext.Provider value={data.can_manage ? ask : null}>
        <div
          ref={stage}
          className={cn(
            fullscreen.isFullscreen && "relative overflow-auto layer-0 bg-elevation-default p-6",
            fullscreen.mode === "viewport" && "fixed inset-0 m-0 h-dvh w-screen max-w-none border-0",
          )}
        >
          {fullscreen.isFullscreen && (
            <div className="mb-4 flex items-center justify-between gap-3">
              <h2 className="min-w-0 truncate text-xl font-semibold">{data.title}</h2>
              <Button variant="ghost" size="icon" aria-label={t_i18n("Leave fullscreen")} onClick={fullscreen.exit}>
                <Minimize className="h-4 w-4" />
              </Button>
            </div>
          )}
          <CustomDashboardView
            // A link from another dashboard opens this one afresh with the tokens it passes (`?form.<token>=`).
            key={`${data.id}${location.search}`}
            dashboard={dashboard}
            dashboardId={data.id}
            revision={data.revision}
            editing={editingControls}
            onNavigate={navigate}
            onTabChange={setTabId}
            onKeepInput={keepInput}
          />
        </div>
      </DashboardAskContext.Provider>

      <DashboardShareDialog dashboard={data} open={sharing} onOpenChange={setSharing} />
      <DashboardHistoryDialog dashboard={data} open={history} onOpenChange={setHistory} />
      {studio && <DashboardPublishDialog dashboard={data} open={publishing} onOpenChange={setPublishing} />}

      <ConfirmDeleteDialog
        open={confirmingDelete}
        onOpenChange={setConfirmingDelete}
        name={data.title}
        onConfirm={() =>
          remove.mutate(data.id, {
            onSuccess: () => navigate("/dashboards"),
            onError: (error) =>
              toast({
                title: t_i18n("The dashboard could not be deleted"),
                description: error.message,
                variant: "error",
              }),
          })
        }
      />
    </div>
  );
}
