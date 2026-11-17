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
 * Profile > Agent Accounts: the accounts an agent operates for you -
 * your own account (a delegate) or a dedicated AI user - and their approval
 * queue. Each account opens on a page of its own with what it did.
 */
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { BarChart3, Bot, Pencil, Plus, RefreshCw, Trash2, UserCog, UserRound } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { SegmentedFilter } from "@/components/ui/segmented-filter";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import {
  useAgentAccountReplies,
  useAgentAccounts,
  useDeleteAgentAccount,
  useSyncAgentAccount,
  useUpdateAgentAccount,
} from "@/hooks/api/useAgentAccountApi";
import { useAuth } from "@/hooks/useAuth";
import { useFormatter } from "@/i18n";
import { formatTimeAgo } from "@/lib/relative-time";
import type { AgentAccount, AgentAccountReply } from "@/types";
import { AgentAccountEditor } from "./AgentAccountEditor";
import { AgentAccountReplyCard } from "./AgentAccountReplyCard";
import { AgentAccountSyncProblems } from "./AgentAccountSyncProblems";
import {
  MODE_DESCRIPTIONS,
  MODE_LABELS,
  RESPONSE_MODE_LABELS,
  STATUS_LABELS,
  TRANSPORT_LABELS,
  statusTone,
} from "./agent-accounts";
import { Spinner } from "@filigran/design-system";

type Scope = "visible" | "all";

export function AgentAccountsSection() {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const [scope, setScope] = useState<Scope>("visible");
  const { data: accounts, isLoading } = useAgentAccounts({ scope });
  const { data: pending } = useAgentAccountReplies({ status: "pending" });
  const [editing, setEditing] = useState<AgentAccount | null | "new">(null);

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2">
              <UserCog className="h-5 w-5 text-accent" />
              {t_i18n("Agent Accounts")}
            </CardTitle>
            <CardDescription>
              {t_i18n("Let an agent operate a real user account - not a bot - in chats or in a mailbox. As your delegate, it answers when you are mentioned, messaged or emailed, under your rules, and hands you what it cannot answer. As an AI user, a dedicated account everyone knows is an AI follows its conversations and replies like a colleague.")}
            </CardDescription>
          </div>
          <Button onClick={() => setEditing("new")} className="shrink-0">
            <Plus className="h-4 w-4" />
            {t_i18n("New account")}
          </Button>
        </CardHeader>
        {isAdmin && (
          <CardContent className="pt-0">
            <SegmentedFilter<Scope>
              options={[
                { key: "visible", label: "Mine" },
                { key: "all", label: "All accounts" },
              ]}
              value={scope}
              onChange={setScope}
              ariaLabel={t_i18n("Accounts shown")}
            />
          </CardContent>
        )}
      </Card>

      {(pending?.length ?? 0) > 0 && <PendingReplies replies={pending ?? []} />}

      {isLoading ? (
        <div className="flex justify-center py-10"><Spinner size="md" tone="inherit" className="text-default-secondary" label={t_i18n("Loading...")} /></div>
      ) : (accounts?.length ?? 0) === 0 ? (
        <Card>
          <CardContent className="grid gap-4 py-8 md:grid-cols-2">
            {(["on_behalf", "ai_user"] as const).map((mode) => {
              const Icon = mode === "ai_user" ? Bot : UserRound;
              return (
                <div key={mode} className="rounded-sm border border-dashed border-elevation-subtle p-4">
                  <div className="mb-2 flex items-center gap-2 font-semibold">
                    <Icon className="h-4 w-4 text-accent" />
                    {t_i18n(MODE_LABELS[mode])}
                  </div>
                  <p className="content-base text-default-secondary">{t_i18n(MODE_DESCRIPTIONS[mode])}</p>
                </div>
              );
            })}
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {(accounts ?? []).map((account) => (
            <AccountCard key={account.id} account={account} onEdit={() => setEditing(account)} />
          ))}
        </div>
      )}

      {editing && <AgentAccountEditor account={editing === "new" ? null : editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

function AccountCard({ account, onEdit }: { account: AgentAccount; onEdit: () => void }) {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const page = `/profile/agent-accounts/${account.id}`;
  const { toast } = useToast();
  const update = useUpdateAgentAccount();
  const sync = useSyncAgentAccount();
  const remove = useDeleteAgentAccount();
  const [confirmElement, confirm] = useConfirm();
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
    if (ok) await run(() => remove.mutateAsync(account.id), t_i18n("Agent account deleted"));
  };

  return (
    <Card>
      <CardContent className="space-y-3 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <Icon className="h-4 w-4 text-accent" />
              <Link to={page} className="font-semibold hover:text-accent hover:underline">{account.name}</Link>
              <Badge variant="cyan">{t_i18n(MODE_LABELS[account.mode])}</Badge>
              <Badge variant={statusTone(account.status)}>
                {t_i18n(STATUS_LABELS[account.status] ?? STATUS_LABELS.setup)}
              </Badge>
              {account.pending_count > 0 && (
                <Badge variant="warning">{t_i18n("{count} waiting for approval", { count: account.pending_count })}</Badge>
              )}
            </div>
            <div className="content-base text-default-secondary">
              {account.provider_label ?? account.provider}
              {account.account_display_name && ` - ${account.account_display_name}`}
              {account.account_email && ` (${account.account_email})`}
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-1 content-compact text-default-secondary">
              <span>{t_i18n("Agent: {name}", { name: account.agent_name ?? "-" })}</span>
              <span>{t_i18n(RESPONSE_MODE_LABELS[account.response_mode])}</span>
              <span>{t_i18n(TRANSPORT_LABELS[account.transport] ?? TRANSPORT_LABELS.none)}</span>
              <span>{t_i18n("{count} rules", { count: account.rules.length })}</span>
              {account.last_reply_at && <span>{t_i18n("Last reply {when}", { when: formatTimeAgo(intl, account.last_reply_at) })}</span>}
              {account.owner_name && !account.can_manage && <span>{account.owner_name}</span>}
            </div>
          </div>
          <div className="flex items-center gap-1">
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
            <Button variant="outline" size="sm" className="gap-1.5" onClick={() => navigate(page)}>
              <BarChart3 className="h-3.5 w-3.5" />
              {t_i18n("Activity")}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={sync.isPending}
              onClick={onSync}
              aria-label={t_i18n("Synchronize now")}
            >
              <RefreshCw className={sync.isPending ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
            </Button>
            <Button variant="ghost" size="sm" onClick={onEdit} aria-label={t_i18n("Edit")}>
              <Pencil className="h-4 w-4" />
            </Button>
            {account.can_manage && (
              <Button variant="ghost" size="sm" onClick={onDelete} aria-label={t_i18n("Delete")}>
                <Trash2 className="h-4 w-4" />
              </Button>
            )}
          </div>
        </div>
        <AgentAccountSyncProblems
          account={account}
          onEdit={onEdit}
          onSync={onSync}
          syncing={sync.isPending}
        />
        {account.last_error && (
          <p className="whitespace-pre-wrap break-words rounded-sm border border-feedback-error-primary/30 bg-feedback-error-primary/10 px-3 py-2 content-compact text-feedback-error-primary">
            {account.last_error}
          </p>
        )}
      </CardContent>
      {confirmElement}
    </Card>
  );
}

function PendingReplies({ replies }: { replies: AgentAccountReply[] }) {
  const { t_i18n } = useFormatter();
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t_i18n("Replies waiting for your approval")}</CardTitle>
        <CardDescription>
          {t_i18n("Send them as written, edit them first, or drop them. A reply nobody approves expires after a day.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {replies.map((reply) => <AgentAccountReplyCard key={reply.id} reply={reply} showAccount />)}
      </CardContent>
    </Card>
  );
}

