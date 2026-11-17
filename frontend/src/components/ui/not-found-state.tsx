// SPDX-License-Identifier: Apache-2.0

/**
 * Shared not-found state for entity detail pages.
 *
 * Replaces the inconsistent "not found" patterns duplicated across
 * AgentDetail, IntegrationDetail, KnowledgeBaseDetail, MCPServerDetail,
 * and CustomToolDetail detail pages.
 */

import { Card, CardContent, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { BackButton } from "@/components/ui/back-button";
import { useFormatter } from "@/i18n";

interface NotFoundStateProps {
  /** Lucide icon rendered above the title. */
  icon: React.ElementType;
  /** Main heading (e.g. "Agent not found"). */
  title: string;
  /** Descriptive subtitle. */
  description: string;
  /** Label on the action button (e.g. "View All Agents"). */
  actionLabel: string;
  /** Called when the action button is clicked. */
  onAction: () => void;
  /** Back button route path. */
  backTo: string;
  /** Back button label (e.g. "Back to Agents"). */
  backLabel: string;
}

export function NotFoundState({
  icon: Icon,
  title,
  description,
  actionLabel,
  onAction,
  backTo,
  backLabel,
}: NotFoundStateProps) {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-6">
      <BackButton to={backTo} label={backLabel} />
      <Card className="border-dashed">
        <CardContent className="flex flex-col items-center justify-center py-12 text-center">
          <Icon className="h-10 w-10 text-default-secondary mb-3" />
          <CardTitle className="text-base mb-1">{t_i18n(title)}</CardTitle>
          <p className="content-base text-default-secondary">{t_i18n(description)}</p>
          <Button className="mt-4" variant="secondary" onClick={onAction}>
            {t_i18n(actionLabel)}
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
