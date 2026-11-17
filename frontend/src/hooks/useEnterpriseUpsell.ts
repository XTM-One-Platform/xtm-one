// SPDX-License-Identifier: Apache-2.0

import {
  useAgenticAIEnabled,
  useAgenticSource,
  useAiEnabled,
  useIsXtmOne,
  usePlatformConfig,
} from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";

/**
 * The one place the "AI is not licensed here" state is worded.
 *
 * Separate from `components/ui/enterprise-upsell.tsx` so that file exports
 * only components (react-refresh), exactly like `useAiDisabled.ts` does for
 * the administrator's switch. Every locked affordance — the nav rows, the
 * creation buttons, the composer, the settings sections — explains itself
 * through here so the wording cannot drift per screen, and every explanation
 * names the two things that actually change the answer: connect an
 * Enterprise Edition platform, or activate a Filigran license.
 */

/**
 * The shared explanation, or `null` when AI is licensed (or still unknown).
 *
 * Also `null` while an administrator has AI switched off: AI off wins over
 * the licence (FR-031, `lockReasonFor` in `nav-model.ts`, the gate dialogs).
 * Activating Enterprise Edition unlocks nothing on a platform whose AI is
 * off, so the upsell would send an administrator to buy what they already
 * have — `useAiDisabledReason()` is what speaks then.
 */
export function useEnterpriseLockedReason(): string | null {
  const config = usePlatformConfig();
  const isXtmOne = useIsXtmOne();
  const licensed = useAgenticAIEnabled();
  const aiEnabled = useAiEnabled();
  const { t_i18n } = useFormatter();
  // Say nothing while the config is in flight: a lock that flashes on every
  // page load of a licensed install teaches users to ignore it.
  if (!config || licensed || !aiEnabled) return null;
  return isXtmOne
    ? t_i18n(
        "AI is not activated on this Community Edition deployment. Connect an Enterprise Edition OpenCTI, OpenAEV or OpenCRQ platform, or activate a Filigran license to unlock agents, chat and automations.",
      )
    : t_i18n("A valid Filigran license is required to use AI on this platform.");
}

/**
 * `true` when an AI affordance must lead to the upgrade path instead of
 * running. The licence half of "can this button do its job"; pair it with
 * `useAiActionDisabled()` for the administrator's switch.
 */
export function useEnterpriseLocked(): boolean {
  return useEnterpriseLockedReason() !== null;
}

/**
 * Whether the deployment is on the INCLUDED allowance: Enterprise Edition
 * inherited from a connected EE platform, no Filigran license of its own yet. The
 * platform is fully activated; only the acts ceilings are the entry-level
 * ones, so the surfaces that show quotas add "install a license to carry
 * your own quota" — an invitation, never a lock.
 */
export function useOnIncludedAllowance(): boolean {
  const isXtmOne = useIsXtmOne();
  const licensed = useAgenticAIEnabled();
  const source = useAgenticSource();
  return isXtmOne && licensed && source === "ee_platform";
}
