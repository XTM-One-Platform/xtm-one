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
 * The offer to install the companion on the phone's home screen.
 *
 * Chromium browsers hand the app their install prompt (`lib/pwa.ts`): the
 * banner's Install button opens the browser's own dialog. Safari on iOS has no
 * prompt: the banner says where the Share menu's "Add to Home Screen" is.
 * Shown neither once installed nor where the browser cannot install; "Not now"
 * hides it on this device for a month (a per-device convenience, kept in
 * localStorage).
 */
import { useState } from "react";
import { Share, Smartphone, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useInstallApp } from "@/hooks/useInstallApp";
import { usePlatformName } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { installBannerSnoozed } from "@/lib/install-app";

const DISMISSED_KEY = "xtm-companion-install-dismissed-at";

function readDismissedAt(): number | null {
  try {
    const raw = localStorage.getItem(DISMISSED_KEY);
    return raw ? Number(raw) : null;
  } catch {
    return null;
  }
}

export function InstallAppBanner() {
  const { t_i18n } = useFormatter();
  const name = usePlatformName("XTM One");
  const { method, install } = useInstallApp();
  const [snoozed, setSnoozed] = useState(() => installBannerSnoozed(readDismissedAt(), Date.now()));

  if (snoozed || (method !== "prompt" && method !== "ios")) return null;

  const dismiss = () => {
    try {
      localStorage.setItem(DISMISSED_KEY, String(Date.now()));
    } catch {
      // Storage unavailable: hidden for this visit only.
    }
    setSnoozed(true);
  };

  return (
    <Card className="mb-4 flex items-start gap-3 border-accent/30 bg-accent/10 p-4" role="region" aria-label={t_i18n("Install the app")}>
      <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-accent/15">
        <Smartphone className="h-5 w-5 text-accent" aria-hidden="true" />
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <div className="flex flex-col gap-0.5">
          <span className="content-base font-semibold text-default-primary">
            {t_i18n("Install {name} on your phone", { name })}
          </span>
          {method === "prompt" ? (
            <span className="content-compact text-default-secondary">
              {t_i18n("Open your day, your notifications and your assistant from your home screen, full screen, like an app.")}
            </span>
          ) : (
            <span className="content-compact text-default-secondary">
              {t_i18n("In Safari, tap Share")}{" "}
              <Share className="inline h-3.5 w-3.5 align-text-top" aria-hidden="true" />{" "}
              {t_i18n("then Add to Home Screen.")}
            </span>
          )}
        </div>
        {method === "prompt" && (
          <div className="flex gap-2">
            <Button size="sm" className="min-h-9" onClick={() => void install()}>
              {t_i18n("Install")}
            </Button>
            <Button size="sm" variant="ghost" className="min-h-9" onClick={dismiss}>
              {t_i18n("Not now")}
            </Button>
          </div>
        )}
      </div>
      {method === "ios" && (
        <Button variant="ghost" size="icon" aria-label={t_i18n("Not now")} className="-mr-2 -mt-2 shrink-0" onClick={dismiss}>
          <X className="h-4 w-4" />
        </Button>
      )}
    </Card>
  );
}
