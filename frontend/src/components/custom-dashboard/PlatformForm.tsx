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
 * The one form of a platform widget: it draws the description the platform's
 * adapter serves (`platform-form.ts`) and edits the stored configuration in
 * place - a key no control shows is written back as it was. It names no
 * platform: the types, fields, conditions and lists all come from the
 * description, and every list a field shows is asked to the platform through
 * the person's own integration (its `sources`, `usePlatformSchemas`).
 */
import { useMemo, type ReactNode } from "react";
import { Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { MultiSelect } from "@/components/ui/multi-select";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { usePlatformSchemas } from "@/hooks/api/useCustomDashboardApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { PlatformFormDescription, PlatformFormExpr, PlatformFormField, PlatformFormSource } from "@/types";

import { FilterGroupEditor } from "./FilterGroupEditor";
import { ColumnsEditor, DayField, DecimalField, Labelled, NumberField, Pick, SwitchField, TilePick, ValuesPicker } from "./platform-fields";
import {
  addable,
  addItem,
  childScope,
  choicesOf,
  evaluate,
  getIn,
  holds,
  listBounds,
  matches,
  numberOf,
  pathOf,
  questionsOf,
  readField,
  removeItem,
  rootScope,
  setIn,
  writeField,
  type FilterKeyOption,
  type Scope,
} from "./platform-form";
import { asGroup } from "./platform-native";

type Raw = Record<string, unknown>;

interface Env {
  form: PlatformFormDescription;
  base: { source: string; integration_id: string };
  onChange: (native: Raw) => void;
  t: (key: string, values?: Record<string, string | number>) => string;
}

const isObject = (value: unknown): value is Raw => typeof value === "object" && value !== null && !Array.isArray(value);
const strings = (value: unknown): string[] => (Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : []);

export function PlatformForm({
  form,
  source,
  integrationId,
  native,
  onChange,
}: {
  form: PlatformFormDescription;
  source: string;
  integrationId: string;
  native: Raw;
  onChange: (native: Raw) => void;
}) {
  const { t_i18n } = useFormatter();
  const env = useMemo<Env>(() => ({ form, base: { source, integration_id: integrationId }, onChange, t: t_i18n }), [form, source, integrationId, onChange, t_i18n]);
  const own = useMemo(() => rootScope(form, native), [form, native]);
  const scope = useSourced(form.sources, own, env.base);
  return (
    <div className="flex flex-col gap-3" data-testid="dashboard.sources.form">
      <Fields fields={form.fields} scope={scope} env={env} />
    </div>
  );
}

/** A scope with the answers of its schema questions (`null` until answered). */
function useSourced(sources: Record<string, PlatformFormSource> | undefined, scope: Scope, base: Env["base"]): Scope {
  const questions = questionsOf(sources, scope);
  const names = Object.keys(questions);
  const results = usePlatformSchemas(names.map((name) => (questions[name] ? { ...base, ...questions[name] } : null)));
  const answers: Scope["sources"] = { ...scope.sources };
  names.forEach((name, index) => {
    const data = results[index]?.data;
    answers[name] = questions[name] && data?.status === "ok" ? data.items : null;
  });
  return { ...scope, sources: answers, fetching: !!scope.fetching || results.some((result) => result.isFetching) };
}

/** An i18n key a description computes, translated; `{n}` is the item's number. */
function textOf(env: Env, scope: Scope, expr: PlatformFormExpr): string {
  const key = evaluate(expr, scope);
  return typeof key === "string" && key ? env.t(key, { n: Number(scope.vars.index ?? 0) + 1 }) : "";
}

function hintOf(env: Env, scope: Scope, field: PlatformFormField): string | undefined {
  const hint = field.hint;
  if (hint === undefined) return undefined;
  if (isObject(hint) && "text" in hint) return holds(hint.when as Raw | undefined, scope) ? textOf(env, scope, hint.text) : undefined;
  return textOf(env, scope, hint) || undefined;
}

function Fields({ fields, scope, env }: { fields: PlatformFormField[]; scope: Scope; env: Env }) {
  return (
    <>
      {fields.map((field, index) => (
        <FieldView key={index} field={field} scope={scope} env={env} />
      ))}
    </>
  );
}

function FieldView({ field, scope, env }: { field: PlatformFormField; scope: Scope; env: Env }): ReactNode {
  if (!holds(field.when, scope)) return null;
  switch (field.kind) {
    case "row": {
      const shown = (field.fields ?? []).filter((item) => holds(item.when, scope));
      if (!shown.length) return null;
      return (
        <div className={cn("grid gap-3", field.columns === 3 ? "sm:grid-cols-3" : "sm:grid-cols-2")}>
          <Fields fields={shown} scope={scope} env={env} />
        </div>
      );
    }
    case "list":
      return <ListField field={field} scope={scope} env={env} />;
    case "section":
      return <Item field={field} parent={scope} env={env} />;
    case "compose":
      return <Fields fields={field.fields ?? []} scope={childScope(scope, field)} env={env} />;
    case "note":
      return <p className="content-caption text-default-secondary">{textOf(env, scope, field.text)}</p>;
    case "filters":
      return <FiltersField field={field} scope={scope} env={env} />;
    case "columns":
      return <ColumnsField field={field} scope={scope} env={env} />;
    case "sorts":
      return <SortsField field={field} scope={scope} env={env} />;
    default:
      return <ValueField field={field} scope={scope} env={env} />;
  }
}

/** A field that edits one value: a select, a switch, a number, a text... */
function ValueField({ field, scope, env }: { field: PlatformFormField; scope: Scope; env: Env }) {
  const label = textOf(env, scope, field.label);
  const hint = hintOf(env, scope, field);
  const value = readField(field, scope);
  const fallback = evaluate(field.default, scope);
  const write = (next: unknown) => env.onChange(writeField(field, scope, next));
  const choices = () => choicesOf(field.options, scope, env.t);
  switch (field.kind) {
    case "select":
      if (field.display === "tiles") {
        return <TilePick label={label} hint={hint} value={typeof value === "string" ? value : typeof fallback === "string" ? fallback : ""} onChange={write} options={choices()} />;
      }
      return (
        <Pick
          label={label}
          hint={hint}
          value={typeof value === "string" ? value : typeof fallback === "string" ? fallback : ""}
          onChange={write}
          options={choices()}
          sort={field.sort}
        />
      );
    case "multiselect": {
      const selected = strings(value);
      const empty = textOf(env, scope, field.emptyLabel);
      return (
        <Labelled label={label} hint={hint}>
          <MultiSelect
            options={choices()}
            selected={selected}
            onToggle={(item) => write(selected.includes(item) ? selected.filter((v) => v !== item) : [...selected, item])}
            placeholder={textOf(env, scope, field.placeholder)}
            searchPlaceholder={env.t("Filter")}
            emptyOptionsLabel={scope.fetching ? env.t("Loading…") : empty}
            noMatchLabel={empty}
          />
        </Labelled>
      );
    }
    case "segmented":
      return (
        <Labelled label={label} hint={hint}>
          <SegmentedFilter
            ariaLabel={label}
            value={typeof value === "string" ? value : String(fallback ?? "")}
            onChange={write}
            options={choices().map((choice) => ({ key: choice.value, label: choice.label }))}
          />
        </Labelled>
      );
    case "text":
      return (
        <Labelled label={label} hint={hint}>
          <Input
            value={typeof value === "string" ? value : ""}
            onChange={(event) => write(event.target.value)}
            className="h-8"
            aria-label={field.ariaLabel ? textOf(env, scope, field.ariaLabel) : label}
          />
        </Labelled>
      );
    case "number": {
      const min = numberOf(evaluate(field.min, scope)) ?? 0;
      const max = numberOf(evaluate(field.max, scope)) ?? Number.MAX_SAFE_INTEGER;
      // Emptied, it stores what its `empty` says (`writeField`).
      if (field.decimal) return <DecimalField label={label} value={numberOf(value)} min={min} max={max} onChange={write} />;
      const preset = numberOf(fallback);
      const shown = value === undefined ? preset : value === null ? null : (numberOf(value) ?? preset);
      return <NumberField label={label} hint={hint} value={shown} min={min} max={max} onChange={write} />;
    }
    case "switch":
      return (
        <SwitchField
          label={label}
          hint={hint}
          checked={field.invert ? value === false : value === true}
          onChange={(on) => write(field.invert ? !on : on)}
        />
      );
    case "day":
      return <DayField label={label} value={value} onChange={write} />;
    case "values": {
      const values = field.single ? (typeof value === "string" && value ? [value] : []) : strings(value);
      return (
        <Labelled label={label} hint={hint}>
          <ValuesPicker
            base={env.base}
            filterKey={field.filterKey ?? ""}
            values={values}
            onChange={(next) => write(field.single ? (next[next.length - 1] ?? null) : next)}
            label={label}
          />
        </Labelled>
      );
    }
    default:
      return null;
  }
}

/** A filter group, with the filters another control edits hidden. */
function FiltersField({ field, scope, env }: { field: PlatformFormField; scope: Scope; env: Env }) {
  const at = pathOf(scope, field.path);
  const keys = evaluate(field.keys, scope);
  const perspective = evaluate(field.perspective, scope);
  return (
    <FilterGroupEditor
      form={env.form}
      group={asGroup(getIn(scope.root, at), env.form.filters?.nested ?? false)}
      onChange={(group) => env.onChange(setIn(scope.root, at, group) as Raw)}
      keys={Array.isArray(keys) ? (keys.filter(isObject) as unknown as FilterKeyOption[]) : []}
      base={{ ...env.base, ...(typeof perspective === "string" ? { perspective } : {}) }}
      label={textOf(env, scope, field.label)}
      hidden={(filter) => (field.hidden ?? []).some((match) => matches(filter, match, scope))}
    />
  );
}

/** A list's columns: objects (the column's value and label in them) or plain names. */
function ColumnsField({ field, scope, env }: { field: PlatformFormField; scope: Scope; env: Env }) {
  const at = pathOf(scope, field.path);
  const stored = getIn(scope.root, at);
  const current = Array.isArray(stored) ? stored : [];
  const write = (next: unknown) => env.onChange(setIn(scope.root, at, next) as Raw);
  const onReset = field.reset === undefined ? undefined : () => write(evaluate(field.reset, scope));
  const offered = evaluate(field.available, scope);
  const available = Array.isArray(offered) ? offered : [];
  if (field.items === "objects") {
    const valueKey = field.valueKey ?? "value";
    const labelKey = field.labelKey ?? "label";
    const columns = current.filter(isObject);
    const offeredColumns = available.filter(isObject);
    const choice = (column: Raw) => ({
      value: String(column[valueKey]),
      label: typeof column[labelKey] === "string" ? (column[labelKey] as string) : String(column[valueKey]),
    });
    return (
      <ColumnsEditor
        selected={columns.map(choice)}
        available={offeredColumns.map(choice)}
        // A column keeps what it holds (its label, a custom field's type); a new one is the platform's own.
        onChange={(values) =>
          write(values.map((v) => columns.find((c) => c[valueKey] === v) ?? offeredColumns.find((c) => c[valueKey] === v) ?? { [valueKey]: v }))
        }
        onReset={onReset}
      />
    );
  }
  const names = evaluate(field.names, scope);
  const named = (v: string) => ({
    value: v,
    label: (Array.isArray(names) ? names.find((n) => isObject(n) && n.value === v)?.text : undefined) ?? v,
  });
  return (
    <ColumnsEditor
      selected={strings(current).map(named)}
      available={strings(available).map(named)}
      onChange={write}
      onReset={onReset}
    />
  );
}

/** A list's sorts: a field and a direction each. */
function SortsField({ field, scope, env }: { field: PlatformFormField; scope: Scope; env: Env }) {
  const at = pathOf(scope, field.path);
  const stored = getIn(scope.root, at);
  const sorts = (Array.isArray(stored) ? stored : []).filter(isObject);
  const fieldKey = field.fieldKey ?? "field";
  const directionKey = field.directionKey ?? "direction";
  const max = numberOf(evaluate(field.max, scope)) ?? 5;
  const write = (next: Raw[]) => env.onChange(setIn(scope.root, at, next) as Raw);
  const set = (index: number, key: string, value: string) => write(sorts.map((s, i) => (i === index ? { ...s, [key]: value } : s)));
  const choices = choicesOf(field.choices, scope, env.t);
  const directions = choicesOf(field.directions, scope, env.t);
  return (
    <Labelled label={textOf(env, scope, field.label)}>
      <div className="flex flex-col gap-2">
        {sorts.map((sort, index) => (
          <div key={index} className="flex items-end gap-2">
            <div className="min-w-0 flex-1">
              <Pick label={env.t("Sort field")} value={String(sort[fieldKey] ?? "")} onChange={(v) => set(index, fieldKey, v)} options={choices} />
            </div>
            <div className="w-36">
              <Pick label={env.t("Direction")} value={String(sort[directionKey] ?? "")} onChange={(v) => set(index, directionKey, v)} options={directions} />
            </div>
            <Button variant="ghost" size="icon" className="h-8 w-8" aria-label={env.t("Remove the sort")} onClick={() => write(sorts.filter((_, i) => i !== index))}>
              <Trash2 className="h-4 w-4" />
            </Button>
          </div>
        ))}
        {sorts.length < max && (
          <Button variant="outline" size="sm" className="w-fit gap-1.5" onClick={() => write([...sorts, evaluate(field.new, scope) as Raw])}>
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            {env.t("Add a sort")}
          </Button>
        )}
      </div>
    </Labelled>
  );
}

/** A list of items (data selections, series), each its own scope, with the buttons that add one. */
function ListField({ field, scope, env }: { field: PlatformFormField; scope: Scope; env: Env }) {
  const { count, max } = listBounds(field, scope);
  const adds = (field.add ?? []).map((add, index) => ({ add, index })).filter(({ add }) => holds(add.when, scope));
  return (
    <>
      {Array.from({ length: count }, (_, index) => (
        <Item key={index} field={field} parent={scope} env={env} index={index} />
      ))}
      {holds(field.footerWhen, scope) && (adds.length > 0 || field.count !== undefined) && (
        <div className="flex flex-wrap items-center gap-2">
          {adds.map(({ add, index }) => (
            <Button
              key={index}
              variant="outline"
              size="sm"
              className="gap-1.5"
              disabled={!addable(field, scope, index)}
              onClick={() => env.onChange(addItem(field, scope, index))}
            >
              <Plus className="h-3.5 w-3.5" aria-hidden="true" />
              {textOf(env, scope, add.label)}
            </Button>
          ))}
          {field.count !== undefined && (
            <span className="self-center content-caption text-default-secondary">{env.t(String(evaluate(field.count, scope)), { count, max })}</span>
          )}
        </div>
      )}
    </>
  );
}

/** One item of a list (`index`), or a section's object: its title, its fields, its remove button. */
function Item({ field, parent, env, index }: { field: PlatformFormField; parent: Scope; env: Env; index?: number }) {
  const own = useMemo(() => childScope(parent, field, index), [parent, field, index]);
  const scope = useSourced(field.sources, own, env.base);
  const title = textOf(env, scope, field.title);
  const { count, min } = listBounds(field, parent);
  const removable = index !== undefined && count > min;
  return (
    <section className="flex flex-col gap-3 rounded-sm border border-elevation-subtle p-3" aria-label={title}>
      <div className="flex items-center gap-2">
        <span className="content-compact font-medium text-default-primary">{title}</span>
        {field.badge && holds(field.badge.when, scope) && (
          <span className="rounded-sm border border-elevation-subtle px-1.5 py-px content-caption text-default-secondary">{textOf(env, scope, field.badge.text)}</span>
        )}
        {removable && (
          <Button
            variant="ghost"
            size="icon"
            className="ml-auto h-7 w-7"
            aria-label={textOf(env, scope, field.removeLabel)}
            onClick={() => env.onChange(removeItem(field, parent, index))}
          >
            <Trash2 className="h-4 w-4" />
          </Button>
        )}
      </div>
      <Fields fields={field.fields ?? []} scope={scope} env={env} />
    </section>
  );
}
