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
 * Agent → Budget: what one run of this agent may spend.
 *
 * A PARTIAL override of the platform budget, which is the whole design of this
 * form: an empty field is not "0", it is "inherit". So every input renders the
 * value it would inherit as its placeholder, and clearing a field is how you
 * hand a dimension back to the platform. Pre-filling the inherited numbers
 * would look friendlier and be wrong — the copy would stop tracking the
 * platform value the moment an operator changed it.
 *
 * The other half of the design is authority. A user may tighten any limit; only
 * an admin may raise one above the platform ceiling. The server enforces that
 * (`budget_policy.ceiling_violations`), but a form that lets you type a value
 * the save will refuse reads as a bug — so the ceiling is shown inline, and a
 * non-admin gets the refusal explained before they attempt it.
 */

import { useMemo, useState } from "react";
import { Slider } from "@filigran/design-system";
import {
  Wallet, LifeBuoy, Save, RotateCcw, Info, Lock,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { ReadOnlyBadge } from "@/components/ui/read-only-badge";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { useToast } from "@/components/ui/toast";
import { useBudgetPolicy, useUpdateAgent } from "@/hooks/api/useAgentApi";
import { useAvailableModels } from "@/hooks/api/useSettingsApi";
import { useFormatter } from "@/i18n";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { PROVIDER_DISPLAY_NAMES } from "@/lib/constants";
import {
  BUDGET_LIMITS, blockedLimits, limitsDraft, parseLimit,
} from "@/lib/run-budget-limits";
import { BudgetLimitFields } from "@/components/budget/BudgetLimitFields";
import type { Agent, AgentBudgetConfig, BudgetLimitKey } from "@/types";

type Draft = Record<BudgetLimitKey, string> & {
  final_answer_reserve: number | null;
  fallback_enabled: boolean | null;
  fallback_model: string | null;
};

function toDraft(config: AgentBudgetConfig | null): Draft {
  return {
    ...limitsDraft(config),
    final_answer_reserve: config?.final_answer_reserve ?? null,
    fallback_enabled: config?.fallback_enabled ?? null,
    fallback_model: config?.fallback_model ?? null,
  };
}

/** The payload shape the API stores: only the pinned keys, or null for none. */
function toPayload(draft: Draft): AgentBudgetConfig | null {
  const config: AgentBudgetConfig = {};
  for (const { key } of BUDGET_LIMITS) {
    const value = parseLimit(draft[key], key);
    if (value !== null) config[key] = value;
  }
  if (draft.final_answer_reserve !== null) {
    config.final_answer_reserve = draft.final_answer_reserve;
  }
  if (draft.fallback_enabled !== null) {
    config.fallback_enabled = draft.fallback_enabled;
  }
  if (draft.fallback_model) config.fallback_model = draft.fallback_model;
  return Object.keys(config).length > 0 ? config : null;
}

export function BudgetTab({
  agent,
  canManage,
}: {
  agent: Agent;
  canManage: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const update = useUpdateAgent();
  const { data: policy } = useBudgetPolicy();
  const { data: availableModels } = useAvailableModels({ scope: "all" });

  const stored = agent.budget_config ?? null;
  const [draft, setDraft] = useState<Draft>(() => toDraft(stored));
  // Saving goes through `PUT /agents/{id}`, which is AI-gated: with AI off
  // the control must be visibly unavailable rather than refuse on click.
  const aiOff = useAiActionDisabled();

  const canExceed = policy?.can_exceed_ceiling ?? false;
  const dirty = useMemo(
    () => JSON.stringify(toPayload(draft)) !== JSON.stringify(stored),
    [draft, stored],
  );

  /**
   * Which fields the save would be refused for — computed here so the reason
   * is visible next to the field, not delivered as a 403 after the attempt.
   * Mirrors the server rule exactly, including its escape hatch: a value an
   * admin already stored is not re-litigated when a non-admin re-saves it.
   */
  const blocked = useMemo(() => blockedLimits(draft, policy, stored), [policy, draft, stored]);

  const reserve = draft.final_answer_reserve;
  const inheritedReserve = policy?.final_answer_reserve ?? 0.1;
  const fallbackOn = draft.fallback_enabled ?? policy?.fallback_enabled ?? false;
  // Only chat-capable models can carry a run, so the fallback picker offers the
  // same catalog the model selectors elsewhere do.
  const models = availableModels ?? [];

  const save = () => {
    if (blocked.size > 0) return;
    update.mutate(
      { id: agent.id, budget_config: toPayload(draft) },
      {
        onSuccess: () => toast({ title: t_i18n("Budget saved") }),
        onError: (error: Error) =>
          toast({ title: t_i18n("Could not save the budget"), description: error.message, variant: "error" }),
      },
    );
  };

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-start justify-between gap-4">
            <div>
              <CardTitle className="flex items-center gap-2 text-base">
                <Wallet className="h-4 w-4 text-accent" /> {t_i18n("Run budget")}
              </CardTitle>
              <p className="mt-1 text-[11px] text-default-secondary">
                {t_i18n("What ONE run of this agent may spend: a chat turn, an assignment run, or a step in a flow. Leave a field empty to inherit the platform budget; a value here only ever applies to this agent.")}
              </p>
            </div>
            {!canManage && <ReadOnlyBadge />}
          </div>
        </CardHeader>
        <CardContent className="space-y-5">
          {!canExceed && (
            <div className="flex items-start gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3">
              <Lock className="mt-0.5 h-3.5 w-3.5 shrink-0 text-default-secondary" />
              <p className="text-[11px] leading-relaxed text-default-secondary">
                {t_i18n("You can lower any limit below the platform budget. Raising one above it is an administrator's decision. Ask an admin if this agent needs a larger budget than the platform allows.")}
              </p>
            </div>
          )}

          <BudgetLimitFields
            draft={draft}
            onChange={(key, value) => setDraft((d) => ({ ...d, [key]: value }))}
            policy={policy}
            blocked={blocked}
            disabled={!canManage}
          />

          {/* Final-answer reserve */}
          <div className="space-y-2 border-t border-elevation-subtle-soft pt-4">
            <div className="flex items-center justify-between">
              <label className="content-compact text-default-secondary">
                {t_i18n("Final-answer reserve")}
              </label>
              {reserve !== null && canManage && (
                <button
                  className="content-caption text-accent hover:text-accent/80"
                  onClick={() => setDraft((d) => ({ ...d, final_answer_reserve: null }))}
                >
                  {t_i18n("Inherit")}
                </button>
              )}
            </div>
            <div className="flex items-center gap-3">
              <Slider
                min={0}
                max={0.5}
                step={0.05}
                disabled={!canManage}
                value={[reserve ?? inheritedReserve]}
                onValueChange={([value]) =>
                  setDraft((d) => ({
                    ...d,
                    final_answer_reserve: Math.round(value * 100) / 100,
                  }))
                }
                aria-label={t_i18n("Final-answer reserve")}
                wrapperClassName="flex-1"
              />
              <span className="w-12 text-right font-mono-plex content-base tabular-nums">
                {Math.round((reserve ?? inheritedReserve) * 100)}%
              </span>
            </div>
            <p className="content-caption text-default-secondary">
              {t_i18n("Share of the cost and token ceilings held back so a run that hits its budget can still write its answer, instead of returning a raw digest of tool output. Inherits {value}% when empty.", { value: Math.round(inheritedReserve * 100) })}
            </p>
          </div>
        </CardContent>
      </Card>

      {/* ── The failsafe ─────────────────────────────────────────────────── */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <LifeBuoy className="h-4 w-4 text-feedback-success-primary" />
            {t_i18n("Finish on a cheaper model")}
          </CardTitle>
          <p className="mt-1 text-[11px] text-default-secondary">
            {t_i18n("A cost ceiling usually means \"don't spend more than this at this quality\", not \"abandon the task\". The same budget buys several times more work on a cheaper model. With this on, a run that reaches its cost ceiling switches models and carries on, once. The rest of the run is charged at the cheaper model's rates, so the ceiling still holds.")}
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between rounded-sm bg-elevation-default p-3.5">
            <div className="space-y-0.5">
              <p className="content-base font-medium">{t_i18n("Enable the failsafe")}</p>
              <span className="content-compact text-default-secondary">
                {draft.fallback_enabled === null
                  ? t_i18n("Inherits the platform setting ({value})", { value: policy?.fallback_enabled ? t_i18n("on") : t_i18n("off") })
                  : t_i18n("Pinned for this agent")}
              </span>
            </div>
            <div className="flex items-center gap-3">
              {draft.fallback_enabled !== null && canManage && (
                <button
                  className="content-caption text-accent hover:text-accent/80"
                  onClick={() => setDraft((d) => ({ ...d, fallback_enabled: null }))}
                >
                  {t_i18n("Inherit")}
                </button>
              )}
              <Switch aria-label={t_i18n("Enable the failsafe")}
                checked={fallbackOn}
                disabled={!canManage}
                onCheckedChange={(v) =>
                  setDraft((d) => ({ ...d, fallback_enabled: v }))
                }
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <label className="content-compact text-default-secondary">
              {t_i18n("Fallback model")}
            </label>
            <SearchableSelect
              aria-label={t_i18n("Fallback model")}
              options={models.map((m) => ({
                value: m.model,
                label: m.model,
                description: PROVIDER_DISPLAY_NAMES[m.provider] || m.provider,
                selectedLabel: `${m.model} (${PROVIDER_DISPLAY_NAMES[m.provider] || m.provider})`,
              }))}
              pinned={[{
                value: "__platform__",
                label: policy?.fallback_model
                  ? t_i18n("Platform fallback model ({value})", { value: policy.fallback_model })
                  : t_i18n("Platform fallback model"),
              }]}
              value={draft.fallback_model || "__platform__"}
              disabled={!canManage}
              onValueChange={(v) =>
                setDraft((d) => ({
                  ...d,
                  fallback_model: v === "__platform__" ? null : v,
                }))
              }
              placeholder={t_i18n("Search models...")}
            />
            <p className="flex items-start gap-1.5 content-caption text-default-secondary">
              <Info className="mt-0.5 h-3 w-3 shrink-0" />
              {t_i18n("Pick a model on a cheaper price tier. The downgrade needs pricing for it: with none known, the run hard-stops at its cost ceiling instead. A model whose context window cannot hold this agent's system prompt is refused too, so the run stops rather than failing mid-flight.")}
            </p>
          </div>
        </CardContent>
      </Card>

      {canManage && (
        <div className="flex items-center justify-end gap-2">
          {dirty && (
            <Button
              variant="ghost"
              size="sm"
              className="gap-1.5"
              onClick={() => setDraft(toDraft(stored))}
            >
              <RotateCcw className="h-3 w-3" /> {t_i18n("Discard changes")}
            </Button>
          )}
          {blocked.size > 0 && (
            <Badge variant="outline" className="border-feedback-error-primary/40 text-feedback-error-primary">
              {t_i18n("{count, plural, one {# limit needs an administrator} other {# limits need an administrator}}", { count: blocked.size })}
            </Badge>
          )}
          <AiDisabledTip>
            <Button
              size="sm"
              className="gap-1.5"
              disabled={!dirty || blocked.size > 0 || update.isPending || aiOff}
              onClick={save}
            >
              <Save className="h-3 w-3" /> {t_i18n("Save budget")}
            </Button>
          </AiDisabledTip>
        </div>
      )}
    </div>
  );
}
