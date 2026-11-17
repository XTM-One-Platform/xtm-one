// SPDX-License-Identifier: Apache-2.0

import type { ReactNode } from "react";
import { useTriggerTabIndex } from "@/components/ui/row-target-context";
import { Tip } from "@/components/ui/tooltip";

/**
 * The one trigger for an icon-only mark on an entity card's icon row
 * (company-managed, shared, view only, paused, asks first...).
 *
 * A bare svg with an `aria-label` inside a `Tip` is skipped by Tab and opens
 * its tooltip on pointer hover only. This trigger is a tab stop
 * (`tabIndex={0}`, `role="img"`, the accessible name on the trigger), draws
 * the shared 2 px focus ring and opens its tooltip on focus, as a pointer
 * user gets it on hover. The glyph passed as `children` is decorative: give
 * it `aria-hidden="true"` (or `decorative` on `HitlIcon`), the name is here.
 * Inside a routed data table row the row is the one tab stop, so the mark adds none.
 *
 * @param label the accessible name, already translated.
 * @param tip the tooltip body when it says more than the name (rich content);
 *   defaults to `label`.
 */
export function IconMark({
  label,
  tip,
  testId,
  children,
}: {
  label: string;
  tip?: ReactNode;
  testId?: string;
  children: ReactNode;
}) {
  const tabIndex = useTriggerTabIndex();
  return (
    <Tip content={tip ?? label}>
      <span
        role="img"
        tabIndex={tabIndex}
        aria-label={label}
        data-testid={testId}
        className="inline-flex shrink-0 rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-focus"
      >
        {children}
      </span>
    </Tip>
  );
}
