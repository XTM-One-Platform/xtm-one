// SPDX-License-Identifier: Apache-2.0

/**
 * Settings > Organization > Ticketing and intelligence: how CTEM cases sync
 * with the ticketing target (#917, dev-docs/ctem-ticketing.md).
 *
 * Read view: one tile per setting, the program default named when a value is
 * not set. Edit view: when a case gets its ticket, the record type, the labels,
 * the priority per severity, the status mapping (the system's status name to a
 * case ticket status, read before the system's own categories), and the three
 * loop switches (done asks the re-test, a reopened case reopens or links its
 * ticket, the due date from the SLA). Part of the whole-profile form: nothing
 * is written until the profile is saved, and the next sync reads it with no
 * restart.
 */
import { Icon } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SettingsField, SettingsFieldGrid, SettingsSwitchField, settingsFieldHintId, settingsFieldLabelId } from "@/components/ui/settings-form";
import { SettingsReadField, SettingsReadGrid } from "@/components/ui/settings-read-view";
import { useFormatter } from "@/i18n";
import { CASE_SEVERITIES, CASE_TICKET_STATUSES, type CaseTicketStatus, type TicketingSync, ticketingDefaults, ticketingSyncOf } from "@/lib/organization-profile";

/** @i18n-keys */
const CREATE_ON_LABELS = {
  mobilize_p1_p2: "At Mobilize, for P1 and P2 cases",
  mobilize_p1: "At Mobilize, for P1 cases only",
  manual: "By hand only",
} as const satisfies Record<TicketingSync["create_on"], string>;

/** @i18n-keys */
const ON_REOPEN_LABELS = {
  reopen: "Reopen the same ticket",
  linked: "Open a linked ticket",
} as const satisfies Record<TicketingSync["on_reopen"], string>;

/** @i18n-keys */
const TICKET_STATUS_LABELS = {
  open: "Open (ticket)",
  in_progress: "In progress (ticket)",
  done: "Done (ticket)",
  cancelled: "Cancelled (ticket)",
} as const satisfies Record<CaseTicketStatus, string>;

interface Props {
  /** The stored sync; absent on an older profile (the defaults apply). */
  sync: TicketingSync | undefined;
  /** The provider of the ticketing integration (jira, servicenow...), "" when none is set. */
  provider: string;
  editing: boolean;
  onChange: (sync: TicketingSync) => void;
  idPrefix: string;
}

export function OrganizationTicketingSync({ sync: stored, provider, editing, onChange, idPrefix }: Props) {
  const { t_i18n } = useFormatter();
  const sync = ticketingSyncOf(stored);
  const defaults = ticketingDefaults(provider);
  const set = (patch: Partial<TicketingSync>) => onChange({ ...sync, ...patch });
  const statusRows = Object.entries(sync.status_map);
  const priorityLine = CASE_SEVERITIES.map((severity) => `${severity}: ${sync.priority_map[severity] || defaults.priority[severity]}`).join(" · ");
  const statusLine = statusRows.length
    ? statusRows.map(([name, status]) => `${name} → ${t_i18n(TICKET_STATUS_LABELS[status as CaseTicketStatus] ?? status)}`).join(" · ")
    : t_i18n("The system's own status categories");

  if (!editing) {
    return (
      <SettingsReadGrid data-testid="settings.organization.ticketing-sync">
        <SettingsReadField label={t_i18n("Ticket for a case")} value={t_i18n(CREATE_ON_LABELS[sync.create_on])} />
        <SettingsReadField label={t_i18n("Record type")} value={sync.record_type || t_i18n("Default: {value}", { value: defaults.record_type })} />
        <SettingsReadField label={t_i18n("Labels")} value={sync.labels.join(", ")} />
        <SettingsReadField label={t_i18n("Priority by severity")} value={priorityLine} />
        <SettingsReadField label={t_i18n("Status mapping")} value={statusLine} wide />
        <SettingsReadField label={t_i18n("Done asks for the re-test")} value={t_i18n(sync.done_starts_retest ? "On" : "Off")} />
        <SettingsReadField label={t_i18n("Case reopened")} value={t_i18n(ON_REOPEN_LABELS[sync.on_reopen])} />
        <SettingsReadField label={t_i18n("Due date from the SLA")} value={t_i18n(sync.due_date_from_sla ? "On" : "Off")} />
      </SettingsReadGrid>
    );
  }

  const setStatusRow = (index: number, name: string, status: string) => {
    const rows = statusRows.map((row, i) => (i === index ? [name, status] : row));
    set({ status_map: Object.fromEntries(rows) });
  };
  const removeStatusRow = (index: number) => set({ status_map: Object.fromEntries(statusRows.filter((_, i) => i !== index)) });
  // A new row starts with an empty name; the save drops a row whose name stays empty.
  const addStatusRow = () => set({ status_map: { ...sync.status_map, "": "done" } });

  return (
    <div className="space-y-4" data-testid="settings.organization.ticketing-sync-form">
      <SettingsFieldGrid>
        <SettingsField
          id={`${idPrefix}-create-on`}
          label={t_i18n("Ticket for a case")}
          helper={t_i18n("When a case gets its ticket in the ticketing system.")}
        >
          <SearchableSelect
            id={`${idPrefix}-create-on`}
            aria-labelledby={settingsFieldLabelId(`${idPrefix}-create-on`)}
            aria-describedby={settingsFieldHintId(`${idPrefix}-create-on`)}
            searchable="auto"
            sort={false}
            options={(Object.keys(CREATE_ON_LABELS) as TicketingSync["create_on"][]).map((value) => ({ value, label: t_i18n(CREATE_ON_LABELS[value]) }))}
            value={sync.create_on}
            onValueChange={(value) => set({ create_on: value as TicketingSync["create_on"] })}
          />
        </SettingsField>
        <SettingsField
          id={`${idPrefix}-record-type`}
          label={t_i18n("Record type")}
          helper={t_i18n("The Jira issue type or the ServiceNow table. Empty: {value}.", { value: defaults.record_type })}
        >
          <Input
            id={`${idPrefix}-record-type`}
            aria-describedby={settingsFieldHintId(`${idPrefix}-record-type`)}
            placeholder={defaults.record_type}
            value={sync.record_type}
            onChange={(e) => set({ record_type: e.target.value })}
          />
        </SettingsField>
        <SettingsField
          id={`${idPrefix}-labels`}
          label={t_i18n("Labels")}
          helper={t_i18n("Every ticket carries them, separated by commas.")}
          wide
        >
          <Input
            id={`${idPrefix}-labels`}
            aria-describedby={settingsFieldHintId(`${idPrefix}-labels`)}
            placeholder="ctem, security"
            value={sync.labels.join(", ")}
            onChange={(e) => set({ labels: e.target.value.split(",").map((label) => label.trimStart()) })}
          />
        </SettingsField>
        {CASE_SEVERITIES.map((severity) => (
          <SettingsField
            key={severity}
            id={`${idPrefix}-priority-${severity}`}
            label={t_i18n("Priority for {severity} cases", { severity })}
            helper={t_i18n("Empty: {value}.", { value: defaults.priority[severity] })}
          >
            <Input
              id={`${idPrefix}-priority-${severity}`}
              aria-describedby={settingsFieldHintId(`${idPrefix}-priority-${severity}`)}
              placeholder={defaults.priority[severity]}
              value={sync.priority_map[severity] ?? ""}
              onChange={(e) => set({ priority_map: { ...sync.priority_map, [severity]: e.target.value } })}
            />
          </SettingsField>
        ))}
      </SettingsFieldGrid>

      <div className="space-y-2" data-testid="settings.organization.ticketing-status-map">
        <div className="space-y-1">
          <p className="content-base font-semibold text-default-primary">{t_i18n("Status mapping")}</p>
          <p className="content-compact text-default-secondary">
            {t_i18n("A ticket status name to the case ticket status. Read first; unmapped names follow the system's own categories.")}
          </p>
        </div>
        {statusRows.map(([name, status], index) => (
          <div key={index} className="flex items-center gap-2" data-testid="settings.organization.ticketing-status-row">
            <Input
              className="flex-1"
              aria-label={t_i18n("Ticket status name")}
              placeholder={t_i18n("Ready for QA")}
              value={name}
              onChange={(e) => setStatusRow(index, e.target.value, status)}
            />
            <SearchableSelect
              className="w-56 shrink-0"
              aria-label={t_i18n("Case ticket status")}
              searchable="auto"
              sort={false}
              options={CASE_TICKET_STATUSES.map((value) => ({ value, label: t_i18n(TICKET_STATUS_LABELS[value]) }))}
              value={status}
              onValueChange={(value) => setStatusRow(index, name, value)}
            />
            <Button variant="ghost" size="icon" aria-label={t_i18n("Remove")} onClick={() => removeStatusRow(index)}>
              <Icon name="trash-2" size={16} aria-hidden />
            </Button>
          </div>
        ))}
        <Button variant="secondary" className="gap-1.5" onClick={addStatusRow} disabled={"" in sync.status_map}>
          <Icon name="plus" size={16} aria-hidden /> {t_i18n("Map a status")}
        </Button>
      </div>

      <SettingsSwitchField
        id={`${idPrefix}-done-retest`}
        label={t_i18n("Done asks for the re-test")}
        helper={t_i18n("A ticket moved to done starts the case's re-test; the case closes on the re-test, never on the ticket.")}
        checked={sync.done_starts_retest}
        onCheckedChange={(checked) => set({ done_starts_retest: checked })}
      />
      <SettingsField id={`${idPrefix}-on-reopen`} label={t_i18n("Case reopened")} helper={t_i18n("When a failed re-test reopens the case.")}>
        <SearchableSelect
          id={`${idPrefix}-on-reopen`}
          aria-labelledby={settingsFieldLabelId(`${idPrefix}-on-reopen`)}
          aria-describedby={settingsFieldHintId(`${idPrefix}-on-reopen`)}
          searchable="auto"
          sort={false}
          options={(Object.keys(ON_REOPEN_LABELS) as TicketingSync["on_reopen"][]).map((value) => ({ value, label: t_i18n(ON_REOPEN_LABELS[value]) }))}
          value={sync.on_reopen}
          onValueChange={(value) => set({ on_reopen: value as TicketingSync["on_reopen"] })}
        />
      </SettingsField>
      <SettingsSwitchField
        id={`${idPrefix}-due-sla`}
        label={t_i18n("Due date from the SLA")}
        helper={t_i18n("The ticket's due date is the case's SLA due date.")}
        checked={sync.due_date_from_sla}
        onCheckedChange={(checked) => set({ due_date_from_sla: checked })}
      />
    </div>
  );
}
