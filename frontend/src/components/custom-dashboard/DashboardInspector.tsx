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
 * The Dashboard Studio's inspector: the widget picked on the stage.
 *
 * Where a widget sits and how large it is are set on the stage's grid (drag
 * it, drag its corner). Here a person edits its heading and, for a live
 * platform widget, how it is drawn (view), and saves it as an `upsert`
 * of the stored widget at the revision read: the server validates it like an
 * agent's write (a view that does not suit the data, an integration one may
 * not use: a 422 said in a toast). What a widget SHOWS is the assistant's to
 * write; its configuration is shown read-only, with its source.
 */
import { useEffect, useMemo, useState } from "react";
import { Spinner } from "@filigran/design-system";
import { LayoutGrid, MousePointerClick, Radar, Save, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useFormatter } from "@/i18n";

import { PLATFORM_NAMES, PLATFORM_VIEWS, type DashboardWidget, type PlatformView } from "./spec";

/** The views drawing the same data, by the view a widget has (`SHAPE_VIEWS` in the backend). */
const VIEW_FAMILIES: PlatformView[][] = [
  ["number", "icon", "radial", "filler", "marker"],
  ["line", "area", "bar", "scatter", "heatmap", "radar"],
  ["distribution", "donut", "hbar", "bar", "treemap", "radar", "polar", "wordcloud"],
  ["list", "table", "scatter", "bubble", "punchcard", "timeline", "linkgraph", "networkgraph", "sankey", "parallel"],
  ["map", "choroplethsvg"],
  ["networkgraph", "linkgraph", "sankey", "attackflow"],
  ["timeline"],
  ["treemap"],
  ["outcomematrix", "pathgraph", "exposureconsole", "scoregauge", "ringgauge", "domaincards"],
];

function viewsLike(view: PlatformView | undefined): PlatformView[] {
  if (!view) return [];
  const families = VIEW_FAMILIES.filter((family) => family.includes(view));
  return PLATFORM_VIEWS.filter((v) => families.some((family) => family.includes(v)));
}

/** How a view is called. @i18n-keys */
const VIEW_LABELS = {
  number: "Figure",
  list: "List",
  distribution: "Ranked bars",
  bar: "Bars",
  hbar: "Horizontal bars",
  line: "Line",
  area: "Area",
  donut: "Donut",
  table: "Table",
  scatter: "Scatter",
  bubble: "Bubbles",
  punchcard: "Punchcard",
  icon: "Figure with icon",
  radial: "Radial figure",
  filler: "Filler gauge",
  marker: "Marker gauge",
  map: "Map",
  choroplethsvg: "Choropleth on an SVG",
  linkgraph: "Link graph",
  networkgraph: "Network graph",
  attackflow: "Attack flow",
  sankey: "Sankey",
  parallel: "Parallel coordinates",
  timeline: "Timeline",
  treemap: "Treemap",
  radar: "Radar",
  polar: "Polar area",
  wordcloud: "Word cloud",
  heatmap: "Heatmap",
  outcomematrix: "Technique matrix",
  pathgraph: "Path graph",
  exposureconsole: "Exposure console",
  scoregauge: "Score gauge",
  ringgauge: "Ring gauge",
  domaincards: "Domain cards",
} as const satisfies Record<PlatformView, string>;

export interface DashboardInspectorProps {
  /** The widget as the renderer reads it. */
  widget: DashboardWidget | null;
  /** The widget as it is stored: what an upsert starts from. */
  stored: Record<string, unknown> | null;
  canEdit: boolean;
  busy: boolean;
  onSave: (widget: Record<string, unknown>) => void;
  onRemove: () => void;
}

export function DashboardInspector({
  widget,
  stored,
  canEdit,
  busy,
  onSave,
  onRemove,
}: DashboardInspectorProps) {
  const { t_i18n } = useFormatter();
  const [title, setTitle] = useState("");
  const [view, setView] = useState<PlatformView | undefined>(undefined);

  // Re-seed when another widget is picked or THIS one changed in store - not
  // on every refetch (a save elsewhere, the window regaining focus), which
  // would wipe what the person is typing.
  const storedKey = stored ? JSON.stringify(stored) : "";
  useEffect(() => {
    if (!widget) return;
    setTitle(widget.title ?? "");
    if (widget.kind === "platform") setView(widget.view);
    // `widget` is re-derived on every refetch; `storedKey` says when it really changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storedKey]);

  const views = useMemo(() => (widget?.kind === "platform" ? viewsLike(widget.view) : []), [widget]);

  if (!widget || !stored) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center content-compact text-default-secondary">
        <MousePointerClick className="h-5 w-5" aria-hidden="true" />
        {t_i18n("Pick a widget on the page to see and change it.")}
      </div>
    );
  }

  const platform = widget.kind === "platform" ? widget : null;
  const dirty = (title || "") !== (widget.title ?? "") || (platform !== null && view !== platform.view);

  const save = () => {
    const next: Record<string, unknown> = { ...stored };
    if (title.trim()) next.title = title.trim();
    else delete next.title;
    if (platform && view) next.view = view;
    onSave(next);
  };

  return (
    <div className="flex h-full flex-col gap-4 overflow-y-auto p-4" data-testid="dashboard.studio.inspector">
      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0 truncate font-mono-plex text-[11px] text-default-secondary">
          {widget.id} · {platform ? `${PLATFORM_NAMES[platform.source]} · ${platform.nativeType}` : widget.kind}
        </span>
        {canEdit && (
          <div className="flex shrink-0 items-center gap-0.5">
            <Button variant="ghost" size="icon" className="h-7 w-7" aria-label={t_i18n("Remove widget")} disabled={busy} onClick={onRemove}>
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        )}
      </div>

      <Input
        label={t_i18n("Heading")}
        value={title}
        onChange={(event) => setTitle(event.target.value)}
        maxLength={120}
        disabled={!canEdit}
      />

      <p className="flex items-start gap-2 content-caption text-default-secondary">
        <LayoutGrid className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" />
        {t_i18n("Column {column}, row {row}, {width} columns wide, {height} rows high. Drag it on the page to move it, its corner to resize it.", {
          column: widget.layout.x + 1,
          row: widget.layout.y + 1,
          width: widget.layout.w,
          height: widget.layout.h,
        })}
      </p>

      {platform && (
        <>
          {views.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <span className="content-caption text-default-secondary">{t_i18n("Drawn as")}</span>
              <Select value={view} onValueChange={(value) => setView(value as PlatformView)} disabled={!canEdit}>
                <SelectTrigger className="w-full" aria-label={t_i18n("Drawn as")}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {views.map((key) => (
                    <SelectItem key={key} value={key}>
                      {t_i18n(VIEW_LABELS[key])}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
          <p className="flex items-start gap-2 content-caption text-default-secondary">
            <Radar className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" />
            {t_i18n("Read live from {platform} with each viewer's own access.", {
              platform: PLATFORM_NAMES[platform.source],
            })}
          </p>
        </>
      )}

      <details className="rounded-sm border border-elevation-subtle">
        <DisclosureSummary size="block" className="px-3 py-2 content-caption text-default-secondary">
          {platform ? t_i18n("Platform configuration") : t_i18n("Content")}
        </DisclosureSummary>
        <pre className="max-h-64 overflow-auto px-3 pb-3 font-mono-plex text-[11px] leading-snug text-default-secondary">
          {JSON.stringify(platform ? stored.native : stored, null, 2)}
        </pre>
      </details>
      <p className="content-caption text-default-secondary">
        {t_i18n("Ask the assistant to change what the widget shows.")}
      </p>

      {canEdit && (
        <Button className="mt-auto gap-1.5" disabled={!dirty || busy} aria-busy={busy || undefined} onClick={save}>
          {busy && <Spinner size="sm" tone="inherit" />}
          {!busy && <Save className="h-4 w-4" />}
          {t_i18n("Save widget")}
        </Button>
      )}
    </div>
  );
}
