// SPDX-License-Identifier: Apache-2.0

/**
 * "Open in <Module>": the one pivot from an object XTM One shows to the same
 * entity in the module it came from (#1357). A new tab with the external-link
 * icon; nothing at all when the URL cannot be built (no base URL, no id), never
 * a broken or guessed link. `iconOnly` draws the icon alone, for a table row or
 * a widget row, with the action as its tooltip and accessible name.
 */
import { Icon } from "@filigran/design-system";
import { Tip, TIP_WRAP_FOCUS_RING } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { SUITE_PRODUCT_NAMES } from "@/components/layout/product-switcher";
import { modulePivotUrl, type PivotKind, type PivotModule } from "@/lib/module-pivot";
import { cn } from "@/lib/utils";

export function ModulePivotLink({
  module,
  baseUrl,
  id,
  kind = "entity",
  iconOnly = false,
  className,
  testId = "module-pivot-link",
}: {
  module: PivotModule | null | undefined;
  baseUrl: string | null | undefined;
  id: string | null | undefined;
  kind?: PivotKind;
  iconOnly?: boolean;
  className?: string;
  testId?: string;
}) {
  const { t_i18n } = useFormatter();
  const href = modulePivotUrl(module, baseUrl, id, kind);
  if (!href || !module) return null;
  const label = t_i18n("Open in {platform}", { platform: SUITE_PRODUCT_NAMES[module] });
  const link = (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={iconOnly ? label : undefined}
      data-testid={testId}
      // A click on the pivot inside a clickable row opens the module, not the row.
      onClick={(e) => e.stopPropagation()}
      className={cn(
        "inline-flex shrink-0 items-center gap-1 whitespace-nowrap content-compact text-accent hover:underline",
        iconOnly && "size-6 justify-center rounded-sm hover:bg-elevation-hover",
        TIP_WRAP_FOCUS_RING,
        className,
      )}
    >
      {!iconOnly && label}
      <Icon name="external-link" size={16} aria-hidden />
    </a>
  );
  return iconOnly ? <Tip content={label}>{link}</Tip> : link;
}
