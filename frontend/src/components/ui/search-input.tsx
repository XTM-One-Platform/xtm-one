// SPDX-License-Identifier: Apache-2.0

/**
 * Shared search field — an adapter over the design system's `SearchField`.
 *
 * The pattern this used to assemble by hand (a relative wrapper, an absolutely
 * positioned `Search` icon, and an `Input` with left padding) is what
 * `SearchField` is: the icon, its placement, the clear affordance and the
 * submit-on-Enter behaviour all come from the library now.
 *
 * The `onChange(value: string)` signature is kept rather than switched to the
 * library's event-based one, because 44 files call it and the value is what they
 * all want.
 */

import type { FocusEventHandler, KeyboardEventHandler, Ref } from "react";
import { SearchField } from "@filigran/design-system";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";

/**
 * Toolbar mode: the design-system field and its input may shrink, so the hidden
 * placeholder sizer is the only min-content source of the wrapper. Without it the
 * input's intrinsic `size=20` width (169 px plus the field chrome, 201 px) set a
 * floor of about 211 px and pushed the total chip of Presentations to row 2 at
 * 1024. A min-width alone does not remove that intrinsic
 * width from the min-content (round 3 B1): the input also gets a zero basis
 * width (`w-0`) and, being `flex-1` in the field, still fills it.
 */
const TOOLBAR_FIELD_CLASS = "min-w-0 [&_input]:min-w-0 [&_input]:w-0";

/**
 * The one toolbar control height (32 px, design standards s.3): the filter pills
 * and clear control (`filter-pill.tsx`), the visibility segments and the
 * grid/list toggle are 32 px; the field's own 36 px stood out beside them (UX r1
 * A3 on #10109). A caller's `inputClassName` still wins. A toolbar search that
 * keeps a fixed width of its own (so no `toolbar` prop) passes it as
 * `inputClassName` (UX r1 B1 on #10117).
 */
export const TOOLBAR_SEARCH_HEIGHT = "h-8";

interface SearchInputProps {
  /**
   * Placeholder text shown when the field is empty, ALREADY TRANSLATED — the
   * same contract as every other shared control here.
   *
   * This used to run the prop through `t_i18n()`, which made the prop a message
   * key rather than a string. Two things went wrong with that. Eight call sites
   * passed `t_i18n(...)` already and were translated twice, working only because
   * the second lookup missed and fell back to its input. And eight other
   * placeholders were not message ids at all, so the translation quietly did
   * nothing — a gap the adapter's own `t_i18n` call hid from view.
   */
  placeholder?: string;
  /** Current search value. */
  value: string;
  /** Called when the user types. */
  onChange: (value: string) => void;
  /** Extra Tailwind classes on the outer wrapper. */
  className?: string;
  /** Extra Tailwind classes on the field itself (e.g. to match sibling filter sizes). */
  inputClassName?: string;
  /**
   * Accessible name of the input, ALREADY TRANSLATED. Defaults to the
   * placeholder: a placeholder alone is not a label, so without this the field
   * had no accessible name at all.
   */
  "aria-label"?: string;
  /**
   * The one search behaviour of a list-page toolbar: the field takes the
   * free width of its flex-wrap row up to the shared cap (`max-w-sm`), gives it
   * back first so the trailing items stay on the first row, and never
   * shrinks below the width its placeholder needs. The floor is the rendered
   * placeholder itself: an invisible, aria-hidden, nowrap copy of it (with the
   * field's chrome as padding and the 128 px `min-w-32` floor) sets the
   * wrapper's min-content, so the floor is exact in every locale and needs no
   * inline style. The field and its input get `min-w-0` (TOOLBAR_FIELD_CLASS) so
   * the input's intrinsic width never raises that floor. Every list page passes it
   * instead of a width class of its own;
   * `src/pages/list-toolbar-search.test.ts` keeps it that way.
   */
  toolbar?: boolean;
  /**
   * Toolbar mode on a crowded row that must not wrap (a `flex-nowrap` row below
   * 1280): the floor is the 128 px `w-32` sizer instead of the placeholder, so
   * the search gives its width back before any trailing control is squeezed
   * (UX r2 B1 on #10152). The 128 px floor cannot hold a long placeholder, which
   * would be cut mid-word with no ellipsis (UX r3 B1 on #10152), so the field
   * shows the short "Search..." placeholder and keeps the caller's full text as
   * its accessible name (unless `aria-label` is given). Ignored outside `toolbar`.
   */
  compact?: boolean;
  /** Focus the field on mount: the search row of a picker popover that just opened. */
  autoFocus?: boolean;
  /** Disables the field (a picker the edition or the AI switch locks). */
  disabled?: boolean;
  /** The field's id, for a form `Label htmlFor` above a picker search. */
  id?: string;
  /** The input element, for a picker that refocuses its search after a pick. */
  inputRef?: Ref<HTMLInputElement>;
  /** Called when the field is clicked: a picker search reopens its list. */
  onClick?: () => void;
  /** Opens a picker's list when the field takes focus. */
  onFocus?: FocusEventHandler<HTMLInputElement>;
  /** Key handling of a picker (Escape closes it), on top of the field's own clear-on-Escape. */
  onKeyDown?: KeyboardEventHandler<HTMLInputElement>;
  /**
   * The field drives a list under it (a typeahead): the input takes the ARIA
   * combobox pattern, so a screen reader announces the list and the option the
   * arrows highlight while the focus stays in the field.
   */
  combobox?: {
    /** Whether the list is shown. */
    expanded: boolean;
    /** The id of the listbox element. */
    controls: string;
    /** The id of the highlighted option, if any. */
    activeDescendant?: string;
  };
}

export function SearchInput({
  placeholder,
  value,
  onChange,
  className,
  inputClassName,
  "aria-label": ariaLabel,
  toolbar = false,
  compact = false,
  autoFocus,
  disabled,
  id,
  inputRef,
  onClick,
  onFocus,
  onKeyDown,
  combobox,
}: SearchInputProps) {
  const { t_i18n } = useFormatter();
  const fullPlaceholder = placeholder ?? t_i18n("Search...");
  const compactField = toolbar && compact;
  const shownPlaceholder = compactField ? t_i18n("Search...") : fullPlaceholder;
  return (
    <div
      className={cn("relative max-w-sm", toolbar && "flex-1", className)}
      data-toolbar-search={toolbar ? "true" : undefined}
    >
      {toolbar && (
        <span
          aria-hidden="true"
          data-testid="search-input-sizer"
          data-floor={compactField ? "fixed" : "placeholder"}
          className={cn(
            "invisible block h-0 overflow-hidden whitespace-nowrap pl-8 pr-4 content-base",
            compactField ? "w-32" : "min-w-32",
          )}
        >
          {shownPlaceholder}
        </span>
      )}
      <SearchField
        placeholder={shownPlaceholder}
        aria-label={ariaLabel ?? fullPlaceholder}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onClear={() => onChange("")}
        clearLabel={t_i18n("Clear search")}
        fullWidth
        ref={inputRef}
        id={id}
        autoFocus={autoFocus}
        disabled={disabled}
        onClick={onClick}
        onFocus={onFocus}
        onKeyDown={onKeyDown}
        {...(combobox && {
          role: "combobox",
          "aria-autocomplete": "list" as const,
          "aria-expanded": combobox.expanded,
          "aria-controls": combobox.controls,
          "aria-activedescendant": combobox.activeDescendant,
        })}
        className={cn(toolbar && TOOLBAR_FIELD_CLASS, toolbar && TOOLBAR_SEARCH_HEIGHT, inputClassName)}
        data-toolbar-field={toolbar ? "shrinkable" : undefined}
      />
    </div>
  );
}
