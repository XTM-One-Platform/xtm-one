// SPDX-License-Identifier: Apache-2.0

/**
 * The fold of a case-page list that is not a table (#1679, point 5): past
 * CASE_LIST_SHOWN rows a list draws its first rows and a "Show all (N)"
 * button, the way the "Opened by" groups and the Act now queue do, so no card
 * of the case page grows without bound. The tables of the page (Issues,
 * Affected assets, Running versions, History) page on their own.
 */
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { useNumberFormat } from "@/hooks/useNumberFormat";
import { useFormatter } from "@/i18n";

/** Rows a folded case-page list shows before "Show all (N)". */
const CASE_LIST_SHOWN = 5;

interface CaseFold<T> {
  /** The rows to draw: all of them when expanded or when the list is short. */
  visible: readonly T[];
  /** Whether the list is longer than what it shows folded. */
  more: boolean;
  expanded: boolean;
  total: number;
  toggle: () => void;
}

/** The fold state of *rows*: folded to *shown* rows until the viewer opens it. */
export function useCaseFold<T>(rows: readonly T[], shown: number = CASE_LIST_SHOWN): CaseFold<T> {
  const [expanded, setExpanded] = useState(false);
  const more = rows.length > shown;
  return {
    visible: expanded || !more ? rows : rows.slice(0, shown),
    more,
    expanded,
    total: rows.length,
    toggle: () => setExpanded((e) => !e),
  };
}

/** The "Show all (N)" / "Show fewer" button of a folded list; nothing when the list is short. */
export function CaseFoldToggle<T>({ fold, controls, testId }: { fold: CaseFold<T>; controls: string; testId: string }) {
  const { t_i18n } = useFormatter();
  const { count } = useNumberFormat();
  if (!fold.more) return null;
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className="h-9 self-start text-accent"
      onClick={fold.toggle}
      aria-expanded={fold.expanded}
      aria-controls={controls}
      data-testid={testId}
    >
      {fold.expanded ? t_i18n("Show fewer") : t_i18n("Show all ({count})", { count: count(fold.total) })}
    </Button>
  );
}
