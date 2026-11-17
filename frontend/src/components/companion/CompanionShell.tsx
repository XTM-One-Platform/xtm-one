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
 * The app on a phone: the mobile companion of XTM One, not the platform
 * squeezed onto a small screen (`companion-routes.ts`).
 *
 * Three tabs in a bottom bar within thumb reach: Today, Inbox, Ask. A page
 * of the platform the companion does not offer (an agent's configuration, the
 * settings, a link from a notification) says it is made for a computer, with
 * the way back, rather than being drawn half-usable. The rest of the shell's
 * duties are the desktop shell's: the theme, the live activity stream, the
 * stale-build notice, the emergency-stop banner and impersonation.
 */
import type { CSSProperties, ReactNode } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import { Bell, Home, Monitor, Sparkles } from "lucide-react";

import { EmergencyStopBanner } from "@/components/layout/EmergencyStopBanner";
import { IMPERSONATION_BANNER_HEIGHT } from "@/components/layout/layout-metrics";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useNotifications } from "@/hooks/api/useNotificationApi";
import { useActivityStream } from "@/hooks/useActivityStream";
import { useAuth } from "@/hooks/useAuth";
import { useStaleBuildNotice } from "@/hooks/useStaleBuildNotice";
import { usePlatformName } from "@/hooks/usePlatformConfig";
import { useThemeEffect } from "@/hooks/useThemeEffect";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

import { companionTab, companionView } from "./companion-routes";
import { CompanionToday } from "./CompanionToday";
import { EnableNotificationsCard } from "./EnableNotificationsCard";
import { InstallAppBanner } from "./InstallAppBanner";

/** Height of the bottom bar, safe area excluded. */
const NAV_HEIGHT = 64;

/** @i18n-keys */
const TAB_LABELS = {
  today: "Today",
  inbox: "Inbox",
  ask: "Ask",
} as const;

const TABS = [
  { id: "today", to: "/", icon: Home },
  { id: "inbox", to: "/inbox", icon: Bell },
  { id: "ask", to: "/chat", icon: Sparkles },
] as const;

function CompanionNav({ active }: { active: keyof typeof TAB_LABELS | null }) {
  const { t_i18n } = useFormatter();
  const { data: feed } = useNotifications();
  const unread = feed?.unread_count ?? 0;
  return (
    <nav
      aria-label={t_i18n("Main")}
      className="fixed inset-x-0 bottom-0 z-40 grid grid-cols-3 border-t border-elevation-subtle layer-1 bg-elevation-default pb-[env(safe-area-inset-bottom)]"
    >
      {TABS.map(({ id, to, icon: Icon }) => {
        const current = active === id;
        return (
          <Link
            key={id}
            to={to}
            aria-current={current ? "page" : undefined}
            style={{ height: NAV_HEIGHT }}
            className={cn(
              "flex flex-col items-center justify-center gap-1 text-[11px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus",
              current ? "font-semibold text-accent" : "text-default-secondary hover:text-default-primary",
            )}
          >
            <span className="relative">
              <Icon className="h-[22px] w-[22px]" aria-hidden="true" />
              {id === "inbox" && unread > 0 && (
                <span className="absolute -right-2 -top-1.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-accent px-1 text-[10px] font-bold text-negative-primary">
                  {unread > 99 ? "99+" : unread}
                  <span className="sr-only"> {t_i18n("unread")}</span>
                </span>
              )}
            </span>
            {t_i18n(TAB_LABELS[id])}
          </Link>
        );
      })}
    </nav>
  );
}

/** A platform page the companion does not offer. */
function MadeForAComputer() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const name = usePlatformName("XTM One");
  return (
    <Card className="mt-8 flex flex-col items-center gap-4 border-dashed px-6 py-10 text-center">
      <Monitor className="h-10 w-10 text-accent" aria-hidden="true" />
      <div className="flex flex-col gap-2">
        <h1 className="title-xs text-default-primary">{t_i18n("This page is made for a computer")}</h1>
        <p className="content-compact text-default-secondary">
          {t_i18n("On your phone, {name} is your companion: your day, your notifications and your assistant. Open this page on a computer to use it.", { name })}
        </p>
      </div>
      <Button className="min-h-11" onClick={() => navigate("/")}>
        {t_i18n("Back to Today")}
      </Button>
    </Card>
  );
}

export function CompanionShell({ banner }: { banner?: ReactNode }) {
  useActivityStream();
  useThemeEffect();
  useStaleBuildNotice();
  const { isImpersonating } = useAuth();
  const { pathname } = useLocation();
  const view = companionView(pathname);

  let content: ReactNode;
  if (view === "today") content = <CompanionToday />;
  else if (view === null) content = <MadeForAComputer />;
  else content = <Outlet />;

  return (
    <div
      className="flex min-h-dvh flex-col bg-elevation-default"
      // The chat sizes itself to the window minus this: here, the bottom bar.
      style={
        {
          "--fds-header-height": `calc(${NAV_HEIGHT}px + env(safe-area-inset-bottom, 0px))`,
          ...(isImpersonating ? { paddingTop: IMPERSONATION_BANNER_HEIGHT } : {}),
        } as CSSProperties
      }
      data-companion
    >
      {banner}
      <EmergencyStopBanner />
      <main
        id="app-content"
        className={cn(
          "flex-1 animate-fade-in p-4 pt-[max(1rem,env(safe-area-inset-top))] sm:p-6",
          view !== "ask" && view !== "share" && "pb-[calc(64px+env(safe-area-inset-bottom)+1.5rem)]",
        )}
      >
        {view !== "ask" && view !== "share" && <InstallAppBanner />}
        {(view === "today" || view === "inbox") && <EnableNotificationsCard />}
        {content}
      </main>
      <CompanionNav active={view ? companionTab(view) : null} />
    </div>
  );
}
