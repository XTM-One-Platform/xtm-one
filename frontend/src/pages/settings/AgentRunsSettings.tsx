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

import { useEffect, useState } from "react";
import { Check, Pencil, Timer } from "lucide-react";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { EnvLockBadge } from "@/components/ui/env-lock-badge";
import { Input } from "@/components/ui/input";
import {
  SettingsField,
  SettingsFieldGrid,
  SettingsGroupDescription,
  SettingsGroupTitle,
  SettingsSwitchField,
  settingsFieldHintId,
  settingsFieldLabelId,
} from "@/components/ui/settings-form";
import { SettingsReadField } from "@/components/ui/settings-read-view";
import { useToast } from "@/components/ui/toast";
import { usePlatformSettings, useUpdatePlatformSettings } from "@/hooks/api/useAiAdoptionApi";
import { useFormatter } from "@/i18n";
import {
  MAXIMUM_TOTAL_WAIT_TIME_CEILING,
  MAXIMUM_TOTAL_WAIT_TIME_DEFAULT,
  MAXIMUM_WAIT_TIME_CEILING,
  MAXIMUM_WAIT_TIME_DEFAULT,
} from "@/lib/constants";
import { formatDurationShort } from "@/lib/utils";
import { Spinner } from "@filigran/design-system";

/**
 * A draft in seconds, as typed: a whole number from 1 to the ceiling, or null
 * while the field is empty or out of range. The field keeps the raw text so it
 * can be cleared and retyped; it is parsed on Save, never on every keystroke.
 */
function parseSeconds(draft: string, ceiling: number): number | null {
  if (!/^\d+$/.test(draft.trim())) return null;
  const value = Number(draft.trim());
  return value >= 1 && value <= ceiling ? value : null;
}

/**
 * Settings > AI > Agent runs: the `wait` policy that bounds EVERY run
 * (chat, assignments, evaluations), stored on the platform settings. Agents
 * and assignments may narrow it with their own `wait_policy`; an env-pinned
 * value (AGENT_MAXIMUM_WAIT_TIME / AGENT_MAXIMUM_TOTAL_WAIT_TIME) is read-only.
 */
export function AgentRunsSettings({ fieldId, canEdit }: { fieldId: string; canEdit: boolean }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data: settings } = usePlatformSettings();
  const update = useUpdatePlatformSettings();

  const perCallCeiling = settings?.maximum_wait_time_ceiling ?? MAXIMUM_WAIT_TIME_CEILING;
  const totalCeiling = settings?.maximum_total_wait_time_ceiling ?? MAXIMUM_TOTAL_WAIT_TIME_CEILING;
  const perCall = settings?.maximum_wait_time ?? MAXIMUM_WAIT_TIME_DEFAULT;
  const total = settings?.maximum_total_wait_time ?? MAXIMUM_TOTAL_WAIT_TIME_DEFAULT;
  const unbounded = settings?.wait_unbounded_with_until ?? false;
  const locked = new Set(settings?.wait_env_locked_fields ?? []);
  const perCallLocked = locked.has("maximum_wait_time");
  const totalLocked = locked.has("maximum_total_wait_time");

  const [editing, setEditing] = useState(false);
  const [perCallDraft, setPerCallDraft] = useState(String(perCall));
  const [totalDraft, setTotalDraft] = useState(String(total));
  const [unboundedDraft, setUnboundedDraft] = useState(unbounded);

  useEffect(() => {
    if (!editing) {
      setPerCallDraft(String(perCall));
      setTotalDraft(String(total));
      setUnboundedDraft(unbounded);
    }
  }, [editing, perCall, total, unbounded]);

  const perCallValue = parseSeconds(perCallDraft, perCallCeiling);
  const totalValue = parseSeconds(totalDraft, totalCeiling);
  const perCallInvalid = !perCallLocked && perCallValue === null;
  const totalInvalid = !totalLocked && totalValue === null;
  const perCallEditable = editing && !perCallLocked;
  const totalEditable = editing && !totalLocked;

  const save = () => {
    if (perCallInvalid || totalInvalid) return;
    const body: Record<string, number | boolean> = { wait_unbounded_with_until: unboundedDraft };
    if (!perCallLocked && perCallValue !== null) body.maximum_wait_time = perCallValue;
    if (!totalLocked && totalValue !== null) body.maximum_total_wait_time = totalValue;
    update.mutate(body, {
      onSuccess: () => {
        setEditing(false);
        toast({ title: t_i18n("Saved"), variant: "success" });
      },
      onError: () => toast({ title: t_i18n("Could not save the agent run limits"), variant: "error" }),
    });
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <div>
          <SettingsGroupTitle icon={Timer}>{t_i18n("Agent runs")}</SettingsGroupTitle>
          <SettingsGroupDescription>
            {t_i18n("How long any agent may pause with the wait tool, waiting for delegated work instead of polling for it. Applies to chat, assignments and evaluations; an agent or an assignment may lower these limits, never raise them. A pause does not consume iterations, and users can skip or cancel a wait at any time.")}
          </SettingsGroupDescription>
        </div>
        {canEdit && (
          !editing ? (
            <Button variant="secondary" size="sm" className="gap-1.5 shrink-0 ml-4" onClick={() => setEditing(true)}>
              <Pencil className="h-3 w-3" /> {t_i18n("Configure")}
            </Button>
          ) : (
            <div className="flex gap-3 shrink-0 ml-4">
              <Button variant="secondary" onClick={() => setEditing(false)}>{t_i18n("Cancel")}</Button>
              <Button className="gap-1.5" disabled={update.isPending || perCallInvalid || totalInvalid} onClick={save} aria-busy={update.isPending || undefined}>
                {update.isPending && <Spinner size="sm" tone="inherit" />}{!update.isPending && <Check className="h-4 w-4" />}
                {t_i18n("Save")}
              </Button>
            </div>
          )
        )}
      </div>
      <SettingsFieldGrid className="rounded-sm bg-elevation-default p-3.5">
        <WaitField
          id={`${fieldId}-wait-per-call`}
          label={<>{t_i18n("Longest single wait")} {perCallLocked && <EnvLockBadge />}</>}
          info={t_i18n("The longest one call to the wait tool may pause, in seconds, e.g. 300 for 5 minutes. Every agent run reads it; an agent or an assignment may only lower it. It cannot be empty; AGENT_MAXIMUM_WAIT_TIME pins it.")}
          helper={t_i18n("Longest single pause. Max {max}.", { max: formatDurationShort(perCallCeiling) })}
          editable={perCallEditable}
          invalid={perCallInvalid}
          ceiling={perCallCeiling}
          draft={perCallDraft}
          parsed={perCallValue}
          stored={perCall}
          onDraft={setPerCallDraft}
        />
        <WaitField
          id={`${fieldId}-wait-total`}
          label={<>{t_i18n("Total wait per run")} {totalLocked && <EnvLockBadge />}</>}
          info={t_i18n("The sum of every wait of one run, in seconds, e.g. 3600 for 1 hour. Every agent run reads it; an agent or an assignment may only lower it. It cannot be empty; AGENT_MAXIMUM_TOTAL_WAIT_TIME pins it.")}
          helper={t_i18n("Total per run. Max {max}.", { max: formatDurationShort(totalCeiling) })}
          editable={totalEditable}
          invalid={totalInvalid}
          ceiling={totalCeiling}
          draft={totalDraft}
          parsed={totalValue}
          stored={total}
          onDraft={setTotalDraft}
        />
        {!editing ? (
          // At rest (#10855): the switch reads as a word, never as a disabled control.
          <SettingsReadField
            className="md:col-span-2"
            label={t_i18n("Waits on named work are not counted")}
            info={t_i18n("On: a wait that names the tasks or runs it waits for is not charged to the total per run. Off: every wait is charged. Every agent run reads it.")}
            value={unbounded ? t_i18n("On") : t_i18n("Off")}
            data-testid="settings.agent-runs.read.unbounded"
          />
        ) : (
        <SettingsSwitchField
          className="md:col-span-2"
          id={`${fieldId}-wait-unbounded`}
          label={t_i18n("Waits on named work are not counted")}
          info={t_i18n("On: a wait that names the tasks or runs it waits for is not charged to the total per run. Off: every wait is charged. Every agent run reads it.")}
          helper={t_i18n("When a wait names the tasks or runs it waits for, it is not charged to the total per run; only blind waits are. The longest single wait still applies.")}
          checked={unboundedDraft}
          onCheckedChange={(checked) => setUnboundedDraft(checked)}
        />
        )}
      </SettingsFieldGrid>
    </div>
  );
}

/** One wait limit: the number of seconds while editing, the duration otherwise. */
function WaitField({
  id,
  label,
  info,
  helper,
  editable,
  invalid,
  ceiling,
  draft,
  parsed,
  stored,
  onDraft,
}: {
  id: string;
  label: ReactNode;
  info: string;
  helper: string;
  editable: boolean;
  invalid: boolean;
  ceiling: number;
  draft: string;
  parsed: number | null;
  stored: number;
  onDraft: (value: string) => void;
}) {
  const { t_i18n } = useFormatter();
  return (
    <SettingsField id={id} label={label} info={info} helper={editable && invalid ? undefined : helper} error={editable && invalid ? helper : undefined}>
      {editable ? (
        <div className="flex items-center gap-2">
          <Input
            id={id}
            aria-labelledby={settingsFieldLabelId(id)}
            aria-describedby={settingsFieldHintId(id)}
            aria-invalid={invalid}
            type="number"
            inputMode="numeric"
            min={1}
            max={ceiling}
            step={1}
            value={draft}
            onChange={(e) => onDraft(e.target.value)}
            className="w-32"
          />
          <span className="content-compact text-default-secondary">
            {t_i18n("seconds")}{parsed !== null && <> · {formatDurationShort(parsed)}</>}
          </span>
        </div>
      ) : (
        <p className="content-base font-mono-plex">{formatDurationShort(stored)}</p>
      )}
    </SettingsField>
  );
}
