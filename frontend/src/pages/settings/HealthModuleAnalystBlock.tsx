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
 * Settings > Agents and automations > Platform Health > Spend limits, the "Module Analyst" row
 * (#101 S14d-2, dev-docs/platform-health.md 12.5 and 12.7): its switch, its own ceilings outside
 * the agentic quota (tokens per cycle, tokens and dollars per UTC day and per month) and today's
 * and this month's spend on its own ledger, next to the analysis row. XTM One modes only: Copilot
 * mode has no module. Same draft, same Edit / Save as the section's other blocks.
 */
import { useId } from "react";
import { Icon } from "@filigran/design-system";

import { Input } from "@/components/ui/input";
import { SettingsField, SettingsFieldGrid, SettingsSwitchField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { SettingsReadField } from "@/components/ui/settings-read-view";
import { useHealthAnalysis } from "@/hooks/api/useHealthApi";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useIsXtmOne } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import type { HealthAnalysisSpend, HealthModuleAnalystSettings, HealthSettings } from "@/types";

import { BlockTitle, SpendLockNotice, useSpendLock, type BlockProps } from "./health-block";

/** The design-system glyph the Module Analyst wears on the Health page (`ModuleAnalystParts`). */
const AnalystGlyph = ({ className }: { className?: string }) => <Icon name="file-search" size={16} className={className} aria-hidden />;

type AnalystCeilingKey = "cycle_tokens" | "day_tokens" | "day_cost_usd" | "month_tokens" | "month_cost_usd";

/** The English label of each Module Analyst ceiling. @i18n-keys */
const ANALYST_CEILING_LABELS = {
  cycle_tokens: "Tokens per cycle",
  day_tokens: "Tokens per day",
  day_cost_usd: "Cost per day (USD)",
  month_tokens: "Tokens per month",
  month_cost_usd: "Cost per month (USD)",
} as const;

/** What each Module Analyst ceiling counts and what reaching it does. @i18n-keys */
const ANALYST_CEILING_INFO = {
  cycle_tokens: "The most tokens one Module Analyst cycle may spend on one module, e.g. 80000. A cycle that reaches it stops with what it has written.",
  day_tokens: "The most tokens the Module Analyst may spend in a UTC day, every module included, e.g. 500000. A model call that would cross it is refused.",
  day_cost_usd: "The most the Module Analyst may cost in a UTC day, in US dollars, e.g. 4. A model call that would cross it is refused.",
  month_tokens: "The most tokens the Module Analyst may spend in a calendar month, e.g. 10000000. A model call that would cross it is refused.",
  month_cost_usd: "The most the Module Analyst may cost in a calendar month, in US dollars, e.g. 60. A model call that would cross it is refused.",
} as const;

const ANALYST_CEILING_KEYS = Object.keys(ANALYST_CEILING_LABELS) as AnalystCeilingKey[];
const TOKEN_KEYS: ReadonlySet<AnalystCeilingKey> = new Set(["cycle_tokens", "day_tokens", "month_tokens"]);

function analystCeilingValid(key: AnalystCeilingKey, value: number | string | null): boolean {
  if (value === null) return true;
  const n = Number(value);
  return Number.isFinite(n) && n >= 0 && (!TOKEN_KEYS.has(key) || Number.isInteger(n));
}

/** Whether every Module Analyst ceiling of the draft is a number of 0 or more (Save stays off otherwise). */
export function moduleAnalystValid(draft: HealthSettings): boolean {
  const analyst = draft.module_analyst as HealthModuleAnalystSettings | undefined;
  return !analyst || ANALYST_CEILING_KEYS.every((key) => analystCeilingValid(key, analyst[key]));
}

export function ModuleAnalystBlock({ draft, editing, onChange }: BlockProps) {
  const { t_i18n } = useFormatter();
  const { count: n, money: usd } = useNumberFormat("USD");
  const fieldId = useId();
  const isXtmOne = useIsXtmOne();
  // #1202: the ceilings are locked at their defaults; the switch stays editable (off only lowers spend).
  const { locked, envVar } = useSpendLock();
  const editable = editing && !locked;
  const analyst = draft.module_analyst as HealthModuleAnalystSettings | undefined;
  if (!isXtmOne || !analyst) return null;
  const setAnalyst = (next: Partial<HealthModuleAnalystSettings>) => onChange({ ...draft, module_analyst: { ...analyst, ...next } });
  const setCeiling = (key: AnalystCeilingKey, raw: string) => {
    const value = raw.trim() === "" ? null : TOKEN_KEYS.has(key) ? Number(raw) : raw.trim();
    setAnalyst({ [key]: value });
  };
  const info = t_i18n("On: the Module Analyst investigates each registered module read-only when its findings change and once a day, within the ceilings below. Off: no module is investigated.");
  return (
    <div data-testid="health.module-analyst">
      <BlockTitle
        icon={AnalystGlyph}
        title={t_i18n("Module Analyst")}
        hint={t_i18n("Its own ceilings, outside the agentic quota. Its spend is traced as Platform Health and never counted in agentic quotas.")}
      />
      <div className="space-y-4 rounded-sm bg-elevation-default p-3.5">
        {editing ? (
          <SettingsSwitchField
            id={`${fieldId}-enabled`}
            label={t_i18n("Investigate the modules")}
            helper={t_i18n("Reads each module through its integration, never writes to it.")}
            info={info}
            checked={analyst.enabled}
            onCheckedChange={(enabled) => setAnalyst({ enabled })}
          />
        ) : (
          <SettingsReadField label={t_i18n("Investigate the modules")} info={info} value={analyst.enabled ? t_i18n("On") : t_i18n("Off")} />
        )}
        {locked && <SpendLockNotice envVar={envVar} testId="health.module-analyst-locked" />}
        {/* One period per row, as the analysis grid above and the Today / This month spend below:
            tokens per cycle alone (an empty cell beside it), then the day pair, then the month pair. */}
        <SettingsFieldGrid data-testid="health.module-analyst-ceilings">
          {ANALYST_CEILING_KEYS.flatMap((key) => {
            const id = `${fieldId}-${key}`;
            const valid = analystCeilingValid(key, analyst[key]);
            const field = (
              <SettingsField
                key={key}
                id={id}
                label={t_i18n(ANALYST_CEILING_LABELS[key])}
                info={t_i18n(ANALYST_CEILING_INFO[key])}
                helper={t_i18n("Empty: every Module Analyst model call is refused.")}
                error={
                  editable && !valid
                    ? TOKEN_KEYS.has(key)
                      ? t_i18n("Enter a whole number of 0 or more.")
                      : t_i18n("Enter a number of 0 or more.")
                    : undefined
                }
              >
                {editable ? (
                  <Input
                    id={id}
                    aria-labelledby={settingsFieldLabelId(id)}
                    aria-describedby={settingsFieldHintId(id)}
                    aria-invalid={!valid || undefined}
                    type="number"
                    min={0}
                    step={TOKEN_KEYS.has(key) ? 1 : 0.01}
                    className="w-40"
                    value={analyst[key] ?? ""}
                    onChange={(e) => setCeiling(key, e.target.value)}
                  />
                ) : (
                  <p className="content-base font-medium tabular-nums">
                    {analyst[key] === null ? t_i18n("Not set") : TOKEN_KEYS.has(key) ? n(Number(analyst[key])) : usd(Number(analyst[key]))}
                  </p>
                )}
              </SettingsField>
            );
            return key === "cycle_tokens"
              ? [field, <div key="cycle-spacer" className="hidden md:block" aria-hidden data-testid="health.module-analyst-cycle-spacer" />]
              : [field];
          })}
        </SettingsFieldGrid>
        <ModuleAnalystSpendLines />
      </div>
    </div>
  );
}

/** Today's and this month's Module Analyst spend against its own ceilings: stored ledger rows, no model call. */
function ModuleAnalystSpendLines() {
  const { t_i18n } = useFormatter();
  const { count: n, money: usd } = useNumberFormat("USD");
  const { data } = useHealthAnalysis();
  const row = data?.module_analyst;
  if (!row) return null;
  const line = (label: string, spend: HealthAnalysisSpend, tokens: number | null, cost: string | null) => (
    <div className="min-w-0 space-y-0.5">
      <p className="content-compact text-default-secondary">{label}</p>
      <p className="content-base font-medium tabular-nums text-default-primary">
        {tokens === null
          ? t_i18n("Tokens: {tokens}, no ceiling", { tokens: n(spend.tokens) })
          : t_i18n("Tokens: {tokens} of {ceiling}", { tokens: n(spend.tokens), ceiling: n(tokens) })}
      </p>
      <p className="content-compact tabular-nums text-default-secondary">
        {cost === null
          ? t_i18n("Cost: {cost}, no ceiling", { cost: usd(Number(spend.cost_usd)) })
          : t_i18n("Cost: {cost} of {ceiling}", { cost: usd(Number(spend.cost_usd)), ceiling: usd(Number(cost)) })}
      </p>
    </div>
  );
  return (
    <div className="grid grid-cols-1 gap-4 border-t border-elevation-subtle-soft pt-3 sm:grid-cols-2" data-testid="health.module-analyst-spend">
      {line(t_i18n("Today"), row.day, row.ceilings.day_tokens, row.ceilings.day_cost_usd)}
      {line(t_i18n("This month"), row.month, row.ceilings.month_tokens, row.ceilings.month_cost_usd)}
    </div>
  );
}
