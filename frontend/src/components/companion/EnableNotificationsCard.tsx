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
 * The offer to turn on notifications on this phone, until it is linked.
 *
 * One tap asks the browser for permission and links the phone
 * (`lib/push.ts`). Said plainly when it cannot: an iPhone needs the app
 * installed first, and a blocked permission is changed in the browser's own
 * settings, which no page can do.
 */
import { BellRing } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useFormatter } from "@/i18n";

import { usePhoneLink } from "./usePhoneLink";

export function EnableNotificationsCard() {
  const { t_i18n } = useFormatter();
  const { state, linked, loading, busy, failed, link } = usePhoneLink();

  if (loading || linked || state === "unsupported") return null;

  return (
    <Card className="mb-4 flex items-start gap-3 p-4" role="region" aria-label={t_i18n("Notifications on this phone")}>
      <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-accent/15">
        <BellRing className="h-5 w-5 text-accent" aria-hidden="true" />
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <span className="content-base font-semibold text-default-primary">{t_i18n("Get your notifications here")}</span>
        {state === "needs-install" ? (
          <span className="content-compact text-default-secondary">
            {t_i18n("On iPhone, install the app on your home screen first, then turn notifications on from it.")}
          </span>
        ) : state === "denied" ? (
          <span className="content-compact text-default-secondary">
            {t_i18n("Notifications are blocked for this site. Allow them in your browser's site settings, then come back.")}
          </span>
        ) : (
          <>
            <span className="content-compact text-default-secondary">
              {t_i18n("Approvals, reminders and what your agents want you to know, even when the app is closed.")}
            </span>
            {failed && (
              <span className="content-caption text-feedback-error-primary">
                {t_i18n("Notifications could not be turned on. Try again.")}
              </span>
            )}
            <div>
              <Button size="sm" className="min-h-9" disabled={busy} onClick={() => void link()}>
                {t_i18n("Turn on notifications")}
              </Button>
            </div>
          </>
        )}
      </div>
    </Card>
  );
}
