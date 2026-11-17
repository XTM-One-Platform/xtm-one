// SPDX-License-Identifier: Apache-2.0

/**
 * One compact figure in the reader's locale, the exact value on hover
 * (#10476). The text comes from `useNumberFormat` (hooks/useNumberFormat);
 * the exact form is the `title`, only when it differs from the text drawn, so
 * "921" carries no tooltip.
 */
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

interface CompactFigureProps {
  /** The compact text drawn. */
  children: ReactNode;
  /** The exact text, shown on hover when it differs from the compact one. */
  exact: string;
  className?: string;
  testId?: string;
}

/** One compact figure, its exact value in `title`. Never wraps. */
export function CompactFigure({ children, exact, className, testId }: CompactFigureProps) {
  const title = typeof children === "string" && children === exact ? undefined : exact;
  return (
    <span className={cn("whitespace-nowrap tabular-nums", className)} title={title} data-testid={testId}>
      {children}
    </span>
  );
}
