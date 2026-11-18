// SPDX-License-Identifier: Apache-2.0

import { Outlet } from "react-router-dom";
import { Eye, X } from "lucide-react";
import { Sidebar } from "./Sidebar";
import { Header } from "./Header";
import { EmergencyStopBanner } from "./EmergencyStopBanner";
import { OrganizationSetupPrompt } from "./OrganizationSetupPrompt";
import { HealthCompanion } from "@/components/health/HealthCompanion";
import { GlobalSearchProvider } from "./GlobalSearch";
import { useArcade } from "@/components/arcade/arcade-context";
import { CompanionShell } from "@/components/companion/CompanionShell";
import { useActivityStream } from "@/hooks/useActivityStream";
import { useAuth } from "@/hooks/useAuth";
import { useCompanionDevice } from "@/hooks/useCompanionDevice";
import { useStaleBuildNotice } from "@/hooks/useStaleBuildNotice";
import { useThemeEffect } from "@/hooks/useThemeEffect";
import { NavPrefsProvider, useNavPrefs } from "@/hooks/useNavPrefs";
import { CommandsProvider, useCommandHandler } from "@/lib/commands/CommandsProvider";
import { cn } from "@/lib/utils";
import {
  HEADER_HEIGHT,
  IMPERSONATION_BANNER_HEIGHT,
  NAVBAR_WIDTH_COLLAPSED,
  NAVBAR_WIDTH_EXPANDED,
} from "./layout-metrics";

/** The notice an admin sees while acting as another user. Exported for its render test. */
export function ImpersonationBanner() {
  const { user, stopImpersonation } = useAuth();

  if (!user?.impersonating) return null;

  return (
    <div
      style={{ height: IMPERSONATION_BANNER_HEIGHT }}
      className="fixed top-0 left-0 right-0 z-[60] flex items-center justify-center gap-3 bg-feedback-alert-primary/90 px-4 content-base font-medium text-on-feedback-fill backdrop-blur-xs"
    >
      <Eye className="h-4 w-4 shrink-0" />
      <span>
        Impersonating <strong>{user.full_name}</strong> ({user.email})
        {user.impersonated_by_name && (
          <span className="opacity-75"> — logged in as {user.impersonated_by_name}</span>
        )}
      </span>
      <button
        onClick={stopImpersonation}
        className="ml-2 inline-flex items-center gap-1 rounded-sm bg-on-feedback-fill/10 px-2.5 py-0.5 content-compact font-semibold transition-colors hover:bg-on-feedback-fill/20"
      >
        <X className="h-3 w-3" />
        Stop Impersonating
      </button>
    </div>
  );
}

function AppShell() {
  useActivityStream();
  useThemeEffect();
  useStaleBuildNotice();
  const { isImpersonating } = useAuth();
  const { rail, setRail } = useNavPrefs();
  const arcade = useArcade();

  /* Shell-level commands: their manifest is `app.commands.ts`, and the handlers
     live here because this is where the state they act on is held. */
  useCommandHandler("app.nav.toggleRail", () => setRail(!rail));
  /* The arcade is mounted above the routes (`ArcadeProvider`); its commands
     are declared in `components/arcade/arcade.commands.ts`. */
  useCommandHandler("arcade.play", () => arcade.launch({ source: "palette", origin: null }));
  useCommandHandler("arcade.leaderboard", () => arcade.openLeaderboard());

  return (
    /* `--fds-header-height` is the design system's own layout hook: its `Header`
       reads it (falling back to 68px) and so does anything offsetting against
       the bar. Publishing it from the JavaScript constant makes that constant
       the single source of truth for the bar's height, the content offset and
       the assistant panel's `topOffset` — the three values this module exists
       to keep in step. */
    <div
      className="flex min-h-screen bg-gradient-default"
      style={{ "--fds-header-height": `${HEADER_HEIGHT}px` } as React.CSSProperties}
    >
      <ImpersonationBanner />
      <Sidebar impersonating={isImpersonating} />
      {/* The Concierge companion (#8261), for who turned it on. */}
      <HealthCompanion />
      {/* The first-login organization setup (#9173, S3): administrators, XTM One, once per instance. */}
      <OrganizationSetupPrompt />
      {/* pl-0 on mobile (sidebar is overlay), the sidebar's width on desktop
          (where it is fixed) — 180px normally, 48px when collapsed to the rail.
          min-w-0 is essential: without it this flex child defaults to min-width:auto
          and grows to its content's intrinsic width, so a long non-wrapping line on any
          page would expand the whole column and trigger horizontal page scroll instead
          of truncating within the viewport. */}
      <div
        className={cn(
          "flex flex-1 flex-col min-w-0 transition-[padding] duration-300",
          // The offset itself is the custom property set below; the breakpoint
          // is what has to be a class.
          "pl-0 lg:pl-[var(--navbar-offset,0px)]",
        )}
        // paddingTop from the shared constant, not a `pt-9` that has to be
        // kept in step with it by hand.
        style={{
          ...(isImpersonating ? { paddingTop: IMPERSONATION_BANNER_HEIGHT } : {}),
          // `lg:` in JavaScript: the rail is a desktop affordance and the
          // mobile nav is an overlay, so the offset only applies above the
          // breakpoint. A media query cannot live in an inline style, so
          // the width is published here and the class above applies it
          // only at `lg`.
          "--navbar-offset": `${rail ? NAVBAR_WIDTH_COLLAPSED : NAVBAR_WIDTH_EXPANDED}px`,
        } as React.CSSProperties}
      >
        <Header />
        <EmergencyStopBanner />
        {/* p-6 is the Content padding rules/Layout.md fixes for every product.
            Its 880 px Content minimum is not applied: with the expanded
            navbar it would scroll every page sideways at 1024 px, a width
            the product supports without a page scroll (LIBRARY-FEEDBACK F14). */}
        <main id="app-content" className="flex-1 p-6 animate-fade-in">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

export function AppLayout() {
  /* On a phone the app is the mobile companion, not this shell: decided by
     the device, never by the screen width (`components/companion`). */
  const phone = useCompanionDevice();
  return (
    <NavPrefsProvider>
      {/* Commands wrap the top bar search: its `>` mode reads the catalog and the
          `when` context from it, and every screen inside registers into it. */}
      <CommandsProvider>
        <GlobalSearchProvider>
          {phone ? <CompanionShell banner={<ImpersonationBanner />} /> : <AppShell />}
        </GlobalSearchProvider>
      </CommandsProvider>
    </NavPrefsProvider>
  );
}
