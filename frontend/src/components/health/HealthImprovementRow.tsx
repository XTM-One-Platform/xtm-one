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
 * The Platform Health improvement of an agent as ONE compact row at the bottom of its
 * overview, after the configuration (#101 S10, spec section 2.2, Sam's ask 4): the
 * opt-in switch, the count of proposals awaiting approval, and "Details", a dialog
 * holding what the former opt-in card and Improvements card showed (consent notice,
 * "Improve with", proposals, cycles). Read-only for a viewer who cannot manage it.
 * Turning it on asks first, the consent notice in the confirm, so a manager reads what
 * Platform Health will read before the switch saves (PRD B16, UX round 1 of #733).
 */
import { useState } from "react";
import { Icon, Spinner } from "@filigran/design-system";

import { HealthConsentNotice } from "@/components/health/HealthConsentNotice";
import { HealthCyclesList } from "@/components/health/HealthCyclesList";
import { ImprovementsInbox } from "@/components/health/ImprovementsInbox";
import { ImproveWithHealthButton } from "@/components/health/ImproveWithHealthButton";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useHealthOptIn, useImprovements, useSetHealthOptIn } from "@/hooks/api/useHealthApi";
import { useFormatter } from "@/i18n";

export function HealthImprovementRow({ agentId, canManage }: { agentId: string; canManage: boolean }) {
  const { data, isLoading } = useHealthOptIn("agent", agentId);
  if (isLoading || !data || !data.eligible) return null;
  return <Row agentId={agentId} optIn={data.opt_in} canSwitch={data.can_manage} canManage={canManage} />;
}

function Row({
  agentId,
  optIn,
  canSwitch,
  canManage,
}: {
  agentId: string;
  optIn: boolean;
  canSwitch: boolean;
  canManage: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const save = useSetHealthOptIn("agent", agentId);
  // Only the count: limit 1 reads the total, never a page of proposals.
  const pending = useImprovements(
    { agent_id: agentId, status: ["pending_approval"], limit: 1, offset: 0 },
    { enabled: canManage },
  ).data?.total;

  const apply = (next: boolean) =>
    save.mutate(next, {
      onSuccess: () =>
        toast({
          title: t_i18n(next ? "Platform Health may now improve this agent" : "Platform Health no longer reads this agent's content"),
          variant: "success",
        }),
      onError: (err) =>
        toast({
          title: t_i18n("Platform Health opt-in was not changed"),
          description: err instanceof Error ? err.message : undefined,
          variant: "error",
        }),
    });
  // Off to on waits for the consent confirm; on to off stops the reads at once.
  const onChange = (next: boolean) => (next ? setConfirming(true) : apply(false));

  return (
    <Card data-testid="agent.health-row">
      <CardContent className="flex flex-wrap items-center gap-x-3 gap-y-2 p-4">
        <Icon name="compass" size={16} className="shrink-0 text-accent" />
        <div className="min-w-0 flex-1">
          <p className="content-base font-medium">{t_i18n("Improvement by Platform Health")}</p>
          <p className="content-compact text-default-secondary">
            {canSwitch
              ? t_i18n("Reads its memories and conversations to propose improvements. Off by default.")
              : t_i18n("Only the agent's managers can change this.")}
          </p>
        </div>
        {/* Off, no proposal can be pending: the count shows only when there is one, or once on. */}
        {canManage && pending !== undefined && (optIn || pending > 0) && (
          <Badge variant="outline" className="shrink-0">
            {pending > 0
              ? t_i18n("{count, plural, one {# proposal awaits approval} other {# proposals await approval}}", { count: pending })
              : t_i18n("No proposal awaits approval")}
          </Badge>
        )}
        <div className="flex shrink-0 items-center gap-2">
          {save.isPending && <Spinner size="sm" tone="inherit" className="text-default-secondary" />}
          <Switch
            checked={optIn}
            onCheckedChange={onChange}
            disabled={!canSwitch || save.isPending}
            aria-label={t_i18n("Opt in to improvement")}
          />
          {canManage && (
            <Button variant="ghost" size="sm" onClick={() => setOpen(true)}>
              {t_i18n("Proposals and cycles")}
            </Button>
          )}
        </div>
      </CardContent>
      {canSwitch && (
        <ConfirmDialog
          open={confirming}
          onOpenChange={setConfirming}
          variant="default"
          title="Let Platform Health improve this agent?"
          confirmLabel="Turn on"
          onConfirm={() => apply(true)}
        >
          <HealthConsentNotice targetType="agent" />
        </ConfirmDialog>
      )}
      {canManage && (
        <Dialog open={open} onOpenChange={setOpen}>
          <DialogContent className="max-w-4xl" data-testid="agent.health-details">
            <DialogHeader>
              <DialogTitle>{t_i18n("Improvement by Platform Health")}</DialogTitle>
              <DialogDescription className="content-base leading-relaxed">
                {t_i18n(
                  "Let Platform Health consolidate this agent's memories, diagnose it from its conversations and propose improvements. Every proposal waits for approval by the agent's managers.",
                )}
              </DialogDescription>
            </DialogHeader>
            <DialogBody className="space-y-4">
              <HealthConsentNotice targetType="agent" />
              <div className="flex justify-end">
                <ImproveWithHealthButton targetType="agent" targetId={agentId} canManage={canManage} />
              </div>
              <ImprovementsInbox fixed={{ agent_id: agentId }} />
              <div className="space-y-2">
                <p className="content-compact font-medium text-default-secondary">{t_i18n("Cycles")}</p>
                <HealthCyclesList targetType="agent" targetId={agentId} />
              </div>
            </DialogBody>
          </DialogContent>
        </Dialog>
      )}
    </Card>
  );
}
