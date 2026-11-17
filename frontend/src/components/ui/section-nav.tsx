// SPDX-License-Identifier: Apache-2.0

import type { LucideIcon } from "lucide-react";
import { useFormatter } from "@/i18n";
import { useCommandSections } from "@/lib/commands/CommandsProvider";
import { cn } from "@/lib/utils";

/**
 * The one section navigation row of the frontend (#9173, UX B3): the pill row
 * under a page tab that switches between the sections of that tab (Settings
 * sections, Access and security, the agent configuration).
 *
 * A `nav` with a translated accessible name, one button per section with
 * `aria-current="page"` on the active one and the 2 px focus ring, wrapping on
 * narrow widths. The sections are published as the command palette's `@`
 * targets. The caller owns the state, URL-backed through `useSectionParam`.
 * `section-nav.test.ts` fails on a second hand-written row under `pages/`.
 */
export interface SectionNavItem<T extends string> {
  key: T;
  /** English label, translated here. */
  label: string;
  icon: LucideIcon;
}

interface SectionNavProps<T extends string> {
  sections: readonly SectionNavItem<T>[];
  active: T;
  onSelect: (key: T) => void;
  /** English accessible name of the row, translated here. */
  label: string;
  /** Prefix of the command palette ids, unique per row. */
  idPrefix: string;
  /** `data-testid` of every button; `data-section` carries the key. */
  testId?: string;
}

export function SectionNav<T extends string>({ sections, active, onSelect, label, idPrefix, testId = "section-nav-tab" }: SectionNavProps<T>) {
  const { t_i18n } = useFormatter();

  useCommandSections(
    sections.map(({ key, label: title }) => ({ id: `${idPrefix}-${key}`, title, select: () => onSelect(key) })),
  );

  return (
    <nav aria-label={t_i18n(label)} className="flex w-fit flex-wrap items-center gap-1 rounded-sm bg-elevation-hover p-1">
      {sections.map(({ key, label: text, icon: Icon }) => {
        const isActive = active === key;
        return (
          <button
            key={key}
            type="button"
            aria-current={isActive ? "page" : undefined}
            onClick={() => onSelect(key)}
            data-testid={testId}
            data-section={key}
            className={cn(
              "flex items-center gap-1.5 rounded-sm px-3 py-1.5 content-compact font-medium transition-all",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
              isActive
                ? "bg-accent/10 text-accent shadow-xs"
                : "text-default-secondary hover:text-default-primary hover:bg-elevation-hover",
            )}
          >
            <Icon className="h-3.5 w-3.5" />
            {t_i18n(text)}
          </button>
        );
      })}
    </nav>
  );
}
