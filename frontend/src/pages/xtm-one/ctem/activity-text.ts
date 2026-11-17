// SPDX-License-Identifier: Apache-2.0

import { CheckCircle2, PlugZap, RefreshCw, ServerOff, type LucideIcon } from "lucide-react";
import { CTEM_PRODUCT_INFO } from "@/lib/constants";
import type { ActivityEvent } from "./activity-feed";

/**
 * The words and icon of one deterministic event (D3-4, #9567), shared by the
 * Live activity feed and the Done for you timeline (C2 V3): one sentence per
 * event kind wherever it is shown.
 */
export function activityText(
  event: ActivityEvent,
  t_i18n: (id: string, values?: Record<string, string | number>) => string,
): { icon: LucideIcon; title: string; detail: string } {
  switch (event.kind) {
    case "posture_synced":
      return {
        icon: RefreshCw,
        title: t_i18n("Posture synced"),
        detail: t_i18n("{covered} of {total} stages covered", { covered: event.covered, total: event.total }),
      };
    case "product_synced":
      return {
        icon: RefreshCw,
        title: t_i18n("Data synced from {name}", { name: CTEM_PRODUCT_INFO[event.product]?.name ?? event.product }),
        detail: t_i18n("Facts refreshed in the exposure posture"),
      };
    case "platform_registered":
      return { icon: PlugZap, title: t_i18n("{platform} registered", { platform: event.title }), detail: t_i18n("Platform connected to the exposure posture") };
    case "platform_unhealthy":
      return { icon: ServerOff, title: t_i18n("{platform} stopped responding", { platform: event.title }), detail: t_i18n("No recent heartbeat") };
    case "validation":
      return {
        icon: CheckCircle2,
        title: t_i18n("Validation results"),
        detail: t_i18n("{met} of {total} expectations met in simulations", { met: event.met, total: event.total }),
      };
  }
}
