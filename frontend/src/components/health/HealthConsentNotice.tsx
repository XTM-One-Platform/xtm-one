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

import { Icon } from "@filigran/design-system";
/**
 * The Platform Concierge consent notice (#8254, PRD decision B16): what the
 * Concierge reads once an agent or a flow is opted in, and who then sees it.
 * Shown where its managers opt it in and, the agent text, where a user opts out.
 */

import { useFormatter } from "@/i18n";

/** The notice per target. @i18n-keys */
const NOTICES = {
  agent:
    "When an agent is opted in, Platform Health reads its memories and the content of its conversations to improve it. Conversation excerpts become evaluation cases, unredacted, visible to the agent's managers and to administrators. Anyone can opt out in their profile: their memories and conversations are then never read, and the cases already built from them are deleted.",
  flow: "When a flow is opted in, Platform Health reads the journals of its runs to improve it. Run excerpts become evaluation cases, unredacted, visible to the flow's managers and to administrators. Anyone can opt out in their profile: their content is then never read, and the cases already built from it are deleted.",
} as const;

export function HealthConsentNotice({ targetType = "agent" }: { targetType?: keyof typeof NOTICES }) {
  const { t_i18n } = useFormatter();
  return (
    <div
      className="flex items-start gap-2.5 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2.5"
      data-testid="health.consent-notice"
    >
      <Icon name="info" size={16} className="mt-px shrink-0 text-default-secondary" />
      <p className="content-compact text-default-secondary">{t_i18n(NOTICES[targetType])}</p>
    </div>
  );
}
