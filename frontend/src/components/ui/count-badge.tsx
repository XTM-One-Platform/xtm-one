// SPDX-License-Identifier: Apache-2.0

import * as React from "react";
import type { VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";
import { badgeVariants } from "@/components/ui/badge";

/**
 * CountBadge: a number on its own (a card header count, a tab count), drawn
 * as the design-system `Badge` draws a standalone count: a 20 px circle that
 * widens into a capsule past two digits. A chip carries a label and takes the
 * `rounded-sm` chip shape (`Badge`, #1333); a count is not a chip, and a
 * squared count reads like an empty checkbox next to the round toolbar
 * counters. Colours are the `Badge` variants, unchanged.
 * Guard: chip-radius.test.ts (a `<Badge>` holding only a count fails).
 */
interface CountBadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

function CountBadge({ className, variant, ...props }: CountBadgeProps) {
  return (
    <span
      data-slot="count-badge"
      className={cn(
        badgeVariants({ variant }),
        "h-5 min-w-5 shrink-0 justify-center rounded-full px-1 py-0 content-caption leading-none tabular-nums",
        className,
      )}
      {...props}
    />
  );
}

export { CountBadge };
