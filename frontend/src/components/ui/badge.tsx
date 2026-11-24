// SPDX-License-Identifier: Apache-2.0

import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";
import { useTriggerTabIndex } from "@/components/ui/row-target-context";

/**
 * Badge — the one primitive that stays local, and the reason is semantic.
 *
 * The library's closest component is `Chip`, and it is genuinely close in shape:
 * a compact pill, optional leading icon, optional delete affordance. But its
 * vocabulary is SEVERITY and ENTITY — neutral, info, low, medium, high,
 * critical, plus the CTI entity families (analyses, cases, events, arsenal, …).
 * This app's badges express STATUS: success, warning, destructive.
 *
 * Those are not the same thing. `success` has no counterpart at all, and the
 * tempting matches are colour coincidences rather than meanings: mapping
 * `success` onto `low` would label a completed run "low severity", and 561 call
 * sites would carry that. `Chip` also takes `label: string` where this app
 * passes arbitrary children, including counts and icons.
 *
 * So this stays, painted entirely from the library's feedback tokens — no local
 * colour decisions, only a local vocabulary. Filed upstream in
 * fds-migration/LIBRARY-FEEDBACK.md: the library has no status chip, and every
 * product that shows a run outcome or a health state needs one.
 *
 * The SHAPE is the library's, though (#1333): `Chip` draws `rounded-sm` (the
 * 4 px step of the design-system radius scale), a 1 px edge and `px-2`, so the
 * badge does too, never a full pill. Height and type stay compact (a 20 px
 * line in `content-compact`): the tables and chip strips are sized to it.
 * Guard: chip-radius.test.ts.
 */

const badgeVariants = cva(
  "inline-flex items-center rounded-sm border px-2 py-0.5 content-compact font-semibold transition-colors",
  {
    variants: {
      variant: {
        default: "border-transparent bg-elevation-highlight text-default-primary",
        secondary: "border-transparent bg-elevation-hover text-default-primary",
        /* The edge is the library's neutral feedback tone, not an elevation
           border. `border-elevation-subtle` measured 1.0 to 1.7:1 on
           the elevation panels and `border-elevation-default` 1.72 to 2.16:1
           on a light layer-1 card: neither reaches the 3:1 of a non-text
           boundary. `feedback-neutral-primary` (gray-600 light, gray-400
           dark) reads 3.69:1 or more on every default, highlight and hover
           surface of layers 0 to 2 in light, 4.82:1 or more in dark. Guard:
           tests_unit/outline-badge-border-contrast.test.ts. */
        outline: "border-feedback-neutral-primary text-default-primary",
        /* The status three use the library's own alert-surface utilities — a
           tint of the secondary tone, with the border to match — and ordinary
           primary text on top. The obvious alternative, a `/20` tint of the
           PRIMARY tone with same-hue text, is what this shipped first and axe
           measured it at 3.73–4.4:1 in both themes: a coloured label on a
           coloured tint of its own hue never has much contrast to work with.
           Letting the surface carry the meaning and the text carry the contrast
           scores above 10:1 in both. */
        success: "bg-alert-success border-alert-success text-default-primary",
        warning: "bg-alert-warning border-alert-warning text-default-primary",
        destructive: "bg-alert-error border-alert-error text-default-primary",
        cyan: "border-accent/30 bg-accent/10 text-accent",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

/**
 * `data-slot="badge"` is the handle a container sizes its badges by (the
 * Operations chip strip fits every badge to its 20 px line, border included).
 */
function Badge({ className, variant, ...props }: BadgeProps) {
  return <div data-slot="badge" className={cn(badgeVariants({ variant }), className)} {...props} />;
}

/**
 * The tab stop around a badge whose tooltip must open from the keyboard too
 * (`Tip` opens on focus, Escape closes it). The ring is the design-system
 * focus token (`ring-2 ring-focus`): the hand-written `ring-1 ring-accent/50`
 * span it replaces measured 2.3:1 on the hover panel, under the 3:1 a focus
 * indicator needs. Guard: focusable-badge-trigger.test.ts. Inside a routed data table
 * row the row is the one tab stop, so the trigger adds none (#12070).
 */
const BadgeFocusTrigger = React.forwardRef<HTMLSpanElement, React.HTMLAttributes<HTMLSpanElement>>(
  ({ className, ...props }, ref) => {
    const tabIndex = useTriggerTabIndex();
    return (
      <span
        ref={ref}
        tabIndex={tabIndex}
        className={cn("inline-flex rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus", className)}
        {...props}
      />
    );
  },
);
BadgeFocusTrigger.displayName = "BadgeFocusTrigger";

export { Badge, BadgeFocusTrigger, badgeVariants };
