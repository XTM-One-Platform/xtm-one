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

import { IconButton } from "@filigran/design-system";
import { Phone, Search } from "lucide-react";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { DesktopMachineBar } from "@/pages/chat/DesktopMachineBar";
import { RunnerApprovalCard } from "@/pages/chat/RunnerApprovalCard";
import { SessionToolPicker, type SessionToolPickerProps } from "@/pages/chat/SessionToolPicker";
import type { DesktopBridge, RunnerApprovalRequest } from "@/types";

/**
 * The page's controls in the left group of the panel's composer toolbar: the
 * session tools and Deep Investigation. The agentic quota is the panel's own
 * indicator (`CHAT_PANEL_ENDPOINTS.quota`), as in the other products; the
 * voice conversation is in the right group (`ComposerVoiceCallButton`).
 */
export function ComposerToolbar({
  disabled,
  tools,
  toolCount,
  deepInvestigation,
  onToggleDeepInvestigation,
}: {
  disabled: boolean;
  /** The session tools (`useSessionTools().pickerProps`). */
  tools: SessionToolPickerProps;
  toolCount: number;
  deepInvestigation: boolean;
  /** Omitted while Deep Investigation is off on the platform. */
  onToggleDeepInvestigation?: () => void;
}) {
  const { t_i18n } = useFormatter();
  return (
    <span className="inline-flex items-center gap-0.5">
      <SessionToolPicker {...tools} disabled={disabled} count={toolCount} />
      {onToggleDeepInvestigation && (
        <Tip
          content={
            deepInvestigation
              ? t_i18n("Deep Investigation on: the next message is a subject to investigate")
              : t_i18n("Deep Investigation")
          }
          wrap="auto"
        >
          <IconButton
            variant="default"
            priority="tertiary"
            active={deepInvestigation}
            aria-pressed={deepInvestigation}
            aria-label={t_i18n("Deep Investigation")}
            className={cn(
              "h-8 w-8 shrink-0",
              deepInvestigation ? "text-accent hover:text-accent/80" : "text-default-secondary hover:text-default-primary",
            )}
            icon={<Search className="h-4 w-4" />}
            onClick={onToggleDeepInvestigation}
            disabled={disabled}
          />
        </Tip>
      )}
    </span>
  );
}

/**
 * The voice conversation, in the right group of the composer toolbar
 * (`composerTrailingToolbar`), directly before dictation. It carries the
 * panel's icon-button look, as the microphone does: the design system's
 * `IconButton`, default variant, tertiary priority, size md, a 20 px glyph and
 * no colour override, so it takes the panel's accent of the mode.
 */
export function ComposerVoiceCallButton({ disabled, onStart }: { disabled: boolean; onStart: () => void }) {
  const { t_i18n } = useFormatter();
  return (
    <Tip content={t_i18n("Start voice conversation")} wrap="auto">
      <IconButton
        variant="default"
        priority="tertiary"
        size="md"
        aria-label={t_i18n("Start voice conversation")}
        data-testid="chat.voice-call.start"
        icon={<Phone className="h-5 w-5" />}
        onClick={onStart}
        disabled={disabled}
      />
    </Tip>
  );
}

/**
 * Above the panel's composer: what the next message acts on. That the next message is a Deep Investigation
 * subject while the mode is on, the commands a machine holds for approval
 * and the machine this chat acts on inside XTM One Desktop.
 */
export function ComposerHeader({
  deepInvestigation,
  runnerApprovals,
  onRunnerApprovalDecided,
  desktop,
}: {
  deepInvestigation: boolean;
  runnerApprovals: RunnerApprovalRequest[];
  onRunnerApprovalDecided: (commandId: string) => void;
  desktop: { bridge: DesktopBridge | null; enabled: boolean; setEnabled: (enabled: boolean) => void } | null;
}) {
  const runnerId = desktop?.bridge?.runner_id;
  const { t_i18n } = useFormatter();
  if (!deepInvestigation && runnerApprovals.length === 0 && !runnerId) return null;
  return (
    <div className="space-y-2">
      {deepInvestigation && (
        <p className="flex items-center gap-1.5 px-1 content-caption text-accent">
          <Search className="h-3 w-3 shrink-0" aria-hidden="true" />
          {t_i18n("Deep Investigation on: the next message is a subject to investigate")}
        </p>
      )}
      {runnerApprovals.length > 0 && (
        // The held commands scroll inside a capped stack, so the composer and
        // the latest message stay in view however many commands wait.
        <div
          data-testid="runner-approvals-stack"
          role="region"
          aria-label={t_i18n("Commands waiting for your approval")}
          className="max-h-[45vh] space-y-2 overflow-y-auto overscroll-contain"
        >
          {runnerApprovals.map((request) => (
            <RunnerApprovalCard key={request.command_id} request={request} onDecided={onRunnerApprovalDecided} />
          ))}
        </div>
      )}
      {desktop && runnerId && (
        <DesktopMachineBar
          runnerId={runnerId}
          fallbackName={desktop.bridge?.runner_name ?? null}
          enabled={desktop.enabled}
          onToggle={desktop.setEnabled}
        />
      )}
    </div>
  );
}
