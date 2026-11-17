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

import { useState } from "react";
import { Brain, AlertTriangle, Trash2 } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { usePurgeAllMemories } from "@/hooks/api/useSettingsApi";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledNotice } from "@/components/ui/ai-disabled";
import { Spinner } from "@filigran/design-system";

// ---------------------------------------------------------------------------
// Emergency Memory Purge (admin-only section)
// ---------------------------------------------------------------------------
export function MemoryPurgeSection() {
  const aiOff = useAiActionDisabled();
  const { t_i18n } = useFormatter();
  const purge = usePurgeAllMemories();
  const [ConfirmEl, confirm] = useConfirm();
  const { toast } = useToast();
  const [lastResult, setLastResult] = useState<{
    persistent_deleted: number;
    style_embeddings_deleted: number;
    redis_deleted: number;
    total_deleted: number;
  } | null>(null);

  const handlePurge = async () => {
    const confirmed = await confirm({
      title: "Purge all agent memories?",
      description:
        "This will permanently delete ALL agent memories across the entire platform, " +
        "including persistent memories, style learning embeddings, " +
        "short-term memories, medium-term consolidated memories, and user memories. " +
        "This action cannot be undone.",
      confirmLabel: "Purge All Memories",
      variant: "danger",
    });
    if (!confirmed) return;

    purge.mutate(undefined, {
      onSuccess: (data) => {
        setLastResult({
          persistent_deleted: data.persistent_deleted,
          style_embeddings_deleted: data.style_embeddings_deleted,
          redis_deleted: data.redis_deleted,
          total_deleted: data.total_deleted,
        });
        toast({
          title: t_i18n("Memories purged"),
          description: t_i18n("Deleted {count} memory entries across all agents.", { count: data.total_deleted }),
          variant: "success",
        });
      },
      onError: (err) => {
        toast({
          title: t_i18n("Purge failed"),
          description: err.message || t_i18n("Failed to purge memories. Please try again."),
          variant: "error",
        });
      },
    });
  };

  return (
    <>
      {/* FR-022: an AI settings section renders read-only with the reason
          stated, rather than disappearing. */}
      <AiDisabledNotice />
      {ConfirmEl}
      <Card className="border-feedback-error-primary/20">
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="flex items-center gap-2">
                <Brain className="h-4 w-4 text-feedback-error-primary" />
                {t_i18n("Emergency Memory Purge")}
              </CardTitle>
              <CardDescription className="mt-1">
                {t_i18n("Permanently delete all agent memories across the platform. Use this if you discover a flaw in the memory system or need to reset all agent knowledge.")}
              </CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="rounded-sm bg-feedback-error-primary/5 border border-feedback-error-primary/10 p-4 space-y-3">
            <div className="flex items-start gap-3">
              <AlertTriangle className="h-5 w-5 text-feedback-error-primary shrink-0 mt-0.5" />
              <div className="space-y-2">
                <p className="content-base font-medium text-feedback-error-primary">{t_i18n("Danger Zone")}</p>
                <p className="content-compact text-default-secondary">
                  {t_i18n("This will wipe all three memory tiers for every agent on the platform:")}
                </p>
                <ul className="content-compact text-default-secondary space-y-1 ml-4 list-disc">
                  <li><strong>{t_i18n("Persistent memories")}</strong> — {t_i18n("durable facts, style notes, and extracted knowledge stored in the database")}</li>
                  <li><strong>{t_i18n("Style learning embeddings")}</strong> — {t_i18n("learned writing styles and communication patterns")}</li>
                  <li><strong>{t_i18n("Short-term memories")}</strong> — {t_i18n("recent interactions cached in Redis (24h rolling window)")}</li>
                  <li><strong>{t_i18n("Medium-term memories")}</strong> — {t_i18n("consolidated summaries stored in Redis (30-day TTL)")}</li>
                  <li><strong>{t_i18n("User memories")}</strong> — {t_i18n("cross-agent user personalization data")}</li>
                </ul>
                <p className="content-compact text-default-secondary">
                  {t_i18n("Agents will continue to function but will have no memory of past interactions. This action is irreversible and will be recorded in the audit log.")}
                </p>
              </div>
            </div>
          </div>

          {lastResult && (
            <div className="rounded-sm bg-elevation-default p-3 content-compact text-default-secondary space-y-1">
              <p className="font-medium text-default-primary">{t_i18n("Last purge result:")}</p>
              <p>{t_i18n("Persistent memories deleted:")} <span className="text-default-primary">{lastResult.persistent_deleted}</span></p>
              <p>{t_i18n("Style learning embeddings deleted:")} <span className="text-default-primary">{lastResult.style_embeddings_deleted}</span></p>
              <p>{t_i18n("Redis memory keys deleted:")} <span className="text-default-primary">{lastResult.redis_deleted}</span></p>
              <p>{t_i18n("Total entries removed:")} <span className="text-default-primary font-medium">{lastResult.total_deleted}</span></p>
            </div>
          )}

          <Button
            variant="destructive"
            className="gap-2"
            onClick={handlePurge}
            disabled={purge.isPending || aiOff} aria-busy={purge.isPending || undefined}>
            {purge.isPending && <Spinner size="sm" tone="inherit" />}{!purge.isPending && <Trash2 className="h-4 w-4" />}
            {purge.isPending ? t_i18n("Purging...") : t_i18n("Purge All Agent Memories")}
          </Button>
        </CardContent>
      </Card>
    </>
  );
}
