// SPDX-License-Identifier: Apache-2.0

/**
 * The nav model as the current reader resolves it (role, mode, flags, AI
 * state), and one group of it: the single source the hub pages build their
 * cards from (#9301 N2c, UX r1 B3). A hub (`/agents-hub`, `/work`,
 * `/agent-studio`, `/tools-and-integrations`) shows one card per VISIBLE child
 * of its group, in the group's order and with the child's lock, so a page
 * added to the menu appears on its hub and a page the reader cannot see never
 * does. The per-route card data (description, items, create) stays with each
 * hub, keyed by route: `hubCards` drops nothing and fails loudly in the hub's
 * test when a visible child has no data.
 */

import { useMemo } from "react";
import { useAuth } from "@/hooks/useAuth";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import {
  useAgenticAIEnabled, useAiEnabled, useChatEnabled, useCustomVariablesEnabled, useIsXtmOne,
  usePlatformConfig,
} from "@/hooks/usePlatformConfig";
import { useRelaysEnabled } from "@/hooks/api/useRelayApi";
import { ctemMayView } from "@/lib/ctem-access";
import { resolveNav, type NavFlags, type NavGroup, type NavLeaf } from "./nav-model";
import { findNavFamily, findNavGroup } from "./nav-hub";

/** The flags the Sidebar and the top bar search resolve the nav with. */
export function useNavFlags(): NavFlags {
  const platformConfig = usePlatformConfig();
  const chat = useChatEnabled();
  const customVariables = useCustomVariablesEnabled();
  const relays = useRelaysEnabled() === true;
  const xtmOne = useIsXtmOne();
  const aiEnabled = useAiEnabled();
  // As the Sidebar: lock only on a RESOLVED "not licensed".
  const aiLicensed = useAgenticAIEnabled() || !platformConfig;
  const deepInvestigation = useFeatureFlag("DEEP_INVESTIGATION");
  const health = true;
  const videoStudio = useFeatureFlag("VIDEO_STUDIO");
  const dashboardStudio = useFeatureFlag("DASHBOARD_STUDIO");
  const evaluation = useFeatureFlag("EVALUATION");
  const aiAdoption = platformConfig?.features.ai_adoption !== false;
  const { user, isAdmin } = useAuth();
  // CTEM only with a right (#2441): an administrator or a CTEM view or decide group.
  const ctem = xtmOne && ctemMayView(user, isAdmin);
  return useMemo(() => ({
    chat, customVariables, relays, xtmOne, aiAdoption, aiEnabled, aiLicensed,
    deepInvestigation, health, videoStudio, dashboardStudio, evaluation,
    // Intents are XTM One domain content: no row or hub card in Copilot mode (#445).
    intents: xtmOne,
    // The CTEM section is XTM One domain content (#1135), for a person with a CTEM right only (#2441).
    ctemCases: ctem,
  }), [chat, customVariables, relays, xtmOne, aiAdoption, aiEnabled, aiLicensed,
    deepInvestigation, health, videoStudio, dashboardStudio, evaluation, ctem]);
}

/** The group `id` as the current reader resolves it. */
export function useNavGroup(id: string): NavGroup | null {
  const flags = useNavFlags();
  const { isAdmin, isGroupManager } = useAuth();
  return useMemo(
    () => findNavGroup(resolveNav(flags, isAdmin, isGroupManager), id),
    [flags, isAdmin, isGroupManager, id],
  );
}

/** The family row of page `to` as the current reader resolves it (`findNavFamily`). */
export function useNavFamily(to: string): NavLeaf | null {
  const flags = useNavFlags();
  const { isAdmin, isGroupManager } = useAuth();
  return useMemo(
    () => findNavFamily(resolveNav(flags, isAdmin, isGroupManager), to),
    [flags, isAdmin, isGroupManager, to],
  );
}

export { hubCards } from "./nav-hub";
