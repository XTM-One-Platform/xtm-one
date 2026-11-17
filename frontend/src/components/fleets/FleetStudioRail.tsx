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
 * The fleet page's "Modify with AI" panel (#10162): the shared studio rail
 * (`components/studio/StudioRail`) bound to the fleet on screen, docked on the
 * right of the page (UX verdict 5dcb88e3, decision A).
 *
 * Its conversations carry `fleet_id` (S2a #10170), so the agent is told which
 * fleet it changes and never asks (the fleet studio context names it, its
 * orchestrator, members and workspace on every turn); the picker offers the
 * assistants carrying the fleet tools (`FLEET_EDIT_JOB`); a
 * fleet tool that returns refreshes the fleet, its members, health and About
 * at once (`useChatApi`'s `FLEET_WRITE_TOOLS`), so the page follows the agent.
 * A change that removes, pauses or widens is applied by the tools only on the
 * user's confirmation in the chat (#10165: `set_fleet_automations` answers a
 * summary until `confirm=true`, `update_fleet` refuses an autonomy raise):
 * the summary shows as the amber confirm card and the refusal as a card with
 * "Open the autonomy control" (`FleetChatCard`, R8/R9).
 */
import { useMemo } from "react";

import { FleetChatCard } from "@/components/fleets/FleetChatCard";
import { fleetChatCard } from "@/components/fleets/fleet-chat-cards";
import { FLEET_EDIT_JOB } from "@/components/fleets/fleet-handoff";
import { StudioRail, type StudioSubject } from "@/components/studio/StudioRail";
import { useToolAgents } from "@/hooks/useToolAgents";
import { useFormatter } from "@/i18n";
import type { Fleet } from "@/hooks/api/useFleetApi";

/**
 * The panel's width before anyone drags (px): the mockup's 400 from 1280 px,
 * 320 below so the page keeps a readable column at 1024 (UX r1 B1, R13).
 */
const fleetStudioDefaultWidth = () =>
  typeof window !== "undefined" && window.matchMedia("(min-width: 1280px)").matches ? 400 : 320;

export interface FleetStudioRailProps {
  fleet: Pick<Fleet, "id" | "name">;
  initialConversationId: string | null;
  onConversationChange: (id: string | null) => void;
  onCollapse: () => void;
  /** Switches the page to its Approval tab (the raise refusal's "Open the autonomy control"). */
  onShowApprovalTab: () => void;
  className?: string;
}

export function FleetStudioRail({ fleet, onShowApprovalTab, ...rail }: FleetStudioRailProps) {
  const { t_i18n } = useFormatter();
  const editors = useToolAgents(FLEET_EDIT_JOB);

  // The binding carries the fleet; no per-message focus (the fleet context reads no tab).
  const sendContext = useMemo(() => ({}), []);
  const suggestions = useMemo(
    () => [
      t_i18n("Add an agent to this fleet as a member"),
      t_i18n("Set the autonomy level of this fleet to Low"),
      t_i18n("Make the orchestrator the only agent exposed to other fleets"),
      t_i18n("Regenerate the documentation of this fleet"),
    ],
    [t_i18n],
  );

  const subject: StudioSubject = {
    kind: "fleet",
    field: "fleet_id",
    id: fleet.id,
    title: fleet.name,
    toolAgents: editors,
    sendContext,
    focus: {
      label: fleet.name,
      tip: t_i18n("The assistant is told which fleet it changes"),
    },
    suggestions,
    transcriptFooter: ({ messages, send, canSend }) => {
      const card = fleetChatCard(messages);
      return card ? (
        <FleetChatCard key={card.messageId} card={card} send={send} canSend={canSend} onShowApprovalTab={onShowApprovalTab} />
      ) : null;
    },
    testId: "fleet.studio",
    labels: {
      rail: t_i18n("Modify the fleet with AI"),
      history: t_i18n("Conversations about this fleet"),
      noAgentShort: t_i18n("No agent with the fleet tools"),
      noAgentTitle: t_i18n("No agent carries the fleet tools yet"),
      blockedHint: t_i18n(
        "Modify with AI works with agents that have the Platform tool with its fleet functions. Turn it on in one of your agents' tools and come back here.",
      ),
      unavailableHint: t_i18n("The panel opened a conversation about this fleet instead."),
      // The rail says what it is and what it changes (UX r1 B6, the mockup's binding line).
      title: t_i18n("Modify with AI"),
      binding: t_i18n("Changes apply to {name}", { name: fleet.name }),
      placeholder: t_i18n("Describe a change to this fleet"),
    },
  };

  return <StudioRail subject={subject} side="right" defaultWidth={fleetStudioDefaultWidth} {...rail} />;
}
