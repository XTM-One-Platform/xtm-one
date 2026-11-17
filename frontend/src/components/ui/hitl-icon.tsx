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
 * The one picto for the approval gate, everywhere it appears.
 *
 * Five surfaces grew their own shield and they had drifted: the same idea drawn
 * as `ShieldCheck` in cyan on the profile, emerald on the integrations page,
 * and `ShieldAlert` for two different meanings — "nothing is gating" and
 * "something is waiting for you". A reader who learns the icon on one screen
 * has to relearn it on the next, which is worse than having no icon.
 *
 * So the mapping lives here, once:
 *
 *   gated    the call pauses and asks — the protected state, green
 *   open     nothing pauses — amber, because that is the state to notice
 *   unknown  we cannot tell (a platform that never said whether it can ask)
 *   waiting  a decision is owed by a person right now
 *   topic    a heading about approvals, no state at all
 *
 * `open` and `waiting` are deliberately different glyphs despite sharing the
 * amber: one says the gate is off, the other says the gate is on and holding
 * something. `ShieldAlert` used to mean both, which is the drift this fixes.
 *
 * Colour is a default, not a rule — pass `className` to size it or to override
 * the tone where a surface has its own palette. Callers keep their own wording:
 * "runs unattended" and "does not pause" are the same state described for
 * different readers, and only the caller knows which.
 *
 * Every picto carries a tooltip, and that is enforced here rather than asked of
 * each surface: a shield with no words is a guess about what green means. Pass
 * `label` for the surface's own wording, or let the state's default stand. The
 * one exception is `decorative`, for a heading icon that names a topic and has
 * the heading's own text beside it.
 */

import { forwardRef } from "react";
import { DEFAULT_LABEL, GLYPH, type HitlState } from "@/lib/hitl";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

interface HitlIconProps {
  state: HitlState;
  /** Size and any tone override. `cn` merges, so a later text-* class wins. */
  className?: string;
  /** The surface's own wording. Falls back to the state's default sentence. */
  label?: string;
  /**
   * No tooltip and no accessible name — for a heading's icon, where the
   * heading says it. Anywhere the picto stands for a state, leave this off.
   */
  decorative?: boolean;
}

/**
 * `forwardRef` because `Tip` renders its child through Radix's `asChild`, which
 * needs a ref to position the tooltip. Without it the icon silently loses its
 * tooltip on every surface that wraps one.
 */
export const HitlIcon = forwardRef<SVGSVGElement, HitlIconProps>(
  function HitlIcon({ state, className, label, decorative }, ref) {
    const { t_i18n } = useFormatter();
    const { Icon, tone } = GLYPH[state];
    const icon = (
      <Icon
        ref={ref}
        className={cn("size-3.5 shrink-0", tone, className)}
        aria-label={decorative ? undefined : (label ?? t_i18n(DEFAULT_LABEL[state]))}
        aria-hidden={decorative ? true : undefined}
      />
    );
    if (decorative) return icon;
    return <Tip content={label ?? t_i18n(DEFAULT_LABEL[state])}>{icon}</Tip>;
  },
);
