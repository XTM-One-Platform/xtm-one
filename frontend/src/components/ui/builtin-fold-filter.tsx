// SPDX-License-Identifier: Apache-2.0

/**
 * The built-in fold of a list (Agents, Fleets, the Agentic Flow map;
 * `lib/builtin-fold.ts`, #11831), folded into the existing toolbar:
 *
 * - `useBuiltinPropertyFilter`: the toggle as one option of the page's
 *   Properties filter, so it is found and combined with the other properties;
 * - `useBuiltinCompanyTip`: Company's tooltip while built-ins are hidden.
 *
 * Nothing is drawn under the list (#11905): a line under the records table made
 * it a widget, so it lost its own scroller and the whole page scrolled.
 */

import { useCallback, useMemo } from "react";
import { Icon } from "@filigran/design-system";
import { useFormatter } from "@/i18n";
import type { PropertyFilterOption } from "@/components/ui/property-filter";
import type { BuiltinFoldState } from "@/hooks/useEntityFilter";
import { BUILTIN_PROPERTY_KEY, mergeBuiltinSelection, splitBuiltinSelection } from "@/lib/builtin-fold";
import type { Visibility } from "@/lib/visibility";

/** The option's glyph, through the design system's `<Icon>` (no direct lucide-react import). */
const BuiltinGlyph = ({ className }: { className?: string }) => <Icon name="server" size={16} className={className} />;

/** The Properties filter option that mirrors the choice (checked = built-ins shown). */
const BUILTIN_PROPERTY_OPTION: PropertyFilterOption = {
  key: BUILTIN_PROPERTY_KEY,
  label: "Built-in",
  icon: BuiltinGlyph,
  description: "Show the built-in catalogue under Company",
};

/**
 * The page's Properties filter with the built-in option added: its options, the
 * checked keys (the option checked while built-ins are shown) and the change
 * handler that splits the option back out of the `has:` keys.
 */
export function useBuiltinPropertyFilter<O extends PropertyFilterOption>(
  options: readonly O[],
  selected: string[],
  onChange: (keys: string[]) => void,
  fold: BuiltinFoldState | null,
): { options: PropertyFilterOption[]; selected: string[]; onChange: (keys: string[]) => void } {
  const merged = useMemo(() => (fold ? [...options, BUILTIN_PROPERTY_OPTION] : [...options]), [options, fold]);
  const checked = useMemo(() => (fold ? mergeBuiltinSelection(selected, fold.choice) : selected), [selected, fold]);
  const change = useCallback(
    (keys: string[]) => {
      if (!fold) {
        onChange(keys);
        return;
      }
      const split = splitBuiltinSelection(keys);
      onChange(split.properties);
      if (split.choice !== fold.choice) fold.setChoice(split.choice);
    },
    [fold, onChange],
  );
  return { options: merged, selected: checked, onChange: change };
}

/** Company's tooltip while built-ins are hidden: "N built-in hidden". */
export function useBuiltinCompanyTip(fold: BuiltinFoldState | null): Partial<Record<Visibility, React.ReactNode>> | undefined {
  const { t_i18n } = useFormatter();
  if (!fold || fold.choice !== "hide" || fold.builtinInCompany === 0) return undefined;
  return { company: t_i18n("{count, plural, one {# built-in hidden} other {# built-in hidden}}", { count: fold.builtinInCompany }) };
}

