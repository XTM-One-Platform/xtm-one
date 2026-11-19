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
 * One agent's or flow's Platform Health card (#8261): its improvement proposals,
 * its cycles and their digests, and "Improve with {name}". Shown only to who
 * may manage the target (built in since #101 S1, no flag): the server reads the
 * same rule (`cycle_access`), so nothing here widens what the viewer sees.
 */
import { Icon } from "@filigran/design-system";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useFormatter } from "@/i18n";
import { HealthCyclesList } from "./HealthCyclesList";
import { ImprovementsInbox } from "./ImprovementsInbox";
import { ImproveWithHealthButton } from "./ImproveWithHealthButton";
import { useHealthName } from "@/hooks/api/useHealthApi";

export function HealthTargetCard({
  targetType,
  targetId,
  canManage,
}: {
  targetType: "agent" | "flow";
  targetId: string;
  canManage: boolean;
}) {
  const { t_i18n } = useFormatter();
  const healthName = useHealthName();
  if (!canManage) return null;
  const fixed = targetType === "agent" ? { agent_id: targetId } : { target_type: "flow" as const, target_id: targetId };
  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-4 space-y-0">
        <div>
          <CardTitle className="flex items-center gap-2">
            <Icon name="compass" size={16} className="text-accent" />
            {t_i18n("Improvements")}
          </CardTitle>
          <CardDescription>
            {t_i18n("What {name} proposes for it, and its cycles. A change reaches production only once you approve it.", { name: healthName })}
          </CardDescription>
        </div>
        <ImproveWithHealthButton targetType={targetType} targetId={targetId} canManage={canManage} />
      </CardHeader>
      <CardContent className="space-y-4">
        <ImprovementsInbox fixed={fixed} />
        <div className="space-y-2">
          <p className="content-compact font-medium text-default-secondary">{t_i18n("Cycles")}</p>
          <HealthCyclesList targetType={targetType} targetId={targetId} />
        </div>
      </CardContent>
    </Card>
  );
}
