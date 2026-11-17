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
 * A filter group of a platform widget, edited the way the platform's own
 * filter editor edits it (`filters` of its form description, the dialect):
 * filters (a key from the platform's schema, the operators the platform
 * offers for its type, values searched in the platform or typed), each with
 * its mode when it holds several values; the group's mode and nested groups
 * where the platform has them. A filter's keys the editor does not change - a
 * filter's `id`, values it cannot show - are written back as they were.
 */
import { Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import type { PlatformSchemaQuestion } from "@/hooks/api/useCustomDashboardApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { PlatformFormDescription } from "@/types";

import { FreeValues, Labelled, Pick, ValuesPicker } from "./platform-fields";
import { defaultOperatorFor, newFilter, operatorsFor, searchedFor, type FilterKeyOption } from "./platform-form";
import { emptyGroup, filterKey, isValueless, type PlatformFilter, type PlatformFilterGroup } from "./platform-native";

/** The operators' names. @i18n-keys */
const OPERATOR_LABELS = {
  eq: "Equals",
  not_eq: "Not equals",
  gt: "Greater than",
  gte: "Greater than or equal",
  lt: "Lower than",
  lte: "Lower than or equal",
  contains: "Includes",
  not_contains: "Does not include",
  starts_with: "Starts with",
  not_starts_with: "Does not start with",
  ends_with: "Ends with",
  not_ends_with: "Does not end with",
  search: "Search",
  nil: "Is empty",
  not_nil: "Is not empty",
  empty: "Is empty",
  not_empty: "Is not empty",
  within: "Within",
  only_eq_to: "Only equals",
  not_only_eq_to: "Not only equals",
  has_changed: "Has changed",
  not_has_changed: "Has not changed",
} as const;

const NUMERIC = ["integer", "float", "double", "long", "int", "short", "number", "bigdecimal"];

export function FilterGroupEditor({
  form,
  group,
  onChange,
  keys,
  base,
  label,
  hidden,
  depth = 1,
}: {
  /** The platform's form description: its filter dialect (`filters`). */
  form: PlatformFormDescription;
  group: PlatformFilterGroup;
  onChange: (group: PlatformFilterGroup) => void;
  keys: FilterKeyOption[];
  base: Omit<PlatformSchemaQuestion, "kind">;
  label: string;
  /** Top-level filters another control edits (the types counted, a series' entity). */
  hidden?: (filter: PlatformFilter) => boolean;
  depth?: number;
}) {
  const { t_i18n } = useFormatter();
  const dialect = form.filters;
  const shown = group.filters.map((filter, index) => ({ filter, index })).filter(({ filter }) => !hidden?.(filter));
  const nested = dialect?.nested ? (group.filterGroups ?? []) : [];
  const setFilter = (index: number, filter: PlatformFilter) => onChange({ ...group, filters: group.filters.map((f, i) => (i === index ? filter : f)) });
  const add = () => {
    const first = keys[0];
    if (!first?.key || !dialect) return;
    onChange({ ...group, filters: [...group.filters, newFilter(dialect, first.key, defaultOperatorFor(form, first))] });
  };
  const items = shown.length + nested.length;
  return (
    <Labelled label={label}>
      <div className={cn("flex flex-col gap-2", depth > 1 && "rounded-sm border border-dashed border-elevation-subtle p-2")}>
        {items > 1 && dialect?.groupMode && (
          <SegmentedFilter
            ariaLabel={t_i18n("Combine the filters of {label}", { label })}
            value={group.mode}
            onChange={(mode) => onChange({ ...group, mode })}
            options={[
              { key: "and", label: t_i18n("All of them (AND)") },
              { key: "or", label: t_i18n("Any of them (OR)") },
            ]}
          />
        )}
        {shown.map(({ filter, index }) => (
          <FilterRow
            key={index}
            form={form}
            filter={filter}
            keys={keys}
            base={base}
            onChange={(next) => setFilter(index, next)}
            onRemove={() => onChange({ ...group, filters: group.filters.filter((_, i) => i !== index) })}
          />
        ))}
        {nested.map((child, index) => (
          <div key={`group-${index}`} className="flex items-start gap-1">
            <div className="min-w-0 flex-1">
              <FilterGroupEditor
                form={form}
                group={child}
                onChange={(next) => onChange({ ...group, filterGroups: nested.map((g, i) => (i === index ? next : g)) })}
                keys={keys}
                base={base}
                label={t_i18n("Filter group {n}", { n: index + 1 })}
                depth={depth + 1}
              />
            </div>
            <Button
              variant="ghost"
              size="icon"
              className="mt-6 h-8 w-8"
              aria-label={t_i18n("Remove the filter group {n}", { n: index + 1 })}
              onClick={() => onChange({ ...group, filterGroups: nested.filter((_, i) => i !== index) })}
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          </div>
        ))}
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" className="w-fit gap-1.5" onClick={add} disabled={keys.length === 0}>
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            {t_i18n("Add a filter")}
          </Button>
          {dialect?.nested && depth < dialect.maxDepth && (
            <Button
              variant="ghost"
              size="sm"
              className="w-fit gap-1.5"
              onClick={() => onChange({ ...group, filterGroups: [...nested, emptyGroup(true)] })}
            >
              <Plus className="h-3.5 w-3.5" aria-hidden="true" />
              {t_i18n("Add a filter group")}
            </Button>
          )}
        </div>
      </div>
    </Labelled>
  );
}

function FilterRow({
  form,
  filter,
  keys,
  base,
  onChange,
  onRemove,
}: {
  form: PlatformFormDescription;
  filter: PlatformFilter;
  keys: FilterKeyOption[];
  base: Omit<PlatformSchemaQuestion, "kind">;
  onChange: (filter: PlatformFilter) => void;
  onRemove: () => void;
}) {
  const { t_i18n } = useFormatter();
  const name = filterKey(filter);
  const definition = keys.find((key) => key.key === name);
  const offered = operatorsFor(form, definition);
  const operatorChoices = offered.includes(filter.operator) ? offered : [filter.operator, ...offered];
  const pairs = form.filters?.pairs?.[name];
  const changeKey = (key: string) => {
    const next = keys.find((k) => k.key === key);
    // Another key starts with no value; everything else the filter holds stays.
    onChange({ ...filter, key: form.filters?.keyForm === "list" ? [key] : key, values: [], operator: defaultOperatorFor(form, next) });
  };
  const values = filter.values;
  const plain = values.every((v) => typeof v === "string" || typeof v === "number" || typeof v === "boolean");
  return (
    <div className="flex flex-wrap items-start gap-2 rounded-sm border border-elevation-subtle p-2" data-testid="dashboard.sources.filter">
      <div className="w-48">
        <Pick label={t_i18n("Key")} value={name} onChange={changeKey} options={keys.map((key) => ({ value: key.key ?? "", label: key.label }))} sort />
      </div>
      <div className="w-40">
        <Pick
          label={t_i18n("Operator")}
          value={filter.operator}
          onChange={(operator) => onChange({ ...filter, operator, ...(isValueless(operator) ? { values: [] } : {}) })}
          options={operatorChoices.map((operator) => ({ value: operator, label: t_i18n(OPERATOR_LABELS[operator as keyof typeof OPERATOR_LABELS] ?? operator) }))}
        />
      </div>
      <div className="flex min-w-48 flex-1 flex-col gap-1.5">
        {isValueless(filter.operator) ? null : pairs ? (
          <PairValues base={base} filter={filter} parts={pairs} onChange={onChange} />
        ) : !plain ? (
          // Values this editor cannot show (a nested group of values): kept as they are.
          <Labelled label={t_i18n("Values")} hint={t_i18n("Edit these values in the source editor.")}>
            <code className="block max-h-24 overflow-auto rounded-sm bg-elevation-hover p-1.5 content-caption">{JSON.stringify(values)}</code>
          </Labelled>
        ) : (
          <Values form={form} filter={filter} definition={definition} base={base} onChange={(next) => onChange({ ...filter, values: next })} />
        )}
        {!isValueless(filter.operator) && values.length > 1 && (
          <SegmentedFilter
            ariaLabel={t_i18n("Combine the values of {key}", { key: definition?.label ?? name })}
            value={filter.mode}
            onChange={(mode) => onChange({ ...filter, mode })}
            options={[
              { key: "or", label: t_i18n("Any value (OR)") },
              { key: "and", label: t_i18n("Every value (AND)") },
            ]}
          />
        )}
      </div>
      <Button variant="ghost" size="icon" className="mt-6 h-8 w-8" aria-label={t_i18n("Remove the filter")} onClick={onRemove}>
        <Trash2 className="h-4 w-4" />
      </Button>
    </div>
  );
}

/** The values of a filter, by the type of its key. */
function Values({
  form,
  filter,
  definition,
  base,
  onChange,
}: {
  form: PlatformFormDescription;
  filter: PlatformFilter;
  definition: FilterKeyOption | undefined;
  base: Omit<PlatformSchemaQuestion, "kind">;
  onChange: (values: string[]) => void;
}) {
  const { t_i18n } = useFormatter();
  const values = filter.values.map(String);
  const type = (definition?.type ?? "").toLowerCase();
  const name = filterKey(filter);
  if (type === "date" || type === "instant") {
    if (filter.operator === "within") {
      // A period: two dates, absolute or relative to now (`now-7d`, `now`).
      return (
        <div className="grid gap-2 sm:grid-cols-2">
          {[t_i18n("From"), t_i18n("To")].map((label, index) => (
            <Labelled key={label} label={label}>
              <Input
                value={values[index] ?? ""}
                onChange={(event) => {
                  const next = [values[0] ?? "", values[1] ?? ""];
                  next[index] = event.target.value;
                  onChange(next);
                }}
                placeholder={index === 0 ? "now-7d" : "now"}
                className="h-8"
                aria-label={label}
              />
            </Labelled>
          ))}
        </div>
      );
    }
    const hint = form.filters?.dateHint;
    return (
      <Labelled label={t_i18n("Date")} hint={hint ? t_i18n(hint) : undefined}>
        {form.filters?.dateInput === "datetime" ? (
          <Input
            type="datetime-local"
            value={values[0] ? values[0].slice(0, 16) : ""}
            onChange={(event) => onChange(event.target.value ? [new Date(event.target.value).toISOString()] : [])}
            className="h-8"
            aria-label={t_i18n("Date")}
          />
        ) : (
          <Input value={values[0] ?? ""} onChange={(event) => onChange(event.target.value ? [event.target.value] : [])} className="h-8" aria-label={t_i18n("Date")} />
        )}
      </Labelled>
    );
  }
  if (type === "boolean") {
    return (
      <Pick label={t_i18n("Value")} value={values[0] ?? ""} onChange={(value) => onChange([value])} options={[{ value: "true", label: t_i18n("Yes") }, { value: "false", label: t_i18n("No") }]} />
    );
  }
  if (NUMERIC.includes(type)) return <FreeValues label={t_i18n("Values")} values={values} onChange={onChange} type="number" />;
  if (definition?.values?.length) {
    return <ValuesPicker base={base} filterKey={name} values={values} fixed={definition.values} onChange={onChange} />;
  }
  const searched = searchedFor(form, definition);
  // Text compared with contains, starts with...: typed. A key whose values live in the platform: searched there.
  if (!searched || ["contains", "not_contains", "starts_with", "not_starts_with", "ends_with", "not_ends_with", "search"].includes(filter.operator)) {
    return <FreeValues label={t_i18n("Values")} values={values} onChange={onChange} />;
  }
  return <ValuesPicker base={base} filterKey={name} values={values} onChange={onChange} />;
}

/** A key whose values are `{key, values}` pairs (an entity and the relationship types to it): each part searched on its own. */
function PairValues({
  base,
  filter,
  parts,
  onChange,
}: {
  base: Omit<PlatformSchemaQuestion, "kind">;
  filter: PlatformFilter;
  parts: { key: string; label: string; search: string }[];
  onChange: (filter: PlatformFilter) => void;
}) {
  const { t_i18n } = useFormatter();
  const pairs = filter.values.filter((v): v is { key: string; values: unknown[] } => typeof v === "object" && v !== null && "key" in v);
  const of = (key: string) => (pairs.find((pair) => pair.key === key)?.values ?? []).map(String);
  const set = (key: string, values: string[]) => {
    const others = filter.values.filter((v) => !(typeof v === "object" && v !== null && (v as { key?: string }).key === key));
    const index = filter.values.findIndex((v) => typeof v === "object" && v !== null && (v as { key?: string }).key === key);
    const pair = { ...(index >= 0 ? (filter.values[index] as object) : {}), key, values };
    onChange({ ...filter, values: values.length ? (index >= 0 ? filter.values.map((v, i) => (i === index ? pair : v)) : [...others, pair]) : others });
  };
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {parts.map((part) => (
        <Labelled key={part.key} label={t_i18n(part.label)}>
          <ValuesPicker base={base} filterKey={part.search} values={of(part.key)} onChange={(values) => set(part.key, values)} label={t_i18n(part.label)} />
        </Labelled>
      ))}
    </div>
  );
}
