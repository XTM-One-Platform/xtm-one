// SPDX-License-Identifier: Apache-2.0

import * as React from "react";
import { Paper } from "@filigran/design-system";
import { cn } from "@/lib/utils";

/**
 * Card — the design system's `Paper` as the surface, with this app's anatomy.
 *
 * `Paper` is the library's neutral surface: it owns the elevation (four depths,
 * painted with colour rather than shadow, resolved against the `.layer-N`
 * context), the radius and the hairline. That is exactly what `Card`'s root
 * used to hand-roll, so the root becomes a `Paper` at elevation 1 — the depth a
 * card sits at above the page.
 *
 * `padding={0}` because this app's cards put their padding on the parts, not the
 * container: `CardHeader` and `CardContent` each carry `p-6`, and 335 call sites
 * rely on that. Letting Paper add its own would double it.
 *
 * The parts stay local. `Paper` has `title` and `action` slots, but a header
 * that composes a title, a description and arbitrary controls is not what those
 * express, and the library has no Card anatomy of its own — it is `todo` on the
 * roadmap. These five are layout primitives (a flex column, a heading, a
 * paragraph), not a re-implementation of a library component.
 */

const Card = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    // `data-slot` is the handle a card has instead of a role: `Paper` renders a
    // plain `div`, so the end-to-end suite was reaching for whatever class
    // painted the surface — which this migration then repainted. The library
    // uses the same attribute name on its own parts.
    <Paper ref={ref} data-slot="card" elevation={1} padding={0} className={cn(className)} {...props} />
  ),
);
Card.displayName = "Card";

const CardHeader = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("flex flex-col space-y-1.5 p-6", className)} {...props} />
  ),
);
CardHeader.displayName = "CardHeader";

const CardTitle = React.forwardRef<HTMLHeadingElement, React.HTMLAttributes<HTMLHeadingElement>>(
  ({ className, ...props }, ref) => (
    <h3
      ref={ref}
      className={cn("title-xs", className)}
      {...props}
    />
  ),
);
CardTitle.displayName = "CardTitle";

const CardDescription = React.forwardRef<HTMLParagraphElement, React.HTMLAttributes<HTMLParagraphElement>>(
  ({ className, ...props }, ref) => (
    <p ref={ref} className={cn("content-compact text-default-secondary", className)} {...props} />
  ),
);
CardDescription.displayName = "CardDescription";

const CardContent = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("p-6 pt-0", className)} {...props} />
  ),
);
CardContent.displayName = "CardContent";

const CardFooter = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div ref={ref} className={cn("flex items-center p-6 pt-0", className)} {...props} />
  ),
);
CardFooter.displayName = "CardFooter";

export { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter };
