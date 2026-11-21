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

import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";
// Scan (the trace link): the design system has no scan glyph (ICON_NAMES 1.5.0);
// kept until its design arbitration (audit table of xtm-one #10911, issue #10910).
import { Scan } from "lucide-react";
import type { ChatMessage as PanelMessage } from "@filigran/chatbot";
import { MessageAttributionLine } from "@/components/chat/MessageAttributionLine";
import { FileChip } from "@/components/chat-panel/FileChips";
import { ActionProposalList } from "@/components/ui/action-proposal";
import { Button } from "@/components/ui/button";
import { ChannelIcon } from "@/components/ui/channel-icon";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { CHANNEL_TYPE_LABELS, INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import { MessageFeedbackButtons } from "@/pages/chat/MessageFeedbackButtons";
import { ResumedTurnNotice } from "@/pages/chat/ResumedTurnNotice";
import { RunnerActivityBlock } from "@/pages/chat/RunnerActivity";
import { WakeTurnNotice } from "@/pages/chat/WakeTurnNotice";
import type { ChatMessage, UnavailableIntegration } from "@/types";

/**
 * What XTM One shows under a message that the panel does not: the channel a
 * message of a continued thread came from, and under a reply an @mentioned
 * agent that did not answer (the panel's own `mentionFailed` / `answeredBy`),
 * and, from the page's own copy of the conversation (`stored`, found by the
 * message's server id, none yet for a reply still streaming), that the platform
 * resumed the turn by itself after a cut, the commands the agent ran on a machine, the approvals nobody gave and the integrations it
 * could not use.
 */
export function MessageExtras({ message, stored }: { message: PanelMessage; stored: ChatMessage | undefined }) {
  const channel = stored?.channel && stored.channel !== "web" ? stored.channel : null;
  const deletedFiles = <DeletedAttachments stored={stored} />;
  if (message.role !== "assistant") {
    return (
      <>
        {deletedFiles}
        {channel && <ChannelMark channel={channel} />}
      </>
    );
  }
  const failedAgent = message.mentionFailed ? message.answeredBy?.agentName : undefined;
  const runnerActivity = stored?.runner_activity ?? [];
  return (
    <>
      {deletedFiles}
      {stored?.wake && <WakeTurnNotice note={stored.wake} variant="marker" />}
      {stored?.resumed_from && <ResumedTurnNotice note={stored.resumed_from} variant="marker" />}
      {failedAgent && (
        <MessageAttributionLine agentName={failedAgent} agentIcon={message.answeredBy?.agentIcon} failed />
      )}
      {runnerActivity.length > 0 && <RunnerActivityBlock records={runnerActivity} className="mb-2" />}
      {stored?.expired_approvals && stored.expired_approvals.length > 0 && (
        <div className="my-2 max-w-2xl">
          <ActionProposalList proposals={stored.expired_approvals} surface="chat" expired />
        </div>
      )}
      {stored?.unavailable_integrations && <UnavailableIntegrationsNotice items={stored.unavailable_integrations} />}
      {stored?.scheduled_outcome?.run_id && <ScheduledOutcomeAction runId={stored.scheduled_outcome.run_id} />}
      {channel && <ChannelMark channel={channel} />}
    </>
  );
}

/**
 * The files of a message whose content was deleted (#396), as the deleted card:
 * the panel is never handed them (the restore leaves them out of its
 * `attachments`, so it draws no live card, no download and no image fetch).
 * A file the text names with a `[[FILE:<id>]]` marker draws its card where the
 * marker stands (`useMarkerRenderers`), so it is not repeated here.
 */
export function DeletedAttachments({ stored }: { stored: ChatMessage | undefined }) {
  const content = stored?.content ?? "";
  const files = (stored?.attachments ?? []).filter(
    (att) => att.deleted_at && !content.toLowerCase().includes(`[[file:${(att.file_id ?? "").toLowerCase()}]]`),
  );
  if (files.length === 0) return null;
  return (
    <div className="mt-1.5 flex flex-wrap gap-2" data-testid="message-deleted-files">
      {files.map((att) => (
        <FileChip key={att.file_id ?? att.filename} att={att} />
      ))}
    </div>
  );
}

/** The run a scheduled one-off's outcome reports on, as the chat's own
 * action under the message rather than a link in its text (#11950): the
 * stored text keeps the absolute link for every surface outside the web app. */
function ScheduledOutcomeAction({ runId }: { runId: string }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="mt-1.5">
      <Button asChild variant="outline" size="sm" className="gap-1.5">
        <Link to={`/operations/executions/${runId}`} data-testid="scheduled-outcome-run">
          <Icon name="history" size={16} aria-hidden />
          {t_i18n("View the run")}
        </Link>
      </Button>
    </div>
  );
}

/** The channel a message of a continued Slack / Teams thread came from: its
 * icon, named on hover and keyboard focus. */
function ChannelMark({ channel }: { channel: string }) {
  const { t_i18n } = useFormatter();
  const label = t_i18n("In {channel}", { channel: CHANNEL_TYPE_LABELS[channel] ?? channel });
  return (
    <Tip content={label}>
      <span
        role="img"
        aria-label={label}
        tabIndex={0}
        className="mt-1 inline-flex rounded-sm opacity-60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
      >
        <ChannelIcon channel={channel} className="h-3 w-3" />
      </span>
    </Tip>
  );
}

/** The page's footer controls sit in the panel's own action row (copy, read
 * aloud): they take that row's gap and no padding of their own, so every
 * control of the row is spaced the same. */
export const FOOTER_ACTIONS_ROW = "inline-flex items-center gap-0.5";

/**
 * The page's own controls in a finished reply's footer: the rating with the
 * platform's feedback criteria, locked on a rating already given (the
 * page's copy, else the one the panel restored with an older page), and the
 * reply's trace for an admin (trace pages are admin-only).
 */
export function MessageFooterActions({
  message,
  stored,
  conversationId,
  isAdmin,
  canRate,
}: {
  message: PanelMessage;
  stored: ChatMessage | undefined;
  conversationId: string | null;
  isAdmin: boolean;
  canRate: boolean;
}) {
  const { t_i18n } = useFormatter();
  const messageId = message.serverId;
  // A change-of-agent note is no one's answer: nothing to rate or trace.
  if (!messageId || stored?.agent_switch) return null;
  return (
    <span className={FOOTER_ACTIONS_ROW}>
      {canRate && conversationId && (
        <MessageFeedbackButtons
          conversationId={conversationId}
          messageId={messageId}
          initialFeedback={stored?.user_feedback ?? message.feedback}
        />
      )}
      {isAdmin && stored?.llm_trace_id && (
        <Tip content={t_i18n("View trace")}>
          <Link
            to={`/observability/traces/${stored.llm_trace_id}`}
            aria-label={t_i18n("View trace")}
            className="text-default-disabled hover:text-accent focus-visible:text-accent transition-colors"
          >
            <Scan className="h-3 w-3" />
          </Link>
        </Tip>
      )}
    </span>
  );
}

/** The integrations a reply's turn did not use for its user, and why -
 * said under the reply, never a silent absence. One whose people each connect
 * their own account links to its page, where the user connects theirs. */
function UnavailableIntegrationsNotice({ items }: { items: UnavailableIntegration[] }) {
  const { t_i18n } = useFormatter();
  const nameOf = (item: UnavailableIntegration) =>
    item.name || INTEGRATION_PROVIDER_NAMES[item.provider] || item.provider;
  const ownAccount = items.filter((item) => item.reason === "own_account_not_connected");
  const labels = items
    .filter((item) => item.reason !== "own_account_not_connected")
    .map((item) => {
      const name = nameOf(item);
      return item.reason === "oauth_credentials_not_shared"
        ? t_i18n("{name} (its owner's connected account is not shared with you)", { name })
        : t_i18n("{name} (not shared with you)", { name });
    });
  if (labels.length === 0 && ownAccount.length === 0) return null;
  return (
    <div className="mt-1.5 space-y-1">
      {labels.length > 0 && (
        <p role="note" className="flex items-start gap-1.5 content-caption text-feedback-warning-primary dark:text-feedback-warning-primary">
          <Icon name="info" size={16} />
          <span>
            {t_i18n(
              "Integrations not used in this reply: {items}. Connect your own, or ask their owner to share them.",
              { items: labels.join(", ") },
            )}
          </span>
        </p>
      )}
      {ownAccount.length > 0 && (
        <p role="note" className="flex items-start gap-1.5 content-caption text-feedback-warning-primary dark:text-feedback-warning-primary">
          <Icon name="info" size={16} />
          <span>
            {t_i18n("Connect your own account to use these integrations:")}{" "}
            {ownAccount.map((item, index) => (
              <span key={item.integration_id ?? `${item.provider}-${index}`}>
                {index > 0 && ", "}
                {item.integration_id ? (
                  <Link to={`/integrations/${item.integration_id}`} className="underline underline-offset-2 hover:text-default-primary">
                    {nameOf(item)}
                  </Link>
                ) : (
                  nameOf(item)
                )}
              </span>
            ))}
          </span>
        </p>
      )}
    </div>
  );
}
