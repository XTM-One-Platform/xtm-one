// SPDX-License-Identifier: Apache-2.0

/** TagInput – reusable tag editor for create/edit forms.
 *
 * Renders existing tags as removable badges and an inline text input
 * for adding new ones. Press Enter or comma to add a tag.
 *
 * Features autocomplete suggestions from all existing tags across the
 * platform. Type to filter, arrow-key to navigate, Enter to select.
 * If the typed text doesn't match any existing tag, it creates a new one.
 *
 * Supports a `disabled` prop that renders the component in a read-only
 * state matching the disabled Input component styling (same height,
 * border, and opacity).
 *
 * Typed text is folded to lower case unless `preserveCase` is set; the
 * rules live in `tag-rules.ts` so they can be unit-tested without a DOM.
 *
 * The autocomplete draws on the platform-wide tag list. A field whose values
 * are owned by something else — a dataset's label vocabulary — turns it off
 * with `suggestFromPlatformTags={false}`.
 */

import { useState, useRef, useEffect, useCallback, type KeyboardEvent } from "react";
import { Icon } from "@filigran/design-system";
import { useClickOutside } from "@/hooks/useClickOutside";
import { cn } from "@/lib/utils";
import { useTags } from "@/hooks/api/useSettingsApi";
import { useFormatter } from "@/i18n";
import { containsTag, normalizeTag, platformSuggestions } from "./tag-rules";

interface TagInputProps {
  value: string[];
  onChange: (tags: string[]) => void;
  placeholder?: string;
  className?: string;
  disabled?: boolean;
  /** Keep what was typed instead of folding it to lower case.
   *
   *  Off by default: folding stops `Malware` and `malware` becoming two tags.
   *  Opt in where the stored text is shown back as written, such as a dataset's
   *  label vocabulary (scoring folds its case). */
  preserveCase?: boolean;
  /** Offer the platform-wide tag list as autocomplete.
   *
   *  On by default, since reusing an existing tag is the point. Off where the
   *  values belong to the edited entity, such as a dataset's label vocabulary,
   *  a closed set no platform tag belongs to. Off also skips the query. */
  suggestFromPlatformTags?: boolean;
  /** The id of the typing box, so a <label htmlFor> names it. */
  id?: string;
  /** The accessible name of the typing box, when no <label> names it. */
  "aria-label"?: string;
  /** The id of the visible label that names the typing box (a column label over a list of rows). */
  "aria-labelledby"?: string;
  /** The id of the help or error line tied to the typing box. */
  "aria-describedby"?: string;
  /** What the read-only box says when it holds nothing (default "No tags"). */
  emptyLabel?: string;
  /** The typing box's invalid state, drawn as the error border; the caller shows the message. */
  "aria-invalid"?: boolean;
  /** Tags the caller refuses: drawn in the error tone, so the bad entry is the one marked. */
  invalidTags?: string[];
}

export function TagInput({
  value,
  onChange,
  placeholder = "Add tag…",
  className,
  disabled,
  preserveCase = false,
  suggestFromPlatformTags = true,
  id,
  "aria-label": ariaLabel,
  "aria-labelledby": ariaLabelledBy,
  "aria-describedby": ariaDescribedBy,
  emptyLabel,
  "aria-invalid": ariaInvalid,
  invalidTags,
}: TagInputProps) {
  const { t_i18n } = useFormatter();
  const [input, setInput] = useState("");
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(-1);
  const inputRef = useRef<HTMLInputElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const suggestionsRef = useRef<HTMLDivElement>(null);

  const { data: allTags = [] } = useTags({ enabled: suggestFromPlatformTags });

  // Which platform tags to offer, if any. The rule lives in `tag-rules.ts` so the
  // gate is covered by a test.
  const suggestions = platformSuggestions(
    allTags,
    value,
    input,
    suggestFromPlatformTags,
  );

  const addTag = useCallback(
    (raw: string) => {
      const tag = normalizeTag(raw, preserveCase);
      if (tag && !containsTag(value, tag)) {
        onChange([...value, tag]);
      }
      setInput("");
      setShowSuggestions(false);
      setHighlightedIndex(-1);
    },
    [value, onChange, preserveCase],
  );

  const removeTag = (tag: string) => {
    onChange(value.filter((t) => t !== tag));
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (showSuggestions && suggestions.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setHighlightedIndex((prev) =>
          prev < suggestions.length - 1 ? prev + 1 : 0,
        );
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setHighlightedIndex((prev) =>
          prev > 0 ? prev - 1 : suggestions.length - 1,
        );
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setShowSuggestions(false);
        setHighlightedIndex(-1);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        if (highlightedIndex >= 0 && highlightedIndex < suggestions.length) {
          e.preventDefault();
          addTag(suggestions[highlightedIndex]);
          return;
        }
      }
    }

    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      addTag(input);
    }
    if (e.key === "Backspace" && !input && value.length > 0) {
      removeTag(value[value.length - 1]);
    }
  };

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setInput(e.target.value);
    setShowSuggestions(true);
    setHighlightedIndex(-1);
  };

  // Close suggestions on outside click
  useClickOutside(containerRef, () => {
    setShowSuggestions(false);
    setHighlightedIndex(-1);
  });

  // Scroll highlighted suggestion into view
  useEffect(() => {
    if (highlightedIndex >= 0 && suggestionsRef.current) {
      const items = suggestionsRef.current.children;
      if (items[highlightedIndex]) {
        (items[highlightedIndex] as HTMLElement).scrollIntoView({
          block: "nearest",
        });
      }
    }
  }, [highlightedIndex]);

  // Exactly what `addTag` would store, so the "Create …" badge shows the value
  // the click adds. Membership checks stay case-insensitive.
  const pendingTag = normalizeTag(input, preserveCase);
  const isNewTag =
    pendingTag.length > 0 &&
    !containsTag(allTags, pendingTag) &&
    !containsTag(value, pendingTag);

  // The box wears the shared Input's skin (the design system's `Input`): the
  // `bg-input-default` fill with a transparent border that turns
  // `border-input-hover` on hover, outlined `border-elevation-disabled` with no
  // fill when disabled, and a 14 px placeholder. Disabled carries no opacity and
  // its "No tags" text uses `text-input-disabled`, as the disabled Input does, so
  // its border and text keep the Input's contrast. Set here, not per page, so a
  // Tags field reads as the same control as the Name field beside it.
  //
  // Height and focus match the Input too: a 36 px base (`min-h-9`, the
  // Input's height) that a chip row fits inside (16 px chip line + 4 px padding +
  // 2 px border, in a box with 4 px padding), so callers pass no height; and the
  // Input's focus ring, 2 px offset then 2 px `ring-focus`, with no accent border.
  // More chips than one row holds still wrap and grow the box.
  return (
    <div ref={containerRef} className="relative">
      <div
        className={cn(
          "flex min-h-9 flex-wrap items-center gap-1.5 rounded-sm border border-transparent bg-input-default px-3 py-1 content-base ring-offset-focus transition-colors duration-200",
          disabled
            ? "cursor-not-allowed bg-transparent border-elevation-disabled"
            : "focus-within:outline-hidden focus-within:ring-2 focus-within:ring-focus focus-within:ring-offset-2",
          // An invalid box keeps its error border on hover; a valid one shows the hover border.
          !disabled && (ariaInvalid ? "border-feedback-error-primary" : "hover:border-input-hover"),
          className,
        )}
        onClick={() => {
          if (!disabled) inputRef.current?.focus();
        }}
      >
        {value.map((tag) => (
          <span
            key={tag}
            data-invalid={invalidTags?.includes(tag) ? "true" : undefined}
            className={cn(
              "inline-flex items-center gap-0.5 rounded-sm border px-2 py-0.5 content-compact leading-4 font-medium",
              invalidTags?.includes(tag)
                ? // Default text on the error tint, 4.5:1 in both themes (skill 13bis.6); the error tone stays on the border.
                  "bg-feedback-error-primary/10 border-feedback-error-primary text-default-primary"
                : "bg-accent/10 border-accent/20 text-accent",
            )}
          >
            {tag}
            {!disabled && (
              <button aria-label={t_i18n("Remove tag")}
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  removeTag(tag);
                }}
                className="-my-0.5 -mr-1 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-sm hover:bg-accent/20"
              >
                <Icon name="x" size={16} />
              </button>
            )}
          </span>
        ))}
        {!disabled && (
          <input
            ref={inputRef}
            id={id}
            type="text"
            aria-label={ariaLabel}
            aria-labelledby={ariaLabelledBy}
            aria-describedby={ariaDescribedBy}
            aria-invalid={ariaInvalid || undefined}
            value={input}
            onChange={handleInputChange}
            onKeyDown={handleKeyDown}
            onFocus={() => setShowSuggestions(true)}
            onBlur={() => {
              // Delay to allow click on suggestion
              setTimeout(() => {
                if (input.trim()) addTag(input);
              }, 150);
            }}
            placeholder={value.length === 0 ? t_i18n(placeholder) : ""}
            className="flex-1 min-w-[60px] bg-transparent outline-hidden content-base leading-5 placeholder:text-default-secondary"
          />
        )}
        {disabled && value.length === 0 && (
          <span className="content-base leading-5 text-input-disabled">{emptyLabel ?? t_i18n("No tags")}</span>
        )}
      </div>

      {/* Autocomplete dropdown — platform tags plus a "create this one" row. */}
      {suggestFromPlatformTags && showSuggestions && !disabled && (suggestions.length > 0 || isNewTag) && (
        <div
          ref={suggestionsRef}
          data-testid="tag-input.suggestions"
          className="absolute z-50 mt-1 w-full max-h-40 overflow-y-auto rounded-sm border border-elevation-subtle layer-2 bg-elevation-default shadow-overlay"
        >
          {suggestions.map((tag, index) => (
            <button
              key={tag}
              type="button"
              onMouseDown={(e) => {
                e.preventDefault();
                addTag(tag);
              }}
              onMouseEnter={() => setHighlightedIndex(index)}
              className={cn(
                "flex w-full items-center px-3 py-1.5 content-compact text-left transition-colors",
                index === highlightedIndex
                  ? "bg-accent/10 text-accent"
                  : "text-default-primary hover:bg-elevation-hover",
              )}
            >
              {tag}
            </button>
          ))}
          {isNewTag && (
            <button
              type="button"
              onMouseDown={(e) => {
                e.preventDefault();
                addTag(input);
              }}
              onMouseEnter={() => setHighlightedIndex(suggestions.length)}
              className={cn(
                "flex w-full items-center gap-1.5 px-3 py-1.5 content-compact text-left transition-colors border-t border-elevation-subtle",
                highlightedIndex === suggestions.length
                  ? "bg-accent/10 text-accent"
                  : "text-default-secondary hover:bg-elevation-hover hover:text-default-primary",
              )}
            >
              {t_i18n("Create")}{" "}
              <span className="inline-flex items-center rounded-sm bg-accent/10 border border-accent/20 px-1.5 py-0.5 content-compact font-medium text-accent">
                {pendingTag}
              </span>
            </button>
          )}
        </div>
      )}
    </div>
  );
}
