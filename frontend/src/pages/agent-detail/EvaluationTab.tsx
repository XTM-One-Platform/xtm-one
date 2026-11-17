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

import { useNavigate } from "react-router-dom";
import { FlaskConical, Plus, Cpu, Database, ExternalLink, Play } from "lucide-react";
import { useEvaluations, useTriggerEvaluation } from "@/hooks/api/useEvaluationApi";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { AutomaticRunsPausedBadge } from "@/components/evaluation/automatic-runs-badge";
import { useTriggerFailedToast } from "@/components/evaluation/use-trigger-failed-toast";
import { EmptyState } from "@/components/ui/empty-state";
import { Tip } from "@/components/ui/tooltip";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import type { Agent, Evaluation } from "@/types";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { AiDisabledTip } from "@/components/ui/ai-disabled";
import { formatTimeAgo } from "@/lib/relative-time";
import { Spinner } from "@filigran/design-system";

function EvaluationCard({ evaluation }: { evaluation: Evaluation }) {
  const { t_i18n, intl } = useFormatter();
  const navigate = useNavigate();
  const trigger = useTriggerEvaluation();
  const triggerFailed = useTriggerFailedToast();
  const aiOff = useAiActionDisabled();
  const { toast } = useToast();

  const handleTrigger = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      const runs = await trigger.mutateAsync({ evaluationId: evaluation.id });
      toast({
        title: t_i18n("Evaluation triggered"),
        description: t_i18n("{count, plural, one {# run} other {# runs}} started.", { count: runs.length }),
        variant: "success",
      });
    } catch (error) {
      triggerFailed(error);
    }
  };

  return (
    <Card
      className="layer-1 bg-elevation-default backdrop-blur-sm border-elevation-subtle-soft hover:border-accent/30 transition-all cursor-pointer"
      onClick={() => navigate(`/evaluations/${evaluation.id}`)}
    >
      <CardContent className="p-4">
        <div className="flex items-start justify-between">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <FlaskConical className="h-4 w-4 text-accent shrink-0" />
              <h3 className="font-medium truncate">{evaluation.name}</h3>
            </div>
            {evaluation.description && (
              <p className="content-compact text-default-secondary mt-1 line-clamp-2">
                {evaluation.description}
              </p>
            )}
            <div className="flex items-center gap-3 mt-2 content-compact text-default-secondary">
              <span className="flex items-center gap-1">
                <Cpu className="h-3 w-3" />
                {t_i18n("{count, plural, one {# model} other {# models}}", { count: evaluation.model_ids.length })}
              </span>
              <span className="flex items-center gap-1">
                <Database className="h-3 w-3" />
                {t_i18n("{count, plural, one {# entry} other {# entries}}", { count: evaluation.entry_count })}
              </span>
              {evaluation.last_triggered_at && (
                <span>{t_i18n("Last run {time}", { time: formatTimeAgo(intl, evaluation.last_triggered_at) })}</span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0 ml-3">
            <AiDisabledTip>
            <Tip content={t_i18n("Run this evaluation now")}>
              <Button
                variant="ghost"
                size="sm"
                className="h-8 w-8 p-0 text-accent hover:text-accent hover:bg-accent/10"
                aria-label={t_i18n("Run this evaluation now")}
                onClick={handleTrigger}
                disabled={trigger.isPending || aiOff} aria-busy={trigger.isPending || undefined}>
                {trigger.isPending && <Spinner size="sm" tone="inherit" />}{!trigger.isPending && <Play className="h-4 w-4" />}
              </Button>
            </Tip>
            </AiDisabledTip>
            <AutomaticRunsPausedBadge evaluation={evaluation} className="content-caption" />
            <ExternalLink className="h-3.5 w-3.5 text-default-secondary" />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export function EvaluationTab({ agent }: { agent: Agent; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const { data: evaluations, isLoading } = useEvaluations(agent.id, { scope: "all" });

  if (isLoading) {
    return (
      <div className="space-y-3">
        {[1, 2].map((i) => (
          <div key={i} className="h-24 rounded-sm bg-elevation-hover animate-pulse" />
        ))}
      </div>
    );
  }

  if (!evaluations || evaluations.length === 0) {
    return (
      <EmptyState
        icon={FlaskConical}
        title="No evaluations"
        description="This agent is not part of any evaluation yet. Create an evaluation to benchmark its performance."
        actionLabel="Create Evaluation"
        onAction={() => navigate("/evaluations/new")}
      />
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="content-base text-default-secondary">
          {t_i18n("{count, plural, one {# evaluation} other {# evaluations}}", { count: evaluations.length })}
        </p>
        <Button
          size="sm"
          variant="outline"
          className="gap-1.5"
          onClick={() => navigate("/evaluations/new")}
        >
          <Plus className="h-3.5 w-3.5" />
          {t_i18n("New Evaluation")}
        </Button>
      </div>
      <div className="space-y-3">
        {evaluations.map((ev) => (
          <EvaluationCard key={ev.id} evaluation={ev} />
        ))}
      </div>
    </div>
  );
}
