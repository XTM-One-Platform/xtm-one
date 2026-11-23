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
 * The fields of the "CTEM signal" trigger tile (#1409): which signal events
 * start a run, and optional any-of filters on the signal's routing. Saved as a
 * `platform_event` trigger (`lib/ctem-signal-trigger.ts`). Every filter is
 * optional: an empty one takes every signal the run's identity can read.
 */
import { Chip } from "@filigran/design-system";
import { useId, useMemo } from "react";
import { Label } from "@/components/ui/label";
import { MultiSelect } from "@/components/ui/multi-select";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useCtemSignalPaths } from "@/hooks/api/useCtemSignalsApi";
import { useFormatter } from "@/i18n";
import {
  CTEM_SIGNAL_LENSES,
  CTEM_SIGNAL_SUBJECT_KINDS,
  CTEM_SIGNAL_VERDICTS,
  type CtemSignalDraft,
  type CtemSignalEventChoice,
  type CtemSignalFilterKey,
  CTEM_SIGNAL_TILE,
  ctemSignalDraft,
} from "@/lib/ctem-signal-trigger";
import { TRIGGER_ICONS } from "@/lib/constants";
import type { TriggerConfig } from "@/types";
import { signalProviderName } from "@/pages/xtm-one/ctem/loop/SignalLoopTrace";
import { SIGNAL_STATE_LABEL } from "@/pages/xtm-one/ctem/loop/DecisionReason";

/** The English label of each choice, an i18n key. @i18n-keys */
const EVENT_CHOICES = {
  both: {
    label: "Recorded or triaged",
    help: "A run starts when a signal is first seen, and again each time the scope gate changes its verdict.",
  },
  recorded: {
    label: "Recorded",
    help: "A run starts when a signal is first seen, with the scope gate's first verdict. Seeing it again starts nothing.",
  },
  triaged: {
    label: "Triaged",
    help: "A run starts when the scope gate changes the verdict of a signal already seen, for example from to validate to confirmed.",
  },
} as const satisfies Record<CtemSignalEventChoice, { label: string; help: string }>;

/** @i18n-keys */
const LENS_LABEL = { inside: "Inside", outside: "Outside", world: "World" } as const;
/** @i18n-keys */
const SUBJECT_KIND_LABEL = {
  asset: "Asset",
  software: "Software",
  identity: "Identity",
  third_party: "Third party",
  vulnerability: "Vulnerability",
  threat: "Threat",
  code: "Code",
} as const;
/** Sources a signal is recorded through without a provider integration. */
const BUILT_IN_SOURCES = ["manual", "api"] as const;
/** @i18n-keys */
const BUILT_IN_SOURCE_LABEL = { manual: "Recorded by hand", api: "API" } as const;

/** The readable name of a signal source: a built-in one translated, a provider by its product name. */
function sourceLabel(provider: string, t_i18n: (s: string) => string): string {
  return provider in BUILT_IN_SOURCE_LABEL
    ? t_i18n(BUILT_IN_SOURCE_LABEL[provider as keyof typeof BUILT_IN_SOURCE_LABEL])
    : signalProviderName(provider);
}

interface FilterField {
  key: CtemSignalFilterKey;
  label: string;
  options: { value: string; label: string }[];
  sort?: boolean;
}

/** The filters of the tile, in the order the form and the summary show them. */
function filterFields(
  t_i18n: (s: string) => string,
  pathOptions: { value: string; label: string }[],
  sourceOptions: { value: string; label: string }[],
): FilterField[] {
  return [
    { key: "trigger_path", label: t_i18n("Trigger path"), options: pathOptions },
    { key: "verdict", label: t_i18n("Verdict"), options: CTEM_SIGNAL_VERDICTS.map((v) => ({ value: v, label: t_i18n(SIGNAL_STATE_LABEL[v]) })), sort: false },
    { key: "lens", label: t_i18n("Lens"), options: CTEM_SIGNAL_LENSES.map((v) => ({ value: v, label: t_i18n(LENS_LABEL[v]) })), sort: false },
    { key: "subject_kind", label: t_i18n("Subject kind"), options: CTEM_SIGNAL_SUBJECT_KINDS.map((v) => ({ value: v, label: t_i18n(SUBJECT_KIND_LABEL[v]) })) },
    { key: "source_provider", label: t_i18n("Source"), options: sourceOptions },
  ];
}

export function CtemSignalTriggerFields({ draft, onChange }: { draft: CtemSignalDraft; onChange: (next: CtemSignalDraft) => void }) {
  const { t_i18n } = useFormatter();
  const { data: pathData, isLoading } = useCtemSignalPaths();
  const ids = useId();
  const eventId = `${ids}-event`;

  const pathOptions = useMemo(
    () => (pathData?.paths ?? []).map((p) => ({ value: p.id, label: t_i18n(p.label) })),
    [pathData, t_i18n],
  );
  const sourceOptions = useMemo(() => {
    const providers = new Set<string>(BUILT_IN_SOURCES);
    for (const path of pathData?.paths ?? []) for (const s of path.sources ?? []) providers.add(s.provider);
    for (const value of draft.filters.source_provider) providers.add(value);
    return [...providers].map((p) => ({ value: p, label: sourceLabel(p, t_i18n) }));
  }, [pathData, draft.filters.source_provider, t_i18n]);

  const toggle = (key: CtemSignalFilterKey) => (value: string) => {
    const current = draft.filters[key];
    const next = current.includes(value) ? current.filter((v) => v !== value) : [...current, value];
    onChange({ ...draft, filters: { ...draft.filters, [key]: next } });
  };

  const filters = filterFields(t_i18n, pathOptions, sourceOptions);

  return (
    <div className="space-y-4 rounded-sm bg-elevation-hover p-3">
      <div className="space-y-1.5">
        <Label htmlFor={eventId} className="block text-default-secondary">{t_i18n("Starts a run when a signal is")}</Label>
        <Select value={draft.on} onValueChange={(v) => onChange({ ...draft, on: v as CtemSignalEventChoice })}>
          <SelectTrigger id={eventId} className="w-64" aria-label={t_i18n("Starts a run when a signal is")}><SelectValue /></SelectTrigger>
          <SelectContent>
            {(Object.keys(EVENT_CHOICES) as CtemSignalEventChoice[]).map((choice) => (
              <SelectItem key={choice} value={choice}>{t_i18n(EVENT_CHOICES[choice].label)}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <p className="content-caption text-default-secondary">{t_i18n(EVENT_CHOICES[draft.on].help)}</p>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {filters.map((f) => {
          const fieldId = `${ids}-${f.key}`;
          return (
            <div key={f.key} className="space-y-1.5">
              <Label htmlFor={fieldId} className="block text-default-secondary">
                {f.label} <span className="text-default-secondary">{t_i18n("(optional)")}</span>
              </Label>
              <MultiSelect
                id={fieldId}
                options={f.options}
                selected={draft.filters[f.key]}
                onToggle={toggle(f.key)}
                placeholder={isLoading && f.key === "trigger_path" ? t_i18n("Loading...") : t_i18n("Every signal")}
                aria-label={f.label}
                searchPlaceholder={t_i18n("Search...")}
                emptyOptionsLabel={t_i18n("No value found")}
                noMatchLabel={t_i18n("No match")}
                sort={f.sort ?? true}
              />
            </div>
          );
        })}
      </div>
      <p className="content-caption text-default-secondary">
        {t_i18n("A run starts for each matching signal, within the assignment's runs in parallel. The agent reads the signal with list_ctem_signals and its own permissions: a signal recorded by an integration listener reaches an assignment only when it runs as an administrator.")}
      </p>
    </div>
  );
}

/**
 * The read-only summary of a CTEM signal trigger (UX round 1 of #1419, B2): the
 * events it starts on, then one labelled group per filled filter with the same
 * translated labels as the form; "Every signal" when no filter is set. Never a
 * raw key, a JSON list or a scope the tile does not ask for. Each filter value
 * is the design system Chip (UX round 2 of #1419), never a hand-rolled span.
 */
export function CtemSignalSummary({ config }: { config: TriggerConfig }) {
  const { t_i18n } = useFormatter();
  const { data: pathData } = useCtemSignalPaths();
  const draft = ctemSignalDraft("platform_event", config);
  const pathLabels = new Map((pathData?.paths ?? []).map((p) => [p.id, t_i18n(p.label)]));
  const pathOptions = draft.filters.trigger_path.map((v) => ({ value: v, label: pathLabels.get(v) ?? v }));
  const sourceOptions = draft.filters.source_provider.map((v) => ({ value: v, label: sourceLabel(v, t_i18n) }));
  const filled = filterFields(t_i18n, pathOptions, sourceOptions).filter((f) => draft.filters[f.key].length > 0);
  const Icon = TRIGGER_ICONS[CTEM_SIGNAL_TILE];
  return (
    <div className="space-y-3 rounded-sm bg-elevation-hover border border-elevation-subtle-soft p-4">
      <div className="flex items-center gap-2">
        <Icon className="h-4 w-4 text-accent" aria-hidden />
        <p className="content-compact font-medium text-default-secondary uppercase tracking-wider">{t_i18n("CTEM signal")}</p>
      </div>
      <div className="space-y-1">
        <p className="content-caption text-default-secondary">{t_i18n("Starts a run when a signal is")}</p>
        <p className="content-base">{t_i18n(EVENT_CHOICES[draft.on].label)}</p>
      </div>
      {filled.length === 0 ? (
        <p className="content-caption">{t_i18n("Every signal")}</p>
      ) : (
        <dl className="grid gap-3 sm:grid-cols-2">
          {filled.map((f) => {
            const labels = new Map(f.options.map((o) => [o.value, o.label]));
            return (
              <div key={f.key} className="space-y-1">
                <dt className="content-caption text-default-secondary">{f.label}</dt>
                <dd className="flex flex-wrap gap-1.5">
                  {draft.filters[f.key].map((v) => (
                    <Chip key={v} label={labels.get(v) ?? v} className="shrink-0" data-testid="ctem-signal-summary-value" />
                  ))}
                </dd>
              </div>
            );
          })}
        </dl>
      )}
    </div>
  );
}
