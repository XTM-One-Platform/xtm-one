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
 * The studio's Data sources panel: every data source of the
 * dashboard, what uses it (an unused one greyed), and what may be done to it.
 *
 * - New data source: its type picked on tiles - a platform widget (first, as
 *   in the modules' editors), written in the dashboard (static), an
 *   assignment's last answer, a question to an agent, a variable, a read
 *   tool of an integration, a table kept in a knowledge base document, another
 *   source through steps (derived) - then a form for it, and where it is read:
 *   on the page with each viewer's access, or on the server - every so often or
 *   when asked - with the access of the person who sets it (`dashboard_refresh`). Saved as one edit,
 *   validated server-side: the reason of a refusal is shown with the field it
 *   is about. A platform widget is drawn at once: its visualization, in its
 *   module's own view, is placed with it in the same edit.
 * - Per source: add a visualization drawing it (placed below the others in
 *   the tab on screen), edit it in the source editor, duplicate it, delete it
 *   (refused while something uses it).
 *
 * The pickers list what the person may reach themselves; the server checks it
 * again on save (`dashboard_sources.check_source_references`).
 */
import { useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Copy, Database, FileJson, Pencil, Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useAssignments } from "@/hooks/api/useAssignmentApi";
import { useIntegrations, useIntegrationTools } from "@/hooks/api/useIntegrationApi";
import { useKnowledgeBases, useKnowledgeDocuments } from "@/hooks/api/useKnowledgeBaseApi";
import { platformFormsQuery, usePlatformForms, usePreviewPlatformWidget } from "@/hooks/api/useCustomDashboardApi";
import { useVariables } from "@/hooks/api/useVariableApi";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import { CodeEditor } from "@/pages/flow-builder/CodeEditor";
import { cn } from "@/lib/utils";
import type { CustomDashboard, CustomDashboardOperation } from "@/types";

import {
  addVisualizationOperations,
  freeId,
  parseSource,
  sourceEntries,
  VISUALIZATION_TYPES_BY_SHAPE,
  type SourceEntry,
} from "./definition";
import { TilePick } from "./platform-fields";
import { formCarries } from "./platform-form";
import { sameNative } from "./platform-native";
import { PlatformSourceEditor, type PlatformOptions } from "./PlatformSourceEditor";

/** Each source type's label, in the order of its tiles. @i18n-keys */
const TYPE_LABELS = {
  platform: "Platform widget",
  static: "Written in the dashboard",
  assignment: "Assignment answer",
  ask: "Question to an agent",
  variable: "Variable",
  integration: "Integration tool",
  knowledge: "Knowledge base table",
  derived: "Derived (steps on a source)",
} as const;
type SourceType = keyof typeof TYPE_LABELS;
/** Each source type's tile. */
const TYPE_ICONS: Record<SourceType, string> = {
  platform: "blocks",
  static: "file-braces",
  assignment: "calendar-clock",
  ask: "message-circle-question-mark",
  variable: "variable",
  integration: "plug",
  knowledge: "book-open",
  derived: "git-branch",
};
/** The sources the server may read on its own (`dashboard_definition.SERVER_REFRESH_TYPES`). */
const SERVER_REFRESHED: readonly SourceType[] = ["platform", "integration", "knowledge", "variable", "derived"];
/** Where a source is read. @i18n-keys */
const REFRESH_MODE_LABELS = {
  live: "On the page, with each viewer's access",
  server: "On the server, every…",
  schedule: "On the server, on a schedule",
  event: "On the server, on an event",
  manual: "On the server, when asked",
} as const;
type RefreshMode = keyof typeof REFRESH_MODE_LABELS;
/** When an alert holds, on the source's figure. @i18n-keys */
const ALERT_OP_LABELS = { none: "No alert", gt: "Above", lt: "Below" } as const;
type AlertOp = keyof typeof ALERT_OP_LABELS;
/** How often the server reads it (at least every 5 minutes). @i18n-keys */
const REFRESH_EVERY_LABELS = { "5m": "5 minutes", "15m": "15 minutes", "1h": "Hour", "6h": "6 hours", "24h": "Day" } as const;
type RefreshEvery = keyof typeof REFRESH_EVERY_LABELS;
/** How a knowledge base document's table is written. @i18n-keys */
const TABLE_FORMAT_LABELS = { auto: "Detect", json: "JSON", jsonl: "JSON lines", csv: "CSV", markdown: "Markdown table" } as const;
type TableFormat = keyof typeof TABLE_FORMAT_LABELS;
/** What a view's name looks like (`xtm.column`, `xtm.outcomematrix`). */
const VIEW_NAME = /^xtm\.[a-z][a-z0-9.]{0,40}$/;
/** A platform widget whose answer names no view of its module's is drawn by its shape. */
const VIEW_BY_SHAPE: Record<string, string> = {
  number: "xtm.singlevalue",
  series: "xtm.line",
  distribution: "xtm.pie",
  rows: "xtm.table",
  text: "xtm.answer",
};

/** Each shape's label. @i18n-keys */
const SHAPE_LABELS = {
  text: "Text",
  number: "Number",
  series: "Series",
  distribution: "Distribution",
  rows: "Rows",
} as const;
type ShapeKey = keyof typeof SHAPE_LABELS;

/** Each visualization type's label. @i18n-keys */
const VISUALIZATION_LABELS = {
  "xtm.singlevalue": "Single value",
  "xtm.line": "Line chart",
  "xtm.area": "Area chart",
  "xtm.column": "Column chart",
  "xtm.bar": "Bar chart",
  "xtm.pie": "Pie chart",
  "xtm.distribution": "Distribution",
  "xtm.list": "List",
  "xtm.table": "Table",
  "xtm.answer": "Answer text",
} as const satisfies Record<string, string>;
const visualizationLabel = (type: string): string => (VISUALIZATION_LABELS as Record<string, string>)[type] ?? type;

const STATIC_TEMPLATES: Record<ShapeKey, unknown> = {
  text: { text: "" },
  number: { value: 0 },
  series: { series: [{ name: "Series", points: [{ x: "2026-10-01", y: 1 }] }] },
  distribution: { items: [{ label: "A", value: 1 }] },
  rows: { columns: [{ key: "name", label: "Name" }], rows: [{ name: "" }] },
};

type Edit = (operations: CustomDashboardOperation[], handlers?: { onSuccess?: () => void; onError?: (error: Error) => void }) => void;

/** The shape a source answers in, when its definition says (a derived or a platform one depends on its steps or widget). */
function shapeOf(source: unknown): ShapeKey | null {
  const options = (source as { options?: { shape?: unknown } } | null)?.options;
  const shape = options?.shape;
  return typeof shape === "string" && shape in SHAPE_LABELS ? (shape as ShapeKey) : null;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="content-caption text-default-secondary">{label}</span>
      {children}
    </label>
  );
}

function Choice<T extends string>({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: T | "";
  onChange: (value: T) => void;
  options: { value: T; label: string }[];
}) {
  return (
    <Field label={label}>
      {/* Lists read from the platform or the person's library can be long: searchable past a few entries. */}
      <SearchableSelect
        aria-label={label}
        value={value}
        onValueChange={(next) => onChange(next as T)}
        options={options}
        sort={false}
        searchable="auto"
        className="h-8 content-compact"
      />
    </Field>
  );
}

/** The form creating a data source of one type. */
function NewSourceDialog({
  dashboard,
  layoutId,
  open,
  onOpenChange,
  edit,
  onVisualizationAdded,
}: {
  dashboard: CustomDashboard;
  /** The tab on screen: a platform widget's visualization is placed there. */
  layoutId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  edit: Edit;
  onVisualizationAdded: (visualizationId: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const [type, setType] = useState<SourceType>("platform");
  const preview = usePreviewPlatformWidget(dashboard.id);
  const [name, setName] = useState("");
  const [shape, setShape] = useState<ShapeKey>("number");
  const [target, setTarget] = useState("");
  const [question, setQuestion] = useState("");
  const [path, setPath] = useState("");
  const [json, setJson] = useState(JSON.stringify(STATIC_TEMPLATES.number, null, 2));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [platform, setPlatform] = useState<PlatformOptions | null>(null);
  const [integrationId, setIntegrationId] = useState("");
  const [tool, setTool] = useState("");
  const [kbId, setKbId] = useState("");
  const [section, setSection] = useState("");
  const [tableFormat, setTableFormat] = useState<TableFormat>("auto");
  const [refreshMode, setRefreshMode] = useState<RefreshMode>("live");
  const [every, setEvery] = useState<RefreshEvery>("15m");
  const [cron, setCron] = useState("0 7 * * 1-5");
  const [eventKind, setEventKind] = useState<"platformEvent" | "integrationEvent">("integrationEvent");
  const [eventName, setEventName] = useState("");
  const [history, setHistory] = useState("");
  const [alertOp, setAlertOp] = useState<AlertOp>("none");
  const [alertValue, setAlertValue] = useState("");
  const integrations = useIntegrations();
  const tools = useIntegrationTools(integrationId, open && type === "integration");
  const knowledgeBases = useKnowledgeBases();
  const documents = useKnowledgeDocuments(open && type === "knowledge" ? kbId : "");
  const assignments = useAssignments();
  const agents = useAgents();
  const variables = useVariables({ enabled: open && type === "variable" });
  const sources = useMemo(() => sourceEntries(dashboard.spec), [dashboard.spec]);

  const pickType = (next: SourceType) => {
    setType(next);
    setTarget("");
    setError(null);
    setShape(next === "variable" ? "text" : next === "static" ? "number" : next === "integration" || next === "knowledge" ? "rows" : "text");
    setRefreshMode("live");
    setJson(
      next === "static"
        ? JSON.stringify(STATIC_TEMPLATES.number, null, 2)
        : next === "derived"
          ? JSON.stringify([{ op: "head", count: 10 }], null, 2)
          : next === "integration"
            ? "{}"
            : "",
    );
  };

  const build = (): { ok: true; value: Record<string, unknown> } | { ok: false; error: string } => {
    if (type === "platform" && !platform) return { ok: false, error: t_i18n("Choose an integration to read through.") };
    const parsed = type === "static" || type === "derived" || type === "integration" ? parseSource(json) : null;
    if (parsed && !parsed.ok) return { ok: false, error: t_i18n("This is not valid JSON: {reason}", { reason: parsed.error }) };
    const value = parsed?.ok ? parsed.value : undefined;
    const answer = { shape, ...(path.trim() ? { path: path.trim() } : {}) };
    const options: Record<string, Record<string, unknown>> = {
      static: { shape, data: value },
      assignment: { assignment_id: target, ...answer, ...(history.trim() ? { history: Number(history), shape: "series" } : {}) },
      ask: { agent_id: target, question, ...answer },
      variable: { variable_id: target, shape: shape === "number" ? "number" : "text" },
      integration: { integration_id: integrationId, tool, arguments: value ?? {}, ...answer },
      knowledge: {
        knowledge_base_id: kbId,
        document_id: target,
        format: tableFormat,
        ...(section.trim() ? { section: section.trim() } : {}),
        ...answer,
      },
      derived: { extends: target, steps: value },
      platform: { ...platform },
    };
    if (refreshMode === "event" && !eventName.trim()) return { ok: false, error: t_i18n("Name the event it is read on.") };
    const alerting = refreshMode !== "live" && alertOp !== "none";
    if (alerting && !Number.isFinite(Number(alertValue)))
      return { ok: false, error: t_i18n("An alert needs a number to compare the figure with.") };
    const refresh =
      SERVER_REFRESHED.includes(type) && refreshMode !== "live"
        ? {
            refreshMode,
            ...(refreshMode === "server" ? { refresh: every } : {}),
            ...(refreshMode === "schedule" ? { cron: cron.trim() } : {}),
            ...(refreshMode === "event" ? { on: { [eventKind]: eventName.trim() } } : {}),
            ...(alerting ? { alerts: [{ id: "alert_1", op: alertOp, value: Number(alertValue), label: name.trim() || undefined }] } : {}),
          }
        : {};
    return { ok: true, value: { type, ...(name.trim() ? { name: name.trim() } : {}), options: { ...options[type], ...refresh } } };
  };

  /** The view a platform widget is drawn with: its module's (the preset its answer carries), else its shape's. */
  const platformView = async (options: PlatformOptions): Promise<string | null> => {
    const answer = await preview.mutateAsync({ kind: "platform", id: "preview", ...options }).catch(() => null);
    if (answer?.status !== "ok") return null;
    const preset = answer.presentation?.view;
    // The server checks the view again on save.
    return typeof preset === "string" && VIEW_NAME.test(preset) ? preset : (VIEW_BY_SHAPE[answer.shape] ?? null);
  };

  const save = async () => {
    const built = build();
    if (!built.ok) {
      setError(built.error);
      return;
    }
    const id = freeId(dashboard.spec, `ds_${(name.trim() || type).toLowerCase()}`);
    setSaving(true);
    setError(null);
    const operations: CustomDashboardOperation[] = [{ op: "set", path: `dataSources.${id}`, value: built.value }];
    // A platform widget is placed with its visualization; one that cannot be read now is kept as a source alone.
    const view = type === "platform" && platform ? await platformView(platform) : null;
    const drawn = view ? addVisualizationOperations(dashboard.spec, id, view, name, layoutId) : null;
    edit(drawn ? [...operations, ...drawn.operations] : operations, {
      onSuccess: () => {
        setSaving(false);
        onOpenChange(false);
        if (drawn) onVisualizationAdded(drawn.id);
      },
      onError: (failure) => {
        setSaving(false);
        setError(failure.message);
      },
    });
  };

  const shapes = (Object.keys(SHAPE_LABELS) as ShapeKey[]).filter((key) => type !== "variable" || key === "text" || key === "number");
  const chatAgents = (agents.data ?? []).filter((agent) => agent.enabled && !agent.disable_chat);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[90dvh] max-w-3xl flex-col gap-4 overflow-y-auto" data-testid="dashboard.sources.new">
        <DialogHeader>
          <DialogTitle>{t_i18n("New data source")}</DialogTitle>
          <DialogDescription>
            {t_i18n("Where a visualization's data comes from. Everyone viewing the dashboard reads it with their own access.")}
          </DialogDescription>
        </DialogHeader>
        <TilePick
          label={t_i18n("Type")}
          value={type}
          onChange={(next) => pickType(next as SourceType)}
          options={(Object.keys(TYPE_LABELS) as SourceType[]).map((key) => ({ value: key, label: t_i18n(TYPE_LABELS[key]), icon: TYPE_ICONS[key] }))}
        />
        <div className="grid gap-x-3 gap-y-5 sm:grid-cols-2">
          <Field label={t_i18n("Name")}>
            <Input value={name} onChange={(event) => setName(event.target.value.slice(0, 80))} className="h-8" aria-label={t_i18n("Name")} />
          </Field>
          {type === "assignment" && (
            <Choice
              label={t_i18n("Assignment")}
              value={target}
              onChange={setTarget}
              options={(assignments.data ?? []).map((a) => ({ value: a.id, label: a.name }))}
            />
          )}
          {type === "ask" && (
            <Choice label={t_i18n("Agent")} value={target} onChange={setTarget} options={chatAgents.map((a) => ({ value: a.id, label: a.name }))} />
          )}
          {type === "variable" && (
            <Choice
              label={t_i18n("Variable")}
              value={target}
              onChange={setTarget}
              // A secret is never drawn: it is not offered.
              options={(variables.data ?? []).filter((v) => !v.is_secret).map((v) => ({ value: v.id, label: v.name }))}
            />
          )}
          {type === "derived" && (
            <Choice
              label={t_i18n("Reads")}
              value={target}
              onChange={setTarget}
              options={sources.map((source) => ({ value: source.id, label: source.name ? `${source.name} (${source.id})` : source.id }))}
            />
          )}
          {type === "integration" && (
            <>
              <Choice
                label={t_i18n("Integration")}
                value={integrationId}
                onChange={(next) => {
                  setIntegrationId(next);
                  setTool("");
                }}
                options={(integrations.data ?? [])
                  .filter((row) => row.status === "connected" || row.status === "configured")
                  .map((row) => ({ value: row.id, label: row.name }))}
              />
              <Choice
                label={t_i18n("Tool that reads")}
                value={tool}
                onChange={setTool}
                // A tool that may change something is never called from a dashboard: the server refuses it.
                options={(tools.data?.tools ?? []).filter((t) => t.tool_type !== "write").map((t) => ({ value: t.name, label: t.name }))}
              />
            </>
          )}
          {type === "knowledge" && (
            <>
              <Choice
                label={t_i18n("Knowledge base")}
                value={kbId}
                onChange={(next) => {
                  setKbId(next);
                  setTarget("");
                }}
                options={(knowledgeBases.data ?? []).map((kb) => ({ value: kb.id, label: kb.name }))}
              />
              <Choice
                label={t_i18n("Document")}
                value={target}
                onChange={setTarget}
                options={(documents.data ?? []).map((doc) => ({ value: doc.id, label: doc.filename }))}
              />
              <Field label={t_i18n("Section (optional)")}>
                <Input value={section} onChange={(event) => setSection(event.target.value.slice(0, 300))} placeholder="Weekly" className="h-8" aria-label={t_i18n("Section (optional)")} />
              </Field>
              <Choice
                label={t_i18n("Table written as")}
                value={tableFormat}
                onChange={(next) => setTableFormat(next as TableFormat)}
                options={(Object.keys(TABLE_FORMAT_LABELS) as TableFormat[]).map((key) => ({ value: key, label: t_i18n(TABLE_FORMAT_LABELS[key]) }))}
              />
            </>
          )}
          {type !== "derived" && type !== "platform" && (
            <Choice
              label={t_i18n("Shape")}
              value={shape}
              onChange={(next) => {
                setShape(next);
                if (type === "static") setJson(JSON.stringify(STATIC_TEMPLATES[next], null, 2));
              }}
              options={shapes.map((key) => ({ value: key, label: t_i18n(SHAPE_LABELS[key]) }))}
            />
          )}
          {type === "assignment" && (
            <Field label={t_i18n("Trend over the last runs (optional)")}>
              <Input
                type="number"
                min={2}
                max={60}
                value={history}
                onChange={(event) => setHistory(event.target.value)}
                placeholder="30"
                className="h-8"
                aria-label={t_i18n("Trend over the last runs (optional)")}
              />
            </Field>
          )}
          {(type === "assignment" || type === "ask" || type === "integration" || type === "knowledge") && (
            <Field label={t_i18n("Path in a JSON answer (optional)")}>
              <Input
                value={path}
                onChange={(event) => setPath(event.target.value)}
                placeholder="response.findings"
                className="h-8"
                aria-label={t_i18n("Path in a JSON answer (optional)")}
              />
            </Field>
          )}
        </div>
        {type === "ask" && (
          <Field label={t_i18n("Question")}>
            <textarea
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              maxLength={2000}
              rows={3}
              aria-label={t_i18n("Question")}
              className="rounded-sm border border-elevation-subtle bg-elevation-default p-2 content-compact"
            />
          </Field>
        )}
        {type === "platform" && <PlatformSourceEditor dashboardId={dashboard.id} onChange={setPlatform} />}
        {SERVER_REFRESHED.includes(type) && (
          <div className="grid gap-x-3 gap-y-5 sm:grid-cols-2" data-testid="dashboard.sources.refresh">
            <Choice
              label={t_i18n("Read")}
              value={refreshMode}
              onChange={(next) => setRefreshMode(next as RefreshMode)}
              options={(Object.keys(REFRESH_MODE_LABELS) as RefreshMode[]).map((key) => ({ value: key, label: t_i18n(REFRESH_MODE_LABELS[key]) }))}
            />
            {refreshMode === "schedule" && (
              <Field label={t_i18n("Schedule (cron, UTC)")}>
                <Input value={cron} onChange={(event) => setCron(event.target.value)} className="h-8" aria-label={t_i18n("Schedule (cron, UTC)")} />
              </Field>
            )}
            {refreshMode === "event" && (
              <>
                <Choice
                  label={t_i18n("Event of")}
                  value={eventKind}
                  onChange={(next) => setEventKind(next as typeof eventKind)}
                  options={[
                    { value: "integrationEvent", label: t_i18n("An integration") },
                    { value: "platformEvent", label: t_i18n("The platform") },
                  ]}
                />
                <Field label={t_i18n("Event")}>
                  <Input
                    value={eventName}
                    onChange={(event) => setEventName(event.target.value.slice(0, 200))}
                    placeholder={eventKind === "platformEvent" ? "run.completed" : "issues.opened"}
                    className="h-8"
                    aria-label={t_i18n("Event")}
                  />
                </Field>
              </>
            )}
            {refreshMode === "server" && (
              <Choice
                label={t_i18n("Every")}
                value={every}
                onChange={(next) => setEvery(next as RefreshEvery)}
                options={(Object.keys(REFRESH_EVERY_LABELS) as RefreshEvery[]).map((key) => ({ value: key, label: t_i18n(REFRESH_EVERY_LABELS[key]) }))}
              />
            )}
            {refreshMode !== "live" && (
              <>
                <Choice
                  label={t_i18n("Alert when the figure is")}
                  value={alertOp}
                  onChange={(next) => setAlertOp(next as AlertOp)}
                  options={(Object.keys(ALERT_OP_LABELS) as AlertOp[]).map((key) => ({ value: key, label: t_i18n(ALERT_OP_LABELS[key]) }))}
                />
                {alertOp !== "none" && (
                  <Field label={t_i18n("Threshold")}>
                    <Input type="number" value={alertValue} onChange={(event) => setAlertValue(event.target.value)} className="h-8" aria-label={t_i18n("Threshold")} />
                  </Field>
                )}
              </>
            )}
            {refreshMode !== "live" && (
              <p className="content-caption text-default-secondary sm:col-span-2">
                {t_i18n("Read with your access and shown to everyone who opens the dashboard, even those who could not read it themselves.")}
              </p>
            )}
          </div>
        )}
        {(type === "static" || type === "derived" || type === "integration") && (
          <Field label={type === "static" ? t_i18n("Data") : type === "integration" ? t_i18n("Arguments") : t_i18n("Steps")}>
            <CodeEditor value={json} language="json" onChange={setJson} className="min-h-40" ariaLabel={t_i18n("Data")} />
          </Field>
        )}
        {error && (
          <p role="alert" className="content-compact text-feedback-error-primary">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {t_i18n("Cancel")}
          </Button>
          <Button onClick={() => void save()} disabled={saving}>
            {type === "platform" ? t_i18n("Create and place") : t_i18n("Create")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** A platform source edited in its editor, saved as one `set` operation. */
function EditPlatformSourceDialog({
  dashboard,
  sourceId,
  onClose,
  edit,
}: {
  dashboard: CustomDashboard;
  sourceId: string | null;
  onClose: () => void;
  edit: Edit;
}) {
  const { t_i18n } = useFormatter();
  const stored = sourceId ? (dashboard.spec as { dataSources?: Record<string, { options?: PlatformOptions }> })?.dataSources?.[sourceId] : null;
  const [options, setOptions] = useState<PlatformOptions | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (!sourceId || !stored) return null;
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="flex max-h-[90dvh] max-w-3xl flex-col gap-4 overflow-y-auto" data-testid="dashboard.sources.edit-platform">
        <DialogHeader>
          <DialogTitle>{t_i18n("Edit {name}", { name: sourceId })}</DialogTitle>
          <DialogDescription>{t_i18n("The platform widget this data source reads, with each viewer's own access.")}</DialogDescription>
        </DialogHeader>
        <PlatformSourceEditor dashboardId={dashboard.id} initial={stored.options} onChange={setOptions} />
        {error && (
          <p role="alert" className="content-compact text-feedback-error-primary">
            {error}
          </p>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            disabled={!options}
            onClick={() =>
              // Nothing changed: nothing is written, not even a new revision.
              sameNative(options, stored.options)
                ? onClose()
                : edit([{ op: "set", path: `dataSources.${sourceId}`, value: { ...stored, options } }], {
                    onSuccess: onClose,
                    onError: (failure) => setError(failure.message),
                  })
            }
          >
            {t_i18n("Save")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** Choose how a new visualization draws a source, and add it to the tab on screen. */
function AddVisualizationDialog({
  dashboard,
  source,
  layoutId,
  onClose,
  edit,
  onAdded,
}: {
  dashboard: CustomDashboard;
  source: SourceEntry | null;
  layoutId: string | null;
  onClose: () => void;
  edit: Edit;
  onAdded: (id: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const stored = source ? (dashboard.spec as { dataSources?: Record<string, unknown> })?.dataSources?.[source.id] : null;
  const shape = shapeOf(stored);
  const types = shape ? VISUALIZATION_TYPES_BY_SHAPE[shape] : Object.keys(VISUALIZATION_LABELS);
  const [type, setType] = useState("");
  const [title, setTitle] = useState("");
  const [error, setError] = useState<string | null>(null);
  const chosen = types.includes(type) ? type : types[0];
  if (!source) return null;
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-md" data-testid="dashboard.sources.visualize">
        <DialogHeader>
          <DialogTitle>{t_i18n("Add a visualization")}</DialogTitle>
          <DialogDescription>{t_i18n("Drawing {source}, below the others on this tab.", { source: source.name ?? source.id })}</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <Choice
            label={t_i18n("Drawn as")}
            value={chosen}
            onChange={setType}
            options={types.map((key) => ({ value: key, label: t_i18n(visualizationLabel(key)) }))}
          />
          <Field label={t_i18n("Title (heading)")}>
            <Input value={title} onChange={(event) => setTitle(event.target.value.slice(0, 120))} className="h-8" aria-label={t_i18n("Title (heading)")} />
          </Field>
          {error && (
            <p role="alert" className="content-compact text-feedback-error-primary">
              {error}
            </p>
          )}
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            {t_i18n("Cancel")}
          </Button>
          <Button
            onClick={() => {
              const { id, operations } = addVisualizationOperations(dashboard.spec, source.id, chosen, title, layoutId);
              edit(operations, { onSuccess: () => onAdded(id), onError: (failure) => setError(failure.message) });
            }}
          >
            {t_i18n("Add")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function DashboardDataSourcesPanel({
  dashboard,
  layoutId,
  edit,
  onEditSource,
  onVisualizationAdded,
}: {
  dashboard: CustomDashboard;
  /** The tab on screen: a new visualization is placed there. */
  layoutId: string | null;
  edit: Edit;
  /** Open the source editor on one data source. */
  onEditSource: (sourceId: string) => void;
  onVisualizationAdded: (visualizationId: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const entries = useMemo(() => sourceEntries(dashboard.spec), [dashboard.spec]);
  const [creating, setCreating] = useState(false);
  // Each opening is a new form: nothing typed for the last source carries over.
  const [form, setForm] = useState(0);
  const [visualizing, setVisualizing] = useState<SourceEntry | null>(null);
  const [editingPlatform, setEditingPlatform] = useState<string | null>(null);
  const canEdit = dashboard.can_manage;
  const queryClient = useQueryClient();
  // Read ahead: the form descriptions decide whether a widget opens as a form.
  usePlatformForms();

  // A widget the form cannot give back as it is opens in the source editor, where nothing is dropped.
  const editWidget = async (sourceId: string) => {
    const options = (dashboard.spec as { dataSources?: Record<string, { options?: Partial<PlatformOptions> }> })?.dataSources?.[sourceId]?.options;
    const forms = await queryClient.fetchQuery(platformFormsQuery).catch(() => null);
    const form = forms?.forms.find((entry) => entry.source === options?.source)?.form;
    if (form && formCarries(form, options?.native)) {
      setEditingPlatform(sourceId);
      return;
    }
    toast({ title: t_i18n("This widget has settings the form does not show: it opens in the source editor."), variant: "info" });
    onEditSource(sourceId);
  };

  const menu = (entry: SourceEntry): OverflowMenuItem[] => [
    { id: "visualize", label: t_i18n("Add a visualization"), icon: Plus, onSelect: () => setVisualizing(entry), disabled: !canEdit },
    ...(entry.type === "platform"
      ? [{ id: "edit-platform", label: t_i18n("Edit the widget"), icon: Pencil, onSelect: () => editWidget(entry.id), disabled: !canEdit }]
      : []),
    { id: "edit", label: t_i18n("Edit in the source editor"), icon: FileJson, onSelect: () => onEditSource(entry.id) },
    {
      id: "duplicate",
      label: t_i18n("Duplicate"),
      icon: Copy,
      disabled: !canEdit,
      onSelect: () => {
        const stored = (dashboard.spec as { dataSources?: Record<string, unknown> })?.dataSources?.[entry.id];
        edit([{ op: "set", path: `dataSources.${freeId(dashboard.spec, `${entry.id}_copy`)}`, value: stored }]);
      },
    },
    {
      id: "delete",
      label: entry.usedBy.length ? t_i18n("Delete (in use)") : t_i18n("Delete"),
      icon: Trash2,
      destructive: true,
      separatorBefore: true,
      disabled: !canEdit || entry.usedBy.length > 0,
      onSelect: () => edit([{ op: "delete", path: `dataSources.${entry.id}` }]),
    },
  ];

  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto p-3" data-testid="dashboard.sources">
      <div className="flex items-center justify-between gap-2">
        <h2 className="content-compact font-semibold text-default-primary">{t_i18n("Data sources")}</h2>
        {canEdit && (
          <Button
            size="sm"
            variant="outline"
            className="gap-1.5"
            onClick={() => {
              setForm((n) => n + 1);
              setCreating(true);
            }}
          >
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            {t_i18n("New")}
          </Button>
        )}
      </div>
      {entries.length === 0 && (
        <p className="content-compact text-default-secondary">{t_i18n("No data source yet: every visualization draws its own content.")}</p>
      )}
      <ul className="flex flex-col gap-2">
        {entries.map((entry) => (
          <li
            key={entry.id}
            className={cn(
              "flex items-start gap-2 rounded-sm border border-elevation-subtle p-2.5",
              entry.usedBy.length === 0 && "opacity-60",
            )}
            data-source-id={entry.id}
          >
            <Database className="mt-0.5 h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
            <div className="flex min-w-0 flex-1 flex-col gap-1">
              <span className="truncate content-compact font-medium text-default-primary">{entry.name ?? entry.id}</span>
              <span className="flex flex-wrap items-center gap-1.5 content-caption text-default-secondary">
                <span className="rounded-sm border border-elevation-subtle px-1.5 py-px">
                  {t_i18n(TYPE_LABELS[entry.type as SourceType] ?? entry.type)}
                </span>
                {entry.usedBy.length === 0
                  ? t_i18n("Unused")
                  : t_i18n("Used by {count}", { count: entry.usedBy.length })}
              </span>
            </div>
            <OverflowMenu items={menu(entry)} label={t_i18n("Actions on {name}", { name: entry.name ?? entry.id })} className="h-6 w-6" />
          </li>
        ))}
      </ul>
      <NewSourceDialog
        key={form}
        dashboard={dashboard}
        layoutId={layoutId}
        open={creating}
        onOpenChange={setCreating}
        edit={edit}
        onVisualizationAdded={onVisualizationAdded}
      />
      <EditPlatformSourceDialog
        key={editingPlatform ?? "none"}
        dashboard={dashboard}
        sourceId={editingPlatform}
        onClose={() => setEditingPlatform(null)}
        edit={edit}
      />
      <AddVisualizationDialog
        key={visualizing?.id ?? "none"}
        dashboard={dashboard}
        source={visualizing}
        layoutId={layoutId}
        onClose={() => setVisualizing(null)}
        edit={edit}
        onAdded={(id) => {
          setVisualizing(null);
          onVisualizationAdded(id);
        }}
      />
    </div>
  );
}
