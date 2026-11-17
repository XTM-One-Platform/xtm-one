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
 * Profile > Agent Accounts > one account: what the account is, how it
 * listens, and what it did - an overview with its analytics, every decision it
 * took and every reply it wrote, on a page of their own.
 */
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { BarChart3, Bell, BellOff, Bot, ListChecks, MessageSquareReply, Pencil, RefreshCw, Trash2, UserRound } from "lucide-react";
import { EntityPageHeader } from "@/components/ui/entity-page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { DetailTabs } from "@/components/ui/detail-tabs";
import { NotFoundState } from "@/components/ui/not-found-state";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { useToast } from "@/components/ui/toast";
import {
  useAgentAccount,
  useDeleteAgentAccount,
  useSyncAgentAccount,
  useUpdateAgentAccount,
} from "@/hooks/api/useAgentAccountApi";
import { useTabParam } from "@/hooks/useTabParam";
import { useFormatter } from "@/i18n";
import { formatTimeAgo } from "@/lib/relative-time";
import { AgentAccountDecisionsTab } from "./AgentAccountDecisionsTab";
import { AgentAccountEditor } from "./AgentAccountEditor";
import { AgentAccountOverview } from "./AgentAccountOverview";
import { AgentAccountMessagesSentTab } from "./AgentAccountMessagesSentTab";
import { AgentAccountSyncProblems } from "./AgentAccountSyncProblems";
import { MODE_LABELS, RESPONSE_MODE_LABELS, STATUS_LABELS, TRANSPORT_LABELS, statusTone } from "./agent-accounts";
import { Spinner } from "@filigran/design-system";

type Tab = "overview" | "decisions" | "sent";

/** The tab an older link names (`/replies`), and the tab it opens now. */
const RENAMED_TABS: Record<string, Tab> = { replies: "sent" };

const LIST_PATH = "/profile/agent-accounts";

export default function AgentAccountPage() {
  const { t_i18n, intl } = useFormatter();
  const { accountId } = useParams<{ accountId: string }>();
  const navigate = useNavigate();
  const { toast } = useToast();
  const [rawTab] = useTabParam<Tab>("overview");
  const tab: Tab = RENAMED_TABS[rawTab] ?? rawTab;
  const { data: account, isLoading, isError } = useAgentAccount(accountId);
  const update = useUpdateAgentAccount();
  const sync = useSyncAgentAccount();
  const remove = useDeleteAgentAccount();
  const [confirmElement, confirm] = useConfirm();
  const [editing, setEditing] = useState(false);

  if (isLoading) {
    return <div className="flex justify-center py-16"><Spinner size="lg" tone="inherit" className="text-default-secondary" label={t_i18n("Loading...")} /></div>;
  }
  if (isError || !account) {
    return (
      <NotFoundState
        icon={Bot}
        title="Agent account not found"
        description="It was deleted, or it is not one of yours."
        actionLabel="Back to Agent Accounts"
        onAction={() => navigate(LIST_PATH)}
        backTo={LIST_PATH}
        backLabel="Back to Agent Accounts"
      />
    );
  }

  const Icon = account.mode === "ai_user" ? Bot : UserRound;
  const run = async (action: () => Promise<unknown>, done: string) => {
    try {
      await action();
      toast({ title: done, variant: "success" });
    } catch (error) {
      toast({ title: t_i18n("The action failed"), description: (error as Error).message, variant: "error" });
    }
  };
  const onSync = () => run(() => sync.mutateAsync(account.id), t_i18n("Synchronization started"));
  const onDelete = async () => {
    const ok = await confirm({
      title: t_i18n("Delete {name}?", { name: account.name }),
      description: t_i18n("The agent stops listening to the account. Its pending replies are dropped."),
      confirmLabel: t_i18n("Delete"),
      variant: "danger",
    });
    if (!ok) return;
    await run(() => remove.mutateAsync(account.id), t_i18n("Agent account deleted"));
    navigate(LIST_PATH);
  };

  return (
    <div className="space-y-6">
      <EntityPageHeader
        backTo={LIST_PATH}
        backLabel="Back to Agent Accounts"
        icon={Icon}
        muted={!account.enabled}
        title={account.name}
        titleClassName="font-sans-geologica"
        badges={
          <>
                <Badge variant="cyan">{t_i18n(MODE_LABELS[account.mode])}</Badge>
                <Badge variant={statusTone(account.status)}>
                  {t_i18n(STATUS_LABELS[account.status] ?? STATUS_LABELS.setup)}
                </Badge>
                {account.pending_count > 0 && (
                  <Badge variant="warning">{t_i18n("{count} waiting for approval", { count: account.pending_count })}</Badge>
                )}
          </>
        }
        description={[
          account.provider_label ?? account.provider,
          account.account_display_name && ` - ${account.account_display_name}`,
          account.account_email && ` (${account.account_email})`,
        ].filter(Boolean).join("")}
        meta={
          <>
                <span>{t_i18n("Agent: {name}", { name: account.agent_name ?? "-" })}</span>
                <span>{t_i18n(RESPONSE_MODE_LABELS[account.response_mode])}</span>
                <span>{t_i18n(TRANSPORT_LABELS[account.transport] ?? TRANSPORT_LABELS.none)}</span>
                <span>{t_i18n("{count} rules", { count: account.rules.length })}</span>
                {account.last_reply_at && <span>{t_i18n("Last reply {when}", { when: formatTimeAgo(intl, account.last_reply_at) })}</span>}
                <Tip
                  content={
                    account.notify_on_send
                      ? t_i18n("You are notified of each reply the account posts on its own.")
                      : t_i18n("You are not notified of the replies the account posts on its own.")
                  }
                >
                  <span className="inline-flex items-center gap-1">
                    {account.notify_on_send ? <Bell className="h-3 w-3" /> : <BellOff className="h-3 w-3" />}
                    {account.notify_on_send ? t_i18n("Notifies you") : t_i18n("No notifications")}
                  </span>
                </Tip>
                {account.owner_name && !account.can_manage && <span>{account.owner_name}</span>}
          </>
        }
        actions={
          <>
              <Switch
                checked={account.enabled}
                disabled={!account.can_manage || update.isPending}
                onCheckedChange={(enabled) =>
                  run(
                    () => update.mutateAsync({ id: account.id, enabled }),
                    enabled ? t_i18n("Agent account enabled") : t_i18n("Agent account paused"),
                  )
                }
                aria-label={t_i18n("Enabled")}
              />
              <Button variant="ghost" size="sm" disabled={sync.isPending} onClick={onSync} aria-label={t_i18n("Synchronize now")}>
                <RefreshCw className={sync.isPending ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
              </Button>
              <Button variant="outline" size="sm" className="gap-1.5" onClick={() => setEditing(true)}>
                <Pencil className="h-3.5 w-3.5" />
                {t_i18n("Edit")}
              </Button>
              {account.can_manage && (
                <Button variant="ghost" size="sm" onClick={onDelete} aria-label={t_i18n("Delete")}>
                  <Trash2 className="h-4 w-4" />
                </Button>
              )}
          </>
        }
      />
      <AgentAccountSyncProblems account={account} onEdit={() => setEditing(true)} onSync={onSync} syncing={sync.isPending} />
      {account.last_error && (
        <p className="whitespace-pre-wrap break-words rounded-sm border border-feedback-error-primary/30 bg-feedback-error-primary/10 px-3 py-2 content-compact text-feedback-error-primary">
          {account.last_error}
        </p>
      )}

      <DetailTabs<Tab>
        tabs={[
          { key: "overview", label: "Overview", icon: BarChart3 },
          { key: "decisions", label: "Decisions", icon: ListChecks },
          { key: "sent", label: "Messages sent", icon: MessageSquareReply },
        ]}
        active={tab}
      />

      {tab === "overview" && <AgentAccountOverview account={account} />}
      {tab === "decisions" && <AgentAccountDecisionsTab account={account} />}
      {tab === "sent" && <AgentAccountMessagesSentTab account={account} />}

      {editing && <AgentAccountEditor account={account} onClose={() => setEditing(false)} />}
      {confirmElement}
    </div>
  );
}
