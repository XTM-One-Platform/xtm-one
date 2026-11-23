// SPDX-License-Identifier: Apache-2.0

import type * as React from "react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/**
 * The hot tones of a status or a priority: red, orange, yellow, green, blue,
 * and neutral (#1267). One meaning per colour, the hot hues Work items and
 * Tasks use, here drawn as an outlined chip on the design-system radius
 * (`rounded-sm`, from Badge, #1333) (their page-local badges have no
 * visible edge yet and are not on this component): a light wash of the hue, an
 * edge in the hue and the text a step away from the surface in the same
 * family (the `-tertiary` feedback tokens), so the label reads at 4.5:1 and
 * the edge at 3:1 on every elevation surface in both themes. Pastel fill when
 * the user's preference is on (Profile > Pastel colours, the `pastel` root
 * class, #1310): the library Chip's pastel fill exactly, edge transparent so
 * the box keeps its size, wash `-secondary-transparency-30` and the label in
 * `text-default-primary` (Sam, 23 Nov 2026: plain when pastel, outlined when
 * standard). Guards:
 * tests_unit/tone-badge-contrast.test.ts (contrast) and
 * tests_unit/ctem-tone-badge-guard.test.ts (CTEM chips use it).
 */
export type HotTone = "red" | "orange" | "yellow" | "green" | "blue" | "neutral";

export const HOT_TONE_CLASS: Record<HotTone, string> = {
  red: "border-feedback-error-tertiary bg-feedback-error-primary/10 text-feedback-error-tertiary pastel:border-transparent pastel:bg-feedback-error-secondary-transparency-30 pastel:text-default-primary",
  orange: "border-feedback-warning-tertiary bg-feedback-warning-primary/10 text-feedback-warning-tertiary pastel:border-transparent pastel:bg-feedback-warning-secondary-transparency-30 pastel:text-default-primary",
  yellow: "border-feedback-alert-tertiary bg-feedback-alert-primary/10 text-feedback-alert-tertiary pastel:border-transparent pastel:bg-feedback-alert-secondary-transparency-30 pastel:text-default-primary",
  green: "border-feedback-success-tertiary bg-feedback-success-primary/10 text-feedback-success-tertiary pastel:border-transparent pastel:bg-feedback-success-secondary-transparency-30 pastel:text-default-primary",
  blue: "border-feedback-info-tertiary bg-feedback-info-primary/10 text-feedback-info-tertiary pastel:border-transparent pastel:bg-feedback-info-secondary-transparency-30 pastel:text-default-primary",
  neutral: "border-feedback-neutral-primary bg-transparent text-default-secondary pastel:border-transparent pastel:bg-feedback-neutral-secondary-transparency-30 pastel:text-default-primary",
};

interface ToneBadgeProps extends React.HTMLAttributes<HTMLDivElement> {
  tone: HotTone;
  icon?: React.ReactNode;
}

export function ToneBadge({ tone, icon, className, children, ...rest }: ToneBadgeProps) {
  return (
    <Badge
      variant="outline"
      data-tone={tone}
      className={cn("shrink-0 gap-1 whitespace-nowrap", HOT_TONE_CLASS[tone], className)}
      {...rest}
    >
      {icon}
      {children}
    </Badge>
  );
}
