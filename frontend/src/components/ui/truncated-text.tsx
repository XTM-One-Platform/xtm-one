// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef, useState } from "react";
import { TIP_WRAP_FOCUS_RING, Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { capTipText } from "@/lib/tip-text";
import { cn } from "@/lib/utils";

/** The focusable element a cut text sits in (a row link, a button), if any. */
const FOCUSABLE_ANCESTOR = "a[href],button,[tabindex]:not([tabindex='-1'])";

/**
 * One line of text cut with an ellipsis when its box is too narrow, and then only named
 * whole by the design-system tooltip (design standards s.3: a cut text carries a
 * tooltip, never a native title; UX r5 B1 on #10581). Measured again when the box or
 * the text changes; text that fits draws no tooltip.
 *
 * The tooltip opens on hover AND on keyboard focus (UX r6 B1 on #10581): inside a
 * focusable element (the row link of a table) it opens when that element takes the
 * focus; elsewhere (an Email or Description cell) the cut text itself becomes a tab
 * stop with the design-system focus ring. A pointer press focuses without opening, as
 * in `Tip`.
 *
 * One tooltip per focus (UX r1 B1 on #10947): when a focusable element holds several cut
 * texts (a card button with a cut name, handle and description), only the first one
 * opens on the element's focus, and its tooltip names every cut line of the element;
 * each line still opens its own tooltip on hover.
 *
 * `lines` clamps the text to 2 or 3 lines instead of one (a description preview on a
 * card): the cut is then measured on the height, and the tooltip names the whole text.
 */
const LINE_CLAMP = { 2: "line-clamp-2", 3: "line-clamp-3" } as const;

export function TruncatedText({ text, className, lines }: { text: string; className?: string; lines?: 2 | 3 }) {
  const ref = useRef<HTMLSpanElement>(null);
  const [cut, setCut] = useState(false);
  const [inFocusable, setInFocusable] = useState(false);
  const [open, setOpen] = useState(false);
  const [hostLines, setHostLines] = useState<string[] | null>(null);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    setInFocusable(Boolean(node.parentElement?.closest(FOCUSABLE_ANCESTOR)));
    // One line: the browser draws the ellipsis at any sub-pixel overflow, and both widths
    // are rounded integers, so any excess is a drawn cut (141 > 140; UX r3 B3 on #1590).
    // A clamp keeps a 1 px tolerance on the height, where line-height rounding alone
    // leaves a pixel without cutting a line.
    const measure = () =>
      setCut(lines ? node.scrollHeight > node.clientHeight + 1 : node.scrollWidth > node.clientWidth);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [text, lines]);

  useEffect(() => {
    const node = ref.current;
    if (!node || !cut) return;
    const host = (node.parentElement?.closest(FOCUSABLE_ANCESTOR) as HTMLElement | null) ?? node;
    let pointerDown = false;
    let focused = false;
    const onPointerDown = () => {
      pointerDown = true;
      document.addEventListener("pointerup", () => (pointerDown = false), { once: true });
    };
    // One frame later, as in `Tip`: focusing a row below the fold scrolls the list, and
    // Radix closes a tooltip whose trigger's ancestor scrolls.
    const onFocus = () => {
      if (pointerDown) return;
      if (host !== node) {
        const cutLines = Array.from(host.querySelectorAll<HTMLElement>("[data-cut-text]"));
        if (cutLines[0] !== node) return;
        setHostLines(cutLines.map((line) => line.textContent ?? ""));
      }
      focused = true;
      requestAnimationFrame(() => {
        if (focused) setOpen(true);
      });
    };
    const onBlur = () => {
      focused = false;
      setOpen(false);
      setHostLines(null);
    };
    host.addEventListener("pointerdown", onPointerDown);
    host.addEventListener("focus", onFocus);
    host.addEventListener("blur", onBlur);
    return () => {
      host.removeEventListener("pointerdown", onPointerDown);
      host.removeEventListener("focus", onFocus);
      host.removeEventListener("blur", onBlur);
    };
  }, [cut]);

  const ownStop = cut && !inFocusable;
  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip
        open={cut && open}
        onOpenChange={(next) => {
          setOpen(cut && next);
          if (!next) setHostLines(null);
        }}
      >
        <TooltipTrigger asChild onFocus={(event) => event.preventDefault()}>
          <span
            ref={ref}
            tabIndex={ownStop ? 0 : undefined}
            data-cut-text={cut ? "" : undefined}
            className={cn("block min-w-0", lines ? LINE_CLAMP[lines] : "truncate", ownStop && TIP_WRAP_FOCUS_RING, className)}
          >
            {text}
          </span>
        </TooltipTrigger>
        <TooltipContent side="top" align="center" collisionPadding={8}>
          {hostLines && hostLines.length > 1
            ? hostLines.map((line, index) => (
                <span key={index}>
                  {index > 0 && <br />}
                  {capTipText(line)}
                </span>
              ))
            : capTipText(text)}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
