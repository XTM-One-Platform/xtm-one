// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * A keyboard key, as drawn in tooltips, menus and shortcut sheets.
 *
 * ```tsx
 * <Kbd>F</Kbd>  <Kbd>Shift</Kbd> <Kbd>Space</Kbd>
 * ```
 */
export function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <kbd
      className={cn(
        "inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-sm border border-elevation-subtle-soft",
        "bg-elevation-default px-1.5 font-sans-plex content-caption font-medium leading-none text-default-secondary",
        className,
      )}
    >
      {children}
    </kbd>
  );
}

/** A tooltip label followed by its shortcut: `Next slide  →`. */
export function LabelWithKeys({ label, keys }: { label: ReactNode; keys: string[] }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span>{label}</span>
      <span className="inline-flex items-center gap-0.5">
        {keys.map((key) => (
          <Kbd key={key}>{key}</Kbd>
        ))}
      </span>
    </span>
  );
}
