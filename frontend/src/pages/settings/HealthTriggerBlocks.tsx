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
 * Settings > Agents and automations > Platform Health, the blocks level 8 adds (#8261) to level 1's page:
 * when each specialist's fleet-wide cycle runs, the signal thresholds
 * (decision B18's defaults), the watch and shadow durations, and dry-run
 * (level 2's switch). Same draft, same Edit / Save as the page's other blocks;
 * the server validates every bound (422).
 */
import { useId } from "react";
import { CalendarClock, FlaskConical, Hourglass, Radio } from "lucide-react";

import { InfoTip } from "@/components/ui/info-tip";
import { Input } from "@/components/ui/input";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, SettingsSwitchField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { SettingsReadField } from "@/components/ui/settings-read-view";
import { Switch } from "@/components/ui/switch";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useHealthScheduleMarks } from "@/hooks/api/useHealthApi";
import { useFormatter } from "@/i18n";
import { useHealthName } from "@/hooks/api/useHealthApi";
import { healthRefusalKey, isScheduleFiring } from "@/lib/health";
import { COMMON_TIMEZONES } from "@/lib/constants";
import { humanizeCron, nextCronRun } from "@/lib/cron";
import { formatCompactInZone } from "@/lib/scheduled-work-items";
import { cn } from "@/lib/utils";
import type { HealthSchedules, HealthSettings, HealthSignalSettings } from "@/types";
import { BlockTitle, type BlockProps } from "./health-block";

const SCHEDULE_KEYS = ["memory", "quality", "flows", "digest"] as const;

/** @i18n-keys */
const SCHEDULE_LABELS = {
  memory: "Memory consolidation",
  quality: "Quality review",
  flows: "Flow review",
  digest: "Weekly digest",
} as const;

/** @i18n-keys */
const SCHEDULE_HINTS = {
  memory: "Memory Steward, every lane Platform Health may consolidate.",
  quality: "Quality Analyst, every supervised agent.",
  flows: "Flow Lab, every supervised flow and assignment.",
  digest: "Platform Health, a summary of the cycles closed since the last one.",
} as const;

/** @i18n-keys */
const SCHEDULE_INFO = {
  memory: "On: the Memory Steward consolidates the memories of every lane Platform Health may touch, on the cron expression below. Off: it never runs on a schedule.",
  quality: "On: the Quality Analyst reviews every supervised agent on the cron expression below. Off: it never runs on a schedule.",
  flows: "On: the Flow Lab reviews every supervised flow and assignment on the cron expression below. Off: it never runs on a schedule.",
  digest: "On: Platform Health writes a summary of the cycles closed since the last one, on the cron expression below. Off: no digest is written.",
} as const;

/** A bounded number with its unit; `display` writes the value at rest (a count with its thousands separator). */
export function NumberField({
  label,
  info,
  unit,
  value,
  min,
  max,
  step = 1,
  editing,
  onChange,
  display = String,
}: {
  label: string;
  info: string;
  unit: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  editing: boolean;
  onChange: (value: number) => void;
  display?: (value: number) => string;
}) {
  const { t_i18n } = useFormatter();
  const id = useId();
  // At rest (#10855): the number and its unit as text, never a disabled input.
  if (!editing) return <SettingsReadField label={label} info={info} mono value={Number.isFinite(value) ? `${display(value)} ${unit}` : ""} />;
  // Out of bounds (or empty): the bound line turns into the field's inline error
  // and the input is marked aria-invalid, so the reason Save is off sits at the field.
  const invalid = !Number.isFinite(value) || value < min || value > max;
  const bounds = t_i18n("From {min} to {max} {unit}.", { min, max, unit });
  return (
    <SettingsField id={id} label={label} info={info} helper={invalid ? undefined : bounds} error={invalid ? bounds : undefined}>
      <div className="flex items-center gap-2">
        <Input
          id={id}
          aria-labelledby={settingsFieldLabelId(id)}
          aria-describedby={settingsFieldHintId(id)}
          aria-invalid={invalid}
          type="number"
          inputMode="decimal"
          min={min}
          max={max}
          step={step}
          value={Number.isFinite(value) ? value : ""}
          onChange={(event) => onChange(event.target.value === "" ? Number.NaN : Number(event.target.value))}
          className="w-28"
        />
        <span className="content-compact text-default-secondary">{unit}</span>
      </div>
    </SettingsField>
  );
}

function SwitchRow({
  title,
  description,
  info,
  checked,
  editing,
  onToggle,
  children,
}: {
  title: string;
  description: string;
  info: string;
  checked: boolean;
  editing: boolean;
  onToggle: (value: boolean) => void;
  children?: React.ReactNode;
}) {
  const id = useId();
  const { t_i18n } = useFormatter();
  if (!editing) {
    // At rest (#10855): the switch reads as On or Off, its settings below it as tiles.
    return (
      <div className="space-y-3 rounded-sm bg-elevation-default p-3.5">
        <SettingsReadField label={title} info={info} value={checked ? t_i18n("On") : t_i18n("Off")} />
        {checked && children && <div className="flex flex-wrap items-start gap-4">{children}</div>}
      </div>
    );
  }
  return (
    <SettingsSwitchField
      className="rounded-sm bg-elevation-default p-3.5"
      id={id}
      label={title}
      helper={description}
      info={info}
      checked={checked}
      onCheckedChange={onToggle}
    >
      {checked && children && <div className="flex flex-wrap items-start gap-4">{children}</div>}
    </SettingsSwitchField>
  );
}

const CRON_INFO =
  "Five fields: minute, hour, day of month, month, day of week, e.g. 0 3 * * * for every day at 03:00 in the timezone of this section. The scheduler reads it at each tick.";
const TIMEZONE_INFO =
  "The timezone the cron expressions of this section are read in, e.g. Europe/Paris. Every schedule of Platform Health uses it. UTC unless chosen.";

/**
 * When each specialist's fleet-wide cycle starts (#735): one table row per
 * specialist (switch, cron, how it reads, last and next run), the timezone on
 * the block's header line. Same draft and Save as the page's other blocks; the
 * server validates the cron expressions and the timezone (422).
 */
export function ScheduleBlock({ draft, editing, onChange }: BlockProps) {
  const { t_i18n, locale } = useFormatter();
  const healthName = useHealthName();
  const { data: marks = [] } = useHealthScheduleMarks();
  const timezoneId = useId();
  const timezone = draft.schedules.timezone || "UTC";
  const set = (patch: Partial<HealthSchedules>) => onChange({ ...draft, schedules: { ...draft.schedules, ...patch } });
  const when = (iso: string) => formatCompactInZone(iso, timezone, locale);
  return (
    <div data-testid="settings.health.schedule">
      <div className="mb-2 flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <BlockTitle
          icon={CalendarClock}
          title={t_i18n("Schedule")}
          hint={t_i18n("When each specialist's fleet-wide cycle starts. Each run opens a budget cycle first.")}
        />
        <div className="flex items-center gap-2" data-testid="settings.health.schedule.timezone">
          <label id={settingsFieldLabelId(timezoneId)} htmlFor={timezoneId} className="content-compact text-default-secondary">
            {t_i18n("Timezone")}
          </label>
          <InfoTip content={<div className="max-w-xs content-compact">{t_i18n(TIMEZONE_INFO)}</div>} label={t_i18n("About this field")} />
          {editing ? (
            <SearchableSelect
              id={timezoneId}
              aria-labelledby={settingsFieldLabelId(timezoneId)}
              options={["UTC", ...COMMON_TIMEZONES].map((tz) => ({ value: tz, label: tz.replace(/_/g, " ") }))}
              value={draft.schedules.timezone}
              onValueChange={(tz) => set({ timezone: tz })}
              className="w-56"
            />
          ) : (
            <span id={timezoneId} className="content-base font-medium">
              {timezone.replace(/_/g, " ")}
            </span>
          )}
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full table-fixed content-compact" data-testid="settings.health.schedule.table">
          {/* Fixed widths sized to the all-valid layout: a row flipping to "Invalid cron" while typing never moves a column (the short
              message fits the When cell whole, about 109 px at 1080, in en, fr, es and ja).
              Status fits its longest value in en, fr, es and ja ("Desactivado", about 68 px) at 1080; Last run and Next run fit
              "Dernière exécution" / "Prochaine exécution" (about 108 / 116 px plus padding) at 1080 and wrap rather than overflow below it. */}
          <colgroup>
            <col className="w-[19%]" />
            <col className="w-[11%]" />
            <col className="w-[23%]" />
            <col className="w-[15%]" />
            <col className="w-[16%]" />
            <col className="w-[16%]" />
          </colgroup>
          <thead>
            <tr className="border-b border-elevation-subtle-soft text-left text-default-secondary">
              <th className="whitespace-nowrap py-1.5 pr-3 font-medium">{t_i18n("Name")}</th>
              <th className="whitespace-nowrap py-1.5 pr-3 font-medium">{t_i18n("Status")}</th>
              <th className="whitespace-nowrap py-1.5 pr-3 font-medium">{t_i18n("Cron expression")}</th>
              <th className="whitespace-nowrap py-1.5 pr-3 font-medium">{t_i18n("When")}</th>
              <th className="py-1.5 pr-3 font-medium">{t_i18n("Last run")}</th>
              <th className="py-1.5 font-medium">{t_i18n("Next run")}</th>
            </tr>
          </thead>
          <tbody>
            {SCHEDULE_KEYS.map((key) => {
              const label = t_i18n(SCHEDULE_LABELS[key]);
              const cron = draft.schedules[key];
              const enabled = draft.schedules[`${key}_enabled`];
              const next = nextCronRun(cron, timezone);
              const mark = marks.find((m) => m.schedule === key);
              const fired = isScheduleFiring(mark) ? mark : undefined;
              const switchId = `${timezoneId}-${key}-switch`;
              const cronId = `${timezoneId}-${key}-cron`;
              return (
                <tr key={key} className="border-b border-elevation-subtle-soft last:border-b-0" data-testid={`settings.health.schedule.row.${key}`}>
                  <td className="py-1.5 pr-3">
                    <span className="flex min-w-0 items-center gap-1.5">
                      <span id={`${switchId}-label`} className="min-w-0">
                        <TruncatedText text={label} className="content-base font-medium" />
                      </span>
                      <InfoTip
                        label={t_i18n("About this field")}
                        content={
                          <div className="max-w-xs space-y-1 content-compact">
                            <p>{t_i18n(SCHEDULE_HINTS[key])}</p>
                            <p>{t_i18n(SCHEDULE_INFO[key])}</p>
                          </div>
                        }
                      />
                    </span>
                  </td>
                  <td className="py-1.5 pr-3">
                    {editing ? (
                      <Switch
                        id={switchId}
                        aria-labelledby={`${switchId}-label`}
                        checked={enabled}
                        onCheckedChange={(on) => set({ [`${key}_enabled`]: on } as Partial<HealthSchedules>)}
                      />
                    ) : (
                      <span className={cn("whitespace-nowrap", enabled ? "text-default-primary" : "text-default-secondary")}>{enabled ? t_i18n("On") : t_i18n("Off")}</span>
                    )}
                  </td>
                  <td className="py-1.5 pr-3">
                    {editing ? (
                      <span className="flex items-center gap-1">
                        <Input
                          id={cronId}
                          aria-label={t_i18n("When {schedule} starts.", { schedule: label })}
                          aria-invalid={next === null || undefined}
                          aria-describedby={next === null ? `${cronId}-error` : undefined}
                          value={cron}
                          onChange={(event) => set({ [key]: event.target.value } as Partial<HealthSchedules>)}
                          className="w-36 [&_input]:font-mono-plex"
                        />
                        <InfoTip content={<div className="max-w-xs content-compact">{t_i18n(CRON_INFO)}</div>} label={t_i18n("About this field")} />
                      </span>
                    ) : (
                      <span className="font-mono-plex content-base">{cron.trim()}</span>
                    )}
                  </td>
                  {(() => {
                    const reading = next === null ? t_i18n("Invalid cron") : `${humanizeCron(cron.trim(), locale)} (${timezone.replace(/_/g, " ")})`;
                    const last = `${fired ? when(fired.fired_at) : t_i18n("Never")}${fired?.refusal ? ` · ${t_i18n(healthRefusalKey({ detail: { code: fired.refusal } }), { name: healthName })}` : ""}`;
                    const upcoming = enabled && next ? when(next.toISOString()) : t_i18n("Not scheduled");
                    return (
                      <>
                        <td className={cn("py-1.5 pr-3", next === null ? "text-feedback-error-primary" : "text-default-secondary")}>
                          <span id={next === null ? `${cronId}-error` : undefined} className="block min-w-0">
                            <TruncatedText text={reading} />
                          </span>
                        </td>
                        <td className="py-1.5 pr-3 text-default-secondary" data-testid={`settings.health.schedule.last.${key}`}>
                          <TruncatedText text={last} />
                        </td>
                        <td className="py-1.5 text-default-secondary" data-testid={`settings.health.schedule.next.${key}`}>
                          <TruncatedText text={upcoming} />
                        </td>
                      </>
                    );
                  })()}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function SignalsBlock({ draft, editing, onChange }: BlockProps) {
  const { t_i18n } = useFormatter();
  const s = draft.signals;
  const set = (patch: Partial<HealthSignalSettings>) => onChange({ ...draft, signals: { ...s, ...patch } });
  const field = (label: string, info: string, unit: string, key: keyof HealthSignalSettings, min: number, max: number, step = 1) => (
    <NumberField
      label={label}
      info={info}
      unit={unit}
      value={s[key] as number}
      min={min}
      max={max}
      step={step}
      editing={editing}
      onChange={(value) => set({ [key]: value } as Partial<HealthSignalSettings>)}
    />
  );
  return (
    <div>
      <BlockTitle
        icon={Radio}
        title={t_i18n("Signals")}
        hint={t_i18n("Thresholds read without any model call. A crossing starts one investigation on its target; it does not start another while one is open there.")}
      />
      <div className="space-y-2">
        <SwitchRow
          title={t_i18n("Feedback drop")}
          description={t_i18n("An agent's weighted feedback score falls over a window, against the window before.")}
          info={t_i18n("On: Platform Health opens an investigation on an agent whose feedback score falls by the drop below. Off: feedback is not watched.")}
          checked={s.feedback_enabled}
          editing={editing}
          onToggle={(on) => set({ feedback_enabled: on })}
        >
          {field(t_i18n("Score drop"), t_i18n("How far the score must fall, in points out of 100, e.g. 15. Read at each signal sweep."), t_i18n("points"), "feedback_drop_points", 1, 100)}
          {field(t_i18n("Window"), t_i18n("The days each score is computed over, compared with the same number of days before, e.g. 7."), t_i18n("days"), "feedback_window_days", 1, 90)}
          {field(t_i18n("At least"), t_i18n("The fewest ratings in the window for the score to count, e.g. 10. Fewer ratings: no signal."), t_i18n("ratings"), "feedback_min_ratings", 1, 10000)}
        </SwitchRow>
        <SwitchRow
          title={t_i18n("Failure spike")}
          description={t_i18n("A flow's or an assignment's failure rate over its last runs.")}
          info={t_i18n("On: Platform Health opens an investigation on a flow or an assignment whose failure rate rises above the threshold below. Off: failures are not watched.")}
          checked={s.failure_enabled}
          editing={editing}
          onToggle={(on) => set({ failure_enabled: on })}
        >
          {field(t_i18n("Above"), t_i18n("The failure rate, in percent of runs, that raises the signal, e.g. 20."), "%", "failure_rate_pct", 0.1, 99, 0.1)}
          {field(t_i18n("Over the last"), t_i18n("How many of the latest runs the rate is computed over, e.g. 20."), t_i18n("runs"), "failure_last_runs", 5, 500)}
        </SwitchRow>
        <SwitchRow
          title={t_i18n("Cost drift")}
          description={t_i18n("An agent's cost per assignment run over a window, against the window before.")}
          info={t_i18n("On: Platform Health opens an investigation on an agent whose cost per assignment run rises by the threshold below. Off: cost is not watched.")}
          checked={s.cost_enabled}
          editing={editing}
          onToggle={(on) => set({ cost_enabled: on })}
        >
          {field(t_i18n("Up by"), t_i18n("The rise of the cost per run, in percent of the window before, that raises the signal, e.g. 50."), "%", "cost_increase_pct", 1, 10000)}
          {field(t_i18n("Window"), t_i18n("The days each average is computed over, compared with the same number of days before, e.g. 7."), t_i18n("days"), "cost_window_days", 1, 90)}
          {field(t_i18n("At least"), t_i18n("The fewest runs in the window for the average to count, e.g. 5. Fewer runs: no signal."), t_i18n("runs"), "cost_min_runs", 1, 10000)}
        </SwitchRow>
        <SwitchRow
          title={t_i18n("Model change")}
          description={t_i18n("The model a supervised agent resolves to changes.")}
          info={t_i18n("On: Platform Health opens an investigation when the model a supervised agent resolves to changes. Off: model changes are not watched.")}
          checked={s.model_change_enabled}
          editing={editing}
          onToggle={(on) => set({ model_change_enabled: on })}
        />
      </div>
    </div>
  );
}

export function DurationsBlock({ draft, editing, onChange }: BlockProps) {
  const { t_i18n } = useFormatter();
  const set = (key: keyof HealthSettings) => (value: number) => onChange({ ...draft, [key]: value });
  return (
    <div>
      <BlockTitle
        icon={Hourglass}
        title={t_i18n("Durations")}
        hint={t_i18n("How long an applied change is watched for a regression, and how long a flow replacing an assignment runs in shadow first.")}
      />
      <div className="flex flex-wrap items-start gap-6 rounded-sm bg-elevation-default p-3.5">
        <NumberField label={t_i18n("Watch an applied proposal")} info={t_i18n("How many days an applied improvement is watched for a regression, e.g. 7. A regression in that time proposes the rollback.")} unit={t_i18n("days")} value={draft.proposal_watch_days} min={1} max={90} editing={editing} onChange={set("proposal_watch_days")} />
        <NumberField label={t_i18n("Flow shadow")} info={t_i18n("How many days a flow that replaces an assignment runs beside it without acting, e.g. 7.")} unit={t_i18n("days")} value={draft.flow_shadow_days} min={1} max={90} editing={editing} onChange={set("flow_shadow_days")} />
        <NumberField label={t_i18n("Watch a switched-over flow")} info={t_i18n("How many days a flow is watched for a regression after it replaced its assignment, e.g. 14.")} unit={t_i18n("days")} value={draft.flow_watch_days} min={1} max={90} editing={editing} onChange={set("flow_watch_days")} />
        <NumberField label={t_i18n("Shadow pairs needed")} info={t_i18n("How many runs of the flow and its assignment side by side are needed before the switch is proposed, e.g. 10.")} unit={t_i18n("runs")} value={draft.flow_shadow_min_pairs} min={1} max={1000} editing={editing} onChange={set("flow_shadow_min_pairs")} />
      </div>
    </div>
  );
}

/** Level 2's switch (#8255). */
export function DryRunBlock({ draft, editing, onChange }: BlockProps) {
  const { t_i18n } = useFormatter();
  return (
    <div>
      <BlockTitle
        icon={FlaskConical}
        title={t_i18n("Dry-run")}
        hint={t_i18n("Every write of every Platform Health agent - memory, proposals, sandbox copies - is logged, not executed. Digests count what was logged.")}
      />
      <SwitchRow
        title={t_i18n("Log every write instead of executing it")}
        description={t_i18n("Use it to see what Platform Health would do before letting it act.")}
        info={t_i18n("On: every write of every Platform Health agent is logged and not executed; digests count them. Off: Platform Health acts within its scope and budget.")}
        checked={draft.dry_run}
        editing={editing}
        onToggle={(on) => onChange({ ...draft, dry_run: on })}
      />
    </div>
  );
}
