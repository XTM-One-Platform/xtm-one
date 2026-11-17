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
 * One message an agent account sent: the message it
 * answers - or, for a message the agent sent on its own, "Proactive send" with
 * its recipients and what produced it - what the agent wrote, what became of
 * it, and, while it waits for its owner, the draft to send as written, edit
 * first, or drop.
 */
import { useState } from "react";
import { Check, ThumbsDown, ThumbsUp, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useToast } from "@/components/ui/toast";
import {
  useAgentAccountProvider,
  useApproveAgentAccountReply,
  useRejectAgentAccountReply,
} from "@/hooks/api/useAgentAccountApi";
import { useFormatter } from "@/i18n";
import { formatShortDateTimeIntl, formatTimeAgo } from "@/lib/relative-time";
import type { AgentAccountReply } from "@/types";
import { OUTCOME_LABELS, outcomeTone, wording } from "./agent-accounts";
import { SendOriginLinks } from "./SendOriginLinks";
import { Spinner } from "@filigran/design-system";

export function AgentAccountReplyCard({ reply, showAccount = false, canAct = true }: {
  reply: AgentAccountReply;
  /** The account's name in the header, for a list mixing accounts. */
  showAccount?: boolean;
  /** Whether the viewer may send or drop a draft. */
  canAct?: boolean;
}) {
  const { t_i18n, intl } = useFormatter();
  const { toast } = useToast();
  const approve = useApproveAgentAccountReply();
  const reject = useRejectAgentAccountReply();
  const kinds = wording(useAgentAccountProvider(reply.provider)?.kind).kinds;
  const [text, setText] = useState(reply.draft_text ?? "");
  const edited = text !== (reply.draft_text ?? "");
  const draft = canAct && (reply.status === "pending" || reply.status === "shadow");
  const written = reply.sent_text ?? reply.draft_text;
  const proactive = reply.kind === "proactive";
  const recipients = (reply.origin?.recipients ?? []).join(", ");

  const act = async (action: () => Promise<unknown>, done: string) => {
    try {
      await action();
      toast({ title: done, variant: "success" });
    } catch (error) {
      toast({ title: t_i18n("The action failed"), description: (error as Error).message, variant: "error" });
    }
  };

  return (
    <div className="space-y-2 rounded-sm border border-elevation-subtle p-3">
      <div className="flex flex-wrap items-center gap-2 content-compact text-default-secondary">
        <Badge variant={outcomeTone(reply.status)}>{t_i18n(OUTCOME_LABELS[reply.status] ?? OUTCOME_LABELS.failed)}</Badge>
        {proactive && <Badge variant="outline" className="whitespace-nowrap content-caption">{t_i18n("Proactive send")}</Badge>}
        {showAccount && <span className="font-medium text-default-primary">{reply.account_name}</span>}
        <span>{t_i18n(kinds[reply.conversation_type] ?? kinds.direct)}</span>
        {reply.conversation_name && <span className="text-default-primary">{reply.conversation_name}</span>}
        <Tip content={formatShortDateTimeIntl(intl, reply.created_at)}>
          <span>{formatTimeAgo(intl, reply.created_at)}</span>
        </Tip>
        {reply.rule_name && <Badge variant="secondary" className="content-caption">{reply.rule_name}</Badge>}
        {reply.decided_at && reply.status === "sent" && (
          <span>{t_i18n("Approved {when}", { when: formatTimeAgo(intl, reply.decided_at) })}</span>
        )}
        {(reply.rating_positive > 0 || reply.rating_negative > 0) && (
          <Tip content={t_i18n("Ratings of this answer, reactions included")}>
            <span className="ml-auto inline-flex items-center gap-2">
              <span className="inline-flex items-center gap-0.5"><ThumbsUp className="h-3 w-3" />{reply.rating_positive}</span>
              <span className="inline-flex items-center gap-0.5"><ThumbsDown className="h-3 w-3" />{reply.rating_negative}</span>
            </span>
          </Tip>
        )}
      </div>
      {proactive ? (
        <p className="content-compact text-default-secondary">
          {t_i18n("Sent by the agent on its own, not in answer to a message.")}
          {recipients && <> {t_i18n("To: {recipients}", { recipients })}</>}
          <SendOriginLinks origin={reply.origin} className="ml-2 inline-flex flex-wrap gap-x-3 gap-y-1" />
        </p>
      ) : (
        <blockquote className="border-l-2 border-elevation-subtle pl-3 content-base">
          <span className="font-medium">{reply.sender_name ?? t_i18n("Someone")}: </span>
          <span className="whitespace-pre-wrap text-default-secondary">{reply.inbound_text}</span>
        </blockquote>
      )}
      {draft ? (
        <>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={Math.min(8, Math.max(3, text.split("\n").length + 1))}
            className="w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base text-default-primary focus:border-accent focus:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
            aria-label={t_i18n("Reply")}
          />
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="content-compact text-default-secondary">
              {reply.ai_label ? t_i18n("The AI label is added when it is sent.") : t_i18n("Sent without a label.")}
            </span>
            <div className="flex gap-2">
              {reply.status === "pending" && (
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={reject.isPending}
                  onClick={() => act(() => reject.mutateAsync(reply.id), t_i18n("Reply dropped"))}
                >
                  <X className="h-3.5 w-3.5" />
                  {t_i18n("Drop")}
                </Button>
              )}
              <Button
                size="sm"
                disabled={approve.isPending || !text.trim()}
                onClick={() =>
                  act(() => approve.mutateAsync({ id: reply.id, text: edited ? text : undefined }), t_i18n("Reply sent"))
                } aria-busy={approve.isPending || undefined}>
                {approve.isPending && <Spinner size="sm" tone="inherit" />}{!approve.isPending && <Check className="h-3.5 w-3.5" />}
                {edited ? t_i18n("Send edited") : t_i18n("Send")}
              </Button>
            </div>
          </div>
        </>
      ) : (
        written && (
          <div className="rounded-sm bg-elevation-hover px-3 py-2 content-base">
            <div className="mb-1 text-[11px] font-medium uppercase tracking-wider text-default-secondary">
              {reply.status === "sent" ? t_i18n("Posted") : t_i18n("Drafted")}
            </div>
            <p className="whitespace-pre-wrap">{written}</p>
          </div>
        )
      )}
      {reply.error && <p className="whitespace-pre-wrap break-words content-compact text-feedback-error-primary">{reply.error}</p>}
    </div>
  );
}
