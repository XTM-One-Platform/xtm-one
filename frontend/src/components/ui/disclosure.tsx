// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * The clickable line of a native `<details>` disclosure.
 *
 * Every `<summary>` of the app goes through it so a disclosure shows the shared
 * keyboard focus ring (`ring-2 ring-focus` on `focus-visible`, never the browser's
 * 1 px outline across the whole card) and keeps a readable target. The `inline`
 * size (default) is the "Details" link under a message: `content-compact`, as wide
 * as its text, with `px-1` so the focus ring never hugs the text or the disclosure
 * triangle. The `block` size is a full-width header row inside a bordered card,
 * whose own classes (padding, type) come in `className`.
 *
 * ```tsx
 * <details>
 *   <DisclosureSummary>{t_i18n("Details")}</DisclosureSummary>
 *   <pre>{run.error}</pre>
 * </details>
 * ```
 */
export function DisclosureSummary({
  children,
  className,
  size = "inline",
}: {
  children: ReactNode;
  className?: string;
  size?: "inline" | "block";
}) {
  return (
    <summary
      className={cn(
        "cursor-pointer rounded-sm outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
        size === "inline" && "w-fit px-1 content-compact text-default-secondary hover:text-default-primary",
        className,
      )}
    >
      {children}
    </summary>
  );
}
