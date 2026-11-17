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

import { Link } from "react-router-dom";
import { Icon } from "@filigran/design-system";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { PageSkeleton } from "@/components/ui/skeleton";
import { useSkillTier3Tools } from "@/hooks/api/useSkillApi";
import { useFormatter } from "@/i18n";

export function ResourcesTab({ skillId }: { skillId: string }) {
  const { t_i18n } = useFormatter();
  const { data, isLoading } = useSkillTier3Tools(skillId);

  if (isLoading) {
    return <PageSkeleton cards={2} />;
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t_i18n("Resources")}</CardTitle>
      </CardHeader>
      <CardContent>
        {!data || data.length === 0 ? (
          <p className="content-base text-default-secondary">{t_i18n("No linked custom-tool resources for this skill.")}</p>
        ) : (
          <div className="space-y-3">
            {data.map((tool) => (
              <div key={`${tool.id}-${tool.resource_path}`} className="rounded-sm border border-elevation-subtle-soft p-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate font-medium">{tool.name}</div>
                    <div className="content-compact text-default-secondary truncate">{tool.description || t_i18n("No description")}</div>
                  </div>
                  <Link
                    to={`/tools/${tool.id}`}
                    className="inline-flex items-center gap-1 content-compact text-accent hover:underline shrink-0"
                  >
                    {t_i18n("Open tool")} <Icon name="external-link" size={16} />
                  </Link>
                </div>
                <div className="mt-2">
                  <Badge variant="outline" className="content-caption">{tool.resource_path}</Badge>
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
