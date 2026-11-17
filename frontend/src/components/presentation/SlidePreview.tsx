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
 * One slide, scaled to whatever box it is given.
 *
 * The speaker view's "current" and "next" panes and the notes handout's
 * thumbnails all need a slide drawn at a fraction of its size with no reveal
 * runtime. This is `StaticSlide` — the very canvas the export prints — laid out
 * at its full 1920px inside a `.deck-root` that carries the deck's theme, and
 * transformed as a whole onto a box of the scaled size. The FRAME is measured
 * with a `ResizeObserver` (the panes resize with the window) and the box sized
 * from it: to the frame's width (`fit="width"`, the height follows the format),
 * or to fit inside the frame's box (`fit="contain"`, letterboxed and centred —
 * the "current slide" pane, whose height is capped so the notes keep the floor).
 *
 * Rendering the real slide (not a screenshot) keeps the preview honest: an
 * in-place edit on the stage shows up here on the next poll, at any size, with
 * the same fonts and the same theme tokens.
 *
 * Memoised: the speaker view re-renders four times a second for its stopwatch,
 * and a slide (a table, a diagram, a chart) is the most expensive thing on it.
 * Its props only change on a move, a poll that brought new content, or a
 * resize, so the tick costs it nothing.
 */
import { memo, useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";

import { cn } from "@/lib/utils";
import type { Presentation, PresentationSlide } from "@/types";

import { deckAspectRatio, deckFormat, deckSize } from "./geometry";
import { StaticSlide } from "./StaticSlide";
import { themeConfigFor, themeStyle } from "./theme";

export const SlidePreview = memo(function SlidePreview({
  presentation,
  slide,
  index,
  fit = "width",
  overlay,
  className,
}: {
  presentation: Presentation;
  slide: PresentationSlide;
  index: number;
  fit?: "width" | "contain";
  /** Drawn over the slide, inside its box (the blackout notice). */
  overlay?: ReactNode;
  className?: string;
}) {
  const frameRef = useRef<HTMLDivElement | null>(null);
  const [frame, setFrame] = useState({ width: 0, height: 0 });

  useEffect(() => {
    const el = frameRef.current;
    if (!el) return;
    const measure = () => setFrame({ width: el.clientWidth, height: el.clientHeight });
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const { spec } = presentation;
  const canvas = deckSize(spec.aspect_ratio);
  const scale =
    fit === "contain"
      ? Math.min(frame.width / canvas.width, frame.height / canvas.height)
      : frame.width / canvas.width;
  const ready = Number.isFinite(scale) && scale > 0;
  const style = {
    ...themeStyle(themeConfigFor(presentation, spec.theme)),
    "--preview-scale": ready ? scale : 0,
    width: ready ? `${canvas.width * scale}px` : "100%",
    height: ready ? `${canvas.height * scale}px` : undefined,
  } as CSSProperties;

  return (
    <div ref={frameRef} className={cn("slide-preview-frame", className)} data-fit={fit}>
      <div
        className="deck-root slide-preview"
        data-deck-theme={spec.theme}
        data-deck-format={deckFormat(spec.aspect_ratio)}
        data-deck-preview="true"
        data-aspect={deckAspectRatio(spec.aspect_ratio)}
        style={style}
      >
        {/* Nothing is laid out before the first measurement: a 1920px canvas
            at scale 0 is invisible, and a flash at full size would be worse. */}
        {ready && <StaticSlide slide={slide} index={index} presentation={presentation} />}
        {overlay}
      </div>
    </div>
  );
});
