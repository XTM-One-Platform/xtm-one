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

/** The frame every Settings > Agents and automations > Platform Health block shares (#8254, #8261): one draft of the whole settings, one Edit / Save. */
import { EnvLockBadge } from "@/components/ui/env-lock-badge";
import { SettingsGroupDescription, SettingsGroupTitle } from "@/components/ui/settings-form";
import { useHealthSpendLimits } from "@/hooks/api/useHealthApi";
import { useFormatter } from "@/i18n";
import type { HealthSettings } from "@/types";

/** The variable that unlocks the spend ceilings, when the server does not name it. */
const SPEND_LIMITS_ENV_VAR = "PLATFORM_HEALTH_SPEND_LIMITS_UNLOCKED";

/**
 * Whether the spend ceilings are locked at their defaults (#1202) and the variable that unlocks
 * them. Locked until the server says otherwise: an editable field the server would refuse is worse
 * than a read-only one for the instant the answer takes.
 */
export function useSpendLock(): { locked: boolean; envVar: string } {
  const { data } = useHealthSpendLimits();
  return { locked: data?.locked ?? true, envVar: data?.env_var ?? SPEND_LIMITS_ENV_VAR };
}

/** One line under a block's title while its ceilings are locked: why, and the variable that unlocks them. */
export function SpendLockNotice({ envVar, testId }: { envVar: string; testId: string }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex items-center gap-2 content-compact text-default-secondary" data-testid={testId}>
      <EnvLockBadge />
      <span>
        {t_i18n("Locked at their defaults, since this spend is outside the agentic quota. Set {variable} to true to change them.", {
          variable: envVar,
        })}
      </span>
    </div>
  );
}

export type BlockProps = {
  draft: HealthSettings;
  editing: boolean;
  onChange: (next: HealthSettings) => void;
  /** The save refused the draft's "Run checks as" user: the message shown under that field (UX r1 B1). */
  runAsError?: string | null;
};

export function BlockTitle({ icon: Icon, title, hint }: { icon: React.ComponentType<{ className?: string }>; title: string; hint: string }) {
  return (
    <div className="mb-2">
      <SettingsGroupTitle icon={Icon}>{title}</SettingsGroupTitle>
      <SettingsGroupDescription>{hint}</SettingsGroupDescription>
    </div>
  );
}
