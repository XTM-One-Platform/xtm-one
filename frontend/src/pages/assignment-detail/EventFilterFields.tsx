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

import { useId } from "react";
import { Filter } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { TagInput } from "@/components/ui/tag-input";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import {
  MAIL_EVENTS,
  TEAMS_EVENTS,
  cleanEventFilterExtra,
  joinList,
  pickEventFilterExtra,
  splitList,
  type EventFilterExtra,
} from "@/lib/event-filters";
import { useFormatter } from "@/i18n";
import type { TriggerConfig } from "@/types";

// ---------------------------------------------------------------------------
// EventFilterFields — the trigger filter keys evaluated before any run or gate
// (sender lists, recipients, automated mail, authors, chats, change kinds, flags).
// ---------------------------------------------------------------------------

const CHANGE_KINDS = ["created", "updated", "cancelled"] as const;
const FLAG_STATUSES = ["flagged", "complete", "notFlagged"] as const;

/** A list filter key on the shared TagInput: one chip per value, the case kept,
 *  no platform tag suggestions (an address, a subject or a chat id is not a tag).
 *  A legacy string value still reads as its split list. */
function ListInput({ label, help, placeholder, values, onChange }: {
  label: string;
  help: string;
  placeholder: string;
  values: string[] | string | undefined;
  onChange: (values: string[]) => void;
}) {
  const id = useId();
  const list = Array.isArray(values) ? values : splitList(values ?? "");
  return (
    <div className="space-y-1.5">
      <Label id={`${id}-label`}>{label}</Label>
      <TagInput
        value={list}
        onChange={onChange}
        placeholder={placeholder}
        preserveCase
        suggestFromPlatformTags={false}
        aria-labelledby={`${id}-label`}
        aria-describedby={`${id}-help`}
      />
      <p id={`${id}-help`} className="content-compact text-default-secondary">{help}</p>
    </div>
  );
}

function ToggleRow({ label, help, checked, onChange }: { label: string; help: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="space-y-0.5">
        <p className="content-compact">{label}</p>
        <p className="content-compact text-default-secondary">{help}</p>
      </div>
      <Switch aria-label={label} checked={checked} onCheckedChange={onChange} />
    </div>
  );
}

function Chips<T extends string>({ label, options, labels, selected, onChange }: {
  label: string;
  options: readonly T[];
  labels: Record<T, string>;
  selected: T[] | undefined;
  onChange: (values: T[]) => void;
}) {
  const chosen = new Set(selected ?? []);
  return (
    <div className="space-y-1.5">
      <p className="content-compact text-default-secondary">{label}</p>
      <div className="flex flex-wrap gap-1.5">
        {options.map((option) => (
          <button
            key={option}
            className={cn(
              "rounded-sm border px-2 py-1 content-caption transition-colors",
              chosen.has(option) ? "border-accent/40 bg-accent/10 text-accent" : "border-elevation-subtle-soft text-default-secondary hover:border-elevation-subtle",
            )}
            onClick={() => onChange(options.filter((o) => (o === option ? !chosen.has(o) : chosen.has(o))))}
          >
            {labels[option]}
          </button>
        ))}
      </div>
    </div>
  );
}

export function EventFilterFields({ event, value, onChange }: {
  event: string;
  value: EventFilterExtra;
  onChange: (value: EventFilterExtra) => void;
}) {
  const { t_i18n } = useFormatter();
  const folderId = useId();
  const set = (patch: EventFilterExtra) => onChange({ ...value, ...patch });
  // Every change kind a trigger filter can carry (To Do adds completed and deleted); the calendar chips show the first three.
  const kindLabels = {
    created: t_i18n("Created"),
    updated: t_i18n("Updated"),
    cancelled: t_i18n("Cancelled"),
    completed: t_i18n("Completed"),
    deleted: t_i18n("Deleted"),
  };
  const flagLabels = { flagged: t_i18n("Flagged"), complete: t_i18n("Completed"), notFlagged: t_i18n("Not flagged") };

  return (
    <div className="space-y-3">
      {MAIL_EVENTS.has(event) && (
        <>
          <ListInput
            label={t_i18n("Allowed senders")}
            help={t_i18n("Only these addresses or domains. Leave empty for every sender.")}
            placeholder={t_i18n("e.g. acme.com, ceo@partner.com")}
            values={value.from?.allow}
            onChange={(allow) => set({ from: { ...value.from, allow } })}
          />
          <ListInput
            label={t_i18n("Blocked senders")}
            help={t_i18n("Mail from these addresses or domains never triggers the assignment.")}
            placeholder={t_i18n("e.g. crowdstrike.com")}
            values={value.from?.deny}
            onChange={(deny) => set({ from: { ...value.from, deny } })}
          />
          <ListInput
            label={t_i18n("Subject contains")}
            help={t_i18n("Only mail whose subject holds one of these.")}
            placeholder={t_i18n("e.g. board, audit")}
            values={value.subject_contains}
            onChange={(subjects) => set({ subject_contains: subjects })}
          />
          <div className="space-y-1.5">
            <Label htmlFor={folderId} id={`${folderId}-label`}>{t_i18n("Folder")}</Label>
            <Input
              id={folderId}
              aria-labelledby={`${folderId}-label`}
              aria-describedby={`${folderId}-help`}
              placeholder={t_i18n("e.g. inbox")}
              value={value.folder ?? ""}
              onChange={(e) => set({ folder: e.target.value })}
            />
            <p id={`${folderId}-help`} className="content-compact text-default-secondary">{t_i18n("Only mail of this folder (Outlook) or label (Gmail). Leave empty for every folder.")}</p>
          </div>
          <ToggleRow
            label={t_i18n("Exclude automated mail")}
            help={t_i18n("Skip no-reply senders, bulk and mailing-list mail, delivery reports and calendar responses.")}
            checked={!!value.exclude_automated}
            onChange={(v) => set({ exclude_automated: v })}
          />
          <ToggleRow
            label={t_i18n("Only mail sent to me")}
            help={t_i18n("The mailbox owner is a To recipient.")}
            checked={!!value.to_me}
            onChange={(v) => set({ to_me: v })}
          />
          <ToggleRow
            label={t_i18n("Only mail where I am in Cc")}
            help={t_i18n("The mailbox owner is a Cc recipient. With the switch above, either one.")}
            checked={!!value.cc_me}
            onChange={(v) => set({ cc_me: v })}
          />
        </>
      )}
      {event === "new_teams_chat_message" && (
        <ListInput
          label={t_i18n("Chat ids")}
          help={t_i18n("Only these chats. Leave empty for every chat with a new message (polled only).")}
          placeholder="19:...@thread.v2"
          values={value.chat_ids}
          onChange={(chat_ids) => set({ chat_ids })}
        />
      )}
      {TEAMS_EVENTS.has(event) && (
        <ListInput
          label={t_i18n("Excluded authors")}
          help={t_i18n("Messages from these people (display names or user ids) never trigger the assignment.")}
          placeholder={t_i18n("e.g. Build Bot")}
          values={value.exclude_authors}
          onChange={(exclude_authors) => set({ exclude_authors })}
        />
      )}
      {event === "calendar_event_changed" && (
        <Chips label={t_i18n("Change kinds (none selected: every change)")} options={CHANGE_KINDS} labels={kindLabels} selected={value.change_kinds} onChange={(change_kinds) => set({ change_kinds })} />
      )}
      {event === "email_flag_changed" && (
        <Chips label={t_i18n("New flag status (none selected: every change)")} options={FLAG_STATUSES} labels={flagLabels} selected={value.flag_status} onChange={(flag_status) => set({ flag_status })} />
      )}
    </div>
  );
}

/** Read-only view: the active filters and how many events they dropped. */
export function EventFilterSummary({ filter, event, filteredCount }: {
  filter: TriggerConfig["filter"] | undefined;
  event: string;
  filteredCount: number;
}) {
  const { t_i18n } = useFormatter();
  const extra = cleanEventFilterExtra(pickEventFilterExtra(filter), event);
  const chips: string[] = [];
  if (extra.exclude_automated) chips.push(t_i18n("Exclude automated mail"));
  if (extra.to_me) chips.push(t_i18n("Only mail sent to me"));
  if (extra.cc_me) chips.push(t_i18n("Only mail where I am in Cc"));
  if (extra.from?.allow?.length) chips.push(t_i18n("Allowed senders: {list}", { list: extra.from.allow.join(", ") }));
  if (extra.from?.deny?.length) chips.push(t_i18n("Blocked senders: {list}", { list: extra.from.deny.join(", ") }));
  if (extra.subject_contains?.length) chips.push(t_i18n("Subject contains: {list}", { list: joinList(extra.subject_contains) }));
  if (extra.folder) chips.push(t_i18n("Folder: {folder}", { folder: extra.folder }));
  if (extra.chat_ids?.length) chips.push(t_i18n("Chats: {count}", { count: extra.chat_ids.length }));
  if (extra.exclude_authors?.length) chips.push(t_i18n("Excluded authors: {list}", { list: extra.exclude_authors.join(", ") }));
  if (extra.change_kinds?.length) chips.push(t_i18n("Change kinds: {list}", { list: extra.change_kinds.join(", ") }));
  if (extra.flag_status?.length) chips.push(t_i18n("Flag status: {list}", { list: extra.flag_status.join(", ") }));
  if (!chips.length && !filteredCount) return null;
  return (
    <div className="flex items-start gap-3 rounded-sm bg-elevation-hover px-4 py-3">
      <Filter className="h-4 w-4 mt-0.5 shrink-0 text-feedback-info-primary" />
      <div className="min-w-0 flex-1 space-y-1.5">
        <p className="content-compact font-medium">{t_i18n("Event filters")}</p>
        {chips.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {chips.map((chip) => <span key={chip} className="rounded-sm bg-elevation-hover px-2 py-0.5 content-caption">{chip}</span>)}
          </div>
        )}
        <p className="content-caption text-default-secondary">{t_i18n("Events filtered: {count}", { count: filteredCount })}</p>
      </div>
    </div>
  );
}
