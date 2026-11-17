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
import { ICON_MAP } from "@/lib/constants";
import { useFormatter } from "@/i18n";

/**
 * Who wrote a reply, on a turn several @mentioned agents answered.
 * Only such replies carry an attribution, so every other message is unchanged.
 * Shared by the chat transcript and voice mode's.
 */
export function MessageAttributionLine({
  agentName,
  agentIcon,
  failed,
}: {
  agentName: string;
  agentIcon?: string;
  failed: boolean;
}) {
  const { t_i18n } = useFormatter();
  const AgentIcon = agentIcon ? ICON_MAP[agentIcon] : undefined;
  return (
    <div className="mb-1 flex items-center gap-1.5 content-compact font-medium text-accent/80">
      {AgentIcon ? <AgentIcon className="size-4" aria-hidden="true" /> : <Icon name="bot" size={16} />}
      <span>{agentName}</span>
      {failed && (
        <span className="font-normal text-default-secondary">
          {"\u00b7 "}
          {t_i18n("Did not answer")}
        </span>
      )}
    </div>
  );
}
