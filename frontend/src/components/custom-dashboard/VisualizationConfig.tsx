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
 * The configuration panel of the visualization picked in the Dashboard
 * Studio, section by section:
 *
 * - General: heading, description, the type (those drawing the same data,
 *   `viz-config.typesLike`), the panel's progress bar and read time.
 * - Data source: the source drawn (and a chart's annotations), edited in the
 *   source editor or the Data panel.
 * - Data display and Coloring: the type's options (`viz-config.displayFields`,
 *   `colorFields`); a colour is the theme's, one colour or ranges of the value.
 * - Interactions and Visibility (`VisualizationInteractions`).
 * - Position: cells of the grid or pixels of the canvas.
 * - Source: the visualization as JSON, opened in the source editor.
 *
 * Every field is one step of the designer's history, saved as it goes (a value
 * typed is one step once it pauses); the server validates each save and a
 * refusal starts again from what is stored.
 */
import { useId, type ReactNode } from "react";
import { Braces, Database, Eye, LayoutTemplate, Move, MousePointerClick, Palette, Plus, SlidersHorizontal, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SearchableSelect } from "@/components/ui/searchable-select";
import {
  Field,
  NumberField,
  Section,
  SelectField,
  SwitchField,
  TextAreaField,
  TextField,
} from "@/components/video-studio/editor/fields";
import { useFormatter } from "@/i18n";
import type { CustomDashboard, CustomDashboardOperation } from "@/types";

import { DraftColorField } from "./CanvasInspector";
import type { DashboardDesigner } from "./useDashboardDesigner";
import { InteractionsEditor, VisibilityEditor } from "./VisualizationInteractions";
import {
  applyOperations,
  CHOICE_LABELS,
  colorFields,
  displayFields,
  FIELD_LABELS,
  placementOf,
  readColorRule,
  setColorRule,
  setOptions,
  setPlacement,
  setType,
  setVisualization,
  STARTER_RANGES,
  takesTrellis,
  TYPE_LABELS,
  typesForShape,
  typesLike,
  isVizType,
  type ColorRange,
  type ColorRule,
  type FieldSpec,
  type PathOperation,
} from "./viz-config";

type Raw = Record<string, unknown>;
const record = (value: unknown): Raw => (typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Raw) : {});

/** How a colour is set. @i18n-keys */
const RULE_LABELS = { theme: "Theme colour", static: "One colour", ranges: "By ranges of the value", custom: "An expression (source)" } as const;
type RuleMode = keyof typeof RULE_LABELS;

/** The labels of a source's types in the picker. @i18n-keys */
const SOURCE_TYPE_LABELS = {
  platform: "Platform",
  static: "Written in the dashboard",
  derived: "Derived",
  assignment: "Assignment",
  ask: "Agent",
  variable: "Variable",
} as const;

/** The types drawing an annotation source over their series. */
const ANNOTATED = ["xtm.line", "xtm.area", "xtm.column", "xtm.bar"];

/** A number with no range of its own (an axis bound, a gauge's ends). */
function PlainNumberField({ label, value, onChange, min }: { label: string; value: number | undefined; onChange: (next: number | undefined) => void; min?: number }) {
  const id = useId();
  return (
    <Field label={label} htmlFor={id}>
      <Input
        id={id}
        aria-label={label}
        type="number"
        min={min}
        value={value ?? ""}
        onChange={(event) => {
          const raw = event.target.value;
          if (raw === "") onChange(undefined);
          else if (Number.isFinite(Number(raw)) && (min === undefined || Number(raw) >= min)) onChange(Number(raw));
        }}
        className="h-8"
      />
    </Field>
  );
}

/** Ranges of a value, each with its colour; `bounded`: every range has both ends (a gauge's). */
function RangesEditor({ ranges, onChange, bounded }: { ranges: ColorRange[]; onChange: (next: ColorRange[]) => void; bounded?: boolean }) {
  const { t_i18n } = useFormatter();
  const set = (index: number, patch: Partial<ColorRange>) => onChange(ranges.map((range, i) => (i === index ? { ...range, ...patch } : range)));
  const end = (value: string): number | undefined => (value === "" || !Number.isFinite(Number(value)) ? undefined : Number(value));
  return (
    <div className="flex flex-col gap-2" role="group" aria-label={t_i18n("Ranges")}>
      {ranges.map((range, index) => (
        <div key={index} className="flex flex-col gap-2 rounded-sm border border-elevation-subtle-soft p-2">
          <div className="flex items-end gap-1.5">
            <label className="flex min-w-0 flex-1 flex-col gap-1 content-caption text-default-secondary">
              {t_i18n("From")}
              <Input
                aria-label={t_i18n("From")}
                type="number"
                placeholder={bounded ? "0" : "-∞"}
                value={range.from ?? ""}
                onChange={(event) => set(index, { from: end(event.target.value) })}
                className="h-8"
              />
            </label>
            <label className="flex min-w-0 flex-1 flex-col gap-1 content-caption text-default-secondary">
              {t_i18n("To")}
              <Input
                aria-label={t_i18n("To")}
                type="number"
                placeholder={bounded ? "100" : "∞"}
                value={range.to ?? ""}
                onChange={(event) => set(index, { to: end(event.target.value) })}
                className="h-8"
              />
            </label>
            <Button variant="ghost" size="icon" className="h-8 w-8 shrink-0" aria-label={t_i18n("Remove the range")} onClick={() => onChange(ranges.filter((_, i) => i !== index))}>
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
          <DraftColorField label={t_i18n("Colour")} value={range.value} alpha={false} onChange={(value) => value && set(index, { value })} />
        </div>
      ))}
      <Button
        size="sm"
        variant="outline"
        className="w-fit gap-1.5"
        disabled={ranges.length >= 10}
        onClick={() => {
          const last = ranges[ranges.length - 1];
          const from = last?.to ?? last?.from ?? 0;
          onChange([...ranges, bounded ? { from, to: from + 10, value: "#2196f3" } : { from, value: "#2196f3" }]);
        }}
      >
        <Plus className="h-3.5 w-3.5" aria-hidden="true" />
        {t_i18n("Add a range")}
      </Button>
    </div>
  );
}

/** A colour option: the theme's, one colour, or ranges of the value it reads. */
function ColorRuleField({ label, rule, onChange }: { label: string; rule: ColorRule; onChange: (next: ColorRule) => void }) {
  const { t_i18n } = useFormatter();
  const modes: RuleMode[] = rule.mode === "custom" ? ["theme", "static", "ranges", "custom"] : ["theme", "static", "ranges"];
  return (
    <div className="flex flex-col gap-2 rounded-sm border border-elevation-subtle-soft p-2">
      <SelectField
        label={label}
        value={rule.mode}
        options={modes.map((mode) => ({ value: mode, label: t_i18n(RULE_LABELS[mode]) }))}
        onChange={(mode) => {
          if (!mode || mode === rule.mode || mode === "custom") return;
          if (mode === "theme") onChange({ mode });
          else if (mode === "static") onChange({ mode, color: "#2196f3" });
          else onChange({ mode, ranges: STARTER_RANGES });
        }}
      />
      {rule.mode === "static" && (
        <DraftColorField label={t_i18n("Colour")} value={rule.color} alpha={false} onChange={(color) => color && onChange({ mode: "static", color })} />
      )}
      {rule.mode === "ranges" && <RangesEditor ranges={rule.ranges} onChange={(ranges) => onChange({ mode: "ranges", ranges })} />}
      {rule.mode === "custom" && <code className="break-all font-mono-plex text-[11px] text-default-secondary">{rule.expression}</code>}
    </div>
  );
}

/** A list of colours, the series' in order. */
function ColorsField({ label, colors, onChange }: { label: string; colors: string[]; onChange: (next: string[] | undefined) => void }) {
  const { t_i18n } = useFormatter();
  return (
    <Field label={label}>
      <div className="flex flex-col gap-1.5">
        {colors.map((color, index) => (
          <div key={index} className="flex items-end gap-1.5">
            <div className="min-w-0 flex-1">
              <DraftColorField
                label={t_i18n("Series {n}", { n: index + 1 })}
                value={color}
                alpha={false}
                onChange={(next) => next && onChange(colors.map((c, i) => (i === index ? next : c)))}
              />
            </div>
            <Button
              variant="ghost"
              size="icon"
              className="h-8 w-8 shrink-0"
              aria-label={t_i18n("Remove the colour")}
              onClick={() => {
                const next = colors.filter((_, i) => i !== index);
                onChange(next.length ? next : undefined);
              }}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        ))}
        <Button size="sm" variant="outline" className="w-fit gap-1.5" disabled={colors.length >= 20} onClick={() => onChange([...colors, "#2196f3"])}>
          <Plus className="h-3.5 w-3.5" aria-hidden="true" />
          {t_i18n("Add a colour")}
        </Button>
      </div>
    </Field>
  );
}

export interface VisualizationConfigProps {
  dashboard: CustomDashboard;
  designer: DashboardDesigner;
  vizId: string;
  /** The open tab's layout: where the Position fields look first. */
  layoutId: string | null;
  /** Open the source editor on a data source (`sid`), or on the visualization (`null`). */
  onEditSource: (sid: string | null) => void;
  onOpenDataPanel: () => void;
  /** Shown above the sections (a canvas element's own fields). */
  children?: ReactNode;
}

export function VisualizationConfig({ dashboard, designer, vizId, layoutId, onEditSource, onOpenDataPanel, children }: VisualizationConfigProps) {
  const { t_i18n } = useFormatter();
  const definition = designer.definition;
  const viz = record(record(definition?.visualizations)[vizId]);
  const type = typeof viz.type === "string" ? viz.type : "";
  const options = record(viz.options);
  const context = record(viz.context);
  const primary = record(viz.dataSources).primary;
  const annotation = record(viz.dataSources).annotation;
  const sources = record(definition?.dataSources);
  const source = typeof primary === "string" ? record(sources[primary]) : null;
  const shape = source ? record(source.options).shape : undefined;
  // A source that says its shape: the types drawing it; otherwise those drawing what this type draws.
  const fit = typeof shape === "string" && source?.type !== "platform" ? typesForShape(shape) : [];
  const types = fit.length ? fit : typesLike(type);
  const placement = definition ? placementOf(definition, vizId, layoutId) : null;
  // The interactions and visibility editors read the definition being designed and write through the designer.
  const designed = { ...dashboard, spec: definition ?? dashboard.spec };
  const edit = (operations: CustomDashboardOperation[], handlers?: { onSuccess?: () => void }) => {
    designer.change((current) => applyOperations(current, operations as PathOperation[]));
    handlers?.onSuccess?.();
  };

  if (!definition || !type) return null;
  const change = (update: (d: Raw) => Raw, coalesce?: string) => designer.change(update, coalesce ? `${vizId}.${coalesce}` : undefined);
  const setOption = (key: string, coalesce = false) => (value: unknown) => change((d) => setOptions(d, vizId, { [key]: value }), coalesce ? `options.${key}` : undefined);
  const sourceOptions = Object.entries(sources).map(([sid, raw]) => {
    const entry = record(raw);
    const kind = typeof entry.type === "string" && entry.type in SOURCE_TYPE_LABELS ? t_i18n(SOURCE_TYPE_LABELS[entry.type as keyof typeof SOURCE_TYPE_LABELS]) : "";
    return { value: sid, label: typeof entry.name === "string" && entry.name ? entry.name : sid, description: kind ? `${sid} · ${kind}` : sid };
  });

  const field = (spec: FieldSpec): ReactNode => {
    const label = t_i18n(FIELD_LABELS[spec.key]);
    const value = options[spec.key];
    switch (spec.kind) {
      case "select":
        return (
          <SelectField
            key={spec.key}
            label={label}
            value={typeof value === "string" ? value : undefined}
            options={spec.choices.map((choice) => ({ value: choice, label: t_i18n(CHOICE_LABELS[choice]) }))}
            unsetLabel={t_i18n("Default")}
            onChange={setOption(spec.key)}
          />
        );
      case "number":
        return spec.min !== undefined && spec.max !== undefined ? (
          <NumberField
            key={spec.key}
            label={label}
            value={typeof value === "number" ? value : undefined}
            min={spec.min}
            max={spec.max}
            step={spec.step}
            onChange={setOption(spec.key, true)}
          />
        ) : (
          <PlainNumberField key={spec.key} label={label} value={typeof value === "number" ? value : undefined} min={spec.min} onChange={setOption(spec.key, true)} />
        );
      case "switch":
        return (
          <SwitchField
            key={spec.key}
            label={label}
            checked={typeof value === "boolean" ? value : spec.fallback}
            onChange={(on) => setOption(spec.key)(on === spec.fallback ? undefined : on)}
          />
        );
      case "text":
        return <TextField key={spec.key} label={label} value={typeof value === "string" ? value : undefined} maxLength={spec.max} onChange={setOption(spec.key, true)} />;
      case "color":
        return <DraftColorField key={spec.key} label={label} value={typeof value === "string" ? value : undefined} alpha={false} placeholder={t_i18n("Theme")} onChange={setOption(spec.key, true)} />;
      case "colorRule":
        return (
          <ColorRuleField
            key={spec.key}
            label={label}
            rule={readColorRule(options, context, spec.key)}
            onChange={(rule) => change((d) => setColorRule(d, vizId, spec.key, spec.value, rule), rule.mode === "theme" ? undefined : `color.${spec.key}`)}
          />
        );
      case "colors":
        return (
          <ColorsField
            key={spec.key}
            label={label}
            colors={Array.isArray(value) ? value.filter((c): c is string => typeof c === "string") : []}
            onChange={setOption(spec.key, true)}
          />
        );
      case "ranges": {
        const ranges = (Array.isArray(value) ? value : []).map((entry) => {
          const r = record(entry);
          return { from: typeof r.from === "number" ? r.from : 0, to: typeof r.to === "number" ? r.to : 0, value: typeof r.value === "string" ? r.value : "#2196f3" };
        });
        return (
          <Field key={spec.key} label={label}>
            <RangesEditor ranges={ranges} bounded onChange={(next) => setOption(spec.key, true)(next.length ? next.map((r) => ({ from: r.from ?? 0, to: r.to ?? 0, value: r.value })) : undefined)} />
          </Field>
        );
      }
    }
  };

  const display = displayFields(type);
  const colors = colorFields(type);
  const trellis = record(options.trellis);

  return (
    <div className="flex flex-col" data-testid="dashboard.viz-config">
      <div className="flex items-center gap-2 px-3 pt-3 font-mono-plex text-[11px] text-default-secondary">
        <span className="truncate">{vizId}</span>
      </div>
      {children}

      <Section title={t_i18n("General")} icon={SlidersHorizontal}>
        <TextField
          label={t_i18n("Heading")}
          value={typeof viz.title === "string" ? viz.title : undefined}
          maxLength={120}
          onChange={(title) => change((d) => setVisualization(d, vizId, { title: title?.trim() ? title : undefined }), "title")}
        />
        <TextAreaField
          label={t_i18n("Description")}
          value={typeof viz.description === "string" ? viz.description : undefined}
          maxLength={200}
          rows={2}
          onChange={(description) => change((d) => setVisualization(d, vizId, { description: description?.trim() ? description : undefined }), "description")}
        />
        {isVizType(type) && (
          <SelectField
            label={t_i18n("Visualization type")}
            value={type}
            options={(types.includes(type) ? types : [type, ...types]).map((candidate) => ({ value: candidate, label: t_i18n(TYPE_LABELS[candidate]) }))}
            disabled={types.length <= 1}
            onChange={(next) => next && next !== type && change((d) => setType(d, vizId, next))}
          />
        )}
        {primary !== undefined && (
          <>
            <SwitchField
              label={t_i18n("Progress bar while it is read again")}
              checked={viz.showProgressBar === true}
              onChange={(on) => change((d) => setVisualization(d, vizId, { showProgressBar: on || undefined }))}
            />
            <SwitchField
              label={t_i18n("When it was read")}
              checked={viz.showLastUpdated === true}
              onChange={(on) => change((d) => setVisualization(d, vizId, { showLastUpdated: on || undefined }))}
            />
          </>
        )}
      </Section>

      {primary !== undefined && (
        <Section title={t_i18n("Data source")} icon={Database}>
          <Field label={t_i18n("Data source")}>
            <SearchableSelect
              aria-label={t_i18n("Data source")}
              value={typeof primary === "string" ? primary : null}
              options={sourceOptions}
              searchable="auto"
              onValueChange={(sid) => sid && change((d) => setVisualization(d, vizId, { dataSources: { ...record(viz.dataSources), primary: sid } }))}
            />
          </Field>
          {ANNOTATED.includes(type) && (
            <Field label={t_i18n("Annotations")}>
              <SearchableSelect
                aria-label={t_i18n("Annotations")}
                value={typeof annotation === "string" ? annotation : null}
                options={sourceOptions.filter((option) => option.value !== primary)}
                searchable="auto"
                clearable
                placeholder={t_i18n("None")}
                onValueChange={(sid) => {
                  const next = { ...record(viz.dataSources) };
                  if (sid) next.annotation = sid;
                  else delete next.annotation;
                  change((d) => setVisualization(d, vizId, { dataSources: next }));
                }}
              />
            </Field>
          )}
          <div className="flex flex-wrap gap-2">
            {typeof primary === "string" && (
              <Button size="sm" variant="outline" className="gap-1.5" onClick={() => onEditSource(primary)}>
                <Braces className="h-3.5 w-3.5" aria-hidden="true" />
                {t_i18n("Edit the data source")}
              </Button>
            )}
            <Button size="sm" variant="ghost" className="gap-1.5" onClick={onOpenDataPanel}>
              <Database className="h-3.5 w-3.5" aria-hidden="true" />
              {t_i18n("All data sources")}
            </Button>
          </div>
        </Section>
      )}

      {(display.length > 0 || takesTrellis(type)) && (
        <Section title={t_i18n("Data display")} icon={LayoutTemplate}>
          {display.map(field)}
          {takesTrellis(type) && (
            <div className="flex flex-col gap-3 rounded-sm border border-elevation-subtle-soft p-2">
              <TextField
                label={t_i18n(FIELD_LABELS.splitBy)}
                hint={t_i18n("A field of the data: one panel per value. Empty: one chart.")}
                value={typeof trellis.splitBy === "string" ? trellis.splitBy : undefined}
                maxLength={100}
                onChange={(splitBy) => setOption("trellis", true)(splitBy?.trim() ? { ...trellis, splitBy } : undefined)}
              />
              {typeof trellis.splitBy === "string" && (
                <>
                  <NumberField
                    label={t_i18n(FIELD_LABELS.columns)}
                    value={typeof trellis.columns === "number" ? trellis.columns : undefined}
                    min={1}
                    max={6}
                    step={1}
                    onChange={(columns) => setOption("trellis", true)({ ...trellis, columns: columns ?? "auto" })}
                  />
                  <SwitchField
                    label={t_i18n(FIELD_LABELS.sharedScale)}
                    checked={trellis.sharedScale !== false}
                    onChange={(on) => setOption("trellis")({ ...trellis, sharedScale: on })}
                  />
                  <SelectField
                    label={t_i18n(FIELD_LABELS.sort)}
                    value={typeof trellis.sort === "string" ? (trellis.sort as "name") : undefined}
                    options={(["name", "-name", "total", "-total"] as const).map((choice) => ({ value: choice, label: t_i18n(CHOICE_LABELS[choice]) }))}
                    unsetLabel={t_i18n("Default")}
                    onChange={(sort) => setOption("trellis")({ ...trellis, sort: sort ?? "name" })}
                  />
                </>
              )}
            </div>
          )}
        </Section>
      )}

      {colors.length > 0 && (
        <Section title={t_i18n("Coloring")} icon={Palette}>
          {colors.map(field)}
        </Section>
      )}

      {primary !== undefined && (
        <Section title={t_i18n("Interactions")} icon={MousePointerClick}>
          <InteractionsEditor dashboard={designed} vizId={vizId} edit={edit} />
        </Section>
      )}

      <Section title={t_i18n("Visibility")} icon={Eye} defaultOpen={false}>
        <VisibilityEditor key={JSON.stringify(viz.containerOptions ?? null)} dashboard={designed} vizId={vizId} edit={edit} />
      </Section>

      {placement && (
        <Section title={t_i18n("Position")} icon={Move}>
          <div className="grid grid-cols-2 gap-2">
            {(["x", "y", "w", "h"] as const).map((key) => {
              const grid = placement.type === "grid";
              const label =
                key === "x" ? (grid ? t_i18n("Column") : "X") : key === "y" ? (grid ? t_i18n("Row") : "Y") : key === "w" ? t_i18n("Width") : t_i18n("Height");
              // The grid counts from 1 for people, from 0 in the definition; `setPlacement` keeps it on its columns.
              const offset = grid && (key === "x" || key === "y") ? 1 : 0;
              return (
                <PlainNumberField
                  key={key}
                  label={grid && (key === "w" || key === "h") ? `${label} (${key === "w" ? t_i18n("columns") : t_i18n("rows")})` : grid ? label : `${label} (px)`}
                  value={placement[key] + offset}
                  min={key === "w" || key === "h" ? 1 : offset}
                  onChange={(value) => {
                    if (value === undefined) return;
                    change((d) => setPlacement(d, vizId, { ...placement, [key]: value - offset }), `position.${key}`);
                  }}
                />
              );
            })}
          </div>
        </Section>
      )}

      <Section title={t_i18n("Source")} icon={Braces} defaultOpen={false}>
        <pre className="max-h-48 overflow-auto rounded-sm bg-elevation-hover p-2 font-mono-plex text-[11px] leading-snug text-default-secondary">
          {JSON.stringify(viz, null, 2)}
        </pre>
        <Button size="sm" variant="outline" className="w-fit gap-1.5" onClick={() => onEditSource(null)}>
          <Braces className="h-3.5 w-3.5" aria-hidden="true" />
          {t_i18n("Open in the source editor")}
        </Button>
      </Section>
    </div>
  );
}
