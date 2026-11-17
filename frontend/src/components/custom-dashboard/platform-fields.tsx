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
 * The fields the platform source form is made of (`PlatformForm.tsx`): a
 * labelled select, a switch, a whole or decimal number, a day, the values of a
 * filter searched in the platform, and an ordered list of columns. Every list a
 * field shows comes from the platform through the person's own integration
 * (`usePlatformSchema`).
 */
import { useEffect, useId, useMemo, useState, type ReactNode } from "react";
import { ArrowDown, ArrowUp, RotateCcw, X } from "lucide-react";

import { DeckIcon } from "@/components/presentation/DeckIcon";
import { Button } from "@/components/ui/button";
import { DateFilterInput } from "@/components/ui/date-filter-input";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { MultiSelect } from "@/components/ui/multi-select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Switch } from "@/components/ui/switch";
import { usePlatformSchema, type PlatformSchemaQuestion } from "@/hooks/api/useCustomDashboardApi";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

/** A form field: the design system label above its control (linked by `htmlFor` when the control has an id; a
 *  group such as a radiogroup names itself by aria-label and gets the same label text, unlinked). */
export function Labelled({ label, children, hint, htmlFor }: { label: string; children: ReactNode; hint?: string; htmlFor?: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      {htmlFor ? <Label htmlFor={htmlFor}>{label}</Label> : <span className="content-base font-medium leading-none">{label}</span>}
      {children}
      {hint && <span className="content-caption text-default-secondary">{hint}</span>}
    </div>
  );
}

export interface PickOption {
  value: string;
  label: string;
  group?: string;
  /** Its tile's icon, a Lucide name (`TilePick`). */
  icon?: string;
}

/** One choice of a few, each a tile with its icon and its label, all in sight (as the modules' widget editors pick a type). */
export function TilePick({ label, value, onChange, options, hint }: { label: string; value: string; onChange: (value: string) => void; options: PickOption[]; hint?: string }) {
  return (
    <Labelled label={label} hint={hint}>
      <div role="radiogroup" aria-label={label} className="grid grid-cols-[repeat(auto-fill,minmax(7.5rem,1fr))] gap-2">
        {options.map((option) => {
          const picked = option.value === value;
          return (
            <button
              key={option.value}
              type="button"
              role="radio"
              aria-checked={picked}
              onClick={() => onChange(option.value)}
              className={cn(
                "flex min-h-16 flex-col items-center justify-center gap-1.5 rounded-sm border px-2 py-2 text-center content-caption transition-colors",
                picked ? "border-accent bg-accent/10 text-default-primary" : "border-elevation-subtle text-default-secondary hover:bg-elevation-hover",
              )}
            >
              {option.icon && <DeckIcon name={option.icon} size={20} className={picked ? "text-accent" : undefined} />}
              <span className="leading-tight">{option.label}</span>
            </button>
          );
        })}
      </div>
    </Labelled>
  );
}

export function Pick({
  label,
  value,
  onChange,
  options,
  hint,
  sort = false,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: PickOption[];
  hint?: string;
  sort?: boolean;
}) {
  // A stored value the list does not hold (yet) is still shown, never replaced.
  const shown = value && !options.some((option) => option.value === value) ? [{ value, label: value }, ...options] : options;
  const id = useId();
  return (
    <Labelled label={label} hint={hint} htmlFor={id}>
      {/* Lists read from the platform can be long: searchable past a few entries. */}
      <SearchableSelect
        id={id}
        aria-label={label}
        value={value}
        onValueChange={onChange}
        options={shown}
        sort={sort}
        searchable="auto"
        className="w-full"
      />
    </Labelled>
  );
}

export function SwitchField({ label, checked, onChange, hint }: { label: string; checked: boolean; onChange: (checked: boolean) => void; hint?: string }) {
  return (
    <div className="flex items-start justify-between gap-3 rounded-sm border border-elevation-subtle-soft px-3 py-2">
      <div className="flex min-w-0 flex-col">
        <span className="content-compact text-default-primary">{label}</span>
        {hint && <span className="content-caption text-default-secondary">{hint}</span>}
      </div>
      <Switch checked={checked} onCheckedChange={onChange} aria-label={label} />
    </div>
  );
}

export function NumberField({
  label,
  value,
  onChange,
  min,
  max,
  hint,
}: {
  label: string;
  value: number | null | undefined;
  /** `null` when the field is emptied. */
  onChange: (value: number | null) => void;
  min: number;
  max: number;
  hint?: string;
}) {
  const id = useId();
  return (
    <Labelled label={label} hint={hint} htmlFor={id}>
      <Input
        id={id}
        type="number"
        min={min}
        max={max}
        value={value ?? ""}
        onChange={(event) => {
          const raw = event.target.value;
          onChange(raw === "" ? null : Math.max(min, Math.min(max, Math.round(Number(raw)) || min)));
        }}
        aria-label={label}
      />
    </Labelled>
  );
}

/** A decimal number (a coordinate), empty until set: `undefined` when emptied or not a number. */
export function DecimalField({
  label,
  value,
  min,
  max,
  onChange,
}: {
  label: string;
  value: number | undefined;
  min: number;
  max: number;
  onChange: (value: number | undefined) => void;
}) {
  const id = useId();
  return (
    <Labelled label={label} htmlFor={id}>
      <Input
        id={id}
        type="number"
        step="any"
        min={min}
        max={max}
        value={value === undefined ? "" : String(value)}
        onChange={(event) => {
          const raw = event.target.value;
          const parsed = Number(raw);
          onChange(raw === "" || !Number.isFinite(parsed) ? undefined : Math.max(min, Math.min(max, parsed)));
        }}
        aria-label={label}
      />
    </Labelled>
  );
}

/** A day, stored as an ISO instant at that day's start. */
export function DayField({ label, value, onChange }: { label: string; value: unknown; onChange: (value: string | undefined) => void }) {
  const day = typeof value === "string" && /^\d{4}-\d{2}-\d{2}/.test(value) ? value.slice(0, 10) : "";
  const id = useId();
  return (
    // A value that is not a date (a dashboard parameter of the platform) is shown as it is, until a day is chosen.
    <Labelled label={label} hint={typeof value === "string" && value && !day ? value : undefined} htmlFor={id}>
      <DateFilterInput
        id={id}
        value={day}
        onChange={(next) => onChange(next ? new Date(`${next}T00:00:00`).toISOString() : undefined)}
        className="w-full"
        aria-label={label}
      />
    </Labelled>
  );
}

/** A short delay before a search is sent: one question per pause in typing. */
function useSettled(value: string, ms = 300): string {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return settled;
}

/** The values of one filter key: searched in the platform, the chosen ones named. */
export function ValuesPicker({
  base,
  filterKey,
  values,
  fixed,
  onChange,
  label,
}: {
  base: Omit<PlatformSchemaQuestion, "kind">;
  filterKey: string;
  values: string[];
  /** Values the schema lists itself (an enum): no search. */
  fixed?: string[];
  onChange: (values: string[]) => void;
  label?: string;
}) {
  const { t_i18n } = useFormatter();
  const [search, setSearch] = useState("");
  const settled = useSettled(search);
  const found = usePlatformSchema(fixed?.length || !filterKey ? null : { ...base, kind: "values", key: filterKey, search: settled });
  const named = usePlatformSchema(values.length && !fixed?.length && filterKey ? { ...base, kind: "labels", key: filterKey, ids: values.slice(0, 50) } : null);
  const options = useMemo(() => {
    const byValue = new Map<string, string>();
    for (const value of fixed ?? []) byValue.set(value, value);
    for (const item of [...(named.data?.items ?? []), ...(found.data?.items ?? [])]) {
      if (item.value) byValue.set(item.value, item.group ? `${item.label} (${item.group})` : item.label);
    }
    for (const value of values) if (!byValue.has(value)) byValue.set(value, value);
    return [...byValue].map(([value, text]) => ({ value, label: text }));
  }, [fixed, named.data, found.data, values]);
  const free = !fixed?.length && found.data?.status === "error" && found.data.code === "unsupported";
  if (free) {
    // A key the platform has no list for (free text, a number): the values are typed.
    return <FreeValues label={label ?? t_i18n("Values")} values={values} onChange={onChange} />;
  }
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-1.5">
      <Input
        value={search}
        onChange={(event) => setSearch(event.target.value)}
        placeholder={t_i18n("Search values")}
        aria-label={label ? `${label} · ${t_i18n("Search values")}` : t_i18n("Search values")}
        hidden={!!fixed?.length}
      />
      <MultiSelect
        options={options}
        selected={values}
        onToggle={(value) => onChange(values.includes(value) ? values.filter((v) => v !== value) : [...values, value])}
        placeholder={label ?? t_i18n("Values")}
        searchPlaceholder={t_i18n("Filter")}
        emptyOptionsLabel={found.isFetching ? t_i18n("Searching…") : t_i18n("No value found")}
        noMatchLabel={t_i18n("No value found")}
      />
    </div>
  );
}

/** Values typed by hand, as chips: Enter adds one. */
export function FreeValues({
  label,
  values,
  onChange,
  type = "text",
}: {
  label: string;
  values: string[];
  onChange: (values: string[]) => void;
  type?: "text" | "number" | "datetime-local";
}) {
  const { t_i18n } = useFormatter();
  const [draft, setDraft] = useState("");
  const add = () => {
    const value = draft.trim();
    if (value && !values.includes(value)) onChange([...values, value]);
    setDraft("");
  };
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-1.5">
      <Input
        type={type}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            add();
          }
        }}
        onBlur={add}
        placeholder={t_i18n("Type a value, then Enter")}
        aria-label={label}
      />
      {values.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {values.map((value) => (
            <span key={value} className="inline-flex items-center gap-1 rounded-sm border border-elevation-subtle px-1.5 py-px content-caption">
              {value}
              <button
                type="button"
                className="text-default-secondary hover:text-default-primary"
                aria-label={t_i18n("Remove {value}", { value })}
                onClick={() => onChange(values.filter((v) => v !== value))}
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

export interface ColumnChoice {
  value: string;
  label: string;
}

/** The ordered columns of a list: add one of the available, move, remove, reset to the defaults. */
export function ColumnsEditor({
  selected,
  available,
  onChange,
  onReset,
}: {
  /** In display order; a stored column the platform no longer lists stays. */
  selected: ColumnChoice[];
  available: ColumnChoice[];
  onChange: (values: string[]) => void;
  onReset?: () => void;
}) {
  const { t_i18n } = useFormatter();
  const values = selected.map((column) => column.value);
  const move = (index: number, by: number) => {
    const next = [...values];
    const [item] = next.splice(index, 1);
    next.splice(index + by, 0, item);
    onChange(next);
  };
  const rest = available.filter((column) => !values.includes(column.value));
  return (
    <Labelled label={t_i18n("Columns")}>
      <div className="flex flex-col gap-1 rounded-sm border border-elevation-subtle p-2" data-testid="dashboard.sources.columns">
        {selected.length === 0 && <span className="content-caption text-default-secondary">{t_i18n("No column")}</span>}
        {selected.map((column, index) => (
          <div key={column.value} className="flex items-center gap-1">
            <span className="min-w-0 flex-1 truncate content-compact">{column.label}</span>
            <Button variant="ghost" size="icon" className="h-6 w-6" aria-label={t_i18n("Move {name} up", { name: column.label })} disabled={index === 0} onClick={() => move(index, -1)}>
              <ArrowUp className="h-3.5 w-3.5" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6"
              aria-label={t_i18n("Move {name} down", { name: column.label })}
              disabled={index === selected.length - 1}
              onClick={() => move(index, 1)}
            >
              <ArrowDown className="h-3.5 w-3.5" />
            </Button>
            <Button variant="ghost" size="icon" className="h-6 w-6" aria-label={t_i18n("Remove {name}", { name: column.label })} onClick={() => onChange(values.filter((v) => v !== column.value))}>
              <X className="h-3.5 w-3.5" />
            </Button>
          </div>
        ))}
        <div className="flex items-center gap-2 pt-1">
          <div className="min-w-0 flex-1">
            <SearchableSelect
              aria-label={t_i18n("Add a column")}
              value={null}
              onValueChange={(value) => onChange([...values, value])}
              options={rest}
              placeholder={t_i18n("Add a column")}
              searchable="auto"
              className="w-full"
              disabled={rest.length === 0}
            />
          </div>
          {onReset && (
            <Button variant="ghost" size="sm" className="gap-1.5" onClick={onReset}>
              <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
              {t_i18n("Reset")}
            </Button>
          )}
        </div>
      </div>
    </Labelled>
  );
}
