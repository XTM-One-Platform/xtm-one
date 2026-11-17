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
 * The card a ``[[WORK_ITEM:uuid]]`` marker renders in the chat: the
 * main surface of a one-off action. Read by id, so it always shows the item as
 * it is now - scheduled, running, done with its proof, blocked or missed.
 */
import { Icon } from "@filigran/design-system";
import { UnavailableCard } from "@/components/chat/UnavailableCard";
import { TruncatedText } from "@/components/ui/truncated-text";
import { useWorkItem } from "@/hooks/api/useWorkItemApi";
import { useFormatter } from "@/i18n";
import { isScheduledItem } from "@/lib/scheduled-work-items";
import { ScheduleActions } from "./ScheduleActions";
import { ScheduleStateBadge, ScheduledDetails } from "./ScheduledDetails";

export function ScheduledWorkItemCard({ workItemId }: { workItemId: string }) {
  const { t_i18n } = useFormatter();
  const { data: item, isLoading, isError } = useWorkItem(workItemId);

  if (isLoading) {
    return <div className="my-2 h-16 animate-pulse rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default" />;
  }
  if (isError || !item || !isScheduledItem(item)) {
    return <UnavailableCard message={t_i18n("This scheduled work item is no longer available.")} />;
  }
  return (
    // The same DS elevation surface and border as the loading state: no accent tint (UX round 3 B2, #10911).
    <div className="my-2 rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default p-3" data-testid="scheduled-work-item-card">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <Icon name="calendar" size={16} className="shrink-0 text-accent" aria-hidden />
          {/* A cut title says itself whole in the design-system tooltip (#10910). */}
          <TruncatedText text={item.title} className="content-base font-medium" />
          <ScheduleStateBadge scheduled={item.scheduled} />
        </div>
        <ScheduleActions item={item} compact />
      </div>
      <div className="mt-2 pl-6">
        <ScheduledDetails item={item} />
      </div>
    </div>
  );
}
