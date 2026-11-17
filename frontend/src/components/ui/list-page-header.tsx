// SPDX-License-Identifier: Apache-2.0

/** ListPageHeader -- the one header row of a list or settings page.
 *
 * Issue #10215 (header and hero density sweep, criterion 5 and 8): a list page
 * draws its title, the "i" that carries its help sentence (and any "small text
 * info" line such as a retention rule), and its actions on ONE row. No page
 * draws a help paragraph under its h1, and no page copies this markup: the
 * pages call this component (UX r1 B1 on #10382), and
 * `src/list-header-help.test.ts` fails on a page that draws its own h1.
 *
 * Entity detail pages use `EntityPageHeader` instead (back link, tile, meta row).
 */

import type { ReactNode } from "react";
import { InfoTip } from "@/components/ui/info-tip";
import { cn } from "@/lib/utils";

export function ListPageHeader({
  title,
  infoLabel,
  info,
  meta,
  actions,
  className,
}: {
  title: ReactNode;
  /** The accessible name of the "i" ("About audit logs"). */
  infoLabel: string;
  /** The page's help, shown on hover and keyboard focus of the "i". */
  info: ReactNode;
  /** A short figure beside the title, outside the h1 (the total of Presentations). */
  meta?: ReactNode;
  /** Buttons drawn at the end of the row. */
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div data-list-page-header="" className={cn("flex flex-wrap items-center justify-between gap-4", className)}>
      <div className="flex min-w-0 items-center gap-2">
        <h1 className="text-3xl font-bold">{title}</h1>
        <InfoTip label={infoLabel} content={info} />
        {meta}
      </div>
      {actions ? <div className="flex items-center gap-3">{actions}</div> : null}
    </div>
  );
}
