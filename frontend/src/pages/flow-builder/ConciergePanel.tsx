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

/** The flow's Concierge panel (#8261): its improvement opt-in, its proposals and cycles, and "Improve with Concierge", for its managers. */
import { Icon } from "@filigran/design-system";
import { ConciergeCyclesList } from "@/components/concierge/ConciergeCyclesList";
import { ConciergeOptInCard } from "@/components/concierge/ConciergeOptInCard";
import { ImprovementsInbox } from "@/components/concierge/ImprovementsInbox";
import { ImproveWithConciergeButton } from "@/components/concierge/ImproveWithConciergeButton";
import { Button } from "@/components/ui/button";
import { useFormatter } from "@/i18n";

export function ConciergePanel({ flowId, canManage, onClose }: { flowId: string; canManage: boolean; onClose: () => void }) {
  const { t_i18n } = useFormatter();
  return (
    <aside className="flex w-96 shrink-0 flex-col border-l border-elevation-subtle layer-1 bg-elevation-default">
      <div className="flex items-center gap-2 border-b border-elevation-subtle p-3">
        <p className="flex-1 content-base font-medium">{t_i18n("Improvements")}</p>
        <Button variant="ghost" size="icon" onClick={onClose} aria-label={t_i18n("Close")}>
          <Icon name="x" size={16} />
        </Button>
      </div>
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-3">
        <ConciergeOptInCard targetType="flow" targetId={flowId} />
        <ImproveWithConciergeButton targetType="flow" targetId={flowId} canManage={canManage} />
        <ImprovementsInbox fixed={{ target_type: "flow", target_id: flowId }} />
        <div className="space-y-2">
          <p className="content-compact font-medium text-default-secondary">{t_i18n("Cycles")}</p>
          <ConciergeCyclesList targetType="flow" targetId={flowId} />
        </div>
      </div>
    </aside>
  );
}
