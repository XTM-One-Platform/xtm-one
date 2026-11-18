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
 * The user's Platform Concierge opt-out (#8254), on Profile > Memory: the
 * Concierge never reads this user's memories and conversations, and the
 * evaluation cases already built from them are deleted (PRD 6.2). Shown with
 * the same consent notice as the agent opt-in (B16). Built in since #101 (S1),
 * no flag.
 */
import { ShieldOff } from "lucide-react";

import { HealthConsentNotice } from "@/components/health/HealthConsentNotice";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useHealthOptOut, useSetHealthOptOut } from "@/hooks/api/useHealthApi";
import { useFormatter } from "@/i18n";
import { Spinner } from "@filigran/design-system";

export function HealthOptOutRow() {
  const { data } = useHealthOptOut();
  if (!data) return null;
  return <OptOutCard optOut={data.opt_out} />;
}

function OptOutCard({ optOut }: { optOut: boolean }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const [ConfirmEl, confirm] = useConfirm();
  const save = useSetHealthOptOut();

  const onChange = async (next: boolean) => {
    if (next) {
      const ok = await confirm({
        title: t_i18n("Opt out of Platform Health?"),
        description: t_i18n(
          "Platform Health will no longer read your memories and conversations, and the evaluation cases built from them are deleted now. Opting back in later does not bring them back.",
        ),
        confirmLabel: t_i18n("Opt out"),
        variant: "warning",
      });
      if (!ok) return;
    }
    save.mutate(next, {
      onSuccess: () =>
        toast({
          title: next ? t_i18n("You opted out of Platform Health") : t_i18n("You opted back in to Platform Health"),
          variant: "success",
        }),
      onError: (err) =>
        toast({
          title: t_i18n("Your Platform Health choice was not changed"),
          description: err instanceof Error ? err.message : undefined,
          variant: "error",
        }),
    });
  };

  return (
    <Card data-testid="profile.concierge-opt-out">
      {ConfirmEl}
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 content-base">
          <ShieldOff className="size-4 text-default-secondary" />
          {t_i18n("Platform Health")}
        </CardTitle>
        <CardDescription className="content-compact">
          {t_i18n("Platform Health improves the agents their managers opted in.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 pt-0">
        <div className="flex items-center justify-between gap-3 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2.5">
          <div className="min-w-0">
            <p className="content-base font-medium">{t_i18n("Opt out of Platform Health")}</p>
            <p className="content-compact text-default-secondary">
              {optOut
                ? t_i18n("Platform Health reads none of your memories and conversations.")
                : t_i18n("Your memories and conversations with opted-in agents may be read.")}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {save.isPending && <Spinner size="sm" tone="inherit" className="text-default-secondary" />}
            <Switch
              checked={optOut}
              onCheckedChange={(next) => void onChange(next)}
              disabled={save.isPending}
              aria-label={t_i18n("Opt out of Platform Health")}
            />
          </div>
        </div>
        <HealthConsentNotice />
      </CardContent>
    </Card>
  );
}
