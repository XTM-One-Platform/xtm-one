/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The one header of a titled section on a fleet page (UX round 1 of #1393, B1):
 * a 16 px design-system icon, one heading level, the title (an info tip may
 * follow it), a one-line summary, then either a fold toggle or an action.
 *
 * The fold toggle reads "Show" / "Hide" with a chevron on every section; its
 * accessible name carries the section ("Hide Members") so two toggles of the
 * same page are told apart. Capabilities, Members and Fleet defaults draw
 * their header through it; `fleet-fold-header-gate.test.ts` refuses a
 * page-local fold header in this folder.
 */
import type { ReactNode } from "react";
import { Icon, type IconName } from "@filigran/design-system";

import { Button } from "@/components/ui/button";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

export interface FleetFoldToggle {
  open: boolean;
  onToggle: () => void;
  /** The id of the region the toggle shows and hides. */
  controls: string;
  /** A fold that would hide unsaved values or a selection is refused while they exist. */
  disabled?: boolean;
  testId?: string;
}

export function FleetFoldHeader({
  icon,
  title,
  titleExtra,
  summary,
  fold,
  action,
}: {
  icon: IconName;
  title: string;
  /** Drawn right after the title, on its line (an InfoTip). */
  titleExtra?: ReactNode;
  /** The one-line summary after the title; wraps under it when the row is short. */
  summary?: ReactNode;
  fold?: FleetFoldToggle;
  /** A section with no fold: its action takes the toggle's place. */
  action?: ReactNode;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className="flex items-start gap-2" data-testid="fleet-fold-header">
      <Icon name={icon} size={16} className="shrink-0 text-default-secondary" aria-hidden />
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2">
        <h3 className="flex shrink-0 items-center gap-1 whitespace-nowrap content-compact font-medium">
          {title}
          {titleExtra}
        </h3>
        {summary}
      </div>
      {fold ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="ml-auto shrink-0 gap-1.5"
          aria-expanded={fold.open}
          aria-controls={fold.controls}
          aria-label={fold.open ? t_i18n("Hide {section}", { section: title }) : t_i18n("Show {section}", { section: title })}
          disabled={fold.disabled}
          onClick={fold.onToggle}
          data-testid={fold.testId}
        >
          {fold.open ? t_i18n("Hide") : t_i18n("Show")}
          <Icon name="chevron-down" size={16} className={cn("transition-transform", fold.open && "rotate-180")} aria-hidden />
        </Button>
      ) : (
        action && <div className="ml-auto shrink-0">{action}</div>
      )}
    </div>
  );
}
