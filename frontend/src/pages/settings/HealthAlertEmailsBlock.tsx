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
 * Settings > Agents and automations > Platform Health, the alert emails
 * (#101 S12b): who receives them, from which severity, and whether a critical
 * finding is sent at once and the open findings once a day
 * (`HealthAlertEmailSettings` of `services/health/settings.py`,
 * dev-docs/platform-health.md section 7). Off by default; same draft, same
 * Edit / Save as the page's other blocks. No model call, nothing metered.
 */
import { useId } from "react";
import { Icon } from "@filigran/design-system";

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SettingsField, SettingsSwitchField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { SettingsReadField, SettingsReadGrid } from "@/components/ui/settings-read-view";
import { TagInput } from "@/components/ui/tag-input";
import { useFormatter } from "@/i18n";
import type { HealthAlertEmailSettings, HealthSettings } from "@/types";

import { BlockTitle, type BlockProps } from "./health-block";
import { NumberField } from "./HealthTriggerBlocks";

const MailGlyph = ({ className }: { className?: string }) => <Icon name="mail" size={16} className={className} aria-hidden />;

/** The most recipients the server accepts (`MAX_ALERT_RECIPIENTS`). */
const MAX_ALERT_RECIPIENTS = 20;
/** The bounds of `alert_after_minutes` the server accepts (0 to a day). */
const ALERT_AFTER_MAX_MINUTES = 24 * 60;

/** The English label of each minimum severity. @i18n-keys */
const SEVERITY_LABELS = {
  critical: "Critical only",
  warning: "Warning and above",
  info: "Every finding",
} as const;

const SEVERITIES = Object.keys(SEVERITY_LABELS) as HealthAlertEmailSettings["min_severity"][];

/** What the selected minimum severity means, shown under the select. @i18n-keys */
const SEVERITY_MEANINGS = {
  critical: "The digest lists critical findings only.",
  warning: "The digest lists warning and critical findings.",
  info: "The digest lists every open finding.",
} as const;

/** The server's rule (`_valid_recipient`): one `@`, a local part, a dotted domain, no space. */
function isRecipient(value: string): boolean {
  const at = value.indexOf("@");
  if (at <= 0 || value.indexOf("@", at + 1) !== -1 || value.length > 254 || /\s/.test(value)) return false;
  return value.slice(at + 1).replace(/^\.+|\.+$/g, "").includes(".");
}

/** The recipients the server would refuse. */
function invalidRecipients(recipients: string[]): string[] {
  return recipients.filter((value) => !isRecipient(value));
}

/** Whether the alert emails of the draft can be saved (Save stays off otherwise). */
export function alertEmailsValid(draft: HealthSettings): boolean {
  const { enabled, recipients, alert_after_minutes: after } = draft.alert_emails;
  const afterValid = after === undefined || (Number.isInteger(after) && after >= 0 && after <= ALERT_AFTER_MAX_MINUTES);
  return afterValid && invalidRecipients(recipients).length === 0 && recipients.length <= MAX_ALERT_RECIPIENTS && (!enabled || recipients.length > 0);
}

export function AlertEmailsBlock({ draft, editing, onChange }: BlockProps) {
  const { t_i18n } = useFormatter();
  const fieldId = useId();
  const recipientsId = `${fieldId}-recipients`;
  const severityId = `${fieldId}-severity`;
  const alerts = draft.alert_emails;
  const set = (patch: Partial<HealthAlertEmailSettings>) => onChange({ ...draft, alert_emails: { ...alerts, ...patch } });
  const bad = invalidRecipients(alerts.recipients);
  const recipientsError =
    bad.length > 0
      ? t_i18n("Not an email address: {addresses}", { addresses: bad.join(", ") })
      : alerts.recipients.length > MAX_ALERT_RECIPIENTS
        ? t_i18n("{max, plural, one {At most # recipient.} other {At most # recipients.}}", { max: MAX_ALERT_RECIPIENTS })
        : alerts.enabled && alerts.recipients.length === 0
          ? t_i18n("Add at least one recipient, or turn the alert emails off.")
          : undefined;
  const info = {
    enabled: t_i18n("On: Platform Health emails the recipients below about its findings, from the platform sender. Off: nothing is sent. Nothing is sent either while the platform email (SMTP) is not configured."),
    recipients: t_i18n("The addresses that receive the alert emails, e.g. soc@example.com. Up to {max}; press Enter or a comma after each.", { max: MAX_ALERT_RECIPIENTS }),
    severity: t_i18n("The lowest severity the daily digest lists, e.g. Warning and above. An immediate email is sent for critical findings only, whatever this says."),
    immediate: t_i18n("On: one email per critical finding once it has stayed open for the delay below, sent once each time it opens. A finding a restart opens resolves before and sends nothing. Off: critical findings wait for the digest."),
    after: t_i18n("How long a critical finding must stay open before its email goes out, e.g. 15 minutes: a restart of XTM One or of a module resolves before and sends nothing. 0 sends it at the pass that opens it."),
    digest: t_i18n("On: one email a day (UTC) listing the open findings at or above the minimum severity, sent only when there is at least one. Off: no digest."),
  };
  const severityLabel = t_i18n(SEVERITY_LABELS[alerts.min_severity]);
  const afterMinutes = alerts.alert_after_minutes ?? 15;
  return (
    <div data-testid="settings.health.alert-emails">
      <BlockTitle
        icon={MailGlyph}
        title={t_i18n("Alert emails")}
        hint={t_i18n("Emails about what the health checks find: critical findings at once and a daily digest. Off by default; no model call.")}
      />
      <div className="space-y-4 rounded-sm bg-elevation-default p-3.5">
        {!editing ? (
          // At rest (#10855): read tiles, never disabled controls.
          <SettingsReadGrid data-testid="health.read.alert-emails">
            <SettingsReadField label={t_i18n("Send alert emails")} info={info.enabled} value={alerts.enabled ? t_i18n("On") : t_i18n("Off")} />
            {alerts.enabled && (
              <>
                <SettingsReadField
                  label={t_i18n("Recipients")}
                  info={info.recipients}
                  value={alerts.recipients.length > 0 ? alerts.recipients.join(", ") : t_i18n("None")}
                  data-testid="health.read.alert-recipients"
                />
                <SettingsReadField label={t_i18n("Minimum severity")} info={info.severity} value={severityLabel} />
                <SettingsReadField label={t_i18n("Critical findings at once")} info={info.immediate} value={alerts.immediate ? t_i18n("On") : t_i18n("Off")} />
                {alerts.immediate && (
                  <NumberField label={t_i18n("Send after")} info={info.after} unit={t_i18n("minutes")} value={afterMinutes} min={0} max={ALERT_AFTER_MAX_MINUTES} editing={false} onChange={() => undefined} />
                )}
                <SettingsReadField label={t_i18n("Daily digest")} info={info.digest} value={alerts.digest ? t_i18n("On") : t_i18n("Off")} />
              </>
            )}
          </SettingsReadGrid>
        ) : (
          <>
            <SettingsSwitchField
              id={`${fieldId}-enabled`}
              label={t_i18n("Send alert emails")}
              helper={t_i18n("Emails the recipients below about health findings.")}
              info={info.enabled}
              checked={alerts.enabled}
              onCheckedChange={(enabled) => set({ enabled })}
            />
            {alerts.enabled && (
              <>
                <SettingsField
                  id={recipientsId}
                  className="max-w-xl"
                  label={t_i18n("Recipients")}
                  info={info.recipients}
                  helper={recipientsError ? undefined : t_i18n("Press Enter or a comma after each address.")}
                  error={recipientsError}
                >
                  <TagInput
                    id={recipientsId}
                    aria-labelledby={settingsFieldLabelId(recipientsId)}
                    aria-describedby={settingsFieldHintId(recipientsId)}
                    aria-invalid={recipientsError !== undefined}
                    invalidTags={bad}
                    suggestFromPlatformTags={false}
                    placeholder="soc@example.com"
                    value={alerts.recipients}
                    onChange={(recipients) => set({ recipients })}
                  />
                </SettingsField>
                <SettingsField id={severityId} className="max-w-xs" label={t_i18n("Minimum severity")} info={info.severity} helper={t_i18n(SEVERITY_MEANINGS[alerts.min_severity])}>
                  <Select value={alerts.min_severity} onValueChange={(value) => set({ min_severity: value as HealthAlertEmailSettings["min_severity"] })}>
                    <SelectTrigger id={severityId} aria-labelledby={settingsFieldLabelId(severityId)} aria-describedby={settingsFieldHintId(severityId)}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {SEVERITIES.map((severity) => (
                        <SelectItem key={severity} value={severity}>
                          {t_i18n(SEVERITY_LABELS[severity])}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </SettingsField>
                <SettingsSwitchField
                  id={`${fieldId}-immediate`}
                  label={t_i18n("Critical findings at once")}
                  helper={t_i18n("One email per critical finding that stays open.")}
                  info={info.immediate}
                  checked={alerts.immediate}
                  onCheckedChange={(immediate) => set({ immediate })}
                />
                {alerts.immediate && (
                  <NumberField
                    label={t_i18n("Send after")}
                    info={info.after}
                    unit={t_i18n("minutes")}
                    value={afterMinutes}
                    min={0}
                    max={ALERT_AFTER_MAX_MINUTES}
                    editing
                    onChange={(alert_after_minutes) => set({ alert_after_minutes })}
                  />
                )}
                <SettingsSwitchField
                  id={`${fieldId}-digest`}
                  label={t_i18n("Daily digest")}
                  helper={t_i18n("One email a day with the open findings.")}
                  info={info.digest}
                  checked={alerts.digest}
                  onCheckedChange={(digest) => set({ digest })}
                />
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
