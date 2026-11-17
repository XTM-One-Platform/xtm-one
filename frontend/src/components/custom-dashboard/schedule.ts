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
 * A dashboard's scheduled exports: assignments on an agent (invariant 1) whose
 * run sends the dashboard with the `send_dashboard_export` tool, created
 * through the ordinary assignment API so every assignment rule applies.
 *
 * The assignment carries what it sends in its `trigger_config.dashboard_export`
 * (`{dashboard_id, format, recipients}`), so the dashboard can list its own
 * schedules, and its prompt says it in words for the agent. Pure:
 * `schedule.test.ts` runs it on `node --test`.
 */

export interface DashboardExportSchedule {
  dashboardId: string;
  title: string;
  cron: string;
  format: "pdf" | "png";
  recipients: string[];
  subject: string;
  message: string;
}

/** The tool a scheduled export's run calls; an agent offers it with its Dashboards built-in tools. */
const SEND_TOOL = "send_dashboard_export";
export const DASHBOARD_TOOLS = "custom_dashboards";

/** The most people one export reaches (`dashboard_export.MAX_RECIPIENTS`). */
export const MAX_RECIPIENTS = 10;

/** Addresses written one per line or separated by commas or spaces, lower-cased, each once. */
export function parseRecipients(text: string): string[] {
  const out: string[] = [];
  for (const part of text.split(/[\s,;]+/)) {
    const address = part.trim().toLowerCase();
    if (address && !out.includes(address)) out.push(address);
  }
  return out;
}

/** Whether `address` looks like an email address (the server checks it again, against the accounts). */
export const isEmailAddress = (address: string): boolean => /^[^@\s]{1,64}@[^@\s]{1,255}$/.test(address);

const quoted = (text: string) => JSON.stringify(text);

/** The assignment that sends the dashboard on `schedule.cron`, for `POST /assignments`. */
export function scheduleAssignment(agentId: string, schedule: DashboardExportSchedule) {
  const format = schedule.format.toUpperCase();
  const lines = [
    `Send the dashboard ${quoted(schedule.title)} (dashboard_id ${schedule.dashboardId}) as a ${format} with the ${SEND_TOOL} tool.`,
    `Recipients: ${schedule.recipients.join(", ")}.`,
  ];
  if (schedule.subject.trim()) lines.push(`Subject: ${quoted(schedule.subject.trim())}.`);
  if (schedule.message.trim()) lines.push(`Message: ${quoted(schedule.message.trim())}.`);
  lines.push("Then say in one line who received it and who did not.");
  return {
    agent_id: agentId,
    name: `Send the dashboard ${schedule.title}`.slice(0, 200),
    trigger_type: "schedule",
    trigger_config: {
      cron: schedule.cron,
      dashboard_export: { dashboard_id: schedule.dashboardId, format: schedule.format, recipients: schedule.recipients },
    },
    prompt: lines.join("\n"),
  };
}

/** What a stored assignment sends, when it is a scheduled export of `dashboardId`; `null` otherwise. */
export function exportOf(
  assignment: { trigger_type?: string; trigger_config?: unknown },
  dashboardId: string,
): { format: string; recipients: string[]; cron: string } | null {
  const config = assignment.trigger_config;
  if (assignment.trigger_type !== "schedule" || typeof config !== "object" || config === null) return null;
  const raw = config as Record<string, unknown>;
  const sent = raw.dashboard_export as Record<string, unknown> | undefined;
  if (!sent || typeof sent !== "object" || sent.dashboard_id !== dashboardId) return null;
  return {
    format: typeof sent.format === "string" ? sent.format : "pdf",
    recipients: Array.isArray(sent.recipients) ? sent.recipients.filter((r): r is string => typeof r === "string") : [],
    cron: typeof raw.cron === "string" ? raw.cron : "",
  };
}
