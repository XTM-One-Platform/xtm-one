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

import type React from "react";
import { Icon, IconButton } from "@filigran/design-system";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { OFFER_ACCENT_INTERACTIVE, OFFER_ICON_REST } from "./waiting-offer-style";
import { useWaitingArcade } from "./waiting-arcade";

/**
 * The reasoning window's corner gamepad (#3877, the panel's
 * `renderReasoningAction`): the reasoning window holds the waiting panel's
 * spot on most turns, so the waiting arcade is offered there too, through the
 * waiting panel's own launch path (`useWaitingArcade`: same gate, same
 * reduced-motion rule, same `waitSource`) and in its colours. Nothing when the
 * arcade cannot take control.
 */
export function ReasoningArcadeAction({ waitSource }: { waitSource?: string | null }) {
  const { t_i18n } = useFormatter();
  const { canTakeControl, takeControl } = useWaitingArcade({ waitSource });
  if (!canTakeControl) return null;

  const label = t_i18n("Play Space Invaders while the agent works");
  // The invaders break out of the button.
  const play = (event: React.MouseEvent<HTMLButtonElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    takeControl({ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 });
  };
  return (
    <Tip content={label}>
      <IconButton
        type="button"
        variant="default"
        priority="tertiary"
        size="sm"
        onClick={play}
        aria-label={label}
        className={cn(OFFER_ICON_REST, OFFER_ACCENT_INTERACTIVE)}
        icon={<Icon name="gamepad-2" size={16} aria-hidden />}
      />
    </Tip>
  );
}
