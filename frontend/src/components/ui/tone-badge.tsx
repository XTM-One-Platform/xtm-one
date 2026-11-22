// SPDX-License-Identifier: Apache-2.0

import type * as React from "react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/**
 * The hot tones of a status or a priority: red, orange, yellow, green, blue,
 * and neutral (#1267). One meaning per colour, the hot hues Work items and
 * Tasks use, here drawn as an outlined pill (their page-local badges have no
 * visible edge yet and are not on this component): a light wash of the hue, an
 * edge in the hue and the text a step away from the surface in the same
 * family (the `-tertiary` feedback tokens), so the label reads at 4.5:1 and
 * the edge at 3:1 on every elevation surface in both themes. Never the
 * pastel severity tiles of the library Chip. Guards:
 * tests_unit/tone-badge-contrast.test.ts (contrast) and
 * tests_unit/ctem-tone-badge-guard.test.ts (CTEM chips use it).
 */
export type HotTone = "red" | "orange" | "yellow" | "green" | "blue" | "neutral";

export const HOT_TONE_CLASS: Record<HotTone, string> = {
  red: "border-feedback-error-tertiary bg-feedback-error-primary/10 text-feedback-error-tertiary",
  orange: "border-feedback-warning-tertiary bg-feedback-warning-primary/10 text-feedback-warning-tertiary",
  yellow: "border-feedback-alert-tertiary bg-feedback-alert-primary/10 text-feedback-alert-tertiary",
  green: "border-feedback-success-tertiary bg-feedback-success-primary/10 text-feedback-success-tertiary",
  blue: "border-feedback-info-tertiary bg-feedback-info-primary/10 text-feedback-info-tertiary",
  neutral: "border-feedback-neutral-primary bg-transparent text-default-secondary",
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
