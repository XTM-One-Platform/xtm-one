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
 * What the last sync of an agent account found: how much of the account
 * it hears, and for each cause that keeps it from hearing a conversation, what
 * it means, the conversations it affects, what to do - with the action at hand -
 * and the provider's own words behind a toggle.
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, ChevronDown, ChevronRight, Hash, Inbox, ListFilter, MessagesSquare, Plug, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { CountBadge } from "@/components/ui/count-badge";
import { Button } from "@/components/ui/button";
import { useTick } from "@/hooks/useTick";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { formatTimeAgoLine } from "@/lib/relative-time";
import type { AgentAccount, AgentAccountSyncProblem } from "@/types";
import {
  SYNC_PROBLEM_HINTS,
  SYNC_PROBLEM_TITLES,
  syncProblemAction,
  syncProblemCopy,
  syncProblemDetails,
  syncShortfall,
  syncTargetLabel,
  visibleTargets,
} from "./agent-accounts";

/** Conversations listed before "+N more". */
const TARGETS_SHOWN = 6;

type Formatter = ReturnType<typeof useFormatter>;

/** "Checked 5 minutes ago" in the viewer's language, the stored value as is when it is not a date. */
function checkedLine(checkedAt: string, t_i18n: Formatter["t_i18n"], intl: Formatter["intl"]): string {
  return formatTimeAgoLine(intl, Date.parse(checkedAt), {
    line: (when) => t_i18n("Checked {when}", { when }),
    justNow: () => t_i18n("Checked just now"),
    fallback: checkedAt,
  });
}

export function AgentAccountSyncProblems({ account, onEdit, onSync, syncing }: {
  account: AgentAccount;
  onEdit: () => void;
  onSync: () => void;
  syncing: boolean;
}) {
  const { t_i18n, intl } = useFormatter();
  useTick();
  const report = account.sync_report;
  if (!report || report.problems.length === 0) return null;
  const blocking = account.status === "error";
  const shortfall = syncShortfall(report);
  const provider = account.provider_label ?? account.provider;

  return (
    <section
      aria-label={t_i18n("Listening problems")}
      className={cn(
        "space-y-3 rounded-sm border p-3",
        blocking ? "border-feedback-error-primary/30 bg-feedback-error-primary/5" : "border-feedback-alert-primary/30 bg-feedback-alert-primary/5",
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 items-start gap-2">
          <AlertTriangle
            className={cn("mt-0.5 h-4 w-4 shrink-0", blocking ? "text-feedback-error-primary" : "text-feedback-warning-primary light:text-feedback-warning-primary")}
          />
          <div className="min-w-0">
            <p className="content-base font-medium">
              {shortfall
                ? t_i18n("Not listening to {missing} of {total} conversations", shortfall)
                : t_i18n("The last check found a problem")}
            </p>
            <p className="content-compact text-default-secondary">
              {report.checked_at && checkedLine(report.checked_at, t_i18n, intl)}
              {account.status === "setup" && ` - ${t_i18n("A new check is running.")}`}
            </p>
          </div>
        </div>
        <Button variant="outline" size="sm" className="h-7 gap-1.5 content-compact" disabled={syncing} onClick={onSync}>
          <RefreshCw className={cn("h-3 w-3", syncing && "animate-spin")} />
          {t_i18n("Synchronize now")}
        </Button>
      </div>
      <ul className="space-y-2">
        {report.problems.map((problem, index) => (
          <SyncProblem
            key={`${problem.kind}-${problem.permission ?? ""}-${problem.holder ?? ""}-${index}`}
            problem={problem}
            provider={provider}
            integrationId={account.integration_id}
            canManage={account.can_manage}
            onEdit={onEdit}
          />
        ))}
      </ul>
    </section>
  );
}

function SyncProblem({ problem, provider, integrationId, canManage, onEdit }: {
  problem: AgentAccountSyncProblem;
  provider: string;
  integrationId: string | null;
  canManage: boolean;
  onEdit: () => void;
}) {
  const { t_i18n } = useFormatter();
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [allTargets, setAllTargets] = useState(false);
  const copy = syncProblemCopy(problem, provider);
  const action = syncProblemAction(problem.kind);
  const { shown, more } = visibleTargets(problem.targets, allTargets ? problem.targets.length : TARGETS_SHOWN);
  const details = syncProblemDetails(problem, t_i18n);

  return (
    <li className="space-y-1.5 rounded-sm border border-elevation-subtle bg-elevation-default p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="content-base font-medium">{t_i18n(SYNC_PROBLEM_TITLES[copy.title], copy.values)}</span>
        {problem.count > 1 && <CountBadge variant="secondary">{problem.count}</CountBadge>}
      </div>
      <p className="content-compact leading-relaxed text-default-secondary">{t_i18n(SYNC_PROBLEM_HINTS[copy.hint], copy.values)}</p>
      {shown.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          {shown.map((target, index) => (
            <Badge key={`${target.covers}-${target.name ?? ""}-${index}`} variant="outline" className="gap-1 text-[11px] font-normal">
              {target.covers === "chats" ? (
                <MessagesSquare className="h-3 w-3" />
              ) : target.covers === "inbox" ? (
                <Inbox className="h-3 w-3" />
              ) : (
                <Hash className="h-3 w-3" />
              )}
              {syncTargetLabel(target, t_i18n)}
            </Badge>
          ))}
          {more > 0 && (
            <button type="button" onClick={() => setAllTargets(true)} className="text-[11px] text-accent hover:underline">
              {t_i18n("+{count} more", { count: more })}
            </button>
          )}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2 pt-0.5">
        {action === "integration" && integrationId && (
          <Button asChild variant="outline" size="sm" className="h-7 gap-1.5 content-compact">
            <Link to={`/integrations/${integrationId}`}>
              <Plug className="h-3 w-3" />
              {t_i18n("Open the integration")}
            </Link>
          </Button>
        )}
        {action === "rules" && canManage && (
          <Button variant="outline" size="sm" className="h-7 gap-1.5 content-compact" onClick={onEdit}>
            <ListFilter className="h-3 w-3" />
            {t_i18n("Edit the rules")}
          </Button>
        )}
        {details.length > 0 && (
          <button
            type="button"
            aria-expanded={detailsOpen}
            onClick={() => setDetailsOpen((open) => !open)}
            className="inline-flex items-center gap-1 text-[11px] text-default-secondary hover:text-default-primary"
          >
            {detailsOpen ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            {t_i18n("Technical details")}
          </button>
        )}
      </div>
      {detailsOpen && (
        // Words, not letters, wrap: the global `pre` rule breaks anywhere, which cuts localised notes mid-word.
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words [word-break:normal] rounded-sm bg-elevation-hover p-2 font-mono-plex text-[11px] text-default-secondary">
          {details.join("\n\n")}
        </pre>
      )}
    </li>
  );
}
