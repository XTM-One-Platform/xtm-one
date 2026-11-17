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
 * Whether the bell shows the work items waiting for the person (#9437), on
 * Profile > Profile. On by default; off, the dashboard notice and the
 * Operations "Waiting for me" view still list them.
 */
import { Icon } from "@filigran/design-system";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { InfoTip } from "@/components/ui/info-tip";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useMe, useUpdateProfile } from "@/hooks/api/useAuthApi";
import { useFormatter } from "@/i18n";

const LABEL_ID = "waiting-work-items-notify-label";

export function WaitingWorkItemsNotifyCard() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data: user } = useMe();
  const save = useUpdateProfile();
  const on = user?.notify_waiting_work_items !== false;
  const onChange = (next: boolean) =>
    save.mutate(
      { notify_waiting_work_items: next },
      { onError: () => toast({ title: t_i18n("Your notification choice was not changed"), variant: "error" }) },
    );
  return (
    <Card data-testid="profile.waiting-work-items-notify">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 content-base">
          <Icon name="bell" size={16} className="text-default-secondary" />
          {t_i18n("Work items waiting for you")}
        </CardTitle>
        <CardDescription className="content-compact">
          {t_i18n("The bell shows each work item waiting for your decision until you answer it.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="pt-0">
        <div className="flex items-center justify-between gap-3 rounded-sm bg-elevation-default p-3.5">
          <span className="flex items-center gap-1.5 content-base text-default-primary">
            <span id={LABEL_ID}>{t_i18n("Notify me of work items waiting for me")}</span>
            <InfoTip
              label={t_i18n("What this notification setting does")}
              content={t_i18n(
                "On, the bell lists each work item waiting for your decision until you answer it. Off removes these entries from the bell only: the dashboard notice and Operations \"Waiting for me\" still list them.",
              )}
            />
          </span>
          <Switch
            checked={on}
            disabled={save.isPending}
            onCheckedChange={onChange}
            aria-labelledby={LABEL_ID}
          />
        </div>
      </CardContent>
    </Card>
  );
}
