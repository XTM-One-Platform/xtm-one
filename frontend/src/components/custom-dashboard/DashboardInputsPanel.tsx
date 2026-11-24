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
 * The studio's Inputs panel: the dashboard's inputs and its tokens: the
 * inputs and the token manager.
 *
 * - Inputs: create one (time range, dropdown, multiselect, text, number, an
 *   entity of a platform searched with the viewer's access, or a button
 *   setting a token and switching tab), placed above the tabs; edit it
 *   in the source editor; delete it. Saved as `set` / `delete` operations,
 *   validated server-side.
 * - The Submit button: inputs change their tokens only when it is pressed.
 * - The token manager: every token the dashboard names or sets, its value on
 *   the stage now, its default (editable), what uses it and what sets it.
 */
import { useId, useMemo, useState } from "react";
import { FileJson, Plus, SlidersHorizontal, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Switch } from "@/components/ui/switch";
import { TagInput } from "@/components/ui/tag-input";
import { usePackIntegrations } from "@/hooks/api/useWidgetPackApi";
import { useFormatter } from "@/i18n";
import type { CustomDashboard, CustomDashboardOperation } from "@/types";

import { addInputOperations, freeId, sourceEntries, submitButtonOperation, tokenDefaultOperation } from "./definition";
import { readInputs, tokenTable, type InputKind } from "./inputs";

type Edit = (operations: CustomDashboardOperation[], handlers?: { onSuccess?: () => void; onError?: (error: Error) => void }) => void;

/** Each input type's label. @i18n-keys */
const KIND_LABELS = {
  timerange: "Time range",
  dropdown: "Dropdown",
  multiselect: "Multiselect",
  text: "Text",
  number: "Number",
  button: "Button",
  entity: "Entity",
} as const satisfies Record<InputKind, string>;

const TOKEN_RE = /^[A-Za-z_][A-Za-z0-9_.]{0,59}$/;

/** A form field: the design system label above its control, linked by the control's id. */
function Labelled({ label, htmlFor, children }: { label: string; htmlFor: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
    </div>
  );
}

/** "label=value" lines (or bare values) as a dropdown's items. */
function itemsOf(text: string): { label: string; value: string }[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, 200)
    .map((line) => {
      const at = line.indexOf("=");
      return at > 0 ? { label: line.slice(0, at).trim(), value: line.slice(at + 1).trim() } : { label: line, value: line };
    });
}

function NewInputDialog({ dashboard, open, onOpenChange, edit }: { dashboard: CustomDashboard; open: boolean; onOpenChange: (open: boolean) => void; edit: Edit }) {
  const { t_i18n } = useFormatter();
  const [kind, setKind] = useState<InputKind>("dropdown");
  const [title, setTitle] = useState("");
  const [token, setToken] = useState("");
  const [defaultValue, setDefaultValue] = useState("");
  const [items, setItems] = useState("");
  const [sourceId, setSourceId] = useState("");
  const [valueField, setValueField] = useState("");
  const [buttonValue, setButtonValue] = useState("");
  const [tab, setTab] = useState("");
  const [integrationId, setIntegrationId] = useState("");
  const [entityTypes, setEntityTypes] = useState<string[]>(["Stix-Core-Object"]);
  const [everyWhileEmpty, setEveryWhileEmpty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fieldId = useId();
  const idOf = (name: string) => `${fieldId}-${name}`;
  // The platforms an entity input may search: those of the person's integrations.
  const integrations = usePackIntegrations(open && kind === "entity");
  const platforms = useMemo(() => (integrations.data ?? []).filter((i) => i.provider !== "xtm_hub"), [integrations.data]);
  const sources = useMemo(() => sourceEntries(dashboard.spec), [dashboard.spec]);
  const tabs = useMemo(() => {
    const items = (dashboard.spec as { layout?: { tabs?: { items?: { layoutId: string; label: string }[] } } })?.layout?.tabs?.items;
    return Array.isArray(items) ? items : [];
  }, [dashboard.spec]);

  const save = () => {
    if (!TOKEN_RE.test(token)) {
      setError(t_i18n("A token name is letters, digits, '_' or '.', starting with a letter."));
      return;
    }
    const input: Record<string, unknown> = { type: `input.${kind}`, ...(title.trim() ? { title: title.trim() } : {}) };
    if (kind === "button") {
      input.options = {
        label: title.trim() || token,
        actions: [{ type: "setToken", token, value: buttonValue }, ...(tab ? [{ type: "switchTab", layoutId: tab }] : [])],
      };
    } else {
      input.token = token;
      if (defaultValue.trim()) {
        input.defaultValue =
          kind === "multiselect" ? defaultValue.split(",").map((v) => v.trim()).filter(Boolean) : kind === "number" ? Number(defaultValue) : defaultValue.trim();
      }
      if (kind === "dropdown" || kind === "multiselect") {
        input.options = { items: itemsOf(items), ...(valueField.trim() ? { valueField: valueField.trim() } : {}) };
        if (sourceId) input.dataSources = { primary: sourceId };
      }
      if (kind === "entity") {
        const integration = platforms.find((i) => i.id === integrationId);
        if (!integration) {
          setError(t_i18n("Choose the integration the entities are searched in."));
          return;
        }
        const types = entityTypes.map((t) => t.trim()).filter(Boolean);
        input.options = {
          source: integration.provider,
          integration_id: integration.id,
          entityTypes: types.length ? types : ["Stix-Core-Object"],
          // Not required: its filters are left out while nothing is picked (an OpenAEV dashboard parameter).
          ...(everyWhileEmpty ? { required: false } : {}),
        };
      }
    }
    const id = freeId(dashboard.spec, `input_${token}`);
    edit(addInputOperations(dashboard.spec, id, input), {
      onSuccess: () => onOpenChange(false),
      onError: (failure) => setError(failure.message),
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[90dvh] max-w-xl flex-col gap-4 overflow-y-auto" data-testid="dashboard.inputs.new">
        <DialogHeader>
          <DialogTitle>{t_i18n("New input")}</DialogTitle>
          <DialogDescription>{t_i18n("An input sets a token; a data source, a title or a text names it as $token$.")}</DialogDescription>
        </DialogHeader>
        <div className="grid gap-x-3 gap-y-5 sm:grid-cols-2">
          <Labelled label={t_i18n("Type")} htmlFor={idOf("type")}>
            <SearchableSelect
              id={idOf("type")}
              aria-label={t_i18n("Type")}
              value={kind}
              onValueChange={(next) => setKind(next as InputKind)}
              options={(Object.keys(KIND_LABELS) as InputKind[]).map((key) => ({ value: key, label: t_i18n(KIND_LABELS[key]) }))}
              sort={false}
              searchable="auto"
            />
          </Labelled>
          <Labelled label={t_i18n("Token")} htmlFor={idOf("token")}>
            <Input id={idOf("token")} value={token} onChange={(event) => setToken(event.target.value)} placeholder="severity" aria-label={t_i18n("Token")} />
          </Labelled>
          <Labelled label={kind === "button" ? t_i18n("Label") : t_i18n("Title (heading)")} htmlFor={idOf("title")}>
            <Input id={idOf("title")} value={title} onChange={(event) => setTitle(event.target.value)} aria-label={kind === "button" ? t_i18n("Label") : t_i18n("Title (heading)")} />
          </Labelled>
          {kind === "button" ? (
            <Labelled label={t_i18n("Value it sets")} htmlFor={idOf("button-value")}>
              <Input id={idOf("button-value")} value={buttonValue} onChange={(event) => setButtonValue(event.target.value)} aria-label={t_i18n("Value it sets")} />
            </Labelled>
          ) : (
            <Labelled label={t_i18n("Default value")} htmlFor={idOf("default")}>
              <Input
                id={idOf("default")}
                value={defaultValue}
                onChange={(event) => setDefaultValue(event.target.value)}
                placeholder={kind === "timerange" ? "30d" : kind === "multiselect" ? "a, b" : ""}
                aria-label={t_i18n("Default value")}
              />
            </Labelled>
          )}
          {kind === "entity" && (
            <>
              <Labelled label={t_i18n("Searched in")} htmlFor={idOf("integration")}>
                <SearchableSelect
                  id={idOf("integration")}
                  aria-label={t_i18n("Searched in")}
                  value={integrationId}
                  onValueChange={setIntegrationId}
                  options={platforms.map((i) => ({ value: i.id, label: i.name }))}
                  sort={false}
                  searchable="auto"
                />
              </Labelled>
              <Labelled label={t_i18n("Entity types")} htmlFor={idOf("entity-types")}>
                <TagInput
                  id={idOf("entity-types")}
                  value={entityTypes}
                  onChange={setEntityTypes}
                  placeholder="Country"
                  preserveCase
                  suggestFromPlatformTags={false}
                  aria-label={t_i18n("Entity types")}
                />
              </Labelled>
              <label className="flex items-center justify-between gap-2 content-compact">
                <span>{t_i18n("Read every entity while none is picked")}</span>
                <Switch
                  checked={everyWhileEmpty}
                  onCheckedChange={setEveryWhileEmpty}
                  aria-label={t_i18n("Read every entity while none is picked")}
                />
              </label>
            </>
          )}
          {kind === "button" && (
            <Labelled label={t_i18n("Then switch to tab")} htmlFor={idOf("tab")}>
              <SearchableSelect
                id={idOf("tab")}
                aria-label={t_i18n("Then switch to tab")}
                value={tab}
                onValueChange={setTab}
                options={tabs.map((item) => ({ value: item.layoutId, label: item.label }))}
                sort={false}
                searchable="auto"
                clearable
              />
            </Labelled>
          )}
          {(kind === "dropdown" || kind === "multiselect") && (
            <>
              <Labelled label={t_i18n("Choices from a data source (optional)")} htmlFor={idOf("source")}>
                <SearchableSelect
                  id={idOf("source")}
                  aria-label={t_i18n("Choices from a data source (optional)")}
                  value={sourceId}
                  onValueChange={setSourceId}
                  options={sources.map((source) => ({ value: source.id, label: source.name ?? source.id }))}
                  sort={false}
                  searchable="auto"
                  clearable
                />
              </Labelled>
              <Labelled label={t_i18n("Field giving the value (optional)")} htmlFor={idOf("value-field")}>
                <Input id={idOf("value-field")} value={valueField} onChange={(event) => setValueField(event.target.value)} aria-label={t_i18n("Field giving the value (optional)")} />
              </Labelled>
            </>
          )}
        </div>
        {(kind === "dropdown" || kind === "multiselect") && (
          <Labelled label={t_i18n("Choices, one per line (label=value)")} htmlFor={idOf("items")}>
            <textarea
              id={idOf("items")}
              value={items}
              onChange={(event) => setItems(event.target.value)}
              rows={4}
              aria-label={t_i18n("Choices, one per line (label=value)")}
              className="rounded-sm border border-elevation-subtle bg-elevation-default p-2 font-mono-plex content-compact"
            />
          </Labelled>
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
          <Button onClick={save}>{t_i18n("Create")}</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function DashboardInputsPanel({
  dashboard,
  tokens,
  edit,
  onEditInput,
}: {
  dashboard: CustomDashboard;
  /** The tokens on the stage now. */
  tokens: Record<string, unknown>;
  edit: Edit;
  onEditInput: (path: string) => void;
}) {
  const { t_i18n } = useFormatter();
  const read = useMemo(() => readInputs((dashboard.spec ?? {}) as Record<string, unknown>), [dashboard.spec]);
  const rows = useMemo(() => tokenTable(dashboard.spec, tokens), [dashboard.spec, tokens]);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState(0);
  const [defaults, setDefaults] = useState<Record<string, string>>({});
  const canEdit = dashboard.can_manage;

  // A token an input sets takes its default from the input (`defaultValue`), the others from the definition's defaults.
  const defaultOperation = (name: string, value: string): CustomDashboardOperation => {
    const input = read.inputs.find((candidate) => candidate.token === name);
    const stored = input ? (dashboard.spec as { inputs?: Record<string, Record<string, unknown>> }).inputs?.[input.id] : undefined;
    if (!input || !stored) return tokenDefaultOperation(dashboard.spec, name, value || null);
    const { defaultValue: _previous, ...rest } = stored;
    const parsed = input.kind === "multiselect" ? value.split(",").map((v) => v.trim()).filter(Boolean) : input.kind === "number" ? Number(value) : value;
    return { op: "set", path: `inputs.${input.id}`, value: value ? { ...rest, defaultValue: parsed } : rest };
  };

  const menu = (id: string): OverflowMenuItem[] => [
    { id: "edit", label: t_i18n("Edit in the source editor"), icon: FileJson, onSelect: () => onEditInput(`inputs.${id}`) },
    { id: "delete", label: t_i18n("Delete"), icon: Trash2, destructive: true, disabled: !canEdit, onSelect: () => edit([{ op: "delete", path: `inputs.${id}` }]) },
  ];

  return (
    <div className="flex h-full flex-col gap-4 overflow-y-auto p-3" data-testid="dashboard.inputs">
      <section className="flex flex-col gap-2" aria-label={t_i18n("Inputs")}>
        <div className="flex items-center justify-between gap-2">
          <h2 className="content-compact font-semibold text-default-primary">{t_i18n("Inputs")}</h2>
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
        {read.inputs.length === 0 && <p className="content-compact text-default-secondary">{t_i18n("No input yet.")}</p>}
        <ul className="flex flex-col gap-2">
          {read.inputs.map((input) => (
            <li key={input.id} className="flex items-start gap-2 rounded-sm border border-elevation-subtle p-2.5" data-input-id={input.id}>
              <SlidersHorizontal className="mt-0.5 h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <span className="truncate content-compact font-medium text-default-primary">{input.title ?? input.label ?? input.id}</span>
                <span className="flex flex-wrap items-center gap-1.5 content-caption text-default-secondary">
                  <span className="rounded-sm border border-elevation-subtle px-1.5 py-px">{t_i18n(KIND_LABELS[input.kind])}</span>
                  {input.token && <code className="font-mono-plex">${input.token}$</code>}
                </span>
              </div>
              <OverflowMenu items={menu(input.id)} label={t_i18n("Actions on {name}", { name: input.title ?? input.id })} className="h-6 w-6" />
            </li>
          ))}
        </ul>
        <label className="flex items-center justify-between gap-2 content-compact">
          <span>{t_i18n("Submit button")}</span>
          <Switch
            checked={read.submitButton}
            disabled={!canEdit}
            onCheckedChange={(on) => edit([submitButtonOperation(dashboard.spec, on)])}
            aria-label={t_i18n("Submit button")}
          />
        </label>
      </section>

      <section className="flex flex-col gap-2" aria-label={t_i18n("Tokens")}>
        <h2 className="content-compact font-semibold text-default-primary">{t_i18n("Tokens")}</h2>
        {rows.length === 0 && <p className="content-compact text-default-secondary">{t_i18n("No token yet.")}</p>}
        <ul className="flex flex-col gap-2">
          {rows.map((row) => {
            const computed = row.name.includes(":");
            return (
              <li key={row.name} className="flex flex-col gap-1 rounded-sm border border-elevation-subtle p-2.5 content-caption" data-token={row.name}>
                <code className="font-mono-plex content-compact text-default-primary">${row.name}$</code>
                <span className="text-default-secondary">
                  {t_i18n("Now")}: <span className="text-default-primary">{row.value ?? "—"}</span>
                </span>
                {!computed && canEdit && (
                  <Input
                    value={defaults[row.name] ?? row.defaultValue ?? ""}
                    onChange={(event) => setDefaults((current) => ({ ...current, [row.name]: event.target.value }))}
                    onBlur={() => {
                      const next = defaults[row.name];
                      if (next === undefined || next === (row.defaultValue ?? "")) return;
                      edit([defaultOperation(row.name, next.trim())]);
                    }}
                    placeholder={t_i18n("Default value")}
                    className="h-7"
                    aria-label={t_i18n("Default of {name}", { name: row.name })}
                  />
                )}
                <span className="text-default-secondary">
                  {t_i18n("Used by")}: {row.usedBy.length ? row.usedBy.join(", ") : "—"}
                </span>
                <span className="text-default-secondary">
                  {t_i18n("Set by")}: {row.setBy.length ? row.setBy.join(", ") : "—"}
                </span>
              </li>
            );
          })}
        </ul>
      </section>
      <NewInputDialog key={form} dashboard={dashboard} open={creating} onOpenChange={setCreating} edit={edit} />
    </div>
  );
}
