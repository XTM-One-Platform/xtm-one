// SPDX-License-Identifier: Apache-2.0

import { Building2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { IconMark } from "@/components/ui/icon-mark";
import { useFormatter } from "@/i18n";

/**
 * Company-managed badge — replaces the repeated Building2 + Badge pattern
 * used across all entity list cards and detail pages.
 *
 * @param size "sm" for list-card badges, "default" for detail-page badges; both draw the 12 px
 *   compact type token and a 12 px icon (design standards gate 7, the 12 px floor).
 * @param label Custom label text. Defaults to "Company-managed". Use "Company" in tight spaces.
 *
 * The badge never wraps (design standards: a badge stays on one line): a wrapped badge is twice
 * as tall and clips the fixed-height row it sits in. Where even the short label does not fit,
 * use `CompanyManagedIcon`.
 */
export function CompanyManagedBadge({ label = "Company-managed" }: { size?: "sm" | "default"; label?: string }) {
  const { t_i18n } = useFormatter();
  return (
    <Badge variant="cyan" className="content-compact whitespace-nowrap">
      <Building2 className="h-3 w-3 mr-0.5 shrink-0" />
      {t_i18n(label)}
    </Badge>
  );
}

/**
 * Icon-only variant — for tight rows (entity cards) where the badge label
 * squeezes the content next to it. The tooltip carries the wording.
 *
 * @param label i18n KEY for the tooltip — not display text, it goes through
 *   `t_i18n` here. Defaults to "Company-managed"; pass the entity flavour
 *   (e.g. "Company-managed agent") where it reads better. Any value must
 *   exist in `i18n/lang/*.json`, or it falls back to the key itself.
 *
 * The mark sits in the shared focusable trigger `IconMark`: a
 * keyboard user tabs to it, sees the focus ring and gets the tooltip on focus,
 * as a pointer user does on hover. The trigger carries the accessible name;
 * the svg is decorative.
 */
export function CompanyManagedIcon({ label = "Company-managed" }: { label?: string }) {
  const { t_i18n } = useFormatter();
  const text = t_i18n(label);
  return (
    <IconMark label={text} testId="company-managed-icon">
      <Building2 className="h-3.5 w-3.5 shrink-0 text-accent" aria-hidden="true" />
    </IconMark>
  );
}
