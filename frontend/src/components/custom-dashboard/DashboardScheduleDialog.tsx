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
 * A dashboard's scheduled exports (a scheduled PDF
 * delivery): each an assignment on one of your agents (`schedule.ts`),
 * whose run emails the dashboard as a PDF or a PNG to people with an account
 * who may open it - each receiving it rendered with their own access. The
 * dialog lists this dashboard's schedules (pause, send now, delete) and adds
 * one: the agent (it needs its Dashboards built-in tools), when (UTC), the
 * format, the recipients, a subject and a message. What starts or stops a run
 * waits behind the AI gate (`AiActionGate`): with AI off, an assignment runs nothing.
 */
import { useMemo, useState } from "react";
import { Spinner } from "@filigran/design-system";
import { CalendarClock, Play, Plus, Trash2 } from "lucide-react";

import { AiActionGate } from "@/components/ui/ai-disabled";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { TextAreaField } from "@/components/video-studio/editor/fields";
import { useAgents } from "@/hooks/api/useAgentApi";
import {
  useAssignments,
  useCreateAssignment,
  useDeleteAssignment,
  useTriggerAssignment,
  useUpdateAssignment,
} from "@/hooks/api/useAssignmentApi";
import { useFormatter } from "@/i18n";
import { humanizeCron } from "@/lib/cron";
import type { CreateAssignmentRequest, CustomDashboard } from "@/types";

import {
  DASHBOARD_TOOLS,
  MAX_RECIPIENTS,
  exportOf,
  isEmailAddress,
  parseRecipients,
  scheduleAssignment,
} from "./schedule";

/** When a dashboard is sent (UTC), or a cron expression written. @i18n-keys */
const WHEN_LABELS = {
  "0 8 * * *": "Every day at 8:00",
  "0 8 * * 1-5": "Weekdays at 8:00",
  "0 8 * * 1": "Mondays at 8:00",
  "0 8 1 * *": "The first day of each month at 8:00",
  custom: "Custom (cron)",
} as const;
type When = keyof typeof WHEN_LABELS;

function Labelled({
  label,
  children,
  hint,
}: {
  label: string;
  children: React.ReactNode;
  hint?: string;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="content-caption text-default-secondary">{label}</span>
      {children}
      {hint && (
        <span className="content-caption text-default-secondary">{hint}</span>
      )}
    </div>
  );
}

export function DashboardScheduleDialog({
  dashboard,
  open,
  onOpenChange,
}: {
  dashboard: CustomDashboard;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const assignments = useAssignments();
  const agents = useAgents();
  const create = useCreateAssignment();
  const update = useUpdateAssignment();
  const remove = useDeleteAssignment();
  const trigger = useTriggerAssignment();
  const [agentId, setAgentId] = useState<string | null>(null);
  const [when, setWhen] = useState<When>("0 8 * * 1-5");
  const [cron, setCron] = useState("0 8 * * 1-5");
  const [format, setFormat] = useState<"pdf" | "png">("pdf");
  const [recipientsText, setRecipientsText] = useState("");
  const [subject, setSubject] = useState(dashboard.title);
  const [message, setMessage] = useState("");

  const schedules = useMemo(
    () =>
      (assignments.data ?? []).flatMap((a) => {
        const sent = exportOf(a, dashboard.id);
        return sent ? [{ assignment: a, sent }] : [];
      }),
    [assignments.data, dashboard.id],
  );
  const agentNames = useMemo(
    () => new Map((agents.data ?? []).map((a) => [a.id, a.name])),
    [agents.data],
  );
  const agentOptions = (agents.data ?? [])
    .filter((agent) => agent.can_manage !== false)
    .map((agent) => {
      const ready = agent.builtin_tool_ids?.includes(DASHBOARD_TOOLS);
      return {
        value: agent.id,
        label: agent.name,
        disabled: !ready,
        description: ready
          ? undefined
          : t_i18n("Turn on its Dashboards built-in tools first"),
      };
    });
  const recipients = parseRecipients(recipientsText);
  const invalid = recipients.filter((address) => !isEmailAddress(address));
  const chosenCron = when === "custom" ? cron.trim() : when;
  const ready =
    !!agentId &&
    !!chosenCron &&
    recipients.length > 0 &&
    recipients.length <= MAX_RECIPIENTS &&
    invalid.length === 0;

  const add = () => {
    if (!agentId) return;
    const body = scheduleAssignment(agentId, {
      dashboardId: dashboard.id,
      title: dashboard.title,
      cron: chosenCron,
      format,
      recipients,
      subject,
      message,
    }) as unknown as CreateAssignmentRequest;
    create.mutate(body, {
      onSuccess: () => {
        toast({ title: t_i18n("The export is scheduled"), variant: "success" });
        setRecipientsText("");
        setMessage("");
      },
      onError: (error) =>
        toast({
          title: t_i18n("The export could not be scheduled"),
          description: error.message,
          variant: "error",
        }),
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="flex max-h-[90dvh] max-w-2xl flex-col gap-4 overflow-y-auto"
        data-testid="dashboard.schedule.dialog"
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <CalendarClock className="h-4 w-4 text-accent" aria-hidden="true" />
            {t_i18n("Schedule an export")}
          </DialogTitle>
          <DialogDescription>
            {t_i18n(
              "One of your agents emails this dashboard as a PDF or a PNG on a schedule. Recipients are people with an account who can open it; each receives it as they may read it.",
            )}
          </DialogDescription>
        </DialogHeader>

        <section
          className="flex flex-col gap-2"
          aria-label={t_i18n("Scheduled exports")}
        >
          {schedules.length === 0 && !assignments.isPending && (
            <p className="content-caption text-default-secondary">
              {t_i18n("This dashboard is not sent on a schedule yet.")}
            </p>
          )}
          {schedules.map(({ assignment, sent }) => (
            <div
              key={assignment.id}
              className="flex items-center gap-3 rounded-sm border border-elevation-subtle px-3 py-2"
              data-testid="dashboard.schedule.row"
            >
              <div className="min-w-0 flex-1">
                <p className="content-compact font-medium text-default-primary">
                  {humanizeCron(sent.cron)} (UTC) · {sent.format.toUpperCase()}
                </p>
                <p className="truncate content-caption text-default-secondary">
                  {t_i18n("{agent} sends it to {recipients}", {
                    agent:
                      agentNames.get(assignment.agent_id) ?? t_i18n("An agent"),
                    recipients: sent.recipients.join(", "),
                  })}
                </p>
              </div>
              <AiActionGate>
                <Switch
                  checked={assignment.enabled}
                  aria-label={t_i18n("Scheduled")}
                  onCheckedChange={(enabled) =>
                    update.mutate({ id: assignment.id, enabled })
                  }
                />
              </AiActionGate>
              <AiActionGate>
                <Button
                  variant="ghost"
                  size="sm"
                  className="gap-1.5"
                  disabled={trigger.isPending}
                  onClick={() =>
                    trigger.mutate(
                      { assignmentId: assignment.id },
                      {
                        onSuccess: () =>
                          toast({
                            title: t_i18n("Sending now"),
                            variant: "success",
                          }),
                        onError: (error) =>
                          toast({
                            title: t_i18n("The export could not be sent"),
                            description: error.message,
                            variant: "error",
                          }),
                      },
                    )
                  }
                >
                  <Play className="h-3.5 w-3.5" aria-hidden="true" />
                  {t_i18n("Send now")}
                </Button>
              </AiActionGate>
              <AiActionGate>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8"
                  aria-label={t_i18n("Delete the schedule")}
                  onClick={async () => {
                    if (
                      await confirm({
                        title: t_i18n("Delete this schedule?"),
                        description: t_i18n(
                          "The dashboard will no longer be sent.",
                        ),
                      })
                    ) {
                      remove.mutate(assignment.id);
                    }
                  }}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </AiActionGate>
            </div>
          ))}
        </section>

        <section
          className="flex flex-col gap-3 border-t border-elevation-subtle-soft pt-4"
          aria-label={t_i18n("New schedule")}
        >
          <Labelled
            label={t_i18n("Agent")}
            hint={
              agentOptions.length === 0
                ? t_i18n("You have no agent to send it with.")
                : undefined
            }
          >
            <SearchableSelect
              aria-label={t_i18n("Agent")}
              value={agentId}
              onValueChange={setAgentId}
              options={agentOptions}
              searchable="auto"
            />
          </Labelled>
          <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
            <Labelled label={t_i18n("When (UTC)")}>
              <SearchableSelect
                aria-label={t_i18n("When (UTC)")}
                value={when}
                onValueChange={(value) => setWhen(value as When)}
                options={(Object.keys(WHEN_LABELS) as When[]).map((key) => ({
                  value: key,
                  label: t_i18n(WHEN_LABELS[key]),
                }))}
                sort={false}
                searchable={false}
              />
            </Labelled>
            <Labelled label={t_i18n("Format")}>
              <SegmentedFilter
                ariaLabel={t_i18n("Format")}
                value={format}
                onChange={setFormat}
                options={[
                  { key: "pdf", label: "PDF" },
                  { key: "png", label: "PNG" },
                ]}
              />
            </Labelled>
          </div>
          {when === "custom" && (
            <Input
              label={t_i18n("Cron expression")}
              value={cron}
              onChange={(event) => setCron(event.target.value)}
              className="font-mono-plex"
              maxLength={100}
            />
          )}
          <TextAreaField
            label={t_i18n("Recipients")}
            hint={
              invalid.length
                ? t_i18n("Not an email address: {addresses}", {
                    addresses: invalid.join(", "),
                  })
                : t_i18n(
                    "Up to {count} email addresses of people with an account.",
                    { count: MAX_RECIPIENTS },
                  )
            }
            value={recipientsText}
            onChange={(value) => setRecipientsText(value ?? "")}
            rows={2}
            maxLength={2000}
            placeholder="ann@example.com, bob@example.com"
          />
          <Input
            label={t_i18n("Subject")}
            value={subject}
            onChange={(event) => setSubject(event.target.value)}
            maxLength={200}
          />
          <TextAreaField
            label={t_i18n("Message")}
            value={message}
            onChange={(value) => setMessage(value ?? "")}
            rows={3}
            maxLength={2000}
          />
          <div className="self-end">
            <AiActionGate>
              <Button
                className="w-fit gap-1.5"
                disabled={!ready || create.isPending}
                aria-busy={create.isPending || undefined}
                onClick={add}
              >
                {create.isPending && <Spinner size="sm" tone="inherit" />}
                {!create.isPending && <Plus className="h-4 w-4" />}
                {t_i18n("Schedule")}
              </Button>
            </AiActionGate>
          </div>
        </section>
        {ConfirmEl}
      </DialogContent>
    </Dialog>
  );
}
