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
 * Settings > Agents and automations > Concierge (#8254): turn the Platform Concierge on, choose what it
 * supervises and cap what it spends. Administrators only, built in since
 * #101 (S1), no flag. One draft of the whole settings object, saved at once;
 * each block is its own component so later levels add theirs (schedule,
 * signal thresholds, dry-run) as new blocks of the same draft.
 */
import { useId, useState } from "react";
import { Link } from "react-router-dom";
import { Check, Compass, Gauge, Pencil, Power, Radar } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { MultiSelect } from "@/components/ui/multi-select";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  SettingsField,
  SettingsFieldGrid,
  SettingsSwitchField,
  settingsFieldHintId,
  settingsFieldLabelId,
} from "@/components/ui/settings-form";
import { SettingsReadField, SettingsReadGrid } from "@/components/ui/settings-read-view";
import { useToast } from "@/components/ui/toast";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useConciergeBudget, useConciergeName, useConciergeSettings, useSaveConciergeSettings } from "@/hooks/api/useConciergeApi";
import { useFlows } from "@/hooks/api/useFlowApi";
import { usePlatformConfig } from "@/hooks/usePlatformConfig";
import { defaultConciergeName } from "@/lib/concierge";
import { useFormatter } from "@/i18n";
import type { ConciergeBudgetCaps, ConciergeSettings } from "@/types";

import { BlockTitle, type BlockProps } from "./concierge-block";
import { ConciergePetLibrary } from "./ConciergePetLibrary";
import { DryRunBlock, DurationsBlock, ScheduleBlock, SignalsBlock } from "./ConciergeTriggerBlocks";
import { Spinner } from "@filigran/design-system";

type Draft = ConciergeSettings;

/** The English label of each budget cap. @i18n-keys */
const CAP_LABELS = {
  cycle_tokens: "Tokens per cycle",
  cycle_cost_usd: "Cost per cycle (USD)",
  month_tokens: "Tokens per month",
  month_cost_usd: "Cost per month (USD)",
} as const;

/** What each budget cap counts and what reaching it does. @i18n-keys */
const CAP_INFO = {
  cycle_tokens: "The most tokens one Concierge cycle may spend, every specialist included, e.g. 200000. A cycle that reaches it stops starting work. Empty: no cap.",
  cycle_cost_usd: "The most one Concierge cycle may cost, in US dollars, e.g. 2.50. A cycle that reaches it stops starting work. Empty: no cap.",
  month_tokens: "The most tokens the Concierge may spend in a calendar month, e.g. 5000000. Once reached, it starts no new work until the next month. Empty: no cap.",
  month_cost_usd: "The most the Concierge may cost in a calendar month, in US dollars, e.g. 50. Once reached, it starts no new work until the next month. Empty: no cap.",
} as const;

const CAP_KEYS = Object.keys(CAP_LABELS) as (keyof ConciergeBudgetCaps)[];
const TOKEN_CAPS: ReadonlySet<keyof ConciergeBudgetCaps> = new Set(["cycle_tokens", "month_tokens"]);

function capValid(key: keyof ConciergeBudgetCaps, value: number | string | null): boolean {
  if (value === null) return true;
  const n = Number(value);
  return Number.isFinite(n) && n >= 0 && (!TOKEN_CAPS.has(key) || Number.isInteger(n));
}

/** The longest name the server accepts (`settings.MAX_NAME_LENGTH`). */
const MAX_NAME_LENGTH = 40;

function EnableBlock({ draft, editing, onChange }: BlockProps) {
  const { t_i18n } = useFormatter();
  const fieldId = useId();
  const nameId = `${fieldId}-name`;
  const defaultName = defaultConciergeName(usePlatformConfig()?.platform_mode);
  return (
    <div>
      <BlockTitle
        icon={Power}
        title={t_i18n("Platform Health")}
        hint={t_i18n("While it is off, the Concierge reads nothing and starts no work.")}
      />
      <div className="space-y-4 rounded-sm bg-elevation-default p-3.5">
        {!editing ? (
          // At rest (#10855): read tiles, never disabled controls.
          <SettingsReadGrid data-testid="concierge.read.enable">
            <SettingsReadField
              label={t_i18n("Enable the Concierge")}
              info={t_i18n("On: the Concierge watches its scope, runs its schedules and proposes improvements within its budget. Off: it reads nothing and starts no work; its settings are kept.")}
              value={draft.enabled ? t_i18n("On") : t_i18n("Off")}
            />
            <SettingsReadField
              label={t_i18n("Name")}
              info={t_i18n("The name shown on the Concierge's page, in the inbox and on its agent, e.g. Jeeves. Up to {max} characters. Left empty: {name}.", { max: MAX_NAME_LENGTH, name: defaultName })}
              value={draft.name?.trim() || defaultName}
              data-testid="concierge.name"
            />
          </SettingsReadGrid>
        ) : (
        <>
        <SettingsSwitchField
          id={`${fieldId}-enabled`}
          label={t_i18n("Enable the Concierge")}
          info={t_i18n("On: the Concierge watches its scope, runs its schedules and proposes improvements within its budget. Off: it reads nothing and starts no work; its settings are kept.")}
          checked={draft.enabled}
          onCheckedChange={(enabled) => onChange({ ...draft, enabled })}
        />
        <SettingsField
          id={nameId}
          className="max-w-md"
          label={t_i18n("Name")}
          info={t_i18n("The name shown on the Concierge's page, in the inbox and on its agent, e.g. Jeeves. Up to {max} characters. Left empty: {name}.", { max: MAX_NAME_LENGTH, name: defaultName })}
          helper={t_i18n("What people call it on its page, in the inbox and on its agent. Left empty: {name}.", { name: defaultName })}
        >
          <Input
            id={nameId}
            aria-labelledby={settingsFieldLabelId(nameId)}
            aria-describedby={settingsFieldHintId(nameId)}
            value={draft.name ?? ""}
            placeholder={defaultName}
            maxLength={MAX_NAME_LENGTH}
            onChange={(event) => onChange({ ...draft, name: event.target.value === "" ? null : event.target.value })}
            data-testid="concierge.name"
          />
        </SettingsField>
        </>
        )}
      </div>
    </div>
  );
}

function ScopeBlock({ draft, editing, onChange }: BlockProps) {
  const { t_i18n } = useFormatter();
  const fieldId = useId();
  const selected = draft.scope_mode === "selected";
  return (
    <div>
      <BlockTitle
        icon={Radar}
        title={t_i18n("Supervision scope")}
        hint={t_i18n(
          "What the Concierge observes. It reads the content of an agent or a flow only when its managers opted it in, and never the memories and conversations of a user who opted out.",
        )}
      />
      <div className="space-y-3 rounded-sm bg-elevation-default p-3.5 content-compact">
        {!editing ? (
          <SettingsReadField
            className="max-w-md"
            label={t_i18n("Supervise")}
            info={t_i18n("All: every agent and flow whose managers opted in. Selected: only the agents and flows picked below. The Concierge's sweeps and schedules read it.")}
            value={selected ? t_i18n("Selected agents and flows") : t_i18n("All agents and flows")}
            data-testid="concierge.read.scope-mode"
          />
        ) : (
        <SettingsField
          id={`${fieldId}-mode`}
          className="max-w-md"
          label={t_i18n("Supervise")}
          info={t_i18n("All: every agent and flow whose managers opted in. Selected: only the agents and flows picked below. The Concierge's sweeps and schedules read it.")}
          helper={t_i18n("Which agents and flows the Concierge observes.")}
        >
          <Select
            value={draft.scope_mode}
            onValueChange={(v) => onChange({ ...draft, scope_mode: v as Draft["scope_mode"] })}
          >
            <SelectTrigger
              id={`${fieldId}-mode`}
              aria-labelledby={settingsFieldLabelId(`${fieldId}-mode`)}
              aria-describedby={settingsFieldHintId(`${fieldId}-mode`)}
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t_i18n("All agents and flows")}</SelectItem>
              <SelectItem value="selected">{t_i18n("Selected agents and flows")}</SelectItem>
            </SelectContent>
          </Select>
        </SettingsField>
        )}
        {selected &&
          (editing ? (
            <ScopePickers draft={draft} onChange={onChange} />
          ) : (
            <p className="text-default-secondary" data-testid="concierge.scope-summary">
              {t_i18n("{agents, plural, one {# agent} other {# agents}} and {flows, plural, one {# flow} other {# flows}} supervised", {
                agents: draft.scope_agent_ids.length,
                flows: draft.scope_flow_ids.length,
              })}
            </p>
          ))}
      </div>
    </div>
  );
}

/** The agent and flow pickers, mounted only while editing a "selected" scope: their lists load then. */
function ScopePickers({ draft, onChange }: Pick<BlockProps, "draft" | "onChange">) {
  const { t_i18n } = useFormatter();
  const { data: agents = [] } = useAgents({ scope: "all" });
  const { data: flows = [] } = useFlows({ scope: "all" });
  const toggle = (key: "scope_agent_ids" | "scope_flow_ids", id: string) => {
    const current = draft[key];
    onChange({ ...draft, [key]: current.includes(id) ? current.filter((x) => x !== id) : [...current, id] });
  };
  return (
    <>
      <MultiSelect
        aria-label={t_i18n("Supervised agents")}
        options={agents.map((a) => ({ value: a.id, label: a.name }))}
        selected={draft.scope_agent_ids}
        onToggle={(id) => toggle("scope_agent_ids", id)}
        placeholder={t_i18n("Supervised agents")}
        searchPlaceholder={t_i18n("Search agents…")}
        emptyOptionsLabel={t_i18n("No agents")}
        noMatchLabel={t_i18n("No matching agents")}
      />
      <MultiSelect
        aria-label={t_i18n("Supervised flows")}
        options={flows.map((f) => ({ value: f.id, label: f.name }))}
        selected={draft.scope_flow_ids}
        onToggle={(id) => toggle("scope_flow_ids", id)}
        placeholder={t_i18n("Supervised flows")}
        searchPlaceholder={t_i18n("Search flows…")}
        emptyOptionsLabel={t_i18n("No flows")}
        noMatchLabel={t_i18n("No matching flows")}
      />
    </>
  );
}

function BudgetBlock({ draft, editing, onChange }: BlockProps) {
  const { t_i18n, intl } = useFormatter();
  const n = (value: number) => intl.formatNumber(value);
  const fieldId = useId();
  const { data: budget } = useConciergeBudget();
  const setCap = (key: keyof ConciergeBudgetCaps, raw: string) => {
    const value = raw.trim() === "" ? null : TOKEN_CAPS.has(key) ? Number(raw) : raw.trim();
    onChange({ ...draft, budget: { ...draft.budget, [key]: value } });
  };
  return (
    <div>
      <BlockTitle
        icon={Gauge}
        title={t_i18n("Budget")}
        hint={t_i18n(
          "The Concierge's own line, on top of the platform's licence and quotas. Once a cap is reached it starts no new work and says so in its digest. Leave a cap empty for none.",
        )}
      />
      <div className="space-y-2 rounded-sm bg-elevation-default p-3.5">
        <SettingsFieldGrid>
          {CAP_KEYS.map((key) => {
            const id = `${fieldId}-${key}`;
            return (
              <SettingsField key={key} id={id} label={t_i18n(CAP_LABELS[key])} info={t_i18n(CAP_INFO[key])} helper={t_i18n("Empty: no cap.")}>
                {editing ? (
                  <Input
                    id={id}
                    aria-labelledby={settingsFieldLabelId(id)}
                    aria-describedby={settingsFieldHintId(id)}
                    type="number"
                    min={0}
                    step={TOKEN_CAPS.has(key) ? 1 : 0.01}
                    className="w-40"
                    value={draft.budget[key] ?? ""}
                    onChange={(e) => setCap(key, e.target.value)}
                  />
                ) : (
                  <p className="content-base font-medium tabular-nums">
                    {draft.budget[key] === null ? t_i18n("No cap") : n(Number(draft.budget[key]))}
                  </p>
                )}
              </SettingsField>
            );
          })}
        </SettingsFieldGrid>
        {budget && (
          <div className="mt-2 border-t border-elevation-subtle-soft pt-2 content-compact text-default-secondary" data-testid="concierge.budget-usage">
            {t_i18n("Spent in {month}: {tokens} tokens, {cost} USD.", {
              month: budget.month,
              tokens: n(budget.used.tokens),
              cost: n(Number(budget.used.cost_usd)),
            })}
            {budget.refusal_message && <span className="ml-1 text-feedback-warning-primary">{budget.refusal_message}</span>}
          </div>
        )}
      </div>
    </div>
  );
}

/** The blocks of the page, in order. Later levels append theirs here. */
const BLOCKS: ((props: BlockProps) => React.JSX.Element)[] = [
  EnableBlock,
  ScopeBlock,
  BudgetBlock,
  // Level 8 (#8261): schedule, signal thresholds, durations, dry-run.
  ScheduleBlock,
  SignalsBlock,
  DurationsBlock,
  DryRunBlock,
];

export function ConciergeSection() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data: stored, isLoading } = useConciergeSettings();
  const conciergeName = useConciergeName();
  const save = useSaveConciergeSettings();
  const [draft, setDraft] = useState<Draft | null>(null);
  const editing = draft !== null;
  const shown = draft ?? stored;
  const valid = !!draft && CAP_KEYS.every((key) => capValid(key, draft.budget[key]));

  const submit = () => {
    if (!draft) return;
    save.mutate(draft, {
      onSuccess: () => {
        setDraft(null);
        toast({ title: t_i18n("Concierge settings saved"), variant: "success" });
      },
      onError: (err) =>
        toast({
          title: t_i18n("The Concierge settings were not saved"),
          description: err instanceof Error ? err.message : undefined,
          variant: "error",
        }),
    });
  };

  return (
    <div className="space-y-5 rounded-sm border border-accent/15 bg-gradient-to-br from-accent/[0.03] to-transparent p-5" data-testid="settings.concierge">
      <div className="flex items-center justify-between">
        <p className="content-compact text-default-secondary">
          {t_i18n(
            "The Concierge consolidates memories, diagnoses agents and flows, experiments on sandbox copies and proposes improvements their managers approve.",
          )}
        </p>
        <div className="ml-4 flex shrink-0 gap-3">
          {!editing ? (
            <>
              {/* The page of what it found and proposes, one click away
                  (#9301 N4: the page and the setting link to each other). */}
              <Button variant="outline" size="sm" className="gap-1.5" asChild>
                <Link to="/health">
                  <Compass className="h-3 w-3" /> {t_i18n("Open {name}", { name: conciergeName })}
                </Link>
              </Button>
              <Button variant="secondary" size="sm" className="gap-1.5" onClick={() => stored && setDraft(stored)} disabled={isLoading || !stored}>
                <Pencil className="h-3 w-3" /> {t_i18n("Edit")}
              </Button>
            </>
          ) : (
            <>
              <Button variant="secondary" onClick={() => setDraft(null)}>
                {t_i18n("Cancel")}
              </Button>
              <Button className="gap-1.5" disabled={save.isPending || !valid} onClick={submit} aria-busy={save.isPending || undefined}>
                {save.isPending && <Spinner size="sm" tone="inherit" />}{!save.isPending && <Check className="h-4 w-4" />}
                {t_i18n("Save")}
              </Button>
            </>
          )}
        </div>
      </div>
      {shown &&
        BLOCKS.map((Block, index) => <Block key={index} draft={shown} editing={editing} onChange={setDraft} />)}
      {/* Saved by its own actions, not by the draft above. */}
      <ConciergePetLibrary />
    </div>
  );
}
