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
 * The changes the Memory Steward made to a memory lane, with one-click undo
 * (#8258). Shown on the profile (the user's own lanes) and on an agent's
 * Memory tab (its own lane, for its managers).
 */

import { Undo2, Sparkles } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { formatTimeAgo } from "@/lib/relative-time";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { MemoryConsolidation } from "@/types";
import { Spinner } from "@filigran/design-system";

const OPERATION_LABELS: Record<MemoryConsolidation["operation"], string> = {
  merge: "Duplicates merged",
  remove: "Contradiction settled",
  flag: "Contradiction noted",
  demote: "Outdated entry retired",
  promote: "Kept as a lasting fact",
};

const ROLE_LABELS: Record<MemoryConsolidation["entries"][number]["role"], string> = {
  merged: "Merged",
  removed: "Replaced",
  demoted: "Retired",
  flagged: "In conflict",
  kept: "Kept",
  created: "Added",
};

interface Props {
  items: MemoryConsolidation[];
  retentionDays: number;
  /** Name the agent of each item: the profile lists several agents' lanes. */
  showAgent: boolean;
  onUndo: (id: string, callbacks: { onSuccess: () => void; onError: () => void }) => void;
  undoingId: string | null;
  /** The undo writes shared AI content (an agent's own lane): unavailable while AI is off. */
  aiGated?: boolean;
}

export function MemoryConsolidationsCard({ items, retentionDays, showAgent, onUndo, undoingId, aiGated = false }: Props) {
  const { t_i18n, intl } = useFormatter();
  const aiOff = useAiActionDisabled() && aiGated;
  const { toast } = useToast();

  const undo = (id: string) =>
    onUndo(id, {
      onSuccess: () => toast({ title: t_i18n("Change undone"), variant: "success" }),
      onError: () => toast({ title: t_i18n("This change can no longer be undone"), variant: "error" }),
    });
  const undoButton = (id: string) => (
    <Button
      variant="ghost"
      size="sm"
      className="gap-1.5 shrink-0"
      onClick={() => undo(id)}
      disabled={undoingId !== null || aiOff} aria-busy={(undoingId === id) || undefined}>
      {undoingId === id && <Spinner size="sm" tone="inherit" />}{!(undoingId === id) && <Undo2 className="h-3 w-3" />}
      {t_i18n("Undo")}
    </Button>
  );

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-3">
          <Sparkles className="h-5 w-5 text-accent" />
          <div>
            <CardTitle className="text-base">{t_i18n("Memory consolidations")}</CardTitle>
            <CardDescription>
              {t_i18n("The Memory Steward tidies this memory: it merges duplicates, settles contradictions and retires outdated entries. Each change can be undone for {days, plural, one {# day} other {# days}}.", { days: retentionDays })}
            </CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent className="pt-0">
        {items.length === 0 ? (
          <p className="content-compact text-default-secondary">{t_i18n("No consolidation yet.")}</p>
        ) : (
          <ul className="space-y-3">
            {items.map(item => (
              <li key={item.id} className="rounded-sm bg-elevation-default p-3 space-y-2">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex flex-wrap items-center gap-2 min-w-0">
                    <span className="content-base font-medium">{t_i18n(OPERATION_LABELS[item.operation])}</span>
                    {showAgent && <Badge variant="outline">{item.agent_name}</Badge>}
                    {item.undone_at && <Badge variant="secondary">{t_i18n("Undone")}</Badge>}
                    <span className="content-compact text-default-secondary tabular-nums">{formatTimeAgo(intl, item.applied_at)}</span>
                  </div>
                  {item.undoable && (aiGated ? <AiDisabledTip>{undoButton(item.id)}</AiDisabledTip> : undoButton(item.id))}
                </div>
                <ul className="space-y-1">
                  {item.entries.map(entry => (
                    <li key={`${item.id}-${entry.id}`} className="flex gap-2 content-compact">
                      <span className="shrink-0 w-20 text-default-secondary">{t_i18n(ROLE_LABELS[entry.role])}</span>
                      <span className={cn("min-w-0 break-words", !entry.exists && "italic text-default-secondary")}>
                        {entry.exists ? entry.content : t_i18n("(deleted)")}
                      </span>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
