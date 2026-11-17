// SPDX-License-Identifier: Apache-2.0

import { useAiDisabledGroup } from "@/hooks/useAiDisabledGroup";
import {
  useAgenticAIEnabled, usePlatformAiEnabled, usePlatformConfig,
} from "@/hooks/usePlatformConfig";

/**
 * Why an AI surface cannot run, as ONE answer (dev-docs/ctem-open-source.md,
 * section 2): group AI off > platform AI off > no license > no model > none.
 *
 * - `group_off`: AI is off for the reader's group.
 * - `ai_off`: an administrator switched AI off for the platform.
 * - `unlicensed`: Community Edition, no Enterprise license.
 * - `no_model`: licensed and on, but no default model is configured.
 *
 * AI off wins over the license: activating Enterprise Edition unlocks nothing
 * on a platform whose AI is off, so that is what the surface says first.
 *
 * `status` is the backend's answer for the surface (the CTEM briefing and stage
 * assessment GETs answer `ai_disabled`, `no_model` or `ee_locked`). It fills in
 * what the platform config cannot say on its own (no model) and covers a config
 * read that is a few seconds stale; the config still decides the precedence.
 */
export type AiLockReason = "group_off" | "ai_off" | "unlicensed" | "no_model";

export function useAiLockReason(status?: string | null): AiLockReason | null {
  const config = usePlatformConfig();
  const group = useAiDisabledGroup();
  const platformOn = usePlatformAiEnabled();
  const licensed = useAgenticAIEnabled();
  return aiLockReason({
    configLoaded: !!config, groupOff: group !== null, platformOn, licensed, status,
  });
}

/** The pure resolver behind `useAiLockReason`, exported for its unit test. */
export function aiLockReason({
  configLoaded, groupOff, platformOn, licensed, status,
}: {
  configLoaded: boolean;
  groupOff: boolean;
  platformOn: boolean;
  licensed: boolean;
  status?: string | null;
}): AiLockReason | null {
  if (groupOff) return "group_off";
  if (!platformOn || status === "ai_disabled") return "ai_off";
  // Say nothing about the license while the config is in flight: a lock that
  // flashes on every load of a licensed install teaches users to ignore it.
  if ((configLoaded && !licensed) || status === "ee_locked") return "unlicensed";
  if (status === "no_model") return "no_model";
  return null;
}
