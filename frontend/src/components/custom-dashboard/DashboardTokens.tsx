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

import { Icon } from "@filigran/design-system";
/**
 * The inputs of a dashboard page.
 *
 * `InputControl` draws one input: time range, dropdown (its choices written
 * or read from a data source, with the page's tokens: cascading), multiselect,
 * text, number, an entity of a platform (searched there with the viewer's
 * access: an OpenCTI custom view's "current entity"), or a button running its actions (set or unset a token, open
 * a link - a page of the web app or an http(s) address, tokens URL-encoded -,
 * switch tab). `InputBar` draws the inputs above the tabs, and the Submit
 * button when the definition has one. The tokens are `token-state.tsx`'s.
 */
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Button } from "@/components/ui/button";
import { DateFilterInput } from "@/components/ui/date-filter-input";
import { Input } from "@/components/ui/input";
import { MultiSelect } from "@/components/ui/multi-select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { usePlatformSchema, useSourceData } from "@/hooks/api/useCustomDashboardApi";
import { useDebounce } from "@/hooks/useDebounce";
import { useFormatter } from "@/i18n";

import { chainOf } from "./definition";
import { choicesOf, type DashboardInput } from "./inputs";
import { useDashboardLive } from "./live";
import { useDashboardTokens } from "./token-state";
import { resultTokens, tokensFor, type Tokens } from "./tokens";

const NO_TOKENS: Tokens = {};

/** The time ranges a time range input offers (`dashboard_live.RANGES`). @i18n-keys */
/** Each period key's label (`dashboard_live.RANGES`): days, or the platforms' calendar months. @i18n-keys */
const RANGE_LABELS = {
  "1d": "Last 24 hours",
  "7d": "Last 7 days",
  "30d": "Last 30 days",
  "1m": "Last month",
  "90d": "Last 90 days",
  "3m": "Last 3 months",
  "6m": "Last 6 months",
  "1y": "Last year",
  all: "All time",
} as const;
/** The choice revealing a start and an end date. */
const CUSTOM = "custom";
/** A fixed period as the server reads it (`dashboard_live.PERIOD_RE`): either end may be left out. */
const PERIOD = /^(\d{4}-\d{2}-\d{2})?\/(\d{4}-\d{2}-\d{2})?$/;

/** A time range input: a period before now, all time, or a start and an end date; for those who may edit the
 *  dashboard, the period picked can be kept as the dashboard's (OpenCTI's period, saved with the dashboard). */
function TimeRangePicker({ input, label, value, onChange }: { input: DashboardInput; label: string; value: string; onChange: (value: string) => void }) {
  const { t_i18n } = useFormatter();
  const live = useDashboardLive();
  const fixed = PERIOD.exec(value);
  const [custom, setCustom] = useState(Boolean(fixed));
  const [dates, setDates] = useState<[string, string]>(fixed ? [fixed[1] ?? "", fixed[2] ?? ""] : ["", ""]);
  const keys = (input.items.length ? input.items.map((item) => item.value) : Object.keys(RANGE_LABELS)).filter(
    (key): key is keyof typeof RANGE_LABELS => key in RANGE_LABELS,
  );
  const pickDates = (start: string, end: string) => {
    setDates([start, end]);
    // Sent once it is a period: one end at least, never ending before it starts.
    if ((start || end) && !(start && end && end < start)) onChange(`${start}/${end}`);
  };
  const kept = typeof input.defaultValue === "string" ? input.defaultValue : "";
  return (
    <div className="flex flex-col gap-1.5">
      <SearchableSelect
        aria-label={label}
        value={custom ? CUSTOM : value}
        onValueChange={(next) => {
          if (next === CUSTOM) {
            setCustom(true);
            return;
          }
          setCustom(false);
          onChange(next);
        }}
        options={[...keys.map((key) => ({ value: key, label: t_i18n(RANGE_LABELS[key]) })), { value: CUSTOM, label: t_i18n("Custom range") }]}
        sort={false}
        searchable={false}
        clearable
      />
      {custom && (
        <div className="flex items-start gap-1.5">
          {/* The design system's DatePicker: the day in the app's locale. Each end bounds the other's calendar, and an end
              typed before the start shows the error and is never sent (pickDates). */}
          <DateFilterInput
            aria-label={t_i18n("Start date")}
            value={dates[0]}
            max={dates[1] || undefined}
            onChange={(day) => pickDates(day, dates[1])}
            className="min-w-0 flex-1"
          />
          <DateFilterInput
            aria-label={t_i18n("End date")}
            value={dates[1]}
            min={dates[0] || undefined}
            error={dates[0] && dates[1] && dates[1] < dates[0] ? t_i18n("The end date is before the start date.") : undefined}
            onChange={(day) => pickDates(dates[0], day)}
            className="min-w-0 flex-1"
          />
        </div>
      )}
      {live?.keepInput && value && value !== kept && (
        <Button variant="ghost" size="sm" className="h-7 w-fit gap-1 px-2 content-caption" onClick={() => live.keepInput?.(input, value)}>
          <Icon name="pin" size={16} />
          {t_i18n("Keep as the dashboard's period")}
        </Button>
      )}
    </div>
  );
}

/** One input, drawn with the page's tokens. */
export function InputControl({ input }: { input: DashboardInput }) {
  const { t_i18n } = useFormatter();
  const context = useDashboardTokens();
  const live = useDashboardLive();
  const tokens = context?.tokens ?? NO_TOKENS;
  const sources = useMemo(() => live?.sources ?? {}, [live?.sources]);
  const chain = useMemo(() => (input.sourceId ? chainOf(input.sourceId, sources) : null), [input.sourceId, sources]);
  const used = useMemo(() => (chain ? tokensFor(chain, tokens) : NO_TOKENS), [chain, tokens]);
  const listed = useSourceData(live?.dashboardId, input.sourceId, used, JSON.stringify(chain));
  const choices = useMemo(() => choicesOf(input, listed.data), [input, listed.data]);
  const [draft, setDraft] = useState<string | null>(null);
  if (!context) return null;
  const value = context.valueOf(input);
  const label = input.title ?? input.label ?? input.token ?? input.id;
  const single = typeof value === "string" ? value : Array.isArray(value) ? value[0] ?? "" : "";

  let control: React.ReactNode;
  switch (input.kind) {
    case "timerange":
      control = <TimeRangePicker input={input} label={label} value={single} onChange={(next) => context.setInput(input, next || undefined)} />;
      break;
    case "dropdown":
      control = (
        <SearchableSelect
          aria-label={label}
          value={single}
          onValueChange={(next) => context.setInput(input, next)}
          options={choices}
          sort={false}
          searchable="auto"
          clearable
        />
      );
      break;
    case "entity":
      control = <EntityPicker input={input} label={label} value={single} onChange={(next) => context.setInput(input, next)} />;
      break;
    case "multiselect": {
      const selected = Array.isArray(value) ? value : value ? [value] : [];
      control = (
        <MultiSelect
          options={choices}
          selected={selected}
          onToggle={(choice) =>
            context.setInput(input, selected.includes(choice) ? selected.filter((v) => v !== choice) : [...selected, choice])
          }
          placeholder={label}
          searchPlaceholder={t_i18n("Filter")}
          emptyOptionsLabel={t_i18n("No value found")}
          noMatchLabel={t_i18n("No value found")}
          sort={false}
        />
      );
      break;
    }
    case "text":
    case "number":
      control = (
        <Input
          aria-label={label}
          type={input.kind === "number" ? "number" : "text"}
          min={input.min}
          max={input.max}
          step={input.step}
          placeholder={input.placeholder}
          value={draft ?? single}
          maxLength={500}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={() => {
            if (draft !== null) context.setInput(input, draft);
            setDraft(null);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && draft !== null) {
              context.setInput(input, draft);
              setDraft(null);
            }
          }}
          className="h-8"
        />
      );
      break;
    case "button":
      control = <ActionButton input={input} label={label} />;
      break;
  }
  return (
    <div className="flex min-w-40 flex-col gap-1" data-input-id={input.id}>
      {input.kind !== "button" && <span className="content-caption text-default-secondary">{label}</span>}
      {control}
    </div>
  );
}

/** A button running its input's actions, and following the link they open. */
/** An entity input: the entities of its types matching what is typed, searched in its platform; the chosen one named. */
function EntityPicker({ input, label, value, onChange }: { input: DashboardInput; label: string; value: string; onChange: (value: string) => void }) {
  const { t_i18n } = useFormatter();
  const [search, setSearch] = useState("");
  const settled = useDebounce(search, 300);
  const base =
    input.entitySource && input.integrationId ? { source: input.entitySource, integration_id: input.integrationId, types: input.entityTypes ?? [] } : null;
  const found = usePlatformSchema(base ? { ...base, kind: "entities", search: settled } : null);
  const named = usePlatformSchema(base && value ? { ...base, kind: "entities", ids: [value] } : null);
  const option = (item: { value?: string; label: string; group?: string }) => ({ value: item.value ?? "", label: item.label, description: item.group });
  const options = (found.data?.items ?? []).filter((item) => item.value).map(option);
  const pinned = (named.data?.items ?? []).filter((item) => item.value === value).map(option);
  const failed = found.data?.status === "error";
  return (
    <SearchableSelect
      aria-label={label}
      value={value}
      onValueChange={onChange}
      options={options}
      pinned={pinned.length ? pinned : value ? [{ value, label: value }] : []}
      onSearchChange={setSearch}
      placeholder={input.placeholder ?? label}
      emptyMessage={failed ? t_i18n("The entities could not be read") : found.isFetching ? t_i18n("Searching…") : t_i18n("No entity found")}
      sort={false}
      clearable
    />
  );
}

function ActionButton({ input, label }: { input: DashboardInput; label: string }) {
  const context = useDashboardTokens();
  const navigate = useNavigate();
  return (
    <Button
      variant="outline"
      size="sm"
      onClick={() => {
        const link = context?.runActions(input.actions);
        if (!link) return;
        if (link.newTab) window.open(link.url, "_blank", "noopener,noreferrer");
        else navigate(link.url);
      }}
    >
      {input.label ?? label}
    </Button>
  );
}

/** The inputs above the tabs, and the Submit button when the definition has one. */
export function InputBar({ ids }: { ids: string[] }) {
  const { t_i18n } = useFormatter();
  const context = useDashboardTokens();
  if (!context || ids.length === 0) return null;
  return (
    <div className="mb-4 flex flex-wrap items-end gap-3" role="group" aria-label={t_i18n("Dashboard inputs")}>
      {ids.map((id) => {
        const input = context.inputs.get(id);
        return input ? <InputControl key={id} input={input} /> : null;
      })}
      {context.submitButton && (
        <Button size="sm" onClick={context.submit} disabled={!context.dirty}>
          {t_i18n("Submit")}
        </Button>
      )}
    </div>
  );
}

/** One result token's source, read with the page's tokens; its result reported to the page. */
function SourceResult({
  dashboardId,
  sourceId,
  chain,
  tokens,
  onResult,
}: {
  dashboardId: string;
  sourceId: string;
  chain: unknown;
  tokens: Tokens;
  onResult: (sourceId: string, tokens: Tokens) => void;
}) {
  const used = useMemo(() => tokensFor(chain, tokens), [chain, tokens]);
  const query = useSourceData(dashboardId, sourceId, used, JSON.stringify(chain));
  const result = useMemo(() => resultTokens(sourceId, query.data, query.isFetching), [sourceId, query.data, query.isFetching]);
  const key = JSON.stringify(result);
  useEffect(() => {
    onResult(sourceId, result);
    // `key` stands for `result`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceId, key, onResult]);
  return null;
}

/** The sources the page's result tokens name, read with its tokens even when no widget draws them. */
export function SourceResults({
  dashboardId,
  reads,
  tokens,
  onResult,
}: {
  dashboardId: string | undefined;
  reads: { sourceId: string; chain: unknown }[];
  tokens: Tokens;
  onResult: (sourceId: string, tokens: Tokens) => void;
}) {
  if (!dashboardId) return null;
  return (
    <>
      {reads.map((read) => (
        <SourceResult key={read.sourceId} dashboardId={dashboardId} sourceId={read.sourceId} chain={read.chain} tokens={tokens} onResult={onResult} />
      ))}
    </>
  );
}
