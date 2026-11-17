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

import { Icon } from "@filigran/design-system";
/**
 * Where an open conversation also lives: the Slack or Teams thread it
 * started in, the direct messages it is continued in, or - for a group thread
 * - why the web does not continue it. Nothing for a web-only conversation.
 */

import { ChannelIcon } from "@/components/ui/channel-icon";
import { useFormatter } from "@/i18n";
import { CHANNEL_TYPE_LABELS } from "@/lib/constants";

interface ChannelContinuityBannerProps {
  /** The channel the conversation started on. */
  channel: string;
  webContinuable: boolean;
  continuedIn: string[];
}

export function ChannelContinuityBanner({ channel, webContinuable, continuedIn }: ChannelContinuityBannerProps) {
  const { t_i18n } = useFormatter();
  const label = CHANNEL_TYPE_LABELS[channel] ?? channel;

  if (!webContinuable) {
    return (
      <div className="flex items-center gap-2 border-b border-feedback-alert-primary/20 bg-feedback-alert-primary/5 px-4 py-1.5 content-compact">
        <Icon name="lock" size={16} className="shrink-0 text-feedback-warning-primary" />
        <span className="text-default-secondary">
          {t_i18n("This {channel} thread is shared with other people, so it is continued in {channel} only.", {
            channel: label,
          })}
        </span>
      </div>
    );
  }

  const others = continuedIn.filter((c) => c !== channel);
  if (channel === "web" && others.length === 0) return null;
  return (
    <div className="flex items-center gap-2 border-b border-accent/20 bg-accent/5 px-4 py-1.5 content-compact">
      <span className="flex shrink-0 items-center gap-1">
        {channel !== "web" && <ChannelIcon channel={channel} />}
        {others.map((c) => (
          <ChannelIcon key={c} channel={c} />
        ))}
      </span>
      <span className="text-default-secondary">
        {channel !== "web" &&
          t_i18n("Started in {channel}: continue it here or there, each reply is written where you send your message.", {
            channel: label,
          })}
        {channel !== "web" && others.length > 0 && " "}
        {others.length > 0 &&
          t_i18n("Also continued in {channels}.", {
            channels: others.map((c) => CHANNEL_TYPE_LABELS[c] ?? c).join(", "),
          })}
      </span>
    </div>
  );
}
