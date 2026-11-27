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
 * The interactions and the visibility of the picked visualization, in the
 * studio's Widget panel - its "Interactions" and "Visibility" sections.
 *
 * - Interactions (a visualization drawing data): what a click does, in order -
 *   set a token from what was clicked (its name, its value, a row's field) or
 *   to a written value, unset a token, open a link, open another dashboard
 *   with a token, open the platform's page, switch tab, or (a series) set a
 *   time range from a brush. Each added or removed handler is one `set`.
 * - Visibility: show only when an expression holds (`isSet($host$)`), hide
 *   when one does, hide when there is no data. Saved as the visualization's
 *   own conditions (`expressions.conditions.show_<id>` / `hide_<id>`).
 *
 * Every write is validated server-side; a refusal is shown with its field.
 */
import { useMemo, useState } from "react";
import { ArrowDown, ArrowUp, MousePointerClick, Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Switch } from "@/components/ui/switch";
import { useCustomDashboards } from "@/hooks/api/useCustomDashboardApi";
import { useFormatter } from "@/i18n";
import type { CustomDashboard, CustomDashboardOperation } from "@/types";

import { handlersOperation, readHandlers, visibilityForm, visibilityOperations, type EventHandler } from "./interactions";

type Edit = (operations: CustomDashboardOperation[], handlers?: { onSuccess?: () => void; onError?: (error: Error) => void }) => void;

/** Each handler type's label. @i18n-keys */
const HANDLER_LABELS = {
  "drilldown.setToken": "Set a token",
  "drilldown.unsetToken": "Unset a token",
  "drilldown.linkToUrl": "Open a link",
  "drilldown.linkToDashboard": "Open a dashboard",
  "drilldown.openInPlatform": "Open in the platform",
  "drilldown.switchTab": "Switch tab",
  "drilldown.setTimeRange": "Set a time range (brush)",
} as const;
type HandlerType = keyof typeof HANDLER_LABELS;

/** What a token may take from a click. @i18n-keys */
const KEY_LABELS = { name: "The name clicked", value: "The value clicked", x: "The x clicked", y: "The y clicked", row: "A field of the row clicked", written: "A value written" } as const;
type KeyChoice = keyof typeof KEY_LABELS;

function Labelled({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="content-caption text-default-secondary">{label}</span>
      {children}
    </div>
  );
}

function summary(handler: EventHandler, t: (key: string, values?: Record<string, string | number>) => string): string {
  const o = handler.options as Record<string, unknown>;
  const tokens = (Array.isArray(o.tokens) ? o.tokens : []) as { token?: string; key?: string; value?: string }[];
  switch (handler.type) {
    case "drilldown.setToken":
      return tokens.map((entry) => `$${entry.token}$ ← ${entry.key ?? `"${entry.value}"`}`).join(", ");
    case "drilldown.unsetToken":
      return tokens.map((entry) => `$${entry.token}$`).join(", ");
    case "drilldown.linkToUrl":
      return String(o.url ?? "");
    case "drilldown.linkToDashboard":
      return t("Dashboard {id}", { id: String(o.dashboardId ?? "").slice(0, 8) });
    case "drilldown.switchTab":
      return String(o.layoutId ?? "");
    case "drilldown.setTimeRange":
      return `$${String(o.token ?? "")}.earliest$ – $${String(o.token ?? "")}.latest$`;
    default:
      return "";
  }
}

function NewHandler({ dashboard, onAdd }: { dashboard: CustomDashboard; onAdd: (handler: EventHandler) => void }) {
  const { t_i18n } = useFormatter();
  const [type, setType] = useState<HandlerType>("drilldown.setToken");
  const [token, setToken] = useState("");
  const [from, setFrom] = useState<KeyChoice>("value");
  const [field, setField] = useState("");
  const [value, setValue] = useState("");
  const [target, setTarget] = useState("");
  const [newTab, setNewTab] = useState(false);
  const dashboards = useCustomDashboards();
  const tabs = useMemo(() => {
    const items = (dashboard.spec as { layout?: { tabs?: { items?: { layoutId: string; label: string }[] } } })?.layout?.tabs?.items;
    return Array.isArray(items) ? items : [];
  }, [dashboard.spec]);

  const build = (): EventHandler | null => {
    switch (type) {
      case "drilldown.setToken": {
        if (!token) return null;
        const entry = from === "written" ? { token, value } : { token, key: from === "row" ? `row.${field}.value` : from };
        return { type, options: { tokens: [entry] } };
      }
      case "drilldown.unsetToken":
        return token ? { type, options: { tokens: [{ token }] } } : null;
      case "drilldown.linkToUrl":
        return value ? { type, options: { url: value, newTab } } : null;
      case "drilldown.linkToDashboard":
        return target ? { type, options: { dashboardId: target, newTab, tokens: token ? [{ token, value: value || "$click.value$" }] : [] } } : null;
      case "drilldown.openInPlatform":
        return { type, options: {} };
      case "drilldown.switchTab":
        return target ? { type, options: { layoutId: target } } : null;
      case "drilldown.setTimeRange":
        return token ? { type, options: { token } } : null;
    }
  };
  const handler = build();
  const asksToken = ["drilldown.setToken", "drilldown.unsetToken", "drilldown.setTimeRange", "drilldown.linkToDashboard"].includes(type);

  return (
    <div className="flex flex-col gap-3 rounded-sm border border-dashed border-elevation-subtle p-2.5" data-testid="dashboard.interactions.new">
      <Labelled label={t_i18n("When clicked")}>
        <SearchableSelect
          aria-label={t_i18n("When clicked")}
          value={type}
          onValueChange={(next) => setType(next as HandlerType)}
          options={(Object.keys(HANDLER_LABELS) as HandlerType[]).map((key) => ({ value: key, label: t_i18n(HANDLER_LABELS[key]) }))}
          sort={false}
          searchable="auto"
        />
      </Labelled>
      {asksToken && (
        <Labelled label={t_i18n("Token")}>
          <Input value={token} onChange={(event) => setToken(event.target.value)} placeholder="host" className="h-8" aria-label={t_i18n("Token")} />
        </Labelled>
      )}
      {type === "drilldown.setToken" && (
        <Labelled label={t_i18n("Takes")}>
          <SearchableSelect
            aria-label={t_i18n("Takes")}
            value={from}
            onValueChange={(next) => setFrom(next as KeyChoice)}
            options={(Object.keys(KEY_LABELS) as KeyChoice[]).map((key) => ({ value: key, label: t_i18n(KEY_LABELS[key]) }))}
            sort={false}
            searchable="auto"
          />
        </Labelled>
      )}
      {type === "drilldown.setToken" && from === "row" && (
        <Labelled label={t_i18n("Field")}>
          <Input value={field} onChange={(event) => setField(event.target.value)} placeholder="host" className="h-8" aria-label={t_i18n("Field")} />
        </Labelled>
      )}
      {((type === "drilldown.setToken" && from === "written") || type === "drilldown.linkToUrl" || type === "drilldown.linkToDashboard") && (
        <Labelled label={type === "drilldown.linkToUrl" ? t_i18n("Link") : t_i18n("Value")}>
          <Input
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder={type === "drilldown.linkToUrl" ? "/dashboards?q=$click.value$" : "$click.value$"}
            className="h-8"
            aria-label={type === "drilldown.linkToUrl" ? t_i18n("Link") : t_i18n("Value")}
          />
        </Labelled>
      )}
      {type === "drilldown.linkToDashboard" && (
        <Labelled label={t_i18n("Dashboard")}>
          <SearchableSelect
            aria-label={t_i18n("Dashboard")}
            value={target}
            onValueChange={setTarget}
            options={(dashboards.data ?? []).filter((item) => item.id !== dashboard.id).map((item) => ({ value: item.id, label: item.title }))}
          />
        </Labelled>
      )}
      {type === "drilldown.switchTab" && (
        <Labelled label={t_i18n("Tab")}>
          <SearchableSelect
            aria-label={t_i18n("Tab")}
            value={target}
            onValueChange={setTarget}
            options={tabs.map((item) => ({ value: item.layoutId, label: item.label }))}
            sort={false}
            searchable="auto"
          />
        </Labelled>
      )}
      {(type === "drilldown.linkToUrl" || type === "drilldown.linkToDashboard") && (
        <label className="flex items-center justify-between gap-2 content-compact">
          <span>{t_i18n("In a new tab")}</span>
          <Switch checked={newTab} onCheckedChange={setNewTab} aria-label={t_i18n("In a new tab")} />
        </label>
      )}
      <Button size="sm" variant="outline" className="w-fit gap-1.5" disabled={!handler} onClick={() => handler && onAdd(handler)}>
        <Plus className="h-3.5 w-3.5" aria-hidden="true" />
        {t_i18n("Add the interaction")}
      </Button>
    </div>
  );
}

type EditorProps = { dashboard: CustomDashboard; vizId: string; edit: Edit };

function ErrorNote({ error }: { error: string | null }) {
  return error ? (
    <p role="alert" className="content-caption text-feedback-error-primary">
      {error}
    </p>
  ) : null;
}

/** What a click on the visualization does, in order (none for one drawing no data). */
export function InteractionsEditor({ dashboard, vizId, edit }: EditorProps) {
  const { t_i18n } = useFormatter();
  const viz = (dashboard.spec as { visualizations?: Record<string, { eventHandlers?: unknown; dataSources?: { primary?: string } }> })?.visualizations?.[vizId];
  const handlers = useMemo(() => readHandlers(viz?.eventHandlers), [viz?.eventHandlers]);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const canEdit = dashboard.can_manage;
  if (!viz?.dataSources?.primary) return null;
  const save = (operations: CustomDashboardOperation[], after?: () => void) => {
    setError(null);
    edit(operations, { onSuccess: after, onError: (failure) => setError(failure.message) });
  };
  const writeHandlers = (next: EventHandler[]) => save([handlersOperation(dashboard.spec, vizId, next)]);
  const move = (index: number, by: number) => {
    const next = [...handlers];
    const [item] = next.splice(index, 1);
    next.splice(index + by, 0, item);
    writeHandlers(next);
  };

  return (
    <div className="flex flex-col gap-2" aria-label={t_i18n("Interactions")} role="group">
      {handlers.length === 0 && <p className="content-caption text-default-secondary">{t_i18n("A click does nothing yet.")}</p>}
      <ol className="flex flex-col gap-1.5">
        {handlers.map((handler, index) => (
          <li key={index} className="flex items-center gap-1.5 rounded-sm border border-elevation-subtle px-2 py-1.5 content-caption">
            <span className="min-w-0 flex-1">
              <span className="text-default-primary">{t_i18n(HANDLER_LABELS[handler.type as HandlerType])}</span>{" "}
              <span className="break-all text-default-secondary">{summary(handler, t_i18n)}</span>
            </span>
            {canEdit && (
              <>
                <Button variant="ghost" size="icon" className="h-6 w-6" disabled={index === 0} aria-label={t_i18n("Earlier")} onClick={() => move(index, -1)}>
                  <ArrowUp className="h-3.5 w-3.5" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6"
                  disabled={index === handlers.length - 1}
                  aria-label={t_i18n("Later")}
                  onClick={() => move(index, 1)}
                >
                  <ArrowDown className="h-3.5 w-3.5" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6"
                  aria-label={t_i18n("Remove the interaction")}
                  onClick={() => writeHandlers(handlers.filter((_, i) => i !== index))}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </>
            )}
          </li>
        ))}
      </ol>
      {canEdit &&
        (adding ? (
          <NewHandler dashboard={dashboard} onAdd={(handler) => save([handlersOperation(dashboard.spec, vizId, [...handlers, handler])], () => setAdding(false))} />
        ) : (
          <Button size="sm" variant="outline" className="w-fit gap-1.5" onClick={() => setAdding(true)} disabled={handlers.length >= 10}>
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            {t_i18n("Add an interaction")}
          </Button>
        ))}
      <ErrorNote error={error} />
    </div>
  );
}

/** When the visualization shows: its own show and hide expressions, and whether it hides without data. */
export function VisibilityEditor({ dashboard, vizId, edit }: EditorProps) {
  const { t_i18n } = useFormatter();
  const viz = (dashboard.spec as { visualizations?: Record<string, { dataSources?: { primary?: string } }> })?.visualizations?.[vizId];
  const stored = useMemo(() => visibilityForm(dashboard.spec, vizId), [dashboard.spec, vizId]);
  const [form, setForm] = useState(stored);
  const [error, setError] = useState<string | null>(null);
  const canEdit = dashboard.can_manage;
  if (!viz) return null;
  const drawsData = Boolean(viz.dataSources?.primary);
  return (
    <div className="flex flex-col gap-2" aria-label={t_i18n("Visibility")} role="group">
      <Labelled label={t_i18n("Show only when")}>
        <Input
          value={form.show}
          onChange={(event) => setForm({ ...form, show: event.target.value })}
          placeholder="isSet($host$)"
          className="h-8 font-mono-plex"
          disabled={!canEdit}
          aria-label={t_i18n("Show only when")}
        />
      </Labelled>
      <Labelled label={t_i18n("Hide when")}>
        <Input
          value={form.hide}
          onChange={(event) => setForm({ ...form, hide: event.target.value })}
          placeholder='$mode$ = "quiet"'
          className="h-8 font-mono-plex"
          disabled={!canEdit}
          aria-label={t_i18n("Hide when")}
        />
      </Labelled>
      <label className="flex items-center justify-between gap-2 content-compact">
        <span>{t_i18n("Hide when there is no data")}</span>
        <Switch
          checked={form.hideWhenNoData}
          disabled={!canEdit || !drawsData}
          onCheckedChange={(on) => setForm({ ...form, hideWhenNoData: on })}
          aria-label={t_i18n("Hide when there is no data")}
        />
      </label>
      {canEdit && (
        <Button
          size="sm"
          variant="outline"
          className="w-fit"
          disabled={JSON.stringify(form) === JSON.stringify(stored)}
          onClick={() => {
            setError(null);
            edit(visibilityOperations(dashboard.spec, vizId, form), { onError: (failure) => setError(failure.message) });
          }}
        >
          {t_i18n("Save the visibility")}
        </Button>
      )}
      <ErrorNote error={error} />
    </div>
  );
}

/** Both, one under the other: the Widget panel's sections for a reader or a grid tab. */
export function VisualizationInteractions({ dashboard, vizId, edit }: EditorProps) {
  const { t_i18n } = useFormatter();
  const viz = (dashboard.spec as { visualizations?: Record<string, { dataSources?: { primary?: string } }> })?.visualizations?.[vizId];
  if (!viz) return null;
  return (
    <div className="flex flex-col gap-4 border-t border-elevation-subtle-soft p-3" data-testid="dashboard.interactions">
      {viz.dataSources?.primary && (
        <section className="flex flex-col gap-2" aria-label={t_i18n("Interactions")}>
          <h3 className="flex items-center gap-1.5 content-compact font-semibold text-default-primary">
            <MousePointerClick className="h-4 w-4 text-accent" aria-hidden="true" />
            {t_i18n("Interactions")}
          </h3>
          <InteractionsEditor dashboard={dashboard} vizId={vizId} edit={edit} />
        </section>
      )}
      <section className="flex flex-col gap-2" aria-label={t_i18n("Visibility")}>
        <h3 className="content-compact font-semibold text-default-primary">{t_i18n("Visibility")}</h3>
        <VisibilityEditor dashboard={dashboard} vizId={vizId} edit={edit} />
      </section>
    </div>
  );
}
