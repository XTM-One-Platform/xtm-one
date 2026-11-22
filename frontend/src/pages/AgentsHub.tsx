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
 * Orchestration hub (`/agents-hub`, the path kept from N2c so old links
 * open), the destination behind the "Orchestration" group of the menu
 * (#9301 N2c, renamed N2d), built like Agent Studio and Tools & Integrations: title,
 * one line, the "Create an agent" banner, one card per child row of the
 * group, then "Recently modified" across them.
 *
 * The cards are the group's VISIBLE children as the reader resolves the menu
 * (`useNavGroup` + `hubCards`), in its order and with its locks: a
 * non-administrator gets no Intents card, Community Edition reads Fleets
 * "Unlicensed" with its Create hidden, and the hub can never list a page the
 * menu does not. This file only knows each page's data, keyed by route.
 *
 * Fleets: one Create, its menu the two ways the Fleets page header offers
 * ("Create with a prompt", "New fleet"), in the same order.
 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ResourceHub } from "@/components/ui/resource-hub";
import { useNavGroup, hubCards } from "@/components/layout/use-nav-group";
import { NewFleetDialog } from "@/components/fleets/NewFleetDialog";
import { FLEETS_JOB, createFleetPrompt } from "@/components/fleets/fleet-handoff";
import { useAgents } from "@/hooks/api/useAgentApi";
import { useFleets } from "@/hooks/api/useFleetApi";
import { useIntents } from "@/hooks/api/useIntentApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useToolAgents } from "@/hooks/useToolAgents";
import { useFormatter } from "@/i18n";
import { primeChatDraft } from "@/lib/chat-launch";
import { CHAT_GENERAL_AGENT_PARAM } from "@/lib/constants";

export default function AgentsHub() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const group = useNavGroup("agents");
  const visible = (to: string) => group?.children.some((child) => child.to === to && !child.locked) ?? false;

  /* A request only for a card the reader sees unlocked: no 403 in CE. */
  const agents = useAgents();
  const fleets = useFleets({ enabled: visible("/fleets"), optional: true });
  const intents = useIntents(undefined, undefined, { enabled: visible("/intents") });

  const [fleetDialog, setFleetDialog] = useState(false);
  const creators = useToolAgents(FLEETS_JOB);
  const aiOff = useAiActionDisabled();
  const startFleetPrompt = () => {
    primeChatDraft(createFleetPrompt(t_i18n), {
      agentId: creators.defaultAgentId ?? (creators.generalAssistantOffered ? CHAT_GENERAL_AGENT_PARAM : null),
      autoSend: true,
    });
    navigate("/chat");
  };

  const agentsLocked = group?.children.find((child) => child.to === "/agents")?.locked;
  const agentsLockReason = group?.children.find((child) => child.to === "/agents")?.lockReason;

  const resources = hubCards(group, {
    "/agents": {
      createTo: "/agents/new",
      gatedCreate: true,
      description: "The assistants that answer, act and hand work to each other.",
      unit: ["agent", "agents"],
      items: agents.data,
      isLoading: agents.isLoading,
      detailTo: (item) => `/agents/${item.id}`,
    },
    "/fleets": {
      description: "Agents grouped under an orchestrator, with their own settings, budget and value.",
      unit: ["fleet", "fleets"],
      items: fleets.data,
      isLoading: fleets.isLoading,
      detailTo: (item) => `/fleets/${item.id}`,
      createMenu: [
        { label: "Create with a prompt", onSelect: startFleetPrompt, disabled: aiOff || creators.resolving || creators.none },
        { label: "New fleet", onSelect: () => setFleetDialog(true) },
      ],
    },
    "/flows": {
      description: "Who hands work to whom, on the agent-to-agent map, with its schedule.",
      unit: ["agent", "agents"],
      items: agents.data,
      isLoading: agents.isLoading,
      inRecent: false,
    },
    "/intents": {
      description: "The routes that send a request to the agent that should take it.",
      unit: ["intent", "intents"],
      items: intents.data,
      isLoading: intents.isLoading,
    },
  });

  return (
    <>
      <ResourceHub
        title="Orchestration"
        description="Your agents, the fleets they form and the flows they follow."
        feature={{
          to: "/agents/new",
          icon: "sparkle",
          label: "Create an agent",
          description: "Start from a template or a blank persona, then bind knowledge, tools and assignments.",
          gated: true,
          ...(agentsLocked ? { locked: true, lockReason: agentsLockReason } : {}),
        }}
        resources={resources}
      />
      <NewFleetDialog open={fleetDialog} onOpenChange={setFleetDialog} />
    </>
  );
}
