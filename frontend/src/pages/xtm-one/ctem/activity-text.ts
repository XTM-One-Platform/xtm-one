// SPDX-License-Identifier: Apache-2.0

import { CheckCircle2, PlugZap, RefreshCw, ServerOff, type LucideIcon } from "lucide-react";
import type { IconName } from "@filigran/design-system";
import { CTEM_PRODUCT_INFO } from "@/lib/constants";
import type { ActivityEvent } from "./activity-feed";
import { decisionSentence, SIGNAL_STATE_LABEL } from "./loop/DecisionReason";

const LENS_LABEL: Record<string, string> = { inside: "Inside", outside: "Outside", world: "World" };

/**
 * The words and icon of one deterministic event (D3-4, #9567), shared by the
 * Live activity feed and the Done for you timeline (C2 V3): one sentence per
 * event kind wherever it is shown.
 */
export function activityText(
  event: ActivityEvent,
  t_i18n: (id: string, values?: Record<string, string | number>) => string,
  /** The product day formatter for a date param of the decision sentence (#851 UX round 4 B8). */
  formatDay?: (iso: string) => string,
): { icon: IconName | LucideIcon; title: string; detail: string } {
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
    case "signal_received":
      // Never hidden (section 3): the path, the lens, the state and its why on one line.
      // A DS icon (#851 UX round 1 B3): no new direct lucide icon since DS 1.9.0.
      return {
        icon: "activity",
        title: t_i18n("Signal received: {subject}", { subject: event.label }),
        detail: [
          t_i18n(event.path),
          t_i18n(LENS_LABEL[event.lens] ?? event.lens),
          t_i18n(SIGNAL_STATE_LABEL[event.state] ?? event.state),
          decisionSentence(event.decision, t_i18n, formatDay),
        ].join(" · "),
      };
  }
}
