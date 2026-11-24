// SPDX-License-Identifier: Apache-2.0

import type * as React from "react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/**
 * The hot tones of a status or a priority: red, orange, yellow, green, blue,
 * and neutral (#1267), on the design-system radius (`rounded-sm`, from Badge,
 * #1333). One chip style for every product since @filigran/design-system
 * 1.13.0 (#1595, the P3 hairline of the library Chip): the 30% secondary fill
 * of the hue, the label in the default ink, and a 1px INSET hairline in the
 * hue's `-tertiary` ink at 30%; the border is transparent so the box keeps its
 * size. The hairline is decorative as in the library (the label carries the
 * meaning). Guards: tests_unit/tone-badge-contrast.test.ts (the label at 4.5:1
 * on every surface, both themes) and tests_unit/ctem-tone-badge-guard.test.ts
 * (CTEM chips use it).
 */
export type HotTone = "red" | "orange" | "yellow" | "green" | "blue" | "neutral";

export const HOT_TONE_CLASS: Record<HotTone, string> = {
  red: "border-transparent bg-feedback-error-secondary-transparency-30 text-default-primary inset-ring inset-ring-feedback-error-tertiary/30",
  orange: "border-transparent bg-feedback-warning-secondary-transparency-30 text-default-primary inset-ring inset-ring-feedback-warning-tertiary/30",
  yellow: "border-transparent bg-feedback-alert-secondary-transparency-30 text-default-primary inset-ring inset-ring-feedback-alert-tertiary/30",
  green: "border-transparent bg-feedback-success-secondary-transparency-30 text-default-primary inset-ring inset-ring-feedback-success-tertiary/30",
  blue: "border-transparent bg-feedback-info-secondary-transparency-30 text-default-primary inset-ring inset-ring-feedback-info-tertiary/30",
  neutral: "border-transparent bg-feedback-neutral-secondary-transparency-30 text-default-primary inset-ring inset-ring-feedback-neutral-tertiary/30",
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
