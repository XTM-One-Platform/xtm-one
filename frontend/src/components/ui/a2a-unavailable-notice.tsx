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

import { Handshake } from "lucide-react";
import { EditionBadge } from "@/components/ui/enterprise-upsell";
import { useAiEnabled, useChatSendEnabled, useIsXtmOne } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

/**
 * Notice for A2A surfaces while A2A is not offered — A2A follows agentic chat
 * (`chat_enabled`: AI on and Enterprise Edition). Renders nothing otherwise.
 * The A2A query hooks are disabled meanwhile, so every A2A card renders
 * itself away — this is the line that says why. The edition badge opens the
 * upgrade path when the reason is the licence.
 */
export function A2AUnavailableNotice({
  hint,
  className,
}: {
  /** What is kept and what is withheld on THIS surface, already translated. */
  hint: string;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const offered = useChatSendEnabled();
  const aiEnabled = useAiEnabled();
  const isXtmOne = useIsXtmOne();
  if (offered) return null;
  const reason = !aiEnabled
    ? t_i18n("Agent-to-Agent interoperability is unavailable while AI is disabled.")
    : isXtmOne
      ? t_i18n("Agent-to-Agent interoperability requires Enterprise Edition.")
      : t_i18n("Agent-to-Agent interoperability requires a valid license.");
  return (
    <div
      role="status"
      className={cn(
        "flex items-start gap-2 rounded-sm border border-feedback-alert-primary/30 bg-feedback-alert-primary/[0.07] px-3 py-2 text-left content-compact text-feedback-warning-tertiary",
        className,
      )}
    >
      <Handshake className="mt-0.5 h-3.5 w-3.5 shrink-0 text-feedback-warning-primary" />
      <span className="flex flex-wrap items-center gap-x-1.5 gap-y-1">
        <span>
          {reason} {hint}
        </span>
        {aiEnabled && isXtmOne && <EditionBadge className="content-caption" />}
      </span>
    </div>
  );
}
