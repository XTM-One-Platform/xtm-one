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
 * Inbox - the person's notifications, the bell's feed (`GET /notifications`).
 *
 * What waits on the person comes first and is answered right here: a run
 * paused on tool approvals shows its proposals with the same approval list as
 * the web app (`ActionProposalList`), a question gets an answer box. Anything
 * else opens its page (in the companion when it offers it, else the "made for
 * a computer" screen). Read state is the server's: what is read here is read
 * on the computer too.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { AlarmClock, Bell, Bot, CheckCheck, CheckCircle2, Hand, ShieldAlert, Sparkles } from "lucide-react";

import { ActionProposalList } from "@/components/ui/action-proposal";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { PageSkeleton } from "@/components/ui/skeleton";
import {
  usePendingApprovals,
  useRespondToHumanInput,
  useSubmitApprovalDecisions,
} from "@/hooks/api/useAssignmentApi";
import { useDismissNotification, useMarkNotificationsRead, useNotifications } from "@/hooks/api/useNotificationApi";
import { useFormatter } from "@/i18n";
import { canMarkAllRead as anyReadable, decisionSource, isDecisionKind, notificationSubtitle, readsOnOpen } from "@/lib/notification-kinds";
import { cn, timeAgo } from "@/lib/utils";
import type { NotificationItem } from "@/types";

function KindIcon({ item }: { item: NotificationItem }) {
  const className = "h-4 w-4 shrink-0";
  if (isDecisionKind(item.kind) || item.kind === "run_action_request") return <Hand className={cn(className, "text-feedback-warning-primary")} aria-hidden="true" />;
  if (item.kind === "reminder") return <AlarmClock className={cn(className, "text-accent")} aria-hidden="true" />;
  if (item.kind === "agent_message") return <Bot className={cn(className, "text-filigran-ia-primary")} aria-hidden="true" />;
  if (item.kind === "builtin_tools_outside_allowlist") return <Sparkles className={cn(className, "text-accent")} aria-hidden="true" />;
  if (["auth_failure", "stale_reference", "continuation_refused", "security_audit", "api_key"].includes(item.kind)) {
    return <ShieldAlert className={cn(className, item.critical ? "text-feedback-error-primary" : "text-feedback-warning-primary")} aria-hidden="true" />;
  }
  return <CheckCircle2 className={cn(className, "text-feedback-success-primary")} aria-hidden="true" />;
}

/** What a waiting run asks: its tool calls to decide, or its question to answer. */
function WaitingRun({ runId }: { runId: string }) {
  const { t_i18n } = useFormatter();
  const pending = usePendingApprovals(runId);
  const decide = useSubmitApprovalDecisions();
  const answer = useRespondToHumanInput();
  const [text, setText] = useState("");

  if (pending.isLoading) return null;
  if (pending.isError || !pending.data) {
    return <p className="content-caption text-default-secondary">{t_i18n("Already answered, or not yours to answer.")}</p>;
  }
  if (pending.data.kind === "action_proposal") {
    return (
      <ActionProposalList
        proposals={pending.data.proposals}
        isSubmitting={decide.isPending}
        submitFailed={decide.isError}
        onSubmit={(decisions) => decide.mutate({ runId, decisions })}
      />
    );
  }
  return (
    <form
      className="flex flex-col gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (text.trim()) answer.mutate({ runId, response: text.trim() });
      }}
    >
      <label htmlFor={`answer-${runId}`} className="sr-only">
        {t_i18n("Your answer")}
      </label>
      <textarea
        id={`answer-${runId}`}
        value={text}
        onChange={(event) => setText(event.target.value)}
        rows={3}
        className="w-full rounded-sm border border-elevation-subtle layer-2 bg-elevation-default p-2 content-compact text-default-primary"
        placeholder={t_i18n("Your answer")}
      />
      <Button type="submit" size="sm" className="min-h-9 self-start" disabled={!text.trim() || answer.isPending || answer.isSuccess}>
        {answer.isSuccess ? t_i18n("Sent") : t_i18n("Send")}
      </Button>
    </form>
  );
}

export function CompanionInbox() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { data: feed, isLoading } = useNotifications();
  const markRead = useMarkNotificationsRead();
  const dismiss = useDismissNotification();

  if (isLoading) return <PageSkeleton cards={3} />;
  const items = feed?.items ?? [];
  // A decision still waiting stays unread until answered; opening another entry reads it (#991).
  const canMarkAllRead = anyReadable(items);

  return (
    <div className="flex flex-col gap-4">
      <header className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold text-default-primary">{t_i18n("Inbox")}</h1>
        {canMarkAllRead && (
          <Button variant="ghost" size="sm" className="min-h-9 gap-1.5" onClick={() => markRead.mutate()}>
            <CheckCheck className="h-4 w-4" /> {t_i18n("Mark all read")}
          </Button>
        )}
      </header>
      {items.length === 0 ? (
        <Card className="flex flex-col items-center gap-2 px-6 py-10 text-center">
          <Bell className="h-8 w-8 text-default-secondary" aria-hidden="true" />
          <p className="content-base text-default-primary">{t_i18n("No notifications yet")}</p>
          <p className="content-compact text-default-secondary">
            {t_i18n("Approvals, reminders and what your agents send you will appear here.")}
          </p>
        </Card>
      ) : (
        <ul className="flex flex-col gap-3">
          {items.map((item) => {
            // A run decision is answered in place; a flow or work item one opens its page (#842, S4).
            const waiting = decisionSource(item) === "run" && item.run_id;
            return (
              <li key={item.id}>
                <Card
                  className={cn(
                    "flex flex-col gap-3 p-4",
                    waiting && "border-alert-warning bg-alert-warning",
                    !waiting && item.unread && "border-accent/30",
                  )}
                >
                  <button
                    type="button"
                    className="flex w-full items-start gap-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                    onClick={() => {
                      if (readsOnOpen(item)) dismiss.mutate(item.id);
                      navigate(item.route);
                    }}
                  >
                    <KindIcon item={item} />
                    <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                      <span className="flex items-baseline justify-between gap-2">
                        <span className={cn("truncate content-compact", item.unread ? "font-semibold text-default-primary" : "text-default-secondary")}>
                          {item.title}
                        </span>
                        <span className="shrink-0 content-caption text-default-secondary">{timeAgo(item.created_at)}</span>
                      </span>
                      {notificationSubtitle(item, t_i18n) && (
                        <span className="content-caption text-default-secondary">{notificationSubtitle(item, t_i18n)}</span>
                      )}
                      {item.summary && <span className="line-clamp-3 content-compact text-default-primary">{item.summary}</span>}
                    </span>
                  </button>
                  {waiting && item.run_id && <WaitingRun runId={item.run_id} />}
                </Card>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
