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

import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { HitlIcon } from "@/components/ui/hitl-icon";
import { effectiveApproval, hitlState, PLATFORM_APPROVALS_PATH } from "@/lib/hitl";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { capTipText } from "@/lib/tip-text";
import { useUpdateAgent } from "@/hooks/api/useAgentApi";
import { useToolApprovals } from "@/hooks/api/useToolApprovals";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useAuth } from "@/hooks/useAuth";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { useFormatter } from "@/i18n";
import { useToast } from "@/components/ui/toast";
import type { Agent } from "@/types";

// Whether this agent asks a person before running tools. Its own card,
// saved on change: on a built-in agent it is one of the only fields an
// administrator may write.

type Choice = "default" | "on" | "off";

const toChoice = (v: boolean | null | undefined): Choice =>
  v === null || v === undefined ? "default" : v ? "on" : "off";

const fromChoice = (c: Choice): boolean | null =>
  c === "default" ? null : c === "on";

export function ToolApprovalCard({ agent }: { agent: Agent }) {
  const { t_i18n } = useFormatter();
  const { isAdmin } = useAuth();
  const update = useUpdateAgent();
  const { toast } = useToast();
  const { data: mine } = useToolApprovals();
  const installDefault = mine?.approve_tool_calls_default;
  const suspended = mine?.tool_approvals_enabled === false;
  const [pending, setPending] = useState<Choice | null>(null);
  // `PUT /agents/{id}` is AI-gated, so with AI off this would 403 on change.
  const aiOff = useAiActionDisabled();

  const current = toChoice(agent.approve_tool_calls);
  const effective = effectiveApproval(agent.approve_tool_calls, installDefault);
  const defaultLabel =
    installDefault === undefined
      ? t_i18n("Follow the platform default")
      : installDefault
      ? t_i18n("Follow the platform default (asks)")
      : t_i18n("Follow the platform default (does not ask)");
  const currentLabel = current === "default" ? defaultLabel : current === "on" ? t_i18n("Ask") : t_i18n("Don't ask");

  // Whether the closed value is cut: the trigger then names it whole on hover and
  // focus through a tooltip on the trigger (SelectValue sets pointer-events: none on its content, so a
  // tooltip inside it never opens on hover; #10891 UX r2 B1).
  const valueRef = useRef<HTMLSpanElement>(null);
  const [valueCut, setValueCut] = useState(false);
  const [tipOpen, setTipOpen] = useState(false);
  useEffect(() => {
    const node = valueRef.current;
    if (!node) return;
    const measure = () => setValueCut(node.scrollWidth > node.clientWidth + 1);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [currentLabel]);

  // Only `approve_tool_calls` goes on the wire: the built-in carve-out refuses
  // any other field.
  const apply = (c: Choice) =>
    update.mutate(
      { id: agent.id, approve_tool_calls: fromChoice(c) },
      {
        onError: (e: Error) =>
          toast({ title: t_i18n("Could not save tool approvals"), description: e.message, variant: "error" }),
      },
    );

  const onChange = (c: Choice) => {
    if (c === current) return;
    // Confirm whenever the result may stop it asking, an unknown default included.
    const next = effectiveApproval(fromChoice(c), installDefault);
    if (effective !== false && next !== true) {
      setPending(c);
      return;
    }
    apply(c);
  };

  return (
    <>
      <div
        className="space-y-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-3"
        data-testid="agent-tool-approval"
      >
        <div className="flex items-center justify-between gap-4">
          <div className="space-y-0.5 flex-1">
            <p className="content-base font-medium flex items-center gap-2">
              <HitlIcon
                state={hitlState(effective)}
                className={effective === false ? "h-3.5 w-3.5 text-default-secondary" : "h-3.5 w-3.5"}
              />{" "}
              {t_i18n("Ask before running tools")}
            </p>
            <p className="text-[11px] text-default-secondary">
              {effective
                ? t_i18n(
                    "This agent stops before a tool call that is not known to be read-only and waits for a person to approve it, in chat and on scheduled runs. Read-only tools run without asking.",
                  )
                : t_i18n(
                    "Off: this agent runs its tools without asking anyone. Turn it on to have it wait for a person before a tool call that is not known to be read-only, in chat and on scheduled runs.",
                  )}
            </p>
            {effective && suspended && (
              <p className="content-caption text-feedback-warning-primary">
                {t_i18n("Approvals are suspended for the whole platform right now, so it does not ask anyone yet.")}
              </p>
            )}
            {isAdmin && (
              <p className="text-[11px] text-default-secondary">
                <Link to={PLATFORM_APPROVALS_PATH} className="text-accent underline-offset-2 hover:underline">
                  {t_i18n("Platform default and tool rules")}
                </Link>
              </p>
            )}
          </div>
          <AiDisabledTip>
            <Select
              value={current}
              onValueChange={(v) => onChange(v as Choice)}
              disabled={update.isPending || aiOff}
            >
              {/* Sized to the chosen option up to max-w-lg (512 px: the longest, fr, needs 450); a cut value is named by the tooltip. */}
              <TooltipProvider delayDuration={300}>
                <Tooltip open={valueCut && tipOpen} onOpenChange={(next) => setTipOpen(valueCut && next)}>
                  <TooltipTrigger asChild>
                    <SelectTrigger className="w-fit max-w-lg" aria-label={t_i18n("Ask before running tools")}>
                      <SelectValue>
                        <span ref={valueRef} className="block min-w-0 truncate">
                          {currentLabel}
                        </span>
                      </SelectValue>
                    </SelectTrigger>
                  </TooltipTrigger>
                  <TooltipContent side="top" align="center" collisionPadding={8}>
                    {capTipText(currentLabel)}
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
              <SelectContent>
                <SelectItem value="default">{defaultLabel}</SelectItem>
                <SelectItem value="on">{t_i18n("Ask")}</SelectItem>
                <SelectItem value="off">{t_i18n("Don't ask")}</SelectItem>
              </SelectContent>
            </Select>
          </AiDisabledTip>
        </div>
      </div>

      <ConfirmDialog
        open={pending !== null}
        onOpenChange={(open) => !open && setPending(null)}
        variant="warning"
        // Raw English, not t_i18n(...): ConfirmDialog translates its props.
        title="Stop asking before this agent runs tools?"
        description="This agent's tool calls will run without asking anyone, including calls that create, update, delete or send, in its chats as well as its scheduled runs. The change is recorded in the audit log."
        confirmLabel="Stop asking"
        cancelLabel="Cancel"
        onConfirm={() => {
          if (pending) apply(pending);
          setPending(null);
        }}
      />
    </>
  );
}
