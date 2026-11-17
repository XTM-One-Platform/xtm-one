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
 * AgentFocusPicker - the Focus control of the Agentic Flow map toolbar: a
 * search over the agents the toolbar keeps, the focused agents as removable
 * chips, and Clear. One control for the Graph and the Schedule views of the
 * map, so both narrow to the same agents the same way.
 *
 * `children` is drawn after the chips while a focus is set (the Graph view's
 * "Show only | Highlight" choice).
 *
 * Keyboard (like the ModelDirectory search): the input keeps the
 * focus, ArrowDown / ArrowUp move the active option (announced through
 * `aria-activedescendant`, `aria-selected` on it), Home / End jump to the
 * first and last option once one is active (the caret keeps them before),
 * Enter picks it, Escape closes the list then clears the query. ArrowDown on
 * a closed list reopens it on the first option.
 *
 * The active option carries an inset focus-token edge besides its fill, so it
 * reads without relying on the faint hover fill, and a row under the pointer
 * is told apart from the active one.
 */

import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Focus, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { FilterClearButton } from "@/components/ui/filter-pill";
import { cn } from "@/lib/utils";
import { agentMatchesQuery } from "@/lib/agent-graph-labels";
import { useFormatter } from "@/i18n";

export interface AgentFocusPickerProps {
  /** The agents offered, in display order; found by name, office or fleet. */
  agents: readonly { id: string; name: string; fleet?: { fleet_name?: string | null } | null }[];
  focusedIds: ReadonlySet<string>;
  onToggle: (id: string) => void;
  onClear: () => void;
  /** The name of a focused agent, also when the filters no longer offer it. */
  nameOf: (id: string) => string | undefined;
  children?: ReactNode;
}

export function AgentFocusPicker({ agents, focusedIds, onToggle, onClear, nameOf, children }: AgentFocusPickerProps) {
  const { t_i18n } = useFormatter();
  const [search, setSearch] = useState("");
  const [open, setOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const listId = useId();
  // -1: no active option, the input alone is current.
  const [active, setActive] = useState(-1);
  const offered = agents
    .filter((a) => !focusedIds.has(a.id))
    .filter((a) => agentMatchesQuery(search, a.name, a.fleet?.fleet_name));
  const results = open ? offered : [];
  const activeIndex = active < results.length ? active : results.length - 1;
  const optionId = (index: number) => `${listId}-option-${index}`;
  const activeId = activeIndex >= 0 ? optionId(activeIndex) : undefined;

  useEffect(() => {
    if (!activeId) return;
    document.getElementById(activeId)?.scrollIntoView?.({ block: "nearest" });
  }, [activeId]);

  const pick = (id: string) => {
    onToggle(id);
    setSearch("");
    setActive(-1);
    inputRef.current?.focus();
  };

  const onInputKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (!open) {
        // Reopening press: land on the first option at once.
        setOpen(true);
        setActive(offered.length > 0 ? 0 : -1);
      } else if (results.length > 0) setActive(Math.min(activeIndex + 1, results.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive(Math.max(activeIndex - 1, 0));
    } else if ((event.key === "Home" || event.key === "End") && activeIndex >= 0) {
      event.preventDefault();
      setActive(event.key === "Home" ? 0 : results.length - 1);
    } else if (event.key === "Enter" && activeIndex >= 0) {
      event.preventDefault();
      pick(results[activeIndex].id);
    }
  };

  return (
    <div className="flex items-center gap-2 flex-wrap">
      <Focus className="h-3.5 w-3.5 text-default-secondary shrink-0" />
      <span className="content-compact text-default-secondary shrink-0">{t_i18n("Focus")}</span>
      <div
        className="relative"
        // The list closes when the focus leaves the picker, not when it
        // moves from the input to a result, so a keyboard user can tab
        // to a result and pick it.
        onBlur={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOpen(false);
        }}
        onKeyDown={(e) => {
          if (e.key !== "Escape") return;
          // Escape closes the list first, then clears the query.
          e.stopPropagation();
          if (open) {
            setOpen(false);
            setActive(-1);
            inputRef.current?.focus();
          } else setSearch("");
        }}
      >
        <input
          ref={inputRef}
          type="text"
          role="combobox"
          aria-label={t_i18n("Focus on agents")}
          aria-expanded={results.length > 0}
          aria-autocomplete="list"
          aria-controls={results.length > 0 ? listId : undefined}
          aria-activedescendant={activeId}
          className="h-7 w-48 rounded-sm border border-elevation-subtle bg-elevation-default px-2.5 content-compact placeholder:text-default-secondary focus:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          placeholder={focusedIds.size === 0 ? t_i18n("All agents") : t_i18n("Add agent...")}
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setOpen(true);
            setActive(-1);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={onInputKeyDown}
        />
        {results.length > 0 && (
          <div
            id={listId}
            role="listbox"
            aria-label={t_i18n("Agents")}
            className="absolute z-50 mt-1 w-56 max-h-48 overflow-auto rounded-sm border border-elevation-subtle layer-2 bg-elevation-default shadow-overlay py-1"
          >
            {results.map((a, index) => (
              <button
                key={a.id}
                id={optionId(index)}
                role="option"
                aria-selected={index === activeIndex}
                className={cn(
                  // Light: the 2 px focus ring alone marks a tabbed row, no
                  // heavy fill under it; dark keeps the fill.
                  "w-full px-2.5 py-1.5 text-left content-compact hover:bg-elevation-hover transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-focus dark:focus-visible:bg-elevation-hover",
                  // The active option: an inset focus edge (3:1 or more) besides
                  // the fill, and a distinct fill under the pointer.
                  index === activeIndex && "ring-1 ring-inset ring-focus dark:bg-elevation-hover hover:bg-elevation-highlight",
                )}
                type="button"
                // The input keeps the focus on a mouse pick; Enter and
                // Space pick a result reached with the keyboard.
                onMouseDown={(e) => e.preventDefault()}
                // Real pointer movement only: mouseenter also fires when
                // scrollIntoView slides a row under a still pointer, which
                // took the active option back from the keyboard.
                onMouseMove={() => {
                  if (index !== activeIndex) setActive(index);
                }}
                onClick={() => pick(a.id)}
              >
                {/* One line per row, the full name on hover. */}
                <span className="block truncate" title={a.name}>
                  {a.name}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
      {Array.from(focusedIds).map((id) => (
        <Badge
          key={id}
          variant="outline"
          className="content-caption h-6 max-w-32 gap-1 pl-2 pr-1 border-feedback-alert-primary/40 text-feedback-warning-tertiary bg-feedback-alert-primary/10 cursor-pointer hover:bg-feedback-alert-primary/20"
          onClick={() => onToggle(id)}
        >
          {/* A capped chip: a long name truncates, the full name on hover. */}
          <span className="min-w-0 truncate" title={nameOf(id) ?? id}>
            {nameOf(id) ?? id}
          </span>
          <X className="h-3 w-3 shrink-0" />
        </Badge>
      ))}
      {focusedIds.size > 0 && (
        // The shared toolbar clear control (#9378): 12 px, focus ring.
        <FilterClearButton onClick={onClear}>{t_i18n("Clear")}</FilterClearButton>
      )}
      {focusedIds.size > 0 && children}
    </div>
  );
}
