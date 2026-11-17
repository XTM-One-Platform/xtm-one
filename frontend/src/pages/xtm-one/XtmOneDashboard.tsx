// SPDX-License-Identifier: Apache-2.0

import { usePlatformRegistrations } from "@/hooks/api/usePlatformApi";
import { useMe } from "@/hooks/api/useAuthApi";
import { useEdition, usePlatformConfig } from "@/hooks/usePlatformConfig";
import { PageSkeleton } from "@/components/ui/skeleton";
import { WelcomeLanding } from "./WelcomeLanding";
import CtemCommandCenter from "./CtemCommandCenter";
import { CtemTrialSplash } from "./CtemTrialSplash";

/**
 * XTM One home routes the dashboard between states based on the resolved
 * ``edition``:
 *
 *   - ``enterprise`` -> ``CtemCommandCenter`` (the single, deeply-integrated
 *     CTEM + agentic command center), or ``WelcomeLanding`` while no platform
 *     is connected yet
 *   - ``community`` + platforms registered  -> ``CtemTrialSplash``, once, then
 *     the command center in its Community Edition shape
 *   - ``community`` + no platforms registered -> ``WelcomeLanding``
 *
 * This component is XTM One only; Copilot and standalone use their own home
 * dashboards (selected upstream in ``App.tsx`` ``HomeRoute``).
 */
export default function XtmOneDashboard() {
  const { data: registrations, isLoading } = usePlatformRegistrations();
  const { data: user, isLoading: userLoading } = useMe();
  const config = usePlatformConfig();
  const edition = useEdition();

  // `App` only renders routes once `/platform/config` is resolved, but keep a
  // defensive gate: `useEdition()` returns "" while the config is missing,
  // which would fall through to the Community Edition branch.
  if (!config) return <PageSkeleton />;

  const active = (registrations ?? []).filter((r) => r.status === "active");

  // Enterprise Edition (Filigran license, or a connected EE platform) -> command center.
  if (edition === "enterprise") {
    if (!isLoading && active.length === 0) {
      return <WelcomeLanding />;
    }
    // Mount immediately (registrations still undefined while loading) so the
    // command center's own data queries run in parallel; it gates its own
    // render until posture + stats + registrations resolve.
    return <CtemCommandCenter registrations={isLoading ? undefined : active} />;
  }

  // Community Edition:
  //   - no platforms registered -> welcome landing (connect instructions +
  //     the upgrade path).
  //   - first time (splash not yet seen) -> the upsell splash, ONCE. After the
  //     user enters the command center (ctem_splash_seen), they land on the CE
  //     numeric posture directly on every subsequent login; the notices region
  //     keeps the upgrade path one click away.
  // Wait for `user` too: deciding splash-vs-command-center reads
  // `user.ctem_splash_seen`, so rendering before `/auth/me` resolves would skip
  // the one-time splash for a first-time user.
  if (isLoading || userLoading) return <PageSkeleton />;
  if (active.length === 0) return <WelcomeLanding />;
  if (user && !user.ctem_splash_seen) return <CtemTrialSplash />;
  return <CtemCommandCenter registrations={active} />;
}
