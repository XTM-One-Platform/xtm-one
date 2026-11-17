// SPDX-License-Identifier: Apache-2.0

/**
 * Shared timestamp grid for detail page OverviewTabs.
 *
 * Renders created/updated date pairs with Calendar icons in a
 * responsive grid. Replaces the identical pattern duplicated across
 * mcp-server-detail/OverviewTab and custom-tool-detail/OverviewTab.
 */

import { Calendar } from "lucide-react";
import { formatDate } from "@/lib/utils";
import { useFormatter } from "@/i18n";

interface TimestampGridProps {
  createdAt: string;
  updatedAt: string;
}

export function TimestampGrid({ createdAt, updatedAt }: TimestampGridProps) {
  const { t_i18n } = useFormatter();
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <div className="flex items-center gap-2">
        <Calendar className="h-3 w-3 text-default-secondary" />
        <div>
          <div className="content-caption text-default-secondary">{t_i18n("Created")}</div>
          <p className="content-compact">{formatDate(createdAt)}</p>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <Calendar className="h-3 w-3 text-default-secondary" />
        <div>
          <div className="content-caption text-default-secondary">{t_i18n("Updated")}</div>
          <p className="content-compact">{formatDate(updatedAt)}</p>
        </div>
      </div>
    </div>
  );
}
