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
 * Today - the companion's home: the platform's logo and the account menu, then
 * the platform's own home dashboard for its mode (XTM One's command center, or
 * the Copilot dashboard), the one `/` shows on a computer - which greets the
 * person itself. The command center needs a CTEM right (#2441).
 */
import { Suspense } from "react";
import { LockKeyhole, LogOut, Smartphone } from "lucide-react";

import { useAppLock } from "@/components/companion/app-lock-context";
import { OverflowMenu, type OverflowMenuItem } from "@/components/ui/overflow-menu";
import { PageSkeleton } from "@/components/ui/skeleton";
import { useAuth } from "@/hooks/useAuth";
import { useInstallApp } from "@/hooks/useInstallApp";
import { isXtmOneMode, usePlatformConfig, usePlatformLogoUrl, usePlatformName } from "@/hooks/usePlatformConfig";
import { useFormatter } from "@/i18n";
import { homeIsCtemCommandCenter } from "@/lib/ctem-access";
import { lazyWithRetry } from "@/lib/lazyWithRetry";

const Dashboard = lazyWithRetry(() => import("@/pages/Dashboard"));
const XtmOneDashboard = lazyWithRetry(() => import("@/pages/xtm-one/XtmOneDashboard"));

function TodayHeader() {
  const { t_i18n } = useFormatter();
  const { logout } = useAuth();
  const { method, install } = useInstallApp();
  const logo = usePlatformLogoUrl();
  const name = usePlatformName("XTM One");
  const lock = useAppLock();
  const lockItem: OverflowMenuItem | null = !lock?.status.companion
    ? null
    : lock.status.applies
      ? { id: "lock", label: t_i18n("Lock now"), icon: LockKeyhole, onSelect: lock.lockNow }
      : !lock.status.has_pin
        ? { id: "choose-pin", label: t_i18n("Protect with a PIN"), icon: LockKeyhole, onSelect: lock.choosePin }
        : null;
  const items: OverflowMenuItem[] = [
    ...(method === "prompt"
      ? [{ id: "install", label: t_i18n("Install the app"), icon: Smartphone, onSelect: () => void install() }]
      : []),
    ...(lockItem ? [lockItem] : []),
    {
      id: "sign-out",
      label: t_i18n("Sign out"),
      icon: LogOut,
      onSelect: logout,
      separatorBefore: method === "prompt" || lockItem !== null,
    },
  ];
  return (
    <header className="flex items-center justify-between gap-3">
      <img src={logo} alt={name} className="h-6 w-auto max-w-[60%] object-contain object-left" />
      <OverflowMenu items={items} label={t_i18n("Account")} className="h-11 w-11" />
    </header>
  );
}

export function CompanionToday() {
  const platformConfig = usePlatformConfig();
  const { user, isAdmin } = useAuth();
  return (
    <div className="flex flex-col gap-5">
      <TodayHeader />
      {platformConfig ? (
        <Suspense fallback={<PageSkeleton cards={3} />}>{homeIsCtemCommandCenter(isXtmOneMode(platformConfig.platform_mode), user, isAdmin) ? <XtmOneDashboard /> : <Dashboard />}</Suspense>
      ) : (
        <PageSkeleton cards={3} />
      )}
    </div>
  );
}
