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

import { AlertCircle, CheckCircle2, Clock, PauseCircle, Unplug } from "lucide-react";
import { useFormatter } from "@/i18n";
import { useTick } from "@/hooks/useTick";
import { INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";
import { formatTimeAgoLine } from "@/lib/relative-time";
import { cn } from "@/lib/utils";
import type { TriggerConfig } from "@/types";
import { provisioningView, type ProvisioningKind } from "./provisioning-state";

const ICONS: Record<ProvisioningKind, { icon: React.ElementType; tone: string }> = {
  provisioned: { icon: CheckCircle2, tone: "text-feedback-success-primary" },
  error: { icon: AlertCircle, tone: "text-feedback-error-primary" },
  orphaned: { icon: Unplug, tone: "text-feedback-warning-primary" },
  pending: { icon: Clock, tone: "text-default-secondary" },
  disabled: { icon: PauseCircle, tone: "text-default-secondary" },
};

/**
 * Read-only line of an integration-event assignment whose trigger XTM One
 * creates in the source platform (OpenCTI, OpenAEV): what is there, when it
 * was last checked and when the last event ran the assignment. Nothing is
 * shown for an assignment that declares no provisioning and holds none.
 */
export function ProvisioningSummary({ config, enabled, lastTriggeredAt }: {
  config: TriggerConfig | undefined;
  enabled: boolean;
  lastTriggeredAt: string | null;
}) {
  const { t_i18n, intl } = useFormatter();
  useTick();
  const view = provisioningView(config, enabled);
  if (!view) return null;
  const platform = INTEGRATION_PROVIDER_NAMES[view.provider] ?? view.provider;
  const { icon: Icon, tone } = ICONS[view.kind];
  const name = view.state?.name;

  const title = {
    provisioned: t_i18n("Provisioned in {platform}", { platform }),
    error: t_i18n("Provisioning in {platform} failed", { platform }),
    orphaned: t_i18n("Still held in {platform}", { platform }),
    pending: t_i18n("Waiting to be provisioned in {platform}", { platform }),
    disabled: t_i18n("Not provisioned in {platform} while paused", { platform }),
  }[view.kind];

  const detail = {
    provisioned: name
      ? t_i18n("{count, plural, =0 {A notifier named {name}.} one {One trigger and its notifier, named {name}.} other {# triggers and their notifier, named {name}.}}", { count: view.triggerCount, name })
      : null,
    error: t_i18n("The last check could not set up the trigger. It is tried again at the next check."),
    orphaned: t_i18n("This assignment no longer needs its trigger, but {platform} could not be reached to remove it. It is removed at the next check that reaches it.", { platform }),
    pending: t_i18n("The trigger and its notifier are created at the next check, within a few minutes."),
    disabled: t_i18n("Nothing is created in {platform} until an administrator enables this assignment.", { platform }),
  }[view.kind];

  const checkedAt = view.state?.checked_at;
  const checked = checkedAt
    ? formatTimeAgoLine(intl, Date.parse(checkedAt), {
        line: (when) => t_i18n("Checked {when}", { when }),
        justNow: () => t_i18n("Checked just now"),
        fallback: checkedAt,
      })
    : null;
  const lastEvent = view.kind === "provisioned"
    ? lastTriggeredAt
      ? formatTimeAgoLine(intl, Date.parse(lastTriggeredAt), {
          line: (when) => t_i18n("Last event {when}", { when }),
          justNow: () => t_i18n("Last event just now"),
          fallback: lastTriggeredAt,
        })
      : t_i18n("No event yet")
    : null;

  return (
    <div className="flex items-start gap-3 rounded-sm bg-elevation-hover px-4 py-3" data-testid="provisioning-summary">
      <Icon className={cn("h-4 w-4 mt-0.5 shrink-0", tone)} aria-hidden="true" />
      <div className="min-w-0 flex-1 space-y-1">
        <p className="content-compact font-medium">{title}</p>
        {detail && <p className="content-caption text-default-secondary break-words">{detail}</p>}
        {(checked || lastEvent) && (
          <p className="content-caption text-default-secondary">
            {[checked, lastEvent].filter(Boolean).join(" · ")}
          </p>
        )}
      </div>
    </div>
  );
}
