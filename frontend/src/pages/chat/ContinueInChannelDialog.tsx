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
 * ContinueInChannelDialog - continue a conversation in Slack or Teams.
 *
 * The bot opens a direct message with the user and the conversation goes on
 * there: one record, so its history, files and session tools follow it, and
 * each reply is written where the message was sent. Continuing is the owner's
 * (the menu entry is not offered to a shared viewer, and the endpoints answer
 * anyone else with a 404). Where a channel cannot take the conversation, the
 * row says why instead of offering a button that can only fail.
 */

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ChannelIcon } from "@/components/ui/channel-icon";
import { useToast } from "@/components/ui/toast";
import {
  useConversationChannelBindingMutations,
  useConversationChannelBindings,
} from "@/hooks/api/useChatApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { CHANNEL_TYPE_LABELS } from "@/lib/constants";
import type { ContinuationChannel, ContinuationChannelState } from "@/types";
import { Spinner } from "@filigran/design-system";

interface ContinueInChannelDialogProps {
  conversationId: string | null;
  open: boolean;
  onClose: () => void;
}

export function ContinueInChannelDialog({ conversationId, open, onClose }: ContinueInChannelDialogProps) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const state = useConversationChannelBindings(conversationId, { enabled: open });
  const { continueIn, stopIn } = useConversationChannelBindingMutations(conversationId);
  const aiOff = useAiActionDisabled();
  const pending = continueIn.isPending || stopIn.isPending;
  const bound = new Set(state.data?.bindings.map((b) => b.channel) ?? []);
  const error = continueIn.error?.message ?? stopIn.error?.message ?? null;

  const unavailableText = (channel: ContinuationChannelState): string | null => {
    const label = CHANNEL_TYPE_LABELS[channel.channel] ?? channel.channel;
    switch (channel.reason) {
      case "not_continuable":
        return t_i18n("This conversation is a group thread: continue it where it started.");
      case "not_configured":
        return t_i18n("No {channel} bot is connected. An administrator connects one in Settings > Integrations > Channels & Bots.", {
          channel: label,
        });
      case "direct_messages_off":
        return t_i18n("The {channel} bot does not answer direct messages. An administrator changes it in Settings > Integrations > Channels & Bots.", {
          channel: label,
        });
      case "agent_unavailable":
        return t_i18n("The agent of this conversation does not answer on {channel}.", { channel: label });
      case "teams_chat_unknown":
        return t_i18n("Send a message to the bot in Microsoft Teams once, then try again.");
      case "started_there":
        return t_i18n("This conversation is your chat with the bot in {channel}: continue it there.", {
          channel: label,
        });
      default:
        return channel.detail;
    }
  };

  const start = (channel: ContinuationChannel) => {
    continueIn.mutate(channel, {
      onSuccess: () =>
        toast({
          title: t_i18n("Continued in {channel}", { channel: CHANNEL_TYPE_LABELS[channel] ?? channel }),
          description:
            channel === "slack"
              ? t_i18n("The bot sent you a direct message. Reply in its thread to continue.")
              : t_i18n("The bot wrote in your chat with it. Reply there to continue."),
          variant: "success",
        }),
    });
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="text-base">{t_i18n("Continue in Slack or Teams")}</DialogTitle>
          <DialogDescription className="content-base leading-relaxed">
            {t_i18n(
              "Keep this conversation going in a direct message with the bot. It stays one conversation: its history, files and tools follow it, and each reply is written where you send your message.",
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-2">
          {state.isLoading && (
            <div className="flex items-center justify-center py-6">
              <Spinner size="sm" tone="inherit" className="text-default-secondary" label={t_i18n("Loading...")} />
            </div>
          )}
          {state.data?.channels.map((channel) => {
            const label = CHANNEL_TYPE_LABELS[channel.channel] ?? channel.channel;
            const isBound = bound.has(channel.channel);
            const reason = channel.available ? null : unavailableText(channel);
            return (
              <div
                key={channel.channel}
                className="flex items-center gap-3 rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default px-3 py-2.5 backdrop-blur-sm"
              >
                <ChannelIcon channel={channel.channel} className="h-5 w-5 shrink-0" />
                <div className="min-w-0 flex-1">
                  <p className="content-base font-medium">{label}</p>
                  <p className="content-compact text-default-secondary">
                    {isBound
                      ? t_i18n("Continued in your direct messages with the bot.")
                      : (reason ?? t_i18n("Not continued here yet."))}
                  </p>
                </div>
                {isBound ? (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-7 px-2 content-compact"
                    disabled={pending}
                    onClick={() => stopIn.mutate(channel.channel)}
                  >
                    {t_i18n("Stop")}
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    className="h-7 px-2.5 content-compact"
                    disabled={!channel.available || pending || aiOff}
                    onClick={() => start(channel.channel)}
                  >
                    {continueIn.isPending && continueIn.variables === channel.channel ? (
                      <Spinner size="sm" tone="inherit" label={t_i18n("Loading...")} />
                    ) : (
                      t_i18n("Continue in {channel}", { channel: label })
                    )}
                  </Button>
                )}
              </div>
            );
          })}
          {state.isError && (
            <p className="content-compact text-feedback-error-primary">{state.error.message}</p>
          )}
          {error && <p className="content-compact text-feedback-error-primary">{error}</p>}
        </div>

        <ul className="list-disc space-y-1 pl-5 content-compact text-default-secondary">
          <li>
            {t_i18n("Replies written in Slack or Teams use that app's formatting.")}
          </li>
          <li>
            {t_i18n(
              "In Microsoft Teams your personal chat with the bot continues this conversation until you send /new there.",
            )}
          </li>
          <li>{t_i18n("Tool calls waiting for your approval are answered here, in the web app.")}</li>
        </ul>
      </DialogContent>
    </Dialog>
  );
}
