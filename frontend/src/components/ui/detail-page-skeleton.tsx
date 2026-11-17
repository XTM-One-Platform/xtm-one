// SPDX-License-Identifier: Apache-2.0

import { DetailTabsSkeleton } from "@/components/ui/detail-tabs";
import { EntityPageHeaderSkeleton } from "@/components/ui/entity-page-header";
import { Skeleton } from "@/components/ui/skeleton";
import { useFormatter } from "@/i18n";

/**
 * An entity detail page while its entity is read (#11345): the loaded page's
 * own stack (`space-y-6`), the entity header with its content greyed out
 * (`EntityPageHeaderSkeleton`), the tab bar on the library's triggers
 * (`DetailTabsSkeleton`), then the first tab's panel as greyed lines. It
 * replaces the generic `PageSkeleton` (a 32 px title block over a two-column
 * card grid) that no detail page looks like, so the back link, the header and
 * the tab bar do not move when the entity lands.
 */
export function DetailPageSkeleton({
  backTo,
  backLabel,
  tabs,
  tile = true,
  descriptionLines = 0,
  meta = true,
  actions = 0,
}: {
  backTo: string | number;
  /** An i18n key, translated by BackButton. */
  backLabel: string;
  /** How many tabs the loaded page shows (0: no tab bar). */
  tabs: number;
  tile?: boolean;
  descriptionLines?: 0 | 1 | 2;
  meta?: boolean;
  actions?: number;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-6" role="status" aria-busy="true" aria-label={t_i18n("Loading...")} data-testid="detail-page-skeleton">
      <EntityPageHeaderSkeleton
        backTo={backTo}
        backLabel={backLabel}
        tile={tile}
        descriptionLines={descriptionLines}
        meta={meta}
        actions={actions}
      />
      {tabs > 0 && <DetailTabsSkeleton count={tabs} />}
      <div className="space-y-3 rounded-sm layer-1 bg-elevation-default p-6" data-slot="panel">
        <Skeleton className="h-4 w-40" />
        <Skeleton className="h-3 w-full" />
        <Skeleton className="h-3 w-11/12" />
        <Skeleton className="h-3 w-4/5" />
      </div>
    </div>
  );
}
