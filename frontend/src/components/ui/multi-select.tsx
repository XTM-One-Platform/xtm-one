// SPDX-License-Identifier: Apache-2.0

/**
 * Shared searchable multi-select — on the design system's `Combobox`.
 *
 * This was hand-rolled for a stated reason: the single-select Radix `Select`
 * "never highlights the active value and rejects empty-string option values", so
 * the Analytics filters grew their own trigger, search box, checkmarked list and
 * removable chips. `Combobox` is that control — `multiple` is a prop on it
 * rather than a second component — so the hand-rolled trigger, the outside-click
 * handling, the search filtering and the chip rendering all go.
 *
 * The `selected: string[]` / `onToggle(value)` API is kept so the six call sites
 * are untouched. `Combobox` reports the whole next selection, so the diff back
 * to per-value toggles happens here: the call sites use `onToggle` to flip one
 * filter, and turning that into "replace the array" would change how they read.
 *
 * Options are ordered and matched like every other picker
 * (`lib/select-options.ts`): alphabetical unless `sort={false}`, every typed
 * word found anywhere in the label.
 */
import { useMemo, type ReactNode } from "react";
import {
  Combobox,
  ComboboxChips,
  ComboboxClear,
  ComboboxControls,
  ComboboxField,
  ComboboxInput,
  ComboboxTrigger,
} from "@filigran/design-system";
import { ScrollableComboboxContent } from "@/components/ui/scrollable-combobox-content";
import { useFormatter } from "@/i18n";
import { fieldHeightClasses } from "@/lib/field-height-classes";
import { matchesQuery, sortByLabel } from "@/lib/select-options";
import { cn } from "@/lib/utils";

interface MultiSelectOption {
  value: string;
  label: string;
  /** A 16 px mark before the label in the list (a provider logo, the XTM One emblem; #1373). */
  icon?: ReactNode;
  /**
   * Always selected and never removable (an always-on built-in option, #1373): its row is shown
   * checked and inert, and no change from the field drops it.
   */
  locked?: boolean;
}

interface MultiSelectProps {
  options: MultiSelectOption[];
  selected: string[];
  onToggle: (value: string) => void;
  placeholder: string;
  /** The field's accessible name when the placeholder is not one (a default value shown as the hint). */
  "aria-label"?: string;
  /** The search input's id, for a visible `<Label htmlFor>` placed next to the field. */
  id?: string;
  searchPlaceholder: string;
  emptyOptionsLabel: string;
  noMatchLabel: string;
  className?: string;
  /**
   * The visible field's height, as a Tailwind class ("h-8" on a list toolbar,
   * beside the 32 px search and pills). Without it the field keeps the design
   * system's 36 px floor, which `className` on the root does not change.
   */
  height?: string;
  /** Alphabetical (the default), or the order given when the order means something. */
  sort?: boolean;
  /** Read-only: no list opens, the chips cannot be removed, and the field draws like every other disabled field. */
  disabled?: boolean;
  /** The id of the help or error line under the field, tied to the search input. */
  "aria-describedby"?: string;
  /**
   * Holds the selection to one line, the rest in a `+N` chip (the library's
   * `maxVisibleChips`): "fit" shows as many chips as the width holds. For a
   * field in a fixed-width cell, where wrapped chips would run past it.
   */
  maxVisibleChips?: number | "fit";
}

export function MultiSelect({
  options,
  selected,
  onToggle,
  placeholder,
  "aria-label": ariaLabel,
  id,
  searchPlaceholder,
  emptyOptionsLabel,
  noMatchLabel,
  className,
  height,
  sort = true,
  disabled = false,
  "aria-describedby": ariaDescribedBy,
  maxVisibleChips,
}: MultiSelectProps) {
  const { locale, t_i18n } = useFormatter();
  const ordered = useMemo(
    () => (sort ? sortByLabel(options, (option) => option.label, locale) : options),
    [options, sort, locale],
  );
  const value = useMemo(
    () => ordered.filter((option) => selected.includes(option.value)),
    [ordered, selected],
  );

  const locked = useMemo(() => new Set(options.filter((o) => o.locked).map((o) => o.value)), [options]);
  const hasIcons = options.some((option) => option.icon);

  const handleChange = (next: MultiSelectOption | MultiSelectOption[] | null) => {
    if (disabled) return;
    // A locked option stays selected whatever the field reports (a clear, a chip removal).
    const nextValues = [
      ...new Set([...(Array.isArray(next) ? next : next ? [next] : []).map((o) => o.value), ...selected.filter((v) => locked.has(v))]),
    ];
    // One `onToggle` per actual change, in both directions — the call sites
    // treat each value as an independent filter.
    for (const added of nextValues.filter((v) => !selected.includes(v))) onToggle(added);
    for (const removed of selected.filter((v) => !nextValues.includes(v))) onToggle(removed);
  };

  return (
    <Combobox
      multiple
      options={ordered}
      value={value}
      onValueChange={handleChange}
      getOptionLabel={(option) => option.label}
      isOptionEqualToValue={(a, b) => a.value === b.value}
      isOptionDisabled={locked.size > 0 ? (option) => locked.has(option.value) : undefined}
      renderOption={
        hasIcons
          ? (option) => (
              <span className="flex min-w-0 items-center gap-2">
                {option.icon}
                <span className="min-w-0 truncate">{option.label}</span>
              </span>
            )
          : undefined
      }
      filterOptions={(candidates, query) => candidates.filter((option) => matchesQuery(query, [option.label]))}
      disabled={disabled}
      maxVisibleChips={maxVisibleChips}
      className={cn(className)}
    >
      <ComboboxField className={fieldHeightClasses(height)}>
        <ComboboxChips
          aria-label={ariaLabel ?? placeholder}
          overflowLabel={(count, labels) => t_i18n("+{count} more: {labels}", { count, labels: labels.join(", ") })}
        />
        {/* The visible placeholder switches to the search hint as soon as
            something is selected, and the placeholder is all that names this
            input — so without an explicit label the field loses its identity
            exactly when it has content ("Search agents..." tells you nothing
            about what you are searching). `aria-label` keeps the name stable.
            The library's `ComboboxLabel` would be better still: it wires
            `htmlFor` to the input, so the label the user can already see would
            do this job. That means moving the visible label into this component
            at every call site — recorded as a follow-up. */}
        <ComboboxInput
          id={id}
          aria-label={ariaLabel ?? placeholder}
          aria-describedby={ariaDescribedBy}
          placeholder={selected.length ? searchPlaceholder : placeholder}
        />
        <ComboboxControls>
          {!disabled && <ComboboxClear />}
          <ComboboxTrigger />
        </ComboboxControls>
      </ComboboxField>
      {/* The library owns the empty state; both of this app's messages map onto
          it — "no options at all" and "nothing matched" differ only in whether
          the list was empty before filtering. */}
      <ScrollableComboboxContent emptyMessage={options.length === 0 ? emptyOptionsLabel : noMatchLabel} />
    </Combobox>
  );
}
