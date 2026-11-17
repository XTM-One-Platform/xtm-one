// SPDX-License-Identifier: Apache-2.0

import { useAiDisabledGroup } from "@/hooks/useAiDisabledGroup";
import { useAiEnabled } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";

/**
 * The one place the AI-disabled state is worded (FR-018).
 *
 * Separate from `components/ui/ai-disabled.tsx` so that file exports only
 * components (react-refresh). Every AI action — Create, Run, Chat, Test,
 * Evaluate, Sync — explains itself through here so the wording cannot drift
 * per screen. Gate on the platform config, never on a caught error: a disabled
 * button beats a refused click.
 */

/** The shared explanation, or `null` when AI is on and nothing needs saying. */
export function useAiDisabledReason(): string | null {
  const aiEnabled = useAiEnabled();
  const group = useAiDisabledGroup();
  const { t_i18n } = useFormatter();
  if (aiEnabled) return null;
  if (group !== null) {
    return t_i18n("AI is disabled for your group. Ask an administrator if you need it.");
  }
  return t_i18n(
    "AI is disabled on this platform. An administrator can enable it in Settings → Platform.",
  );
}

/**
 * `true` when an AI action must be unavailable. Pair with `AiDisabledTip` so
 * the control is both disabled and explained:
 *
 * ```tsx
 * const aiOff = useAiActionDisabled();
 * <AiDisabledTip><Button disabled={aiOff}>Run</Button></AiDisabledTip>
 * ```
 */
export function useAiActionDisabled(): boolean {
  return !useAiEnabled();
}
