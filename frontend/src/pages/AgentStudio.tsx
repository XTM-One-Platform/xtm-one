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
 * Agent Studio hub (`/agent-studio`) — the destination behind the sidebar group
 * of the same name. Everything here defines what an agent knows, says, and can
 * do: knowledge, prompts, templates, skills, investigation packs. Building the agent itself starts
 * from the featured card, which reuses the existing `/agents/new` wizard.
 * The cards are the group's visible children (`hubCards`, #9301 N2c).
 */
import { ResourceHub } from "@/components/ui/resource-hub";
import { hubCards, useNavGroup } from "@/components/layout/use-nav-group";
import { useKnowledgeBases } from "@/hooks/api/useKnowledgeBaseApi";
import { usePrompts } from "@/hooks/api/usePromptApi";
import { useTemplateContainers } from "@/hooks/api/useTemplateContainerApi";
import { useSkills } from "@/hooks/api/useSkillApi";
import { useInvestigationPacks } from "@/hooks/api/useInvestigationPackApi";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";

export default function AgentStudio() {
  const knowledge = useKnowledgeBases();
  const prompts = usePrompts();
  const templates = useTemplateContainers();
  const skills = useSkills();
  // Behind `DEEP_INVESTIGATION`, like the nav entry: off, no card and no request.
  const deepInvestigation = useFeatureFlag("DEEP_INVESTIGATION");
  const packs = useInvestigationPacks(deepInvestigation);

  const resources = hubCards(useNavGroup("agent-studio"), {
    "/knowledge": {
      createTo: "/knowledge/new",
      gatedCreate: true,
      description: "Vectorized document collections agents search to answer with your own facts.",
      items: knowledge.data,
      isLoading: knowledge.isLoading,
      detailTo: (item) => `/knowledge/${item.id}`,
    },
    "/prompts": {
      createTo: "/prompts/new",
      gatedCreate: true,
      description: "Reusable instructions injected into agent system prompts, or run straight from chat.",
      items: prompts.data,
      isLoading: prompts.isLoading,
      detailTo: (item) => `/prompts/${item.id}`,
    },
    "/templates": {
      createTo: "/templates/new",
      gatedCreate: true,
      description: "Branded layouts and assets agents use when they produce documents and decks.",
      items: templates.data,
      isLoading: templates.isLoading,
      detailTo: (item) => `/templates/${item.id}`,
    },
    "/skills": {
      createTo: "/skills/new",
      gatedCreate: true,
      description: "Packaged know-how, loaded on demand to teach agents a repeatable procedure.",
      items: skills.data,
      isLoading: skills.isLoading,
      detailTo: (item) => `/skills/${item.id}`,
    },
    "/investigation-packs": {
      createTo: "/investigation-packs/new",
      gatedCreate: true,
      description: "Your team's investigation methods, run from the chat's Deep Investigation with each runner's own access.",
      items: packs.data,
      isLoading: packs.isLoading,
      detailTo: (item: { id: string }) => `/investigation-packs/${item.id}`,
    },
  });

  return (
    <ResourceHub
      title="Agent Studio"
      description="Everything that defines what your agents know, say, and can do."
      feature={{
        to: "/agents/new",
        icon: "sparkle",
        label: "Create an agent",
        description: "Start from a template or a blank persona, then bind knowledge, tools and assignments.",
        gated: true,
      }}
      resources={resources}
    />
  );
}
