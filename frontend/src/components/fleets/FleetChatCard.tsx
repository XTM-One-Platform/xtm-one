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
 * The card under the fleet panel's transcript (#10162, UX verdict 5dcb88e3):
 *
 * - R8, the shared confirm card: amber, a title naming the one action, the
 *   summary the tool answered, then Cancel first and the destructive "Pause
 *   all" (UX r1 B5: one action, never counted as N changes). Each sends the
 *   user's answer as a message, so the agent applies (`confirm=true`) or
 *   drops it; nothing is applied by the card itself. Body at 14 px (A3).
 * - R9, the raise refusal: an agent never raises a fleet's autonomy; the card
 *   says so and "Open the autonomy control" brings the page to its Approval
 *   tab (the strip lives only there since #10176), then scrolls to the strip
 *   and focuses it (UX r1 B4).
 */
import { AlertTriangle, ShieldAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { FLEET_AUTONOMY_STRIP_ID } from "@/components/fleets/FleetAutonomyStrip";
import type { FleetChatCard as FleetChatCardState } from "@/components/fleets/fleet-chat-cards";
import { useFormatter } from "@/i18n";

/** Frames to wait for the strip once its tab was asked for (it mounts on the next renders). */
const STRIP_WAIT_FRAMES = 30;

/**
 * Shows the page's Approval tab, then scrolls its autonomy strip into view and
 * moves the focus to it once it is mounted; `data-landed` draws the focus ring
 * until the strip loses the focus (a script focus after a click never matches
 * :focus-visible).
 */
function openAutonomyControl(showApprovalTab: () => void): void {
  showApprovalTab();
  let frames = 0;
  const reach = () => {
    const strip = document.getElementById(FLEET_AUTONOMY_STRIP_ID);
    if (strip) {
      strip.scrollIntoView({ behavior: "smooth", block: "start" });
      strip.dataset.landed = "true";
      strip.addEventListener(
        "blur",
        () => {
          delete strip.dataset.landed;
        },
        { once: true },
      );
      strip.focus({ preventScroll: true });
      return;
    }
    frames += 1;
    if (frames < STRIP_WAIT_FRAMES) window.requestAnimationFrame(reach);
  };
  window.requestAnimationFrame(reach);
}

export function FleetChatCard({
  card,
  send,
  canSend,
  onShowApprovalTab,
}: {
  card: FleetChatCardState;
  send: (text: string) => void;
  canSend: boolean;
  /** Switches the fleet page to its Approval tab, where the autonomy strip is. */
  onShowApprovalTab: () => void;
}) {
  const { t_i18n } = useFormatter();

  if (card.kind === "raise_refused") {
    return (
      <div
        role="status"
        className="mx-3 mb-2 shrink-0 space-y-2 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-3 py-2 content-base"
        data-testid="fleet.studio.raise-refused"
      >
        <p className="flex items-start gap-2 text-default-primary">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-default-secondary" aria-hidden />
          <span>
            {t_i18n(
              "An assistant can only lower the autonomy of a fleet. Raising it is an administrator's choice on this page.",
            )}
          </span>
        </p>
        <div className="flex justify-end">
          <Button type="button" variant="secondary" onClick={() => openAutonomyControl(onShowApprovalTab)} data-testid="fleet.studio.open-autonomy">
            {t_i18n("Open the autonomy control")}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div
      role="alert"
      aria-labelledby="fleet-studio-confirm-title"
      className="mx-3 mb-2 shrink-0 space-y-2 rounded-sm border border-feedback-warning-primary/40 bg-feedback-warning-primary/10 px-3 py-2 content-base"
      data-testid="fleet.studio.confirm"
      data-count={card.count}
    >
      <p id="fleet-studio-confirm-title" className="flex items-center gap-2 font-semibold text-default-primary">
        <AlertTriangle className="h-4 w-4 shrink-0 text-feedback-warning-primary" aria-hidden />
        {t_i18n("Pause all automations of this fleet?")}
      </p>
      <p className="text-default-primary">
        <span>
          {t_i18n(
            "{count, plural, =0 {Pause all switches off no automation of this fleet.} one {Pause all switches off # automation of this fleet. Runs in flight continue; Resume switches it back on.} other {Pause all switches off # automations of this fleet. Runs in flight continue; Resume switches them back on.}}",
            { count: card.count },
          )}
        </span>
      </p>
      <div className="flex justify-end gap-2">
        <Button
          type="button"
          variant="secondary"
          disabled={!canSend}
          onClick={() => send(t_i18n("Cancel: do not apply this change."))}
          data-testid="fleet.studio.confirm-cancel"
        >
          {t_i18n("Cancel")}
        </Button>
        <Button
          type="button"
          variant="destructive"
          disabled={!canSend}
          onClick={() => send(t_i18n("Confirmed: apply it."))}
          data-testid="fleet.studio.confirm-apply"
        >
          {t_i18n("Pause all")}
        </Button>
      </div>
    </div>
  );
}
