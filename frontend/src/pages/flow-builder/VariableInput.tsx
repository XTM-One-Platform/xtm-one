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
 * Text field with `{{` autocomplete over upstream node outputs.
 *
 * Typing `{{` opens a filtered list of the references this node is actually
 * allowed to use: only nodes that run *before* it, only outputs they actually
 * publish. Anything else would be rejected by the server on save, so offering
 * it would be a trap.
 *
 * The two prefixes the existing assignment renderer owns — `{{var_id.UUID}}`
 * for platform variables and `{{trigger.*}}` for the trigger payload — are left
 * alone by the validator and are simply not suggested here; they are a
 * different namespace, resolved at run time from a different source.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Input } from "@/components/ui/input";
import { VariableInsertButton } from "@/components/ui/variable-insert-button";
import { cn } from "@/lib/utils";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { useFormatter } from "@/i18n";
import { ShapeTypeBadge } from "./ShapeTypeBadge";
import type { VariableSuggestion } from "./upstream";

interface VariableInputProps {
  value: string;
  onChange: (value: string) => void;
  suggestions: VariableSuggestion[];
  multiline?: boolean;
  /** Multiline row count — the drawer default stays compact, the expanded
   *  dialog asks for room. */
  rows?: number;
  placeholder?: string;
  disabled?: boolean;
  id?: string;
  /** The field's name: the design system's `Input` reads only `label`,
   *  `aria-label` or `aria-labelledby`, never a `<Label>` beside it. */
  "aria-label": string;
}

/** The `{{`-and-after fragment the caret currently sits in, if any. */
function activeFragment(text: string, caret: number): { start: number; query: string } | null {
  const before = text.slice(0, caret);
  const open = before.lastIndexOf("{{");
  if (open === -1) return null;
  // A `}}` between the `{{` and the caret means that placeholder is closed.
  if (before.indexOf("}}", open) !== -1) return null;
  return { start: open, query: before.slice(open + 2).trim() };
}

export function VariableInput({
  value,
  onChange,
  suggestions,
  multiline,
  rows,
  placeholder,
  disabled,
  id,
  "aria-label": ariaLabel,
}: VariableInputProps) {
  const { t_i18n } = useFormatter();
  const ref = useRef<HTMLInputElement | HTMLTextAreaElement>(null);
  const [fragment, setFragment] = useState<{ start: number; query: string } | null>(
    null,
  );
  const [highlighted, setHighlighted] = useState(0);

  const matches = useMemo(() => {
    if (!fragment) return [];
    const query = fragment.query.toLowerCase();
    return suggestions
      .filter(
        (s) =>
          !query ||
          s.ref.toLowerCase().includes(query) ||
          s.nodeLabel.toLowerCase().includes(query),
      )
      .slice(0, 40);
  }, [fragment, suggestions]);

  // Keyboard navigation scrolls the list with it.
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>(`[data-index="${highlighted}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [highlighted]);

  const syncFragment = (element: HTMLInputElement | HTMLTextAreaElement) => {
    setFragment(activeFragment(element.value, element.selectionStart ?? 0));
    setHighlighted(0);
  };

  const insert = (suggestion: VariableSuggestion) => {
    const element = ref.current;
    if (!element || !fragment) return;
    const caret = element.selectionStart ?? value.length;
    const next =
      value.slice(0, fragment.start) + `{{${suggestion.ref}}}` + value.slice(caret);
    onChange(next);
    setFragment(null);
    // Put the caret after the inserted placeholder so typing continues inline.
    const position = fragment.start + suggestion.ref.length + 4;
    requestAnimationFrame(() => {
      element.focus();
      element.setSelectionRange(position, position);
    });
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (!fragment || matches.length === 0) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setHighlighted((i) => (i + 1) % matches.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setHighlighted((i) => (i - 1 + matches.length) % matches.length);
    } else if (event.key === "Enter" || event.key === "Tab") {
      event.preventDefault();
      insert(matches[highlighted]);
    } else if (event.key === "Escape") {
      // Stop here: the builder's global Escape would also close the drawer.
      event.stopPropagation();
      setFragment(null);
    }
  };

  const shared = {
    id,
    "aria-label": ariaLabel,
    value,
    placeholder,
    disabled,
    onKeyDown,
    onChange: (
      event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>,
    ) => {
      onChange(event.target.value);
      syncFragment(event.target);
    },
    onClick: (
      event: React.MouseEvent<HTMLInputElement | HTMLTextAreaElement>,
    ) => syncFragment(event.currentTarget),
    // A blur that lands on a suggestion is handled by its `onMouseDown`, which
    // fires first; anything else should dismiss the list.
    onBlur: () => setTimeout(() => setFragment(null), 0),
  };

  return (
    <div className="relative">
      {multiline ? (
        <>
          <textarea
            {...shared}
            ref={ref as React.RefObject<HTMLTextAreaElement>}
            rows={rows ?? 4}
            className={cn("w-full rounded-sm border border-elevation-subtle bg-transparent px-3 py-2 content-base placeholder:text-default-secondary disabled:cursor-not-allowed disabled:opacity-50", FIELD_FOCUS_RING)}
          />
          {/* Platform variables ({{var_id.UUID}}) resolve at run time like in an assignment prompt. */}
          {!disabled && (
            <div className="flex justify-end">
              <VariableInsertButton textareaRef={ref} value={value} onChange={onChange} />
            </div>
          )}
        </>
      ) : (
        <Input {...shared} ref={ref as React.RefObject<HTMLInputElement>} />
      )}

      {fragment && (
        <div
          ref={listRef}
          className="absolute z-20 mt-1 max-h-72 w-full overflow-y-auto rounded-sm border border-elevation-subtle layer-2 bg-elevation-default text-default-primary shadow-overlay"
        >
          {matches.length === 0 ? (
            <p className="px-3 py-2 text-[11px] text-default-secondary">
              {suggestions.length === 0
                ? t_i18n("Connect this node to something upstream to reference its output.")
                : t_i18n("No upstream output matching that name")}
            </p>
          ) : (
            matches.map((suggestion, index) => (
              <button
                key={suggestion.ref}
                type="button"
                data-index={index}
                // mousedown, not click: the input's blur would close the list first.
                onMouseDown={(event) => {
                  event.preventDefault();
                  insert(suggestion);
                }}
                onMouseEnter={() => setHighlighted(index)}
                className={cn(
                  "flex w-full flex-col items-start gap-0.5 px-3 py-1.5 text-left transition-colors",
                  index === highlighted && "bg-elevation-hover",
                )}
              >
                <span className="flex w-full items-center gap-1.5">
                  <span className="min-w-0 truncate font-mono-plex text-[11px] text-accent">
                    {suggestion.ref}
                  </span>
                  <ShapeTypeBadge
                    type={suggestion.type}
                    declared={suggestion.declared}
                    observed={suggestion.observed}
                    className="ml-auto"
                  />
                </span>
                <span className="w-full truncate content-caption text-default-secondary">
                  {suggestion.nodeLabel} ·{" "}
                  {/[{}›]/.test(suggestion.outputLabel) ? suggestion.outputLabel : t_i18n(suggestion.outputLabel)}
                  {suggestion.sample !== undefined && suggestion.sample !== null && (
                    <span className="font-mono-plex"> = {String(suggestion.sample)}</span>
                  )}
                </span>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}
