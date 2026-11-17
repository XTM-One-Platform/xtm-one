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
 * Empty state card for entity detail page Activity tabs.
 *
 * Usage:
 *   <ActivityEmptyState entityLabel="integration" />
 *   <ActivityEmptyState entityLabel="MCP server" />
 */

import { Activity } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { useFormatter } from "@/i18n";

interface ActivityEmptyStateProps {
  entityLabel: string;
  /**
   * Replaces the default sentence. For an entity whose activity is not
   * "runs of the agents using it" — an A2A integration's is work its partner
   * submitted — the default describes the wrong thing.
   */
  message?: string;
}

export function ActivityEmptyState({ entityLabel, message }: ActivityEmptyStateProps) {
  const { t_i18n } = useFormatter();
  return (
    <Card className="border-dashed">
      <CardContent className="py-8 text-center">
        <Activity className="h-8 w-8 text-default-disabled mx-auto mb-2" />
        <p className="content-base text-default-secondary">
          {message ?? t_i18n("No activity yet. Assignment runs from agents using this {entity} will appear here.", { entity: t_i18n(entityLabel) })}
        </p>
      </CardContent>
    </Card>
  );
}
