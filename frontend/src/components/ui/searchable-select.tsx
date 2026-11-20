// SPDX-License-Identifier: Apache-2.0

/**
 * SearchableSelect - the platform's single-value picker for any list a person
 * may have to search.
 *
 * The design system's `Select` is Radix's: a list it cannot filter. Fine for
 * Dark / Light, hopeless for the agent that writes a deck when there are 300
 * agents. `Combobox` is the library's filterable field, so a picker over
 * entities (agents, users, models, integrations...) or over a long static list
 * is this component, never a `Select` with a search box bolted on.
 *
 * What it adds over the bare `Combobox`:
 *   - an options API in strings (`value` / `onValueChange`), like `Select`'s;
 *   - the platform's one order (`lib/select-options.ts`): alphabetical by
 *     default, `pinned` entries ("Platform default", "All users") kept on top,
 *     `sort={false}` where the order means something;
 *   - word-by-word matching on the label and on the option's secondary text
 *     (`description`, `keywords`, `group`), accents and case ignored;
 *   - `searchable="auto"` for a list of unknown length (a schema enum): a plain
 *     `Select` up to `SEARCH_THRESHOLD` options, the search field above;
 *   - `onSearchChange` for a list too long to load (a library): the server
 *     searches, the picker shows its answer as given.
 *
 * Every picker carries an accessible name (`aria-label` or `aria-labelledby`):
 * the type refuses one without.
 */
import { useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Combobox,
  ComboboxClear,
  ComboboxControls,
  ComboboxField,
  ComboboxInput,
  ComboboxTrigger,
} from "@filigran/design-system";

import { COMBOBOX_INVALID_FIELD_CLASS, INVALID_FIELD_CLASS } from "@/components/ui/invalid-field";
import { ScrollableComboboxContent } from "@/components/ui/scrollable-combobox-content";
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useFormatter } from "@/i18n";
import { fieldHeightClasses } from "@/lib/field-height-classes";
import { matchesQuery, sortByGroupThenLabel, sortByLabel } from "@/lib/select-options";
import { cn } from "@/lib/utils";

export interface SelectOption {
  value: string;
  /** The row's text, what the list is sorted by and what the field shows once picked. */
  label: string;
  /**
   * What the field shows once picked, when the row alone would be ambiguous: a
   * model row reads `gpt-4o` under its provider's header, the field
   * `gpt-4o (OpenAI)`.
   */
  selectedLabel?: string;
  /** Muted text after the label in the row. Searched. */
  description?: string;
  /** Searched, never shown: the email behind a name, the provider behind a model. */
  keywords?: readonly (string | null | undefined)[];
  /** A small glyph before the label, in the row and in the field once picked. */
  icon?: ReactNode;
  disabled?: boolean;
  /** Section header. Groups are sorted like labels (or kept in order with `sort={false}`). */
  group?: string;
}

/** Up to this many options an `auto` picker stays a plain select: the list reads at a glance. */
export const SEARCH_THRESHOLD = 8;

/**
 * A list whose rows carry a meaning. The DS caps the Select viewport at
 * max-h-40 (five 32 px one-line rows), which hides most of a list of 48-72 px
 * two-line rows, and Radix's own viewport style (scrollbar-width: none) hides
 * the scroll cue the DS meant to give. Raise the cap to 18rem (max-h-72: four
 * to five two-line rows, never past the room under the trigger) and bring back
 * a thin scrollbar. Both are important: Radix injects its viewport rule as an
 * unlayered style, which outranks any Tailwind utility layer otherwise.
 */
export const TWO_LINE_LIST_CLASS =
  "[&_[data-radix-select-viewport]]:max-h-[min(18rem,var(--radix-select-content-available-height))]! [&_[data-radix-select-viewport]]:[scrollbar-width:thin]!";

/**
 * The plain select's trigger is a flex item of the DS wrapper, so at `min-width: auto` it never
 * shrinks under the picked label's min-content: a long single-word label (a 14-character group
 * name in a 136 px table cell) drew the field past its cell, over the row's remove button
 * (#611). At 0 it takes the width its container gives it and the label ends in an ellipsis,
 * the chevron kept and the whole label on hover.
 */
export const SHRINKING_TRIGGER_STYLE = { minWidth: 0 } as const;

type Naming =
  | { "aria-label": string; "aria-labelledby"?: string }
  | { "aria-label"?: string; "aria-labelledby": string };

export type SearchableSelectProps = Naming & {
  options: readonly SelectOption[];
  /** Kept above the list in the order given and never sorted: "Platform default", "All users". Searched like the rest. */
  pinned?: readonly SelectOption[];
  value: string | null | undefined;
  onValueChange: (value: string) => void;
  /** Alphabetical (the default), or the order given when the order means something. */
  sort?: boolean;
  /** `true` (the default): always a search field. `"auto"`: a plain select for a short list. */
  searchable?: boolean | "auto";
  /**
   * The search runs on the server: the query typed is reported at each
   * keystroke (`""` once a value is picked or the field cleared), and
   * `options` - the answer, the current value kept in it - are shown as given,
   * never filtered again. Always a search field.
   */
  onSearchChange?: (query: string) => void;
  placeholder?: string;
  emptyMessage?: string;
  /** Lets the person empty the field; an emptied field reports `""`. */
  clearable?: boolean;
  disabled?: boolean;
  /** Header of the pinned entries when the list has groups. Defaults to "Other". */
  pinnedGroup?: string;
  id?: string;
  "aria-describedby"?: string;
  /**
   * Marks the field invalid, as the error line under it says (a value that cannot be saved,
   * a failed server search): announced to assistive tech, and the field draws the error border.
   */
  "aria-invalid"?: boolean;
  /**
   * `false` when the caller names the picked value whole in a design-system tooltip around
   * the picker: the field then carries no native title, so one hover never shows two.
   */
  nativeTitle?: boolean;
  className?: string;
};

export function SearchableSelect({
  options,
  pinned = [],
  value,
  onValueChange,
  sort = true,
  searchable = true,
  onSearchChange,
  placeholder,
  emptyMessage,
  clearable = false,
  disabled = false,
  pinnedGroup,
  id,
  nativeTitle = true,
  className,
  ...aria
}: SearchableSelectProps) {
  const { t_i18n, locale } = useFormatter();
  const ariaLabel = aria["aria-label"];
  const ariaLabelledBy = aria["aria-labelledby"];
  const ariaDescribedBy = aria["aria-describedby"];
  const ariaInvalid = aria["aria-invalid"] || undefined;

  const grouped = options.some((option) => option.group) || pinned.some((option) => option.group);
  const ordered = useMemo(() => {
    const other = t_i18n("Other");
    const head = grouped
      ? pinned.map((option) => (option.group ? option : { ...option, group: pinnedGroup ?? other }))
      : [...pinned];
    const rest = grouped
      ? options.map((option) => (option.group ? option : { ...option, group: other }))
      : [...options];
    const body = !sort
      ? rest
      : grouped
        ? sortByGroupThenLabel(rest, (option) => option.group, (option) => option.label, locale)
        : sortByLabel(rest, (option) => option.label, locale);
    // One row per value: a model id offered by two providers is one choice
    // when the picker stores the bare id.
    const seen = new Set<string>();
    return [...head, ...body].filter((option) => !seen.has(option.value) && !!seen.add(option.value));
  }, [options, pinned, sort, grouped, pinnedGroup, locale, t_i18n]);

  const selected = ordered.find((option) => option.value === value) ?? null;
  const searching =
    !!onSearchChange || searchable === true || (searchable === "auto" && ordered.length > SEARCH_THRESHOLD);

  // A `<fieldset disabled>` disables the input, not the field around it, whose
  // click opens the list: the field follows the fieldset like a native control.
  // Read after every render: an ancestor fieldset changes without any prop of
  // this component changing, and the state is only set when the answer does.
  const inputRef = useRef<HTMLInputElement>(null);
  const [fieldsetDisabled, setFieldsetDisabled] = useState(false);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useLayoutEffect(() => {
    const inherited = !!inputRef.current?.closest("fieldset:disabled");
    if (inherited !== fieldsetDisabled) setFieldsetDisabled(inherited);
  });

  if (!searching) {
    const sections = grouped ? groupsOf(ordered) : [{ label: null, options: ordered }];
    const hasDescriptions = ordered.some((option) => !!option.description);
    return (
      <Select
        value={value ?? ""}
        onValueChange={onValueChange}
        disabled={disabled}
        clearable={clearable}
        clearLabel={t_i18n("Clear")}
      >
        <SelectTrigger
          id={id}
          aria-label={ariaLabel}
          aria-labelledby={ariaLabelledBy}
          aria-describedby={ariaDescribedBy}
          aria-invalid={ariaInvalid}
          style={SHRINKING_TRIGGER_STYLE}
          className={cn("w-full", INVALID_FIELD_CLASS.error, className)}
        >
          <SelectValue placeholder={placeholder}>
            {selected ? <OptionText option={selected} selected /> : undefined}
          </SelectValue>
        </SelectTrigger>
        <SelectContent className={hasDescriptions ? TWO_LINE_LIST_CLASS : undefined}>
          {sections.map((section) => (
            <SelectGroup key={section.label ?? ""}>
              {section.label && (
                <div aria-hidden className="px-4 pb-1 pt-2 content-caption text-default-secondary">
                  {section.label}
                </div>
              )}
              {section.options.map((option) => (
                <SelectItem
                  key={option.value}
                  value={option.value}
                  disabled={option.disabled}
                  textValue={option.label}
                  // The DS SelectItem has one floor, the 32 px one-line row; a row with a
                  // meaning takes the 48 px two-line floor the Combobox draws (isOptionTwoLine).
                  data-lines={option.description ? "two" : "one"}
                  className="data-[lines=two]:min-h-12"
                >
                  <OptionText option={option} />
                </SelectItem>
              ))}
            </SelectGroup>
          ))}
        </SelectContent>
      </Select>
    );
  }

  return (
    <Combobox<SelectOption>
      options={ordered}
      value={selected}
      onValueChange={(change) => {
        const next = Array.isArray(change) ? (change[0] ?? null) : change;
        if (next && next.value !== value) onValueChange(next.value);
        else if (!next && clearable) onValueChange("");
      }}
      getOptionLabel={(option) => option.selectedLabel ?? option.label}
      isOptionEqualToValue={(a, b) => a.value === b.value}
      isOptionDisabled={(option) => !!option.disabled}
      filterOptions={
        onSearchChange
          ? (candidates) => candidates
          : (candidates, query) =>
              candidates.filter((option) =>
                matchesQuery(query, [option.label, option.selectedLabel, option.description, option.group, ...(option.keywords ?? [])]),
              )
      }
      onInputChange={onSearchChange ? (input, meta) => onSearchChange(meta.cause === "type" ? input : "") : undefined}
      groupBy={grouped ? (option) => option.group ?? "" : undefined}
      renderOption={(option) => <OptionText option={option} />}
      // A row with a meaning is the DS two-line row (48 px floor), not the 32 px one-line row.
      isOptionTwoLine={(option) => !!option.description}
      // The library selects the label on the first pointer focus only; a click
      // on a field that kept focus (after a pick) would append to the label.
      // Every opening that is not typing selects it, so a keystroke searches.
      onOpenChange={(open, meta) => {
        if (!open || meta.cause === "type") return;
        requestAnimationFrame(() => {
          const input = inputRef.current;
          if (input && document.activeElement === input && selected && input.value === (selected.selectedLabel ?? selected.label)) {
            input.select();
          }
        });
      }}
      clearable={clearable}
      disabled={disabled || fieldsetDisabled}
      className={className}
    >
      {/* The visible field takes the page's height: the root alone would leave
          it at the library's 36 px floor, taller than the plain select. */}
      <ComboboxField
        startIcon={selected?.icon}
        // The visible border is the field's, around the input: drawn from the input's own
        // aria-invalid, on hover too (the way input.tsx INVALID_INPUT_CLASS keys it).
        className={cn(fieldHeightClasses(className), COMBOBOX_INVALID_FIELD_CLASS)}
      >
        <ComboboxInput
          ref={inputRef}
          id={id}
          aria-label={ariaLabel}
          aria-labelledby={ariaLabelledBy}
          aria-describedby={ariaDescribedBy}
          aria-invalid={ariaInvalid}
          placeholder={placeholder ?? t_i18n("Search...")}
          // A label longer than the field ends in an ellipsis; the whole of it
          // is read on hover.
          title={selected && nativeTitle ? (selected.selectedLabel ?? selected.label) : undefined}
          className="truncate"
        />
        <ComboboxControls>
          {clearable && <ComboboxClear aria-label={t_i18n("Clear")} />}
          <ComboboxTrigger aria-label={t_i18n("Show options")} />
        </ComboboxControls>
      </ComboboxField>
      <ScrollableComboboxContent
        emptyMessage={emptyMessage ?? t_i18n("No results")}
        listAriaLabel={ariaLabel ?? t_i18n("Options")}
      />
    </Combobox>
  );
}

/** Consecutive runs of one group, in order: the shape a plain select's sections take. */
function groupsOf(options: readonly SelectOption[]): { label: string | null; options: SelectOption[] }[] {
  const sections: { label: string | null; options: SelectOption[] }[] = [];
  for (const option of options) {
    const label = option.group ?? null;
    const last = sections[sections.length - 1];
    if (last && last.label === label) last.options.push(option);
    else sections.push({ label, options: [option] });
  }
  return sections;
}

function OptionText({ option, selected = false }: { option: SelectOption; selected?: boolean }) {
  return (
    <span className="flex min-w-0 flex-1 items-center gap-2">
      {option.icon && <span className="flex shrink-0 items-center">{option.icon}</span>}
      {selected ? (
        // The field is one line: a long label ends in an ellipsis and reads whole on hover.
        <span className="min-w-0 truncate" title={option.selectedLabel ?? option.label}>
          {option.selectedLabel ?? option.label}
        </span>
      ) : option.description ? (
        // The meaning sits on its own line under the label at 12 px and wraps, never cut
        // (Forms standard; UX round 1 B3 of #9443). The row breathes: 8 px above and below,
        // 2 px between label and meaning, so one option never touches the next (#11593).
        <span className="flex min-w-0 flex-col gap-0.5 py-2" data-slot="option-text">
          <span className="min-w-0 break-words">{option.label}</span>
          <span className="min-w-0 whitespace-normal break-words content-compact text-default-secondary" data-slot="option-description">
            {option.description}
          </span>
        </span>
      ) : (
        <span className="min-w-0 break-words">{option.label}</span>
      )}
    </span>
  );
}
