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
 * Whether the chat composer proposes the person's next message once an agent
 * has answered, on Profile > Profile. On by default; it also needs the
 * administrator's switch and a routing model (Settings > AI Models).
 */
import { Icon } from "@filigran/design-system";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { InfoTip } from "@/components/ui/info-tip";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useMe, useUpdateProfile } from "@/hooks/api/useAuthApi";
import { useFormatter } from "@/i18n";

const LABEL_ID = "reply-suggestions-label";

export function ReplySuggestionsCard() {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const { data: user } = useMe();
  const save = useUpdateProfile();
  const on = user?.reply_suggestions_enabled !== false;
  const onChange = (next: boolean) =>
    save.mutate(
      { reply_suggestions_enabled: next },
      { onError: () => toast({ title: t_i18n("Your reply suggestion choice was not changed"), variant: "error" }) },
    );
  return (
    <Card data-testid="profile.reply-suggestions">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 content-base">
          <Icon name="sparkles" size={16} className="text-default-secondary" />
          {t_i18n("Reply suggestions")}
        </CardTitle>
        <CardDescription className="content-compact">
          {t_i18n("Once an agent has answered, the message box proposes what you would likely ask next. Press Tab to use it, then Enter to send.")}
        </CardDescription>
      </CardHeader>
      <CardContent className="pt-0">
        <div className="flex items-center justify-between gap-3 rounded-sm bg-elevation-default p-3.5">
          <span className="flex items-center gap-1.5 content-base text-default-primary">
            <span id={LABEL_ID}>{t_i18n("Suggest my next message")}</span>
            <InfoTip
              label={t_i18n("What this reply suggestion setting does")}
              content={t_i18n(
                "On, the platform's routing model reads each answer and proposes one next message, greyed out in the message box. Tab puts it in the box, typing or Escape dismisses it, and nothing is sent until you press Enter. Off, no suggestion is generated for you.",
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
