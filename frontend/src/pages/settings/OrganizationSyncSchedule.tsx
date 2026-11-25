// SPDX-License-Identifier: Apache-2.0

/**
 * The schedule of an organization profile sync (#11913): how often (hourly,
 * daily, weekly), at what time, on which day and in which time zone. Shared by
 * the import stepper's "Import and keep in sync" choice and the sync editor of
 * Settings > Organization, so a schedule reads and is set the same way in both.
 */
import { useMemo } from "react";

import { Input } from "@/components/ui/input";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, SettingsFieldGrid, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import type { SyncFrequency } from "@/hooks/api/useOrganizationContextApi";
import { useFormatter } from "@/i18n";
import { COMMON_TIMEZONES } from "@/lib/constants";
import { browserTimeZone, pad2 as pad, weekdayName, type SyncSchedule } from "@/lib/organization-sync-schedule";

/** @i18n-keys */
const FREQUENCY_LABELS = {
  hourly: "Hourly",
  daily: "Daily",
  weekly: "Weekly",
} as const satisfies Record<SyncFrequency, string>;

interface Props {
  /** The prefix of the fields' ids, unique on the page. */
  idPrefix: string;
  value: SyncSchedule;
  onChange: (next: SyncSchedule) => void;
  /** Outbound (#1982 S4): a run writes the platform instead of reading it, and the helper says so. */
  direction?: "inbound" | "outbound";
}

/** Frequency, time (or minute), day and time zone, each a settings field with its helper line. */
export function SyncScheduleFields({ idPrefix, value, onChange, direction = "inbound" }: Props) {
  const { t_i18n, locale } = useFormatter();
  const set = (patch: Partial<SyncSchedule>) => onChange({ ...value, ...patch });
  const ids = { frequency: `${idPrefix}-frequency`, time: `${idPrefix}-time`, minute: `${idPrefix}-minute`, weekday: `${idPrefix}-weekday`, zone: `${idPrefix}-zone` };

  const zones = useMemo(() => {
    const names: string[] = [...COMMON_TIMEZONES];
    for (const extra of [browserTimeZone(), value.timezone]) if (extra && !names.includes(extra)) names.push(extra);
    return names.map((zone) => ({ value: zone, label: zone.replace(/_/g, " ") }));
  }, [value.timezone]);
  const minutes = useMemo(() => Array.from({ length: 12 }, (_, i) => ({ value: String(i * 5), label: pad(i * 5) })), []);
  const days = useMemo(() => Array.from({ length: 7 }, (_, day) => ({ value: String(day), label: weekdayName(locale, day) })), [locale]);
  const frequencies = (Object.keys(FREQUENCY_LABELS) as SyncFrequency[]).map((key) => ({ value: key, label: t_i18n(FREQUENCY_LABELS[key]) }));

  return (
    <SettingsFieldGrid data-testid={`${idPrefix}.schedule`}>
      <SettingsField
        id={ids.frequency}
        label={t_i18n("Frequency")}
        helper={direction === "outbound" ? t_i18n("How often the XTM One rows are pushed to the platform again.") : t_i18n("How often the platform is read again.")}
        info={
          direction === "outbound"
            ? t_i18n("Each run creates, updates and deletes only the platform rows this sync created.")
            : t_i18n("Each run saves a new profile version only when something changed.")
        }
      >
        <SearchableSelect
          id={ids.frequency}
          aria-labelledby={settingsFieldLabelId(ids.frequency)}
          aria-describedby={settingsFieldHintId(ids.frequency)}
          searchable="auto"
          sort={false}
          options={frequencies}
          value={value.frequency}
          onValueChange={(next) => set({ frequency: next as SyncFrequency })}
          className="w-full"
        />
      </SettingsField>
      {value.frequency === "hourly" ? (
        <SettingsField id={ids.minute} label={t_i18n("Minute past the hour")} helper={t_i18n("The run starts at this minute of every hour.")}>
          <SearchableSelect
            id={ids.minute}
            aria-labelledby={settingsFieldLabelId(ids.minute)}
            aria-describedby={settingsFieldHintId(ids.minute)}
            searchable="auto"
            sort={false}
            options={minutes}
            value={String(value.minute - (value.minute % 5))}
            onValueChange={(next) => set({ minute: Number(next) })}
            className="w-full"
          />
        </SettingsField>
      ) : (
        <SettingsField id={ids.time} label={t_i18n("Time")} helper={t_i18n("The time the run starts, in the time zone below.")}>
          <Input
            id={ids.time}
            aria-labelledby={settingsFieldLabelId(ids.time)}
            aria-describedby={settingsFieldHintId(ids.time)}
            type="time"
            step={60}
            value={`${pad(value.hour)}:${pad(value.minute)}`}
            onChange={(event) => {
              const [hour, minute] = event.target.value.split(":").map(Number);
              if (Number.isInteger(hour) && Number.isInteger(minute)) set({ hour, minute });
            }}
          />
        </SettingsField>
      )}
      {value.frequency === "weekly" && (
        <SettingsField id={ids.weekday} label={t_i18n("Day")} helper={t_i18n("The day of the week the run starts.")}>
          <SearchableSelect
            id={ids.weekday}
            aria-labelledby={settingsFieldLabelId(ids.weekday)}
            aria-describedby={settingsFieldHintId(ids.weekday)}
            searchable="auto"
            sort={false}
            options={days}
            value={String(value.weekday)}
            onValueChange={(next) => set({ weekday: Number(next) })}
            className="w-full"
          />
        </SettingsField>
      )}
      {value.frequency !== "hourly" && (
        <SettingsField id={ids.zone} label={t_i18n("Time zone")} helper={t_i18n("Your browser's time zone by default.")}>
          <SearchableSelect
            id={ids.zone}
            aria-labelledby={settingsFieldLabelId(ids.zone)}
            aria-describedby={settingsFieldHintId(ids.zone)}
            options={zones}
            value={value.timezone}
            onValueChange={(next) => set({ timezone: next })}
            className="w-full"
          />
        </SettingsField>
      )}
    </SettingsFieldGrid>
  );
}
