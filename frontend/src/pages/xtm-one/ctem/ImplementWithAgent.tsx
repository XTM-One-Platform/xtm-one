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
 * Follow-up actions on a CTEM recommendation (briefing next steps / recommended
 * focus, and each stage's recommended action). Two complementary buttons:
 *
 *   - FollowUpInChatButton: a quick chat follow-up on the recommendation. Opens
 *     chat with the composer primed with a pre-built message. Routed to the
 *     user's own implementing agent when one is assigned (the per-vertical CTEM
 *     analyst override), otherwise their DEFAULT chat agent (the CTEM Assistant
 *     in XTM One - Chat resolves it when no agent is handed off). Always
 *     available - it's just a chat.
 *
 *   - AssignAgentButton: the EE-gated call to action to assign your OWN agent
 *     (with the integrations to actually act). Shown once per card (bottom-right).
 *     While AI is switched off, `AgenticGateDialog` disables it with the AI-off
 *     explanation before any edition question (AI off wins over the licence).
 *     With AI on: on Community Edition it opens the standard Enterprise upsell
 *     dialog; on Enterprise Edition it routes to the profile where the user
 *     picks their analyst.
 */
import { useNavigate } from "react-router-dom";
import { MessageSquare, UserPlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import {
  AgenticGateDialog,
  AgenticEEBadgeForButton,
} from "@/components/ui/license-gate-dialog";
import { primeChatDraft } from "@/lib/chat-launch";
import { useFormatter } from "@/i18n";
import type { CtemVertical } from "@/types";

function buildPrompt(
  vertical: CtemVertical,
  contextLabel: string,
  action: string,
  rationale?: string,
): string {
  const lines = [
    `Let's follow up on this recommendation from my ${vertical} exposure ${contextLabel}:`,
    "",
    `"${action.trim()}"`,
  ];
  if (rationale?.trim()) {
    lines.push("", `Why it matters: ${rationale.trim()}`);
  }
  lines.push(
    "",
    "Help me implement it - outline the concrete steps, and carry out whatever you can with your connected tools and integrations.",
  );
  return lines.join("\n");
}

/** Quick chat follow-up on a single recommendation. Not gated - it's just chat. */
export function FollowUpInChatButton({
  vertical,
  contextLabel,
  action,
  rationale,
  agentId,
  agentName,
  agentIsBuiltin,
  size = "sm",
  variant = "ghost",
  className,
}: {
  vertical: CtemVertical;
  /** Human label of the source, e.g. "Prioritization stage" or "exposure briefing". */
  contextLabel: string;
  action: string;
  rationale?: string;
  /** The effective analyst agent for this stage/briefing (if resolved). */
  agentId?: string | null;
  agentName?: string | null;
  agentIsBuiltin?: boolean;
  size?: "sm" | "default";
  variant?: "outline" | "ghost" | "secondary";
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  // Use the user's OWN implementing agent when assigned (a custom, non-seeded
  // analyst); otherwise hand off nothing so /chat falls back to the user's default
  // chat agent (the CTEM Assistant in XTM One).
  const useAssignedAgent = !!agentId && !agentIsBuiltin;

  const handleClick = () => {
    // Prime the composer (no auto-send) so the user reviews before sending.
    primeChatDraft(buildPrompt(vertical, contextLabel, action, rationale), {
      agentId: useAssignedAgent ? agentId : null,
    });
    navigate("/chat");
  };

  const tip = useAssignedAgent
    ? t_i18n("Follow up in chat with {name}", { name: agentName ?? t_i18n("your agent") })
    : t_i18n("Follow up in chat with your default agent");

  return (
    <Tip content={tip}>
      <Button
        variant={variant}
        size={size}
        className={className ?? "gap-1.5 h-7 content-compact text-accent hover:text-accent hover:bg-accent/10"}
        onClick={handleClick}
      >
        <MessageSquare className="h-3.5 w-3.5" /> {t_i18n("Follow up in chat")}
      </Button>
    </Tip>
  );
}

/** EE-gated CTA to assign your own implementing agent. One per card (bottom-right). */
export function AssignAgentButton({
  size = "sm",
  variant = "outline",
  className,
}: {
  size?: "sm" | "default";
  variant?: "outline" | "ghost" | "secondary";
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  return (
    <AgenticGateDialog feature="Assigning a custom agent">
      <Tip content={t_i18n("Assign your own agent (with the integrations to act) to implement recommendations")}>
        <Button
          variant={variant}
          size={size}
          className={className ?? "gap-1.5 h-7 content-compact shrink-0"}
          onClick={() => navigate("/profile")}
        >
          <UserPlus className="h-3.5 w-3.5" /> {t_i18n("Assign your own agent")}
          <AgenticEEBadgeForButton />
        </Button>
      </Tip>
    </AgenticGateDialog>
  );
}
