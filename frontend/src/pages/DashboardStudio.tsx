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
 * The Dashboard Studio - `/dashboards/:id/studio`: a dashboard and the
 * assistant building it, side by side, like the Presentation and Video Studios.
 *
 * - Left: the studio rail, a chat bound to this dashboard
 *   (`DashboardStudioRail`): every message says which widget is selected, and
 *   a save by the dashboard tools refreshes the stage at once.
 * - Centre: the stage, the dashboard as everyone sees it - live platform
 *   widgets drawn with the viewer's own access - where a click picks a widget.
 * - Right: the picked visualization's configuration panel
 *   (`VisualizationConfig`: general, data source, data display,
 *   coloring, interactions, visibility, position, source), each field a step
 *   of the designer's history - for a reader, the inspector
 *   (`DashboardInspector`), read-only - and the Packs panel (`DashboardPacksPanel`):
 *   ready-made widgets inserted below the selected one - and the Data panel
 *   (`DashboardDataSourcesPanel`): the dashboard's data sources - and the
 *   Inputs panel (`DashboardInputsPanel`): its inputs and token manager.
 * - The source editor (`DashboardSourceDialog`): the definition as JSON, whole
 *   or the picked widget's visualization and data source.
 * - The bar, the Presentation and Video Studios' (`StudioChrome`): the title,
 *   renamed in place while editing, and whether everything is saved; the
 *   primary verb, `Present` (the viewers' view, full screen); the edit toggle,
 *   undo and redo; Refresh; the studio, sharing, downloads and `More actions`
 *   (publish, source, history, the settings panel, duplicate, delete).
 * - Editing and the viewers' view: while editing (those who may edit) the
 *   toolbar (`DashboardDesignBar`) adds charts and inputs, elements to a canvas
 *   tab, layers, aligns, duplicates and deletes them, opens the data sources
 *   and the source editor and switches the theme; the tab bar adds, renames,
 *   moves, retypes and removes tabs; the inspector edits the picked element,
 *   the canvas and the dashboard's theme and tab bar (`CanvasInspector`). All
 *   of it is the designer's history (`useDashboardDesigner`), saved as it
 *   goes. View mode shows the dashboard as its viewers do. Either can be shown
 *   fullscreen.
 *
 * The rail is out unless the URL folds it (`?studio=0`), on the conversation
 * the URL names (`?c=`). The "Create a dashboard" dialog hands the page a
 * brief to send and the agent to send it to (`DashboardStudioLaunch`, the
 * navigation state, read once). When a dashboard tool saves, the stage
 * follows it: the widget it added or changed is selected and scrolled into
 * view once that revision has loaded. Only those who may edit the dashboard get the rail
 * and the inspector's controls; others see the stage. Behind `DASHBOARD_STUDIO`.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  Braces,
  Copy,
  History,
  LayoutGrid,
  Minimize,
  MonitorPlay,
  PanelRightClose,
  PanelRightOpen,
  Pencil,
  Redo2,
  Send,
  Trash2,
  Undo2,
} from "lucide-react";

import {
  CanvasItemInspector,
  CanvasSelectionSummary,
  CanvasSettings,
  DashboardSettings,
  NothingPicked,
} from "@/components/custom-dashboard/CanvasInspector";
import { placeBlocks, removeItems } from "@/components/custom-dashboard/canvas";
import { DashboardDesignBar } from "@/components/custom-dashboard/DashboardDesignBar";
import { useDashboardDesigner } from "@/components/custom-dashboard/useDashboardDesigner";
import { ToolbarVerb } from "@/components/presentation/DeckToolbar";
import { useElementFullscreen } from "@/components/presentation/fullscreen";

import { CustomDashboardView } from "@/components/custom-dashboard/CustomDashboardView";
import { DashboardDataSourcesPanel } from "@/components/custom-dashboard/DashboardDataSourcesPanel";
import { DashboardHistoryDialog } from "@/components/custom-dashboard/DashboardHistoryDialog";
import { DashboardInputsPanel } from "@/components/custom-dashboard/DashboardInputsPanel";
import { DashboardInspector } from "@/components/custom-dashboard/DashboardInspector";
import { VisualizationConfig } from "@/components/custom-dashboard/VisualizationConfig";
import { VisualizationInteractions } from "@/components/custom-dashboard/VisualizationInteractions";
import { DashboardPacksPanel } from "@/components/custom-dashboard/DashboardPacksPanel";
import { DashboardExportMenu } from "@/components/custom-dashboard/DashboardExportMenu";
import { DashboardPublishDialog } from "@/components/custom-dashboard/DashboardPublishDialog";
import { DashboardShareDialog } from "@/components/custom-dashboard/DashboardShareDialog";
import { DashboardSourceDialog } from "@/components/custom-dashboard/DashboardSourceDialog";
import { DashboardStudioHandle, DashboardStudioRail } from "@/components/custom-dashboard/DashboardStudioRail";
import { DashboardRefresh } from "@/components/custom-dashboard/DashboardRefresh";
import { useKeepInput } from "@/components/custom-dashboard/keep-input";
import { normalizeDashboard, type DashboardWidget } from "@/components/custom-dashboard/spec";
import {
  STUDIO_BAR_BUTTON,
  STUDIO_BAR_TITLE,
  StudioBarLeading,
  STUDIO_STAGE_BACKDROP,
  StudioLoading,
  StudioSaveState,
  StudioShareButton,
  StudioTitleField,
  StudioToggle,
  StudioUnavailable,
} from "@/components/studio/StudioChrome";
import { Button } from "@/components/ui/button";
import { ConfirmDeleteDialog } from "@/components/ui/confirm-delete-dialog";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { PanelResizeHandle } from "@/components/ui/panel-resize-handle";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { useToast } from "@/components/ui/toast";
import { Tip } from "@/components/ui/tooltip";
import {
  useCustomDashboard,
  useDeleteCustomDashboard,
  useDuplicateCustomDashboard,
  useQueuedDashboardEdit,
} from "@/hooks/api/useCustomDashboardApi";
import { useResizableWidth } from "@/hooks/useResizableWidth";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { CustomDashboardOperation } from "@/types";

const STUDIO_PARAM = "studio";
const STUDIO_CONVERSATION_PARAM = "c";
/** A title, at most (`custom_dashboard_spec.MAX_TITLE_CHARS`). */
const MAX_TITLE = 120;
/** The settings panel: the video inspector's width, to a third of the screen. */
const INSPECTOR_WIDTH = { defaultWidth: 360, minWidth: 320, maxWidth: (viewport: number) => Math.floor(viewport / 3) };

/** What the "Create a dashboard" dialog hands the studio: the brief to send the assistant, and who answers. */
export interface DashboardStudioLaunch {
  send?: string;
  /** An agent id, or `null` for the General Assistant. */
  agentId?: string | null;
}

/** The dashboard's visualizations as stored widgets (the response's `widgets`): what an upsert starts from. */
function storedWidgets(widgets: unknown): Record<string, unknown>[] {
  return Array.isArray(widgets) ? widgets.filter((w): w is Record<string, unknown> => !!w && typeof w === "object") : [];
}

export default function DashboardStudio() {
  const { id = "" } = useParams<{ id: string }>();
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const { data, isLoading, isError } = useCustomDashboard(id);
  const update = useQueuedDashboardEdit(id, data?.revision);
  const refused = useCallback(
    (error: Error & { status?: number }) => {
      const conflict = error.status === 409;
      toast({
        title: conflict ? t_i18n("This dashboard changed meanwhile") : t_i18n("The dashboard could not be changed"),
        description: conflict ? t_i18n("Here is its latest version. Make your change again.") : error.message,
        variant: conflict ? "info" : "error",
      });
    },
    [t_i18n, toast],
  );
  const designer = useDashboardDesigner({ dashboard: data, edit: update.edit, replace: update.replace, onRefused: refused });
  // What the stage shows: the designer's definition (saved or being saved), else the stored one.
  const definition = designer.definition ?? (data?.spec as Record<string, unknown> | undefined);
  const dashboard = useMemo(() => (definition ? normalizeDashboard(definition) : null), [definition]);
  const [mode, setMode] = useState<"edit" | "view">("edit");
  const [tabId, setTabId] = useState<string | undefined>(undefined);
  const [zoom, setZoom] = useState<number | null>(null);
  const stage = useRef<HTMLElement>(null);
  const fullscreen = useElementFullscreen(stage);
  // The mode an editor presented from, given back when full screen ends.
  const presentedFrom = useRef<"edit" | "view" | null>(null);
  useEffect(() => {
    if (fullscreen.isFullscreen || !presentedFrom.current) return;
    setMode(presentedFrom.current);
    presentedFrom.current = null;
  }, [fullscreen.isFullscreen]);
  // Esc leaves the presentation: the browser's own full screen hears it, the app's viewport mode does not.
  const { isFullscreen, exit: leaveFullscreen } = fullscreen;
  useEffect(() => {
    if (!isFullscreen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented) leaveFullscreen();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [isFullscreen, leaveFullscreen]);
  const duplicate = useDuplicateCustomDashboard();
  const remove = useDeleteCustomDashboard();
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const selectedId = designer.selection.length === 1 ? designer.selection[0] : null;
  const setSelectedId = useCallback(
    (next: string | null | ((current: string | null) => string | null)) => {
      const value = typeof next === "function" ? next(selectedId) : next;
      designer.select(value ? [value] : []);
    },
    [designer, selectedId],
  );
  const [sharing, setSharing] = useState(false);
  const [history, setHistory] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [editingSource, setEditingSource] = useState(false);
  const [studioOpen, setStudioOpen] = useState(() => searchParams.get(STUDIO_PARAM) !== "0");
  const [studioConvId, setStudioConvId] = useState<string | null>(() => searchParams.get(STUDIO_CONVERSATION_PARAM));
  const [primed, setPrimed] = useState<string | null>(null);
  // The dialog's brief is sent once: the launch state leaves the history
  // entry, so a reload or a return to this page never sends it again.
  const launch = (location.state ?? null) as DashboardStudioLaunch | null;
  const [autoSend, setAutoSend] = useState<string | null>(launch?.send ?? null);
  const [initialAgentId] = useState(() => (launch && "agentId" in launch ? launch.agentId : undefined));
  useEffect(() => {
    if (location.state) navigate(`${location.pathname}${location.search}`, { replace: true, state: null });
  }, [location.pathname, location.search, location.state, navigate]);
  const clearAutoSend = useCallback(() => setAutoSend(null), []);
  const [panel, setPanel] = useState<"widget" | "data" | "inputs" | "packs">("widget");
  // The settings panel folds away to give the canvas the whole width; asking for one of its tabs unfolds it.
  const [panelOpen, setPanelOpen] = useState(true);
  const inspectorPanel = useResizableWidth({ ...INSPECTOR_WIDTH, storageKey: "xtm-dashboard-inspector-width", edge: "start" });
  const showPanel = (next: typeof panel) => {
    setPanel(next);
    setPanelOpen(true);
  };
  const [stageTokens, setStageTokens] = useState<Record<string, unknown>>({});
  const [sourceToEdit, setSourceToEdit] = useState<string | null>(null);

  useEffect(() => {
    const url = new URL(window.location.href);
    if (studioOpen) url.searchParams.delete(STUDIO_PARAM);
    else url.searchParams.set(STUDIO_PARAM, "0");
    if (studioConvId) url.searchParams.set(STUDIO_CONVERSATION_PARAM, studioConvId);
    else url.searchParams.delete(STUDIO_CONVERSATION_PARAM);
    if (url.href !== window.location.href) window.history.replaceState(window.history.state, "", url.href);
  }, [studioConvId, studioOpen]);

  // Every tab's widgets: the picked one may be on any of them.
  const widgets = useMemo(() => dashboard?.tabs.flatMap((tab) => tab.widgets) ?? [], [dashboard]);
  const selected = widgets.find((w) => w.id === selectedId) ?? null;
  const stored = useMemo(
    () => storedWidgets(data?.widgets).find((w) => w.id === selectedId) ?? null,
    [data?.widgets, selectedId],
  );
  const focusedWidget = useMemo(
    () => (selected ? { id: selected.id, label: selected.title || selected.id } : null),
    [selected],
  );
  // Follow the pen: the widget a dashboard tool added or changed, once its revision is on the stage.
  const [follow, setFollow] = useState<{ revision: number; widgetId: string } | null>(null);
  const onSaved = useCallback((revision: number, widgetId: string | null) => {
    if (widgetId) setFollow({ revision, widgetId });
  }, []);
  useEffect(() => {
    if (!follow || !dashboard || (data?.revision ?? 0) < follow.revision) return;
    const home = dashboard.tabs.find((candidate) => candidate.widgets.some((w) => w.id === follow.widgetId));
    setFollow(null);
    if (!home) return;
    setTabId(home.id);
    setSelectedId(follow.widgetId);
    // After the tab switch has drawn the widget.
    window.requestAnimationFrame(() => {
      const element = Array.from(stage.current?.querySelectorAll<HTMLElement>("[data-widget-id]") ?? []).find(
        (candidate) => candidate.dataset.widgetId === follow.widgetId,
      );
      element?.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
    });
  }, [follow, dashboard, data?.revision, setSelectedId]);
  const editing = Boolean(data?.can_manage) && mode === "edit";
  const tab = dashboard?.tabs.find((candidate) => candidate.id === tabId) ?? dashboard?.tabs[0];
  const canvasLayoutId = editing && tab?.type === "absolute" ? tab.id : null;
  const canvasItem = canvasLayoutId && selectedId ? tab?.canvas?.items.find((item) => item.id === selectedId) : undefined;
  // A canvas's shapes, icons, images and markdown have their own inspector; a visualization has the configuration panel.
  const canvasElement = canvasItem && (canvasItem.type === "line" || canvasItem.element) ? canvasItem : undefined;
  const configured =
    editing && selectedId && !canvasElement && (designer.definition?.visualizations as Record<string, unknown> | undefined)?.[selectedId]
      ? selectedId
      : null;
  const openSource = (sid: string | null) => {
    setSourceToEdit(sid);
    setEditingSource(true);
  };

  // Undo and redo wherever the focus is not in a field (the canvas handles its own keys first).
  const { undo, redo } = designer;
  useEffect(() => {
    if (!editing) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || !(event.metaKey || event.ctrlKey) || event.altKey) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, textarea, select, [contenteditable='true'], [role='dialog']")) return;
      const key = event.key.toLowerCase();
      if (key === "z" && !event.shiftKey) undo();
      else if ((key === "z" && event.shiftKey) || key === "y") redo();
      else return;
      event.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [editing, redo, undo]);

  const keepInput = useKeepInput(data?.spec, Boolean(data?.can_manage), update.edit);
  if (isLoading) return <StudioLoading />;
  if (isError || !data || !dashboard) {
    return (
      <StudioUnavailable
        title={t_i18n("Dashboard not found")}
        description={t_i18n("It was deleted, or it is not shared with you.")}
        backLabel={t_i18n("Back to Dashboards")}
        onBack={() => navigate("/dashboards")}
      />
    );
  }

  const edit = (operations: CustomDashboardOperation[], after?: () => void) =>
    update.edit(
      operations,
      {
        onSuccess: () => after?.(),
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

  const title = typeof designer.definition?.title === "string" ? designer.definition.title : data.title;
  const rename = (next: string) =>
    designer.change((current) => ({ ...current, title: next.slice(0, MAX_TITLE) }), "dashboard.title");
  // Present: the dashboard as its viewers see it, full screen; leaving full screen gives the editor back its mode.
  const present = () => {
    presentedFrom.current = mode;
    setMode("view");
    designer.select([]);
    if (!fullscreen.isFullscreen) fullscreen.toggle();
  };
  const menu: OverflowMenuItem[] = [
    { id: "publish", label: t_i18n("Publish or export"), icon: Send, onSelect: () => setPublishing(true) },
    { id: "source", label: t_i18n("Dashboard source"), icon: Braces, onSelect: () => openSource(null) },
    { id: "history", label: t_i18n("Dashboard history"), icon: History, onSelect: () => setHistory(true) },
    {
      id: "panel",
      label: panelOpen ? t_i18n("Hide the settings panel") : t_i18n("Show the settings panel"),
      icon: panelOpen ? PanelRightClose : PanelRightOpen,
      onSelect: () => setPanelOpen((open) => !open),
    },
    {
      id: "duplicate",
      label: t_i18n("Duplicate"),
      icon: Copy,
      separatorBefore: true,
      onSelect: () =>
        duplicate.mutate(data.id, {
          onSuccess: (copy) => navigate(`/dashboards/${copy.id}/studio`),
          onError: (error) => toast({ title: t_i18n("The dashboard could not be duplicated"), description: error.message, variant: "error" }),
        }),
    },
    ...(data.can_manage
      ? [{ id: "delete", label: t_i18n("Delete"), icon: Trash2, destructive: true, onSelect: () => setConfirmingDelete(true) }]
      : []),
  ];

  return (
    <div className="flex h-dvh w-screen overflow-hidden bg-elevation-default text-default-primary" data-testid="dashboard.studio">
      {data.can_manage &&
        (studioOpen ? (
          <DashboardStudioRail
            dashboard={data}
            focusedWidget={focusedWidget}
            onSaved={onSaved}
            initialConversationId={studioConvId}
            onConversationChange={setStudioConvId}
            onCollapse={() => setStudioOpen(false)}
            primedMessage={primed}
            onPrimedMessageApplied={() => setPrimed(null)}
            autoSendMessage={autoSend}
            onAutoSendApplied={clearAutoSend}
            initialAgentId={initialAgentId}
          />
        ) : (
          <DashboardStudioHandle onOpen={() => setStudioOpen(true)} />
        ))}

      <main className="flex min-w-0 flex-1 flex-col">
        {/* A container: under 56rem of bar the save state drops its word, so the title keeps its room. */}
        <header className="@container flex min-h-12 shrink-0 items-center gap-2 border-b border-elevation-subtle-soft layer-1 bg-elevation-default px-3 py-1.5">
          <div className="flex min-w-0 flex-1 items-center gap-2">
            <StudioBarLeading
              backLabel={t_i18n("Back to the dashboard")}
              onBack={() => navigate(`/dashboards/${data.id}`)}
              icon={LayoutGrid}
            >
              {editing ? (
                <StudioTitleField
                  value={title}
                  onChange={rename}
                  onBlur={() => !title.trim() && rename(data.title)}
                  maxLength={MAX_TITLE}
                  testId="dashboard.studio.title"
                />
              ) : (
                <h1 className={STUDIO_BAR_TITLE}>{title}</h1>
              )}
            </StudioBarLeading>
            {data.can_manage && (
              <StudioSaveState
                status={designer.saveState === "saved" ? "saved" : designer.saveState === "saving" ? "saving" : "unsaved"}
                testId="dashboard.studio.saveState"
              />
            )}
          </div>
          <div className="flex shrink-0 items-center gap-0.5">
            <ToolbarVerb
              icon={MonitorPlay}
              label={t_i18n("Present")}
              tooltip={t_i18n("Present: the dashboard as its viewers see it, full screen")}
              onClick={present}
              primary
              labelClassName="@max-4xl:hidden"
              testId="dashboard.studio.present"
            />
            {data.can_manage && (
              <Tip content={editing ? t_i18n("Show it as its viewers see it") : t_i18n("Edit the dashboard")}>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setMode(editing ? "view" : "edit");
                    if (editing) designer.select([]);
                  }}
                  aria-label={t_i18n("Edit the dashboard")}
                  aria-pressed={editing}
                  className={cn(STUDIO_BAR_BUTTON, "ml-1", editing && "bg-accent/15 text-accent")}
                  data-testid="dashboard.studio.edit"
                >
                  <Pencil className="h-4 w-4" />
                </Button>
              </Tip>
            )}
            {editing && (
              <>
                <Tip content={`${t_i18n("Undo")} (Ctrl+Z)`}>
                  <Button variant="ghost" size="sm" className={STUDIO_BAR_BUTTON} onClick={designer.undo} disabled={!designer.canUndo} aria-label={t_i18n("Undo")}>
                    <Undo2 className="h-4 w-4" />
                  </Button>
                </Tip>
                <Tip content={`${t_i18n("Redo")} (Ctrl+Shift+Z)`}>
                  <Button variant="ghost" size="sm" className={STUDIO_BAR_BUTTON} onClick={designer.redo} disabled={!designer.canRedo} aria-label={t_i18n("Redo")}>
                    <Redo2 className="h-4 w-4" />
                  </Button>
                </Tip>
              </>
            )}
            <DashboardRefresh dashboardId={data.id} compact />
            {data.can_manage && (
              <>
                <StudioToggle open={studioOpen} onToggle={() => setStudioOpen((open) => !open)} testId="dashboard.studio" />
                <StudioShareButton onClick={() => setSharing(true)} testId="dashboard.studio.share" />
              </>
            )}
            {!dashboard.appProperties.hideExport && (
              <DashboardExportMenu
                dashboardId={data.id}
                title={data.title}
                tabId={tab?.id ?? null}
                tabCount={dashboard.tabs.length}
                schedule={data}
                className={STUDIO_BAR_BUTTON}
              />
            )}
            <OverflowMenu items={menu} label={t_i18n("More actions")} />
          </div>
        </header>

        {editing && (
          <DashboardDesignBar
            designer={designer}
            layoutId={tab?.id ?? null}
            canvasLayoutId={canvasLayoutId}
            zoom={zoom}
            onZoom={setZoom}
            theme={dashboard.theme}
            onAdded={(_, kind) => showPanel(kind === "input" ? "inputs" : "widget")}
            onOpenData={() => showPanel("data")}
            onOpenSource={() => openSource(null)}
          />
        )}

        <div className="flex min-h-0 flex-1">
          <section
            ref={stage}
            className={cn(
              "min-w-0 flex-1 overflow-y-auto p-6",
              STUDIO_STAGE_BACKDROP,
              fullscreen.isFullscreen && "layer-0 bg-elevation-default",
              fullscreen.mode === "viewport" && "fixed inset-0 m-0 h-dvh w-screen max-w-none border-0",
            )}
            aria-label={t_i18n("Dashboard")}
          >
            {fullscreen.isFullscreen && (
              // The bar is under the presented stage: the way out stays on it.
              <div className="sticky top-0 z-10 flex h-0 justify-end">
                <Tip content={`${t_i18n("Leave fullscreen")} (Esc)`}>
                  <Button variant="outline" size="sm" className="gap-1.5" onClick={fullscreen.exit} data-testid="dashboard.studio.leave">
                    <Minimize className="h-4 w-4" aria-hidden="true" />
                    {t_i18n("Leave fullscreen")}
                  </Button>
                </Tip>
              </div>
            )}
            {dashboard.subtitle && <p className="mb-4 content-base text-default-secondary">{dashboard.subtitle}</p>}
            <CustomDashboardView
              dashboard={dashboard}
              dashboardId={data.id}
              revision={data.revision}
              tabId={tab?.id}
              onTabChange={(next) => {
                setTabId(next);
                designer.select([]);
              }}
              onTokens={setStageTokens}
              onKeepInput={keepInput}
              // A reader picks a widget to read its settings; an editor in View mode sees what viewers see.
              selection={editing || !data.can_manage ? { selectedId, onSelect: (widget: DashboardWidget) => setSelectedId(widget.id) } : null}
              editing={
                editing
                  ? {
                      embedded: true,
                      onLayout: (operation) => {
                        const layoutId = operation.layoutId ?? dashboard.tabs[0]?.id;
                        if (!layoutId || !operation.items) return;
                        designer.change((current) => placeBlocks(current, layoutId, operation.items ?? []));
                      },
                      onRemove: (widget) => {
                        designer.change((current) => removeItems(current, [widget.id]));
                        setSelectedId((current) => (current === widget.id ? null : current));
                      },
                    }
                  : null
              }
              design={
                editing && designer.definition
                  ? {
                      definition: designer.definition,
                      onChange: designer.change,
                      selection: designer.selection,
                      onSelectIds: designer.select,
                      onShortcut: designer.onShortcut(tab?.id ?? ""),
                      zoom,
                    }
                  : null
              }
              expandable={!editing}
            />
          </section>
          <aside
            className={cn(
              "relative hidden shrink-0 flex-col border-l border-elevation-subtle-soft layer-1 bg-elevation-default",
              panelOpen && "lg:flex",
              inspectorPanel.isResizing && "select-none",
            )}
            style={{ width: inspectorPanel.width }}
            aria-label={t_i18n("Inspector")}
          >
            <PanelResizeHandle
              panel={inspectorPanel}
              edge="left"
              label={t_i18n("Resize the inspector")}
              tip={t_i18n("Drag to resize the panel - double-click to reset")}
              testId="dashboard.studio.inspector.resize"
            />
            <div className="shrink-0 border-b border-elevation-subtle-soft p-2">
              <SegmentedFilter
                ariaLabel={t_i18n("Inspector")}
                className="w-full justify-between"
                value={panel}
                onChange={setPanel}
                options={[
                  { key: "widget", label: t_i18n("Widget") },
                  { key: "data", label: t_i18n("Data") },
                  { key: "inputs", label: t_i18n("Inputs") },
                  { key: "packs", label: t_i18n("Packs") },
                ]}
              />
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">
              {panel === "widget" ? (
                configured ? (
                  <VisualizationConfig
                    key={configured}
                    dashboard={data}
                    designer={designer}
                    vizId={configured}
                    layoutId={tab?.id ?? null}
                    onEditSource={openSource}
                    onOpenDataPanel={() => showPanel("data")}
                  />
                ) : canvasLayoutId && tab?.canvas && canvasElement ? (
                  <CanvasItemInspector designer={designer} layoutId={canvasLayoutId} item={canvasElement} model={tab.canvas} dashboardId={data.id} />
                ) : canvasLayoutId && designer.selection.length > 1 ? (
                  <CanvasSelectionSummary count={designer.selection.length} onClear={() => designer.select([])} />
                ) : canvasLayoutId && tab?.canvas ? (
                  <>
                    <NothingPicked />
                    <CanvasSettings designer={designer} layoutId={canvasLayoutId} model={tab.canvas} dashboardId={data.id} />
                    <DashboardSettings
                      designer={designer}
                      theme={dashboard.theme}
                      showTabBar={dashboard.showTabBar}
                      barPosition={dashboard.tabBarPosition ?? "top"}
                    />
                  </>
                ) : editing && !selected ? (
                  // Nothing picked: one line, and the dashboard's own settings in view, as the Video Studio's
                  // inspector always shows what can be set.
                  <>
                    <p className="p-4 pb-0 content-compact text-default-secondary" data-testid="dashboard.studio.nothingPicked">
                      {t_i18n("Pick a widget on the page to see and change it.")}
                    </p>
                    <DashboardSettings
                      designer={designer}
                      theme={dashboard.theme}
                      showTabBar={dashboard.showTabBar}
                      barPosition={dashboard.tabBarPosition ?? "top"}
                    />
                  </>
                ) : (
                  <>
                    <DashboardInspector
                      widget={selected}
                      stored={stored}
                      canEdit={data.can_manage}
                      busy={update.isPending}
                      onSave={(widget) => edit([{ op: "upsert", widget }])}
                      onRemove={() => selected && edit([{ op: "remove", id: selected.id }], () => setSelectedId(null))}
                    />
                    {selected && (
                      <VisualizationInteractions
                        key={`${selected.id}-${data.revision}`}
                        dashboard={data}
                        vizId={selected.id}
                        edit={update.edit}
                      />
                    )}
                  </>
                )
              ) : panel === "inputs" ? (
                <DashboardInputsPanel
                  dashboard={data}
                  tokens={stageTokens}
                  edit={update.edit}
                  onEditInput={openSource}
                />
              ) : panel === "data" ? (
                <DashboardDataSourcesPanel
                  dashboard={data}
                  layoutId={tab?.id ?? null}
                  edit={update.edit}
                  onEditSource={openSource}
                  onVisualizationAdded={(vid) => setSelectedId(vid)}
                />
              ) : (
                <DashboardPacksPanel
                  dashboard={data}
                  afterWidgetId={selectedId}
                  onInserted={(ids) => {
                    setSelectedId(ids[0] ?? null);
                    setPanel("widget");
                  }}
                />
              )}
            </div>
          </aside>
        </div>
      </main>

      <DashboardShareDialog dashboard={data} open={sharing} onOpenChange={setSharing} />
      <ConfirmDeleteDialog
        open={confirmingDelete}
        onOpenChange={setConfirmingDelete}
        name={data.title}
        onConfirm={() =>
          remove.mutate(data.id, {
            onSuccess: () => navigate("/dashboards"),
            onError: (error) =>
              toast({ title: t_i18n("The dashboard could not be deleted"), description: error.message, variant: "error" }),
          })
        }
      />
      <DashboardHistoryDialog dashboard={data} open={history} onOpenChange={setHistory} />
      <DashboardPublishDialog dashboard={data} open={publishing} onOpenChange={setPublishing} />
      <DashboardSourceDialog
        dashboard={data}
        selectedId={selectedId}
        sourceId={sourceToEdit}
        open={editingSource}
        onOpenChange={(open) => {
          setEditingSource(open);
          if (!open) setSourceToEdit(null);
        }}
      />
    </div>
  );
}
