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
 * The toolbar's slide counter, which opens into a "go to slide" menu.
 *
 * Replaces reveal's `G` jump box, which draws an input on top of the slide. The
 * trigger reads `3 / 12`; the menu lists every slide by title with the current
 * one marked, plus a number field for a deck too long to scan. Rendered inline
 * (no portal) so it works inside a fullscreen stage, and hand-rolled on the
 * same lines as `OverflowMenu`: arrows move, Enter jumps, Escape closes and
 * returns focus to the trigger, an outside click closes.
 *
 * The whole surface is marked `data-deck-keys="off"`: while it is open the
 * arrows and Enter belong to the list, never to the deck behind it.
 */
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";

import { useClickOutside } from "@/hooks/useClickOutside";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { PresentationKind, PresentationSlide } from "@/types";

import { DECK_KEYS_OFF_ATTR } from "./deck-keys";
import { slideHeading } from "./slide-label";

export function SlideJumpMenu({
  open,
  onOpenChange,
  slides,
  index,
  total,
  onJump,
  compact = false,
  kind = "deck",
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  slides: PresentationSlide[];
  /** Zero-based index of the slide on screen. */
  index: number;
  total: number;
  onJump: (index: number) => void;
  compact?: boolean;
  /** A page document counts pages, not slides: same menu, its nouns. */
  kind?: PresentationKind;
}) {
  const { t_i18n } = useFormatter();
  const isPage = kind === "page";
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const optionRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const listId = useId();
  const [active, setActive] = useState(index);
  const [typed, setTyped] = useState("");

  const close = useCallback(
    (opts: { restoreFocus?: boolean } = {}) => {
      onOpenChange(false);
      if (opts.restoreFocus) triggerRef.current?.focus();
    },
    [onOpenChange],
  );

  const closeOnOutside = useCallback(() => close(), [close]);
  useClickOutside(rootRef, closeOnOutside, open);

  // Opening (by click or by the `G` shortcut) lands in the number field with
  // the current slide highlighted and scrolled into view.
  useEffect(() => {
    if (!open) return;
    setActive(index);
    setTyped("");
    const frame = requestAnimationFrame(() => {
      inputRef.current?.focus();
      optionRefs.current[index]?.scrollIntoView({ block: "nearest" });
    });
    return () => cancelAnimationFrame(frame);
    // `index` is read once, at open time: a deck that moves while the menu is
    // open (a collaborator's edit) must not yank the highlight around.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const jump = (target: number) => {
    if (Number.isNaN(target) || target < 0 || target >= total) return;
    onJump(target);
    close({ restoreFocus: true });
  };

  const moveActive = (next: number) => {
    const clamped = Math.max(0, Math.min(total - 1, next));
    setActive(clamped);
    optionRefs.current[clamped]?.scrollIntoView({ block: "nearest" });
  };

  const onSurfaceKeyDown = (e: React.KeyboardEvent) => {
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        moveActive(active + 1);
        break;
      case "ArrowUp":
        e.preventDefault();
        moveActive(active - 1);
        break;
      case "Home":
        e.preventDefault();
        moveActive(0);
        break;
      case "End":
        e.preventDefault();
        moveActive(total - 1);
        break;
      case "Enter": {
        e.preventDefault();
        const number = Number.parseInt(typed, 10);
        jump(typed.trim() !== "" && !Number.isNaN(number) ? number - 1 : active);
        break;
      }
      case "Escape":
        e.preventDefault();
        e.stopPropagation();
        close({ restoreFocus: true });
        break;
      case "Tab":
        close();
        break;
      default:
        break;
    }
  };

  const counterValues = { current: Math.min(index + 1, Math.max(total, 1)), total };
  const counterLabel = isPage
    ? t_i18n("Page {current} of {total}", counterValues)
    : t_i18n("Slide {current} of {total}", counterValues);
  const goToLabel = isPage ? t_i18n("Go to page") : t_i18n("Go to slide");
  const numberLabel = isPage ? t_i18n("Page number") : t_i18n("Slide number");
  const untitledLabel = isPage ? t_i18n("Untitled page") : t_i18n("Untitled slide");

  return (
    <div ref={rootRef} className="relative">
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listId : undefined}
        aria-label={`${counterLabel}. ${goToLabel}`}
        onClick={() => onOpenChange(!open)}
        className={cn(
          "inline-flex h-8 items-center gap-1 rounded-sm px-2 font-mono-plex tabular-nums text-default-secondary transition-colors",
          compact ? "text-[11px]" : "content-compact",
          "hover:bg-elevation-hover hover:text-default-primary",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus",
          open && "bg-elevation-hover text-default-primary",
        )}
      >
        <span className="text-default-primary">{Math.min(index + 1, Math.max(total, 1))}</span>
        <span className="opacity-60">/</span>
        <span>{total}</span>
        <ChevronDown className="h-3 w-3 opacity-70" />
      </button>

      {open && (
        <div
          {...DECK_KEYS_OFF_ATTR}
          onKeyDown={onSurfaceKeyDown}
          className={cn(
            "absolute left-1/2 top-full z-50 mt-1 w-[300px] -translate-x-1/2 overflow-hidden rounded-sm",
            "border border-elevation-subtle layer-1 bg-elevation-default shadow-overlay",
            "animate-in fade-in-0 zoom-in-95 duration-100",
          )}
        >
          <div className="border-b border-elevation-subtle-soft p-2">
            <input
              ref={inputRef}
              type="number"
              inputMode="numeric"
              min={1}
              max={total}
              value={typed}
              onChange={(e) => {
                setTyped(e.target.value);
                const number = Number.parseInt(e.target.value, 10);
                if (!Number.isNaN(number) && number >= 1 && number <= total) moveActive(number - 1);
              }}
              placeholder={numberLabel}
              aria-label={numberLabel}
              className={cn(
                "h-8 w-full rounded-sm border border-elevation-subtle bg-elevation-default px-2.5 content-compact",
                "placeholder:text-default-secondary focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
              )}
            />
          </div>
          <div
            id={listId}
            role="listbox"
            aria-label={goToLabel}
            aria-activedescendant={`${listId}-${active}`}
            // The panel opens INTO the deck box (the chat card clips at its
            // rounded edge), so the compact list is kept shorter than the
            // shortest 16:9 box a chat column produces; it scrolls anyway.
            className={cn(
              "overflow-y-auto p-1",
              compact ? "max-h-[min(40vh,220px)]" : "max-h-[min(50vh,320px)]",
            )}
          >
            {slides.map((slide, i) => {
              const heading = slideHeading(slide) || untitledLabel;
              const current = i === index;
              return (
                <button
                  key={slide.id ?? `slide-${i}`}
                  id={`${listId}-${i}`}
                  ref={(el) => {
                    optionRefs.current[i] = el;
                  }}
                  type="button"
                  role="option"
                  aria-selected={current}
                  tabIndex={-1}
                  onMouseEnter={() => setActive(i)}
                  onClick={() => jump(i)}
                  className={cn(
                    "flex w-full items-center gap-2.5 rounded-sm px-2 py-1.5 text-left content-compact transition-colors",
                    i === active && "bg-elevation-hover",
                    current ? "text-accent" : "text-default-primary",
                  )}
                >
                  <span
                    className={cn(
                      "w-6 shrink-0 font-mono-plex tabular-nums text-[11px]",
                      current ? "text-accent" : "text-default-secondary",
                    )}
                  >
                    {i + 1}
                  </span>
                  <span className="min-w-0 flex-1 truncate">{heading}</span>
                  <span className="shrink-0 content-caption uppercase tracking-wide text-default-secondary">
                    {slide.layout.replace(/_/g, " ")}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
