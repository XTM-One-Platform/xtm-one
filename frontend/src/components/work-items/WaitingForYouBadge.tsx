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
 * The "Waiting for you" marker of a work item owed by the reader (the
 * Waiting for me population, #1001). One component, one glyph, one size and
 * the accent tone, so a grid card and a graph node draw the same marker.
 */
import { Icon } from "@filigran/design-system";
import { Badge } from "@/components/ui/badge";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

export function WaitingForYouBadge({ testId, className }: { testId: string; className?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <Badge
      variant="secondary"
      className={cn("shrink-0 gap-1 content-caption bg-accent/10 text-accent", className)}
      data-testid={testId}
    >
      <Icon name="user" size={16} className="size-3 shrink-0" aria-hidden />
      {t_i18n("Waiting for you")}
    </Badge>
  );
}
