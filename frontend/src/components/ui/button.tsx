// SPDX-License-Identifier: Apache-2.0

import * as React from "react";
import { Button as FdsButton } from "@filigran/design-system";
import { cn } from "@/lib/utils";

/**
 * Button — an adapter over the design system's `Button`.
 *
 * The wrapper exists so the migration did not have to touch 728 call sites: this
 * app's `variant`/`size` vocabulary is preserved and translated here. It is a
 * translation layer, not a component — it owns no styling of its own.
 *
 * The library models a button as variant × priority × size where this app used a
 * flat variant list, so the mapping is lossy in two places. Both are visible in
 * the gallery captures and both are deliberate:
 *
 *  - `outline` and `secondary` both become `default/secondary`. The library has
 *    exactly one bordered priority, and both of these were bordered.
 *  - `lg` becomes `md`. The library's scale is sm (24px) and md (36px); a 48px
 *    button is not part of the design language.
 *
 * ICONS PASSED AS CHILDREN — the compensation below, and why it is needed.
 * The library renders `children` inside an unclassed `<span>`, whose computed
 * display is therefore `block`. This app ships Tailwind's preflight, which sets
 * `svg { display: block }`. Put those together and an icon passed as a child
 * takes its own line INSIDE the button: icon above label, in a 36px-tall box.
 * It is silent — no error, correct button geometry, wrong content layout — and it
 * hit every button in the app with an inline icon, which is most of them.
 *
 * The library's supported route is its `iconStart`/`iconEnd` props, and moving
 * hundreds of call sites onto them is the right eventual fix. Until then the
 * span is made to lay out inline here: `gap-[inherit]` picks up whatever gap the
 * call site set on the button, so `gap-2` and `gap-1.5` keep working unchanged.
 * The library does not hit this itself because it ships no preflight and its own
 * components use the icon props. Filed in fds-migration/LIBRARY-FEEDBACK.md.
 *
 * `size="icon"` stays a `Button` with square padding rather than becoming the
 * library's `IconButton`, which would be the right component. `IconButton`
 * requires an accessible label, and 56 of this app's 58 icon-only buttons do not
 * pass one — they rely on a wrapping tooltip, which is not an accessible name.
 * Migrating them means auditing and labelling all 56: a change about
 * accessibility rather than about the design system, and its own commit.
 * Recorded in fds-migration/LIBRARY-FEEDBACK.md.
 */

type LegacyVariant = "default" | "secondary" | "outline" | "ghost" | "destructive" | "link";
type LegacySize = "default" | "sm" | "lg" | "icon";

/** How each of this app's variants lands on the library's variant × priority. */
const VARIANT_MAP: Record<
  LegacyVariant,
  {
    variant: "default" | "destructive";
    priority: "primary" | "secondary" | "tertiary";
    className?: string;
  }
> = {
  default: { variant: "default", priority: "primary" },
  secondary: { variant: "default", priority: "secondary" },
  outline: { variant: "default", priority: "secondary" },
  ghost: { variant: "default", priority: "tertiary" },
  // The library has no link variant. Tertiary is its text-only priority, and the
  // underline on hover is what made this variant read as a link.
  link: {
    variant: "default",
    priority: "tertiary",
    className: "underline-offset-4 hover:underline",
  },
  destructive: { variant: "destructive", priority: "primary" },
};

const SIZE_MAP: Record<LegacySize, { size: "sm" | "md"; className?: string }> = {
  sm: { size: "sm" },
  default: { size: "md" },
  lg: { size: "md" },
  // Square, so a lone icon sits centred instead of in a 36x64 pill.
  icon: { size: "md", className: "w-9 px-0" },
};

/** `color` is the library's primitive-token fill override, not the obsolete
 *  HTML attribute, and this app's vocabulary has no use for it. */
export interface ButtonProps extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "color"> {
  variant?: LegacyVariant | null;
  size?: LegacySize | null;
  /** Radix asChild — renders the child element carrying the button's styling. */
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const mappedVariant = VARIANT_MAP[variant ?? "default"];
    const mappedSize = SIZE_MAP[size ?? "default"];

    return (
      <FdsButton
        ref={ref}
        variant={mappedVariant.variant}
        priority={mappedVariant.priority}
        size={mappedSize.size}
        asChild={asChild}
        // The caller's className stays last so it still wins through
        // tailwind-merge, exactly as it did against the old cva definition.
        className={cn(
          // See "ICONS PASSED AS CHILDREN" above.
          "[&>span]:inline-flex [&>span]:items-center [&>span]:gap-[inherit]",
          mappedVariant.className,
          mappedSize.className,
          className,
        )}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";

export { Button };
