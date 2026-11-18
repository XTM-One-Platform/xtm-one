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
 * The Platform Concierge improvement opt-in of an agent or a flow (#8254):
 * whoever may manage it lets the Concierge read its content to improve it
 * (PRD decision B21), under the consent notice (B16). Built in since #101
 * (S1), no flag; read-only for a viewer who cannot manage it.
 */
import { Sparkles } from "lucide-react";

import { HealthConsentNotice } from "@/components/health/HealthConsentNotice";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { type OptInTarget, useHealthOptIn, useSetHealthOptIn } from "@/hooks/api/useHealthApi";
import { useFormatter } from "@/i18n";
import { Spinner } from "@filigran/design-system";

/** The card's catalog keys, per target. @i18n-keys */
const TEXTS = {
  agent: {
    description:
      "Let Platform Health consolidate this agent's memories, diagnose it from its conversations and propose improvements. Every proposal waits for approval by the agent's managers.",
    on: "Platform Health may now improve this agent",
    off: "Platform Health no longer reads this agent's content",
    readOnly: "Only the agent's managers can change this.",
  },
  flow: {
    description:
      "Let Platform Health diagnose this flow from the journals of its runs and propose improvements. Every proposal waits for approval by the flow's managers.",
    on: "Platform Health may now improve this flow",
    off: "Platform Health no longer reads this flow's content",
    readOnly: "Only the flow's managers can change this.",
  },
} as const satisfies Record<OptInTarget, Record<string, string>>;

export function HealthOptInCard({ targetType, targetId }: { targetType: OptInTarget; targetId: string }) {
  const { data, isLoading } = useHealthOptIn(targetType, targetId);
  if (isLoading || !data || !data.eligible) return null;
  return <OptInCard targetType={targetType} targetId={targetId} optIn={data.opt_in} canManage={data.can_manage} />;
}

function OptInCard({
  targetType,
  targetId,
  optIn,
  canManage,
}: {
  targetType: OptInTarget;
  targetId: string;
  optIn: boolean;
  canManage: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const save = useSetHealthOptIn(targetType, targetId);
  const texts = TEXTS[targetType];
  const onChange = (next: boolean) =>
    save.mutate(next, {
      onSuccess: () => toast({ title: t_i18n(next ? texts.on : texts.off), variant: "success" }),
      onError: (err) =>
        toast({
          title: t_i18n("Platform Health opt-in was not changed"),
          description: err instanceof Error ? err.message : undefined,
          variant: "error",
        }),
    });

  return (
    <Card data-testid={`${targetType}.concierge-opt-in`}>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 content-base">
          <Sparkles className="size-4 text-accent" />
          {t_i18n("Improvement by Platform Health")}
        </CardTitle>
        <CardDescription className="content-compact">{t_i18n(texts.description)}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 pt-0">
        <div className="flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2.5">
          <div className="min-w-0">
            <p className="content-base font-medium">{t_i18n("Opt in to improvement")}</p>
            <p className="content-compact text-default-secondary">
              {canManage ? t_i18n("Off by default. Turning it off stops every read at once.") : t_i18n(texts.readOnly)}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {save.isPending && <Spinner size="sm" tone="inherit" className="text-default-secondary" />}
            <Switch
              checked={optIn}
              onCheckedChange={onChange}
              disabled={!canManage || save.isPending}
              aria-label={t_i18n("Opt in to improvement")}
            />
          </div>
        </div>
        <HealthConsentNotice targetType={targetType} />
      </CardContent>
    </Card>
  );
}
