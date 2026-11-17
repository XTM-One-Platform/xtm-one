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

import type { TriggerConfig } from "@/types";

/** The filter keys the event-filter editor owns (the Scope pickers and the sender text live in TriggerCard). */
export type EventFilterExtra = Omit<NonNullable<TriggerConfig["filter"]>, "channels" | "scope" | "from_contains">;

export const MAIL_EVENTS = new Set(["new_email", "email_flag_changed"]);
export const TEAMS_EVENTS = new Set(["new_teams_message", "new_teams_reply", "new_teams_chat_message"]);

export const splitList = (text: string): string[] => text.split(",").map((v) => v.trim()).filter(Boolean);
export const joinList = (values: string[] | string | undefined): string =>
  Array.isArray(values) ? values.join(", ") : (values ?? "");

/** The keys of *filter* the event-filter editor owns. */
export function pickEventFilterExtra(filter: TriggerConfig["filter"] | undefined): EventFilterExtra {
  const { channels: _channels, scope: _scope, from_contains: _from, ...extra } = filter ?? {};
  return extra;
}

/** *extra* without its empty values and the keys *event* does not take, ready to merge into `trigger_config.filter`. */
export function cleanEventFilterExtra(extra: EventFilterExtra, event: string): EventFilterExtra {
  const out: EventFilterExtra = {};
  if (MAIL_EVENTS.has(event)) {
    const allow = extra.from?.allow ?? [];
    const deny = extra.from?.deny ?? [];
    if (allow.length || deny.length) out.from = { ...(allow.length ? { allow } : {}), ...(deny.length ? { deny } : {}) };
    if (extra.to_me) out.to_me = true;
    if (extra.cc_me) out.cc_me = true;
    if (extra.exclude_automated) out.exclude_automated = true;
    const subjects = Array.isArray(extra.subject_contains) ? extra.subject_contains : splitList(extra.subject_contains ?? "");
    if (subjects.length) out.subject_contains = subjects;
    if (extra.folder?.trim()) out.folder = extra.folder.trim();
  }
  if (TEAMS_EVENTS.has(event) && extra.exclude_authors?.length) out.exclude_authors = extra.exclude_authors;
  if (event === "new_teams_chat_message" && extra.chat_ids?.length) out.chat_ids = extra.chat_ids;
  if (event === "calendar_event_changed" && extra.change_kinds?.length) out.change_kinds = extra.change_kinds;
  if (event === "email_flag_changed" && extra.flag_status?.length) out.flag_status = extra.flag_status;
  // Microsoft To Do: set through the API; an edit of the trigger keeps them.
  if (event === "todo_task_changed") {
    if (extra.lists?.length) out.lists = extra.lists;
    if (extra.exclude_lists?.length) out.exclude_lists = extra.exclude_lists;
    if (extra.change_kinds?.length) out.change_kinds = extra.change_kinds;
    if (extra.exclude_self) out.exclude_self = true;
  }
  return out;
}
