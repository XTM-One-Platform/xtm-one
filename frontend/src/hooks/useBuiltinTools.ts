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
 * The built-in tool registry (`backend/app/services/builtin_tool_groups.py`,
 * served by `GET /settings/builtin-tools/groups`) as a picker
 * renders it: labels translated, the platform builder titled with the
 * platform's name, and each suite's deployment switch resolved. Every
 * built-in tool picker takes its entries here, so a new suite reaches all of
 * them at once. While the registry loads (`isLoading`) or after its read
 * failed (`isError`), the lists are empty and callers render a skeleton or an
 * error line: an id is never shown raw and never reported as not offered
 * before the registry is known.
 */
import { useMemo } from "react";
import { Sparkles, type LucideIcon } from "lucide-react";
import { useFormatter } from "@/i18n";
import type { BuiltinToolSurface } from "@/lib/constants";
import { builtinToolIcon } from "@/lib/builtin-tool-icons";
import type { BuiltinToolGroup } from "@/types";
import { useBuiltinToolGroups } from "@/hooks/api/useSettingsApi";
import { listedInAgentConfig, offeredInAgentChat, type ChatAgentTools } from "@/lib/builtin-tool-offers";
import { aliasIndex, canonicalSuiteIds, carriesSuite, withoutSuite } from "@/lib/builtin-tool-aliases";
import { useAuth } from "@/hooks/useAuth";
import { useCodeInterpreterEnabled, usePlatformName } from "@/hooks/usePlatformConfig";
import { useRunnersEnabled } from "@/hooks/useDesktop";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";

export interface BuiltinToolOption {
  id: string;
  label: string;
  description: string;
  /** The longer description the tool's own page shows. */
  detail: string;
  /** The category chip the Tools page shows. */
  tag: string;
  icon: LucideIcon;
  /** False while the deployment switch the suite depends on is off. */
  available: boolean;
  agentOnly: boolean;
  /** Offered to an administrator's General Assistant only. */
  assistantAdminOnly: boolean;
  /** Only an administrator adds it to an agent, and the viewer is not one:
   *  an agent's grid shows it locked, and keeps it removable where the agent
   *  already carries it (`toggledInAgentConfig`). */
  bindLocked?: boolean;
  /** Internal to Platform Health (#1943): carried by its fleet's own agents
   *  alone, so no picker, Tools page or chat offers it. */
  internal: boolean;
  /** False for an id this build's registry does not list. */
  known: boolean;
  /** Function names the suite grants on this deployment. */
  functions: string[];
}

type BuiltinToolGate = NonNullable<BuiltinToolGroup["gate"]>;

export interface BuiltinToolCatalog {
  /** The suites a picker for `surface` offers right now, in display order. */
  offered: (surface: BuiltinToolSurface) => BuiltinToolOption[];
  /** The suites a chat's session picker offers: the General Assistant's own,
   *  or in an agent chat `offeredInAgentChat` (`lib/builtin-tool-offers.ts`);
   *  never an internal suite (`internal`). */
  offeredInChat: (agentChat: boolean, agent?: ChatAgentTools) => BuiltinToolOption[];
  /** The suites an agent's configuration grid lists: the ones it offers, plus
   *  a switched-off one the agent carries, saved or in the draft, so it can be
   *  removed (`listedInAgentConfig`, `lib/builtin-tool-offers.ts`). */
  listedInAgentConfig: (saved: readonly string[], draft: readonly string[]) => BuiltinToolOption[];
  /** Any id an entity carries, offered or not; a [DEPRECATED] former id
   *  answers its group's card, one this build does not know
   *  keeps its raw id as label. */
  describe: (id: string) => BuiltinToolOption;
  /** Whether stored `ids` carry the suite `suiteId`, directly or through a former id. */
  carries: (ids: readonly string[] | null | undefined, suiteId: string) => boolean;
  /** Stored `ids` with every former id replaced by its group, once, in order. */
  canonicalIds: (ids: readonly string[] | null | undefined) => string[];
  /** Stored `ids` without the suite `suiteId` and its former ids. */
  without: (ids: readonly string[], suiteId: string) => string[];
  /** True until the registry has answered once. */
  isLoading: boolean;
  /** True when the registry read failed and nothing is cached. */
  isError: boolean;
  /** The registry is known: `known: false` from `describe` then means the
   *  id is truly absent from this build. */
  isReady: boolean;
}

export function useBuiltinTools(): BuiltinToolCatalog {
  const { t_i18n } = useFormatter();
  const { user, isAdmin } = useAuth();
  const platformName = usePlatformName();
  const codeInterpreterEnabled = useCodeInterpreterEnabled();
  // Available while `/auth/config` loads, as before: an agent form must not
  // grey out a tool it already carries for the length of a request.
  const runnersEnabled = useRunnersEnabled() !== false;
  const flowBuilderEnabled = useFeatureFlag("FLOW_BUILDER");
  const deepInvestigationEnabled = useFeatureFlag("DEEP_INVESTIGATION");
  const videoStudioEnabled = useFeatureFlag("VIDEO_STUDIO");
  const dashboardStudioEnabled = useFeatureFlag("DASHBOARD_STUDIO");
  const mobileCompanionEnabled = useFeatureFlag("MOBILE_COMPANION");
  const { data: groups, isPending, isError: readFailed } = useBuiltinToolGroups();
  const isReady = groups !== undefined;
  const isError = !isReady && readFailed;
  const isLoading = !isReady && !isError && isPending;
  const userId = user?.id;

  return useMemo(() => {
    const gateOpen: Record<BuiltinToolGate, boolean> = {
      code_interpreter: codeInterpreterEnabled,
      runners: runnersEnabled,
      flow_builder: flowBuilderEnabled,
      deep_investigation: deepInvestigationEnabled,
      video_studio: videoStudioEnabled,
      dashboard_studio: dashboardStudioEnabled,
      mobile_companion: mobileCompanionEnabled,
      // Built in since #101 (S1): the suites of the health specialists carry no flag.
      health: true,
    };
    const toOption = (group: BuiltinToolGroup): BuiltinToolOption => ({
      id: group.id,
      label: group.named_after_platform ? platformName || group.name : t_i18n(group.name),
      description: t_i18n(group.tagline),
      detail: group.detail ? t_i18n(group.detail, { name: platformName }) : t_i18n(group.tagline),
      tag: t_i18n(group.category),
      icon: builtinToolIcon(group.icon),
      available: group.gate === null || gateOpen[group.gate],
      agentOnly: group.agent_keyed,
      assistantAdminOnly: group.admin_only,
      bindLocked: !!group.bind_admin_only && !isAdmin,
      internal: group.internal,
      known: true,
      functions: group.functions,
    });
    const options = (groups ?? []).map(toOption);
    const byId = new Map(options.map((option) => [option.id, option]));
    const aliases = aliasIndex(groups ?? []);
    const offered = (surface: BuiltinToolSurface) =>
      options.filter(
        (option) =>
          option.available &&
          !option.internal &&
          (surface === "agent" || (!option.agentOnly && (isAdmin || !option.assistantAdminOnly))),
      );
    return {
      offered,
      offeredInChat: (agentChat, agent) =>
        (agentChat
          ? offeredInAgentChat(offered("agent"), agent, { userId, isAdmin })
          : offered("general_assistant")
        ),
      listedInAgentConfig: (saved, draft) => listedInAgentConfig(options, saved, draft),
      carries: (ids, suiteId) => carriesSuite(ids, suiteId, aliases),
      canonicalIds: (ids) => canonicalSuiteIds(ids ?? [], aliases),
      without: (ids, suiteId) => withoutSuite(ids, suiteId, aliases),
      describe: (id) =>
        byId.get(id) ??
        byId.get(aliases.get(id) ?? "") ?? {
          id,
          label: id,
          description: "",
          detail: "",
          tag: "",
          icon: Sparkles,
          available: true,
          agentOnly: false,
          assistantAdminOnly: false,
          internal: false,
          known: false,
          functions: [],
        },
      isLoading,
      isError,
      isReady,
    };
  }, [
    groups,
    isLoading,
    isError,
    isReady,
    t_i18n,
    platformName,
    codeInterpreterEnabled,
    runnersEnabled,
    flowBuilderEnabled,
    deepInvestigationEnabled,
    videoStudioEnabled,
    dashboardStudioEnabled,
    mobileCompanionEnabled,
    isAdmin,
    userId,
  ]);
}
