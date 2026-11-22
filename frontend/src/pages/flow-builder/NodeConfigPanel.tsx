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
 * The node configuration drawer.
 *
 * Every field is rendered from the catalog's `config_fields`, not hand-written
 * per node kind: the server declares what a node needs, so a new node type gets
 * a working form without a frontend change — and the form can never offer a
 * field the validator does not know about.
 *
 * Follows the right-drawer pattern of `AgentFlowDrawer`, which the issue calls
 * out as the shape to reuse.
 */

import { useMemo, useState } from "react";
import { ColorPicker, Slider } from "@filigran/design-system";
import { Maximize2, Trash2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { CRON_PRESETS, describeCron } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { useFormatter } from "@/i18n";
import type {
  Agent,
  FlowConfigField,
  FlowFieldOption,
  FlowNodeCatalog,
  FlowNodeKind,
  FlowValidationIssue,
} from "@/types";
import type { CanvasEdge, CanvasNode } from "./graph-adapter";
import { DEFAULT_NODE_ICON, FLOW_NODE_ICONS, nodeStyle } from "./node-visuals";
import type { Shape } from "./output-shapes";
import { UpstreamDataPanel } from "./UpstreamDataPanel";
import { variableSuggestions } from "./upstream";
import { VariableInput } from "./VariableInput";
import { AgentNodeExtras } from "./AgentNodeExtras";
import { EventNodeExtras } from "./EventNodeExtras";
import { FlowCallArguments, ManualParamsExtras } from "./ParamsExtras";
import { IntegrationNodeExtras } from "./IntegrationNodeExtras";
import { PrimitiveExtras } from "./PrimitiveExtras";
import { ExpandedFieldDialog } from "./ExpandedFieldDialog";
import { useJsonFieldDraft } from "./json-field";
import { ScriptNodeExtras } from "./ScriptNodeExtras";
import { ToolNodeExtras } from "./ToolNodeExtras";
import { MultiSelectListField } from "./MultiSelectListField";

interface NodeConfigPanelProps {
  node: CanvasNode | null;
  kindDef: FlowNodeKind | undefined;
  catalog: FlowNodeCatalog | undefined;
  nodes: CanvasNode[];
  edges: CanvasEdge[];
  kindsById: Map<string, FlowNodeKind>;
  /** Node id → output name → shape (`useOutputShapes`), for typed paths. */
  outputShapes?: Map<string, Record<string, Shape>>;
  issues: FlowValidationIssue[];
  readOnly?: boolean;
  onClose: () => void;
  onChangeLabel: (label: string) => void;
  onChangeConfig: (name: string, value: unknown) => void;
  /** Merge several config keys in one commit (agent auto-map defaults). */
  onApplyConfigPatch?: (nodeId: string, patch: Record<string, unknown>) => void;
  /** Agent node: expand the selected agent's capabilities + handovers as nodes+edges. */
  onMaterializeAgentGraph?: (
    nodeId: string,
    agent: Agent,
  ) => void;
  onDelete: () => void;
  /** Imported node: clear the review notes its importer left, once checked. */
  onMarkReviewed?: () => void;
  /** Group panel: click a contained node to open its own drawer. */
  onSelectChild?: (nodeId: string) => void;
}

// Sentinel for the "Platform default" (inherit) row of an inheritable select —
// Radix needs a non-empty item value, so the empty/inherit state is encoded as
// this token and mapped back to `null` on change.
const INHERIT_VALUE = "__inherit__";

/** `key_value` fields persist as an object; the editor works on pairs. */
function toPairs(value: unknown): { key: string; value: string }[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  return Object.entries(value as Record<string, unknown>).map(([key, v]) => ({
    key,
    value: String(v ?? ""),
  }));
}

function fromPairs(pairs: { key: string; value: string }[]): Record<string, string> {
  return Object.fromEntries(pairs.filter((p) => p.key).map((p) => [p.key, p.value]));
}

function KeyValueField({
  value,
  onChange,
  disabled,
}: {
  value: unknown;
  onChange: (next: Record<string, string>) => void;
  disabled?: boolean;
}) {
  const { t_i18n } = useFormatter();
  const pairs = toPairs(value);
  const rows = [...pairs, { key: "", value: "" }];

  const update = (index: number, patch: Partial<{ key: string; value: string }>) => {
    const next = [...pairs];
    if (index >= next.length) next.push({ key: "", value: "" });
    next[index] = { ...next[index], ...patch };
    onChange(fromPairs(next));
  };

  return (
    <div className="space-y-1.5">
      {rows.map((pair, index) => (
        <div key={index} className="flex items-center gap-1.5">
          <Input aria-label={t_i18n("Name")}
            value={pair.key}
            placeholder={t_i18n("Name")}
            disabled={disabled}
            onChange={(e) => update(index, { key: e.target.value })}
            className="h-8 flex-1 content-compact"
          />
          <Input aria-label={t_i18n("Value")}
            value={pair.value}
            placeholder={t_i18n("Value")}
            disabled={disabled}
            onChange={(e) => update(index, { value: e.target.value })}
            className="h-8 flex-1 content-compact"
          />
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8 shrink-0"
            disabled={disabled || index >= pairs.length}
            onClick={() => onChange(fromPairs(pairs.filter((_, i) => i !== index)))}
            aria-label={t_i18n("Remove")}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      ))}
    </div>
  );
}

/**
 * A bounded numeric field as the design system's Slider plus a monospace
 * readout. `null`/unset shows "—" and parks the thumb at the field's default,
 * matching how the agent form renders an inherited value. The visible label is
 * the field row's own Label, so the thumb takes it as its accessible name.
 */
function SliderField({
  field,
  label,
  value,
  disabled,
  onChange,
}: {
  field: FlowConfigField;
  label: string;
  value: unknown;
  disabled?: boolean;
  onChange: (value: number) => void;
}) {
  const min = field.min ?? 0;
  const max = field.max ?? 100;
  const step = field.step ?? 1;
  const isInt = Number.isInteger(step);
  const decimals = isInt ? 0 : (String(step).split(".")[1]?.length ?? 1);
  const num = typeof value === "number" ? value : null;
  const fallback = typeof field.default === "number" ? field.default : min;

  return (
    <div className="flex items-center gap-3">
      <Slider
        aria-label={label}
        min={min}
        max={max}
        step={step}
        value={[num ?? fallback]}
        disabled={disabled}
        onValueChange={([next]) => {
          if (next === undefined) return;
          onChange(isInt ? Math.round(next) : Number(next.toFixed(decimals)));
        }}
        wrapperClassName="min-w-0 flex-1"
      />
      <span className="w-10 text-right font-mono-plex content-base tabular-nums">
        {num === null ? "—" : num.toFixed(decimals)}
      </span>
    </div>
  );
}

function ConfigFieldRow({
  field,
  value,
  options,
  suggestions,
  issue,
  disabled,
  codeLanguage,
  onChange,
}: {
  field: FlowConfigField;
  value: unknown;
  options: { value: string; label: string }[];
  suggestions: ReturnType<typeof variableSuggestions>;
  issue?: FlowValidationIssue;
  disabled?: boolean;
  /** Set when this field holds runnable code (util.code) — the expanded
   *  dialog then mounts the highlighted editor + Validate. */
  codeLanguage?: "python";
  onChange: (value: unknown) => void;
}) {
  const { t_i18n } = useFormatter();
  // Catalog text is English and translated where the catalogs carry it; text
  // holding `{{node.field}}` references is shown as is (not an ICU message).
  const catalogText = (text: string) => (/[{}]/.test(text) ? text : t_i18n(text));
  const label = catalogText(field.label);
  const inputId = `flow-field-${field.name}`;
  const [expanded, setExpanded] = useState(false);
  // A JSON field is edited as text and stores the value the text holds.
  const [jsonText, editJson] = useJsonFieldDraft(value, onChange);
  const isJson = field.type === "json";
  // Any field that edits a paragraph or more earns the bigger lens.
  const expandable =
    field.type === "variable_textarea" ||
    field.type === "textarea" ||
    isJson;

  const control = () => {
    switch (field.type) {
      case "boolean":
        return (
          <Switch
            aria-label={field.label}
            checked={!!value}
            disabled={disabled}
            onCheckedChange={(checked) => onChange(checked)}
          />
        );
      case "number":
        return (
          <Input aria-label={label}
            id={inputId}
            type="number"
            value={value === undefined || value === null ? "" : String(value)}
            placeholder={field.placeholder ?? undefined}
            disabled={disabled}
            onChange={(e) =>
              onChange(e.target.value === "" ? null : Number(e.target.value))
            }
          />
        );
      case "slider":
        return (
          <SliderField
            field={field}
            label={label}
            value={value}
            disabled={disabled}
            onChange={onChange}
          />
        );
      case "select": {
        // Inheritable overrides carry a leading "Platform default" choice that
        // stores an empty value — an agent with no explicit setting then reads
        // as inheriting, exactly like the agent config screen, instead of
        // showing a blank that looks like it needs attention.
        const hasValue = value !== undefined && value !== null && value !== "";
        const current = hasValue
          ? String(value)
          : field.inheritable
            ? INHERIT_VALUE
            : "";
        return (
          // A catalog source is a list of entities (agents, models...): sorted
          // and searchable. A field's own options keep the order it gives them.
          <SearchableSelect
            id={inputId}
            aria-label={label}
            searchable={field.source ? true : "auto"}
            sort={!!field.source}
            options={options}
            pinned={field.inheritable ? [{ value: INHERIT_VALUE, label: t_i18n("Platform default") }] : []}
            value={current}
            disabled={disabled}
            onValueChange={(v) => onChange(v === INHERIT_VALUE ? null : v)}
            placeholder={t_i18n("Choose…")}
            emptyMessage={t_i18n("Nothing available yet")}
          />
        );
      }
      case "multi_select":
        return (
          <MultiSelectListField
            options={options}
            value={Array.isArray(value) ? (value as string[]) : []}
            disabled={disabled}
            // Fields come from the loaded catalog's node kinds, so their
            // options have loaded too: a stored id they miss is unavailable.
            showUnlisted
            onChange={(next) => onChange(next)}
          />
        );
      case "cron":
        return (
          <div className="space-y-1.5">
            <Input aria-label={label}
              id={inputId}
              value={String(value ?? "")}
              placeholder={field.placeholder ?? "0 9 * * 1-5"}
              disabled={disabled}
              onChange={(e) => onChange(e.target.value)}
              className="font-mono-plex content-compact"
            />
            <div className="flex flex-wrap gap-1">
              {CRON_PRESETS.map((preset) => (
                <button
                  key={preset.value}
                  type="button"
                  disabled={disabled}
                  onClick={() => onChange(preset.value)}
                  className="rounded-sm border border-elevation-subtle px-1.5 py-0.5 content-caption text-default-secondary transition-colors hover:bg-elevation-hover disabled:opacity-50"
                >
                  {t_i18n(preset.label)}
                </button>
              ))}
            </div>
            {value ? (
              <p className="content-caption text-default-secondary">
                {describeCron(String(value))}
              </p>
            ) : null}
          </div>
        );
      case "json":
        return (
          <textarea
            id={inputId}
            rows={4}
            disabled={disabled}
            value={jsonText}
            placeholder={field.placeholder ?? "{}"}
            onChange={(e) => editJson(e.target.value)}
            className={cn("w-full rounded-sm border border-elevation-subtle bg-transparent px-3 py-2 font-mono-plex content-compact placeholder:text-default-secondary disabled:opacity-50", FIELD_FOCUS_RING)}
          />
        );
      case "key_value":
        return (
          <KeyValueField value={value} onChange={onChange} disabled={disabled} />
        );
      case "variable_text":
      case "variable_textarea":
        return (
          <VariableInput
            id={inputId}
            aria-label={label}
            value={String(value ?? "")}
            onChange={onChange}
            suggestions={suggestions}
            multiline={field.type === "variable_textarea"}
            placeholder={field.placeholder ?? undefined}
            disabled={disabled}
          />
        );
      case "textarea":
        return (
          <textarea
            id={inputId}
            rows={4}
            disabled={disabled}
            value={String(value ?? "")}
            placeholder={field.placeholder ?? undefined}
            onChange={(e) => onChange(e.target.value)}
            className={cn("w-full rounded-sm border border-elevation-subtle bg-transparent px-3 py-2 content-base placeholder:text-default-secondary disabled:opacity-50", FIELD_FOCUS_RING)}
          />
        );
      default:
        return (
          <Input aria-label={label}
            id={inputId}
            value={String(value ?? "")}
            placeholder={field.placeholder ?? undefined}
            disabled={disabled}
            onChange={(e) => onChange(e.target.value)}
          />
        );
    }
  };

  return (
    <div className="space-y-1.5">
      <Label htmlFor={inputId} className="flex items-center gap-1.5">
        {label}
        {field.required && <span className="text-feedback-error-primary">*</span>}
        {expandable && (
          <button
            type="button"
            className="ml-auto text-default-secondary transition-colors hover:text-default-primary"
            onClick={() => setExpanded(true)}
            aria-label={t_i18n("Expand")}
            title={t_i18n("Expand")}
          >
            <Maximize2 className="h-3 w-3" />
          </button>
        )}
      </Label>
      {control()}
      {expanded && (
        <ExpandedFieldDialog
          title={label}
          shape={
            codeLanguage
              ? "code"
              : field.type === "variable_textarea"
                ? "variable"
                : "plain"
          }
          value={
            isJson
              ? jsonText
              : typeof value === "string"
                ? value
                : value
                  ? JSON.stringify(value, null, 2)
                  : ""
          }
          suggestions={suggestions}
          placeholder={field.placeholder ?? undefined}
          disabled={disabled}
          onChange={isJson ? editJson : onChange}
          onClose={() => setExpanded(false)}
        />
      )}
      {issue && (
        <p
          className={cn(
            "content-caption",
            issue.severity === "error" ? "text-feedback-error-primary" : "text-feedback-warning-primary",
          )}
        >
          {issue.message}
        </p>
      )}
      {!issue && field.help && (
        <p className="content-caption text-default-secondary">{catalogText(field.help)}</p>
      )}
    </div>
  );
}

export function NodeConfigPanel({
  node,
  kindDef,
  catalog,
  nodes,
  edges,
  kindsById,
  outputShapes,
  issues,
  readOnly,
  onClose,
  onChangeLabel,
  onChangeConfig,
  onApplyConfigPatch,
  onMaterializeAgentGraph,
  onDelete,
  onMarkReviewed,
  onSelectChild,
}: NodeConfigPanelProps) {
  const { t_i18n } = useFormatter();

  const suggestions = useMemo(
    () =>
      node ? variableSuggestions(node.id, nodes, edges, kindsById, outputShapes) : [],
    [node, nodes, edges, kindsById, outputShapes],
  );
  const directParentIds = useMemo(
    () => new Set(node ? edges.filter((e) => e.target === node.id).map((e) => e.source) : []),
    [node, edges],
  );

  const issuesByField = useMemo(() => {
    const map = new Map<string, FlowValidationIssue>();
    for (const issue of issues) {
      // Errors win over warnings for the same field.
      if (!issue.field) continue;
      const existing = map.get(issue.field);
      if (!existing || (existing.severity === "warning" && issue.severity === "error")) {
        map.set(issue.field, issue);
      }
    }
    return map;
  }, [issues]);

  if (!node) return null;

  const Icon = FLOW_NODE_ICONS[kindDef?.icon ?? ""] ?? DEFAULT_NODE_ICON;
  const style = nodeStyle(node.data.kind, kindDef?.category);
  // An importer's notes (`source_payload`): `review` keeps the node from
  // running until cleared, `notes` only says what was approximated.
  const importList = (key: "review" | "notes"): string[] => {
    const value = node.data.sourcePayload?.[key];
    return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
  };
  const importReview = importList("review");
  const importNotes = importList("notes");
  // The review block below lists every note: its one-line warning would repeat
  // it, and once the notes are cleared that warning is stale until the next save.
  const nodeIssues = issues.filter((i) => !i.field && i.code !== "node.import_review");

  // A pinned provider narrows the model list to what that provider actually
  // publishes — the same model id can be served by several configured
  // providers, so an unfiltered list would offer choices that silently resolve
  // elsewhere. A stored model the filter would hide stays listed, so pinning a
  // provider never makes the current value vanish without a word.
  const pinnedProviderId =
    node.data.kind === "capability.llm" &&
    typeof node.data.config?.provider === "string"
      ? (node.data.config.provider as string)
      : "";
  const fieldOptions = (field: FlowConfigField): FlowFieldOption[] => {
    const options = field.source
      ? (catalog?.sources[field.source] ?? [])
      : field.options;
    // Keyed off the option SOURCE, not the field name: any other node that
    // grows a `provider` key and a field called `model` would otherwise
    // inherit this filtering by accident.
    if (field.source !== "models" || !pinnedProviderId) return options;
    const current = node.data.config?.model;
    return options.filter(
      (o) =>
        !o.provider_id ||
        o.provider_id === pinnedProviderId ||
        o.value === current,
    );
  };

  const renderField = (field: FlowConfigField) => (
    <ConfigFieldRow
      key={field.name}
      field={field}
      codeLanguage={
        node.data.kind === "util.code" && field.name === "code"
          ? "python"
          : undefined
      }
      value={node.data.config?.[field.name]}
      options={fieldOptions(field)}
      suggestions={suggestions}
      issue={issuesByField.get(field.name)}
      disabled={readOnly}
      onChange={(value) => onChangeConfig(field.name, value)}
    />
  );

  // Agent node: the choice of agent gates the rest of the drawer. Until one is
  // picked (or created), only the agent chooser + create button show — the
  // prompt and per-node overrides have nothing to override yet.
  const isAgent = node.data.kind === "agent";
  // A node with an LLM wired into its `llm` socket runs in LLM mode, and the
  // form follows: only that mode's fields are shown (the server enforces the
  // same split when it checks required fields).
  const llmWired = edges.some(
    (e) => e.target === node.id && e.targetHandle === "llm",
  );
  const activeMode = llmWired ? "llm" : "deterministic";
  const fields = (kindDef?.config_fields ?? []).filter(
    (f) =>
      (!f.mode || f.mode === activeMode) &&
      // The Integration node's operation/parameters are rendered by
      // IntegrationNodeExtras as a picker + a schema-driven form.
      !(
        node.data.kind === "integration" &&
        (f.name === "operation" || f.name === "parameters")
      ) &&
      // The Value node's object type is PrimitiveExtras' picker (with the
      // per-field form) — the generic select would render it twice.
      !(node.data.kind === "util.primitive" && f.name === "object_type_id") &&
      // The Call-tool node's picker + typed form live in ToolNodeExtras — in
      // both modes: the candidate list is a picker over the same callable
      // tools, which the generic multi-select has no option source for.
      !(
        node.data.kind === "action.call_tool" &&
        (f.name === "tool_name" ||
          f.name === "tool_id" ||
          f.name === "arguments" ||
          f.name === "candidate_tools" ||
          f.name === "candidate_tool_ids")
      ) &&
      // The Script node's pick-or-create + signature form live in
      // ScriptNodeExtras.
      !(
        node.data.kind === "action.script" &&
        (f.name === "custom_tool_id" || f.name === "arguments")
      ) &&
      // Get variable: the flow-variable fields and the collection picker are
      // mutually exclusive — only the chosen source's fields show.
      !(
        node.data.kind === "util.get_variable" &&
        (node.data.config?.source === "collection"
          ? f.name === "name" || f.name === "default"
          : f.name === "collection")
      ),
  );
  const agentIdField = isAgent
    ? fields.find((f) => f.name === "agent_id")
    : undefined;
  const agentRestFields = isAgent
    ? fields.filter((f) => f.name !== "agent_id")
    : [];
  const agentPicked = isAgent && !!node.data.config?.agent_id;

  return (
    <aside className="flex w-80 shrink-0 flex-col border-l border-elevation-subtle layer-1 bg-elevation-default">
      <div className="flex items-start gap-2.5 border-b border-elevation-subtle p-3">
        <div
          className={cn(
            "flex h-8 w-8 shrink-0 items-center justify-center rounded-sm",
            style.chip,
          )}
        >
          <Icon className={cn("h-4 w-4", style.accent)} />
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate content-base font-medium">
            {kindDef?.label ?? node.data.kind}
          </p>
          <p className="truncate font-mono-plex content-caption text-default-secondary">
            {node.id}
          </p>
        </div>
        <Button variant="ghost" size="icon" onClick={onClose} aria-label={t_i18n("Close")}>
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-3">
        {kindDef?.description && (
          <p className="text-[11px] text-default-secondary">
            {kindDef.description}
          </p>
        )}
        {/* Nodes that read upstream outputs: annotations and capabilities
            (wired INTO an agent, never executed) read none. */}
        {kindDef &&
          !kindDef.is_annotation &&
          kindDef.category !== "capabilities" &&
          kindDef.inputs.length > 0 && (
            <UpstreamDataPanel
              key={node.id}
              suggestions={suggestions}
              directParentIds={directParentIds}
            />
          )}

        {nodeIssues.map((issue) => (
          <p
            key={issue.code}
            className={cn(
              "rounded-sm border px-2.5 py-1.5 text-[11px]",
              issue.severity === "error"
                ? "border-feedback-error-primary/40 text-feedback-error-primary"
                : "border-feedback-alert-primary/40 text-feedback-warning-primary",
            )}
          >
            {issue.message}
          </p>
        ))}

        {importReview.length > 0 && (
          <div className="space-y-2 rounded-sm border border-feedback-alert-primary/40 px-2.5 py-2">
            <p className="content-caption font-semibold text-feedback-warning-primary">
              {t_i18n("Review before this node can run")}
            </p>
            <ul className="list-disc space-y-1 pl-4 text-[11px] text-default-secondary">
              {importReview.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
            {!readOnly && onMarkReviewed && (
              <Button size="sm" variant="outline" className="w-full" onClick={onMarkReviewed}>
                {t_i18n("Mark as reviewed")}
              </Button>
            )}
          </div>
        )}

        <div className="space-y-1.5">
          <Label htmlFor="flow-node-label">
            {t_i18n("Name on the canvas")}
          </Label>
          <Input aria-label={t_i18n("Name on the canvas")}
            id="flow-node-label"
            value={node.data.label ?? ""}
            placeholder={kindDef?.label}
            disabled={readOnly}
            onChange={(e) => onChangeLabel(e.target.value)}
          />
        </div>

        {node.data.kind === "util.group" && (
          <GroupExtras
            node={node}
            nodes={nodes}
            kindsById={kindsById}
            readOnly={readOnly}
            onChangeConfig={onChangeConfig}
            onSelectChild={onSelectChild}
          />
        )}

        {isAgent ? (
          <>
            {/* Step 1 — pick an existing agent, or create one. */}
            {agentIdField && renderField(agentIdField)}
            <AgentNodeExtras
              // Keyed by node so the auto-map / deploy-prompt refs reset when
              // the drawer moves to another agent node.
              key={node.id}
              node={node}
              disabled={readOnly}
              onApplyConfigPatch={onApplyConfigPatch}
              onMaterializeAgentGraph={onMaterializeAgentGraph}
            />
            {/* Step 2 — the prompt + per-node overrides, only once an agent is
                chosen (there is nothing to override before that). */}
            {agentPicked ? (
              agentRestFields.map(renderField)
            ) : (
              <p className="rounded-sm border border-dashed border-elevation-subtle-soft px-3 py-2 text-[11px] text-default-secondary">
                {t_i18n(
                  "Pick an agent above or create one to configure this step.",
                )}
              </p>
            )}
          </>
        ) : (
          <>
            {/* Why the form just changed shape. Only worth saying on a node
                that actually has an LLM socket to wire. */}
            {llmWired &&
              (kindDef?.inputs ?? []).some((p) => p.id === "llm") && (
                <p className="rounded-sm border border-filigran-tonic-primary/40 px-2.5 py-1.5 content-caption text-filigran-tonic-primary">
                  {t_i18n(
                    "An LLM is wired in: this node judges in plain language instead of comparing strings.",
                  )}
                </p>
              )}
            {fields.map(renderField)}
            {node.data.kind === "trigger.manual" && (
              <ManualParamsExtras
                key={node.id}
                node={node}
                readOnly={!!readOnly}
                onChangeConfig={onChangeConfig}
              />
            )}
            {node.data.kind === "util.primitive" && (
              <PrimitiveExtras
                key={node.id}
                node={node}
                variableOptions={catalog?.sources["variables"] ?? []}
                readOnly={!!readOnly}
                onChangeConfig={onChangeConfig}
                onApplyConfigPatch={onApplyConfigPatch}
              />
            )}
            {node.data.kind === "action.call_tool" && (
              <ToolNodeExtras
                key={node.id}
                node={node}
                suggestions={suggestions}
                readOnly={!!readOnly}
                llmMode={llmWired}
                onChangeConfig={onChangeConfig}
                onApplyConfigPatch={onApplyConfigPatch}
              />
            )}
            {node.data.kind === "action.script" && (
              <ScriptNodeExtras
                key={node.id}
                node={node}
                suggestions={suggestions}
                readOnly={!!readOnly}
                onChangeConfig={onChangeConfig}
                onApplyConfigPatch={onApplyConfigPatch}
              />
            )}
            {node.data.kind === "integration" &&
              typeof node.data.config?.integration_id === "string" &&
              node.data.config.integration_id && (
                <IntegrationNodeExtras
                  key={`${node.id}-${node.data.config.integration_id}`}
                  node={node}
                  integrationId={node.data.config.integration_id as string}
                  suggestions={suggestions}
                  readOnly={!!readOnly}
                  onChangeConfig={onChangeConfig}
                  onApplyConfigPatch={onApplyConfigPatch}
                />
              )}
            {node.data.kind === "flow.call" &&
              typeof node.data.config?.flow_id === "string" &&
              node.data.config.flow_id && (
                <FlowCallArguments
                  key={`${node.id}-${node.data.config.flow_id}`}
                  node={node}
                  flowId={node.data.config.flow_id as string}
                  suggestions={suggestions}
                  readOnly={!!readOnly}
                  onChangeConfig={onChangeConfig}
                />
              )}
            {["action.emit_event", "trigger.event", "util.wait_event", "capability.on_delegate"].includes(
              node.data.kind,
            ) && (
              <EventNodeExtras
                key={node.id}
                node={node}
                suggestions={suggestions}
                readOnly={!!readOnly}
                onChangeConfig={onChangeConfig}
              />
            )}
          </>
        )}

        {/* The reverse index: which nodes consume THIS node's outputs — the
            "find usages" every rename or deletion should start from. */}
        {(() => {
          const marker = `{{${node.id}.`;
          const referencedBy = nodes.filter(
            (n) =>
              n.id !== node.id &&
              JSON.stringify(n.data.config ?? {}).includes(marker),
          );
          if (referencedBy.length === 0) return null;
          return (
            <div className="space-y-1.5 border-t border-elevation-subtle-soft pt-3">
              <p className="content-caption font-semibold uppercase tracking-wide text-default-secondary">
                {t_i18n("Referenced by")}
              </p>
              <div className="space-y-1">
                {referencedBy.map((n) => {
                  const refDef = kindsById.get(n.data.kind);
                  const RefIcon =
                    FLOW_NODE_ICONS[refDef?.icon ?? ""] ?? DEFAULT_NODE_ICON;
                  return (
                    <button
                      key={n.id}
                      type="button"
                      className="flex w-full items-center gap-1.5 rounded-sm px-1.5 py-1 text-left text-[11px] hover:bg-elevation-hover"
                      onClick={() => onSelectChild?.(n.id)}
                    >
                      <RefIcon className="h-3 w-3 shrink-0 text-default-secondary" />
                      <span className="truncate">
                        {n.data.label || refDef?.label || n.data.kind}
                      </span>
                      <span className="ml-auto font-mono-plex text-[9px] text-default-secondary">
                        {n.id}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
          );
        })()}

        {kindDef && kindDef.output_variables.length > 0 && (
          <div className="space-y-1.5 border-t border-elevation-subtle-soft pt-3">
            <p className="content-caption font-semibold uppercase tracking-wide text-default-secondary">
              {t_i18n("Publishes")}
            </p>
            <div className="flex flex-wrap gap-1">
              {kindDef.output_variables.map((output) => (
                <Badge
                  key={output.name}
                  variant="outline"
                  className="font-mono-plex content-caption"
                  title={output.description && !/[{}]/.test(output.description) ? t_i18n(output.description) : output.description}
                >
                  {`{{${node.id}.${output.name}}}`}
                </Badge>
              ))}
            </div>
          </div>
        )}

        {importNotes.length > 0 && (
          <div className="space-y-1.5 border-t border-elevation-subtle-soft pt-3">
            <p className="content-caption font-semibold uppercase tracking-wide text-default-secondary">
              {t_i18n("Import notes")}
            </p>
            <ul className="list-disc space-y-1 pl-4 text-[11px] text-default-secondary">
              {importNotes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          </div>
        )}

        {node.data.sourcePayload && (
          <div className="space-y-1.5 border-t border-elevation-subtle-soft pt-3">
            <p className="content-caption font-semibold uppercase tracking-wide text-default-secondary">
              {t_i18n("Imported definition")}
            </p>
            <pre className="max-h-48 overflow-auto rounded-sm bg-elevation-hover p-2 font-mono-plex content-caption text-default-secondary">
              {JSON.stringify(node.data.sourcePayload, null, 2)}
            </pre>
          </div>
        )}
      </div>

      {!readOnly && (
        <div className="border-t border-elevation-subtle p-3">
          <Button
            variant="outline"
            className="w-full gap-1.5 border-feedback-error-primary/40 text-feedback-error-primary hover:bg-feedback-error-primary/10"
            onClick={onDelete}
          >
            <Trash2 className="h-4 w-4" /> {t_i18n("Delete node")}
          </Button>
        </div>
      )}
    </aside>
  );
}

/** Group drawer extras: border/background colours + the contained nodes. */
function GroupExtras({
  node,
  nodes,
  kindsById,
  readOnly,
  onChangeConfig,
  onSelectChild,
}: {
  node: CanvasNode;
  nodes: CanvasNode[];
  kindsById: Map<string, FlowNodeKind>;
  readOnly?: boolean;
  onChangeConfig: (name: string, value: unknown) => void;
  onSelectChild?: (nodeId: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const children = nodes.filter((n) => n.parentId === node.id);

  const colorRow = (key: "border_color" | "bg_color", label: string, fallback: string) => {
    const value = typeof node.data.config?.[key] === "string" ? (node.data.config[key] as string) : "";
    return (
      <div className="space-y-1.5">
        <ColorPicker
          label={label}
          aria-label={label}
          value={value || fallback}
          disabled={readOnly}
          onValueChange={(next) => {
            // A typed draft that is not a colour yet is not saved: only a
            // whole #rrggbb or a cleared field reaches the node config.
            if (next === "") onChangeConfig(key, undefined);
            else if (/^#[0-9a-fA-F]{6}$/.test(next)) onChangeConfig(key, next);
          }}
        />
        {value && (
          <div className="flex justify-end">
            <button
              type="button"
              className="content-caption text-default-secondary hover:text-default-primary"
              onClick={() => onChangeConfig(key, undefined)}
            >
              {t_i18n("Reset")}
            </button>
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="space-y-3">
      <div className="space-y-2 rounded-sm border border-elevation-subtle-soft p-2.5">
        {colorRow("border_color", t_i18n("Border color"), "#6b7280")}
        {colorRow("bg_color", t_i18n("Background color"), "#6b7280")}
      </div>

      <div className="space-y-1.5">
        <p className="content-caption font-semibold uppercase tracking-wide text-default-secondary">
          {t_i18n("Contains")}
        </p>
        {children.length === 0 ? (
          <p className="rounded-sm border border-dashed border-elevation-subtle-soft px-3 py-2 text-[11px] text-default-secondary">
            {t_i18n("Empty: drag nodes inside the frame to add them.")}
          </p>
        ) : (
          <div className="space-y-0.5">
            {children.map((child) => {
              const def = kindsById.get(child.data.kind);
              const Icon = FLOW_NODE_ICONS[def?.icon ?? ""] ?? DEFAULT_NODE_ICON;
              const style = nodeStyle(child.data.kind, def?.category);
              return (
                <button
                  key={child.id}
                  type="button"
                  onClick={() => onSelectChild?.(child.id)}
                  className="flex w-full items-center gap-2 rounded-sm px-2 py-1 text-left content-compact transition-colors hover:bg-elevation-hover"
                >
                  <Icon className={cn("h-3.5 w-3.5 shrink-0", style.accent)} />
                  <span className="truncate">
                    {child.data.label || def?.label || child.data.kind}
                  </span>
                  <span className="ml-auto truncate font-mono-plex text-[9px] text-default-secondary">
                    {child.id}
                  </span>
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
