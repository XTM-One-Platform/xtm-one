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
 * A slide of the DRAFT drawn small, for the editor's filmstrip.
 *
 * `SlidePreview` (the export's own canvas, scaled) inside a lazy mount: a
 * forty-slide deck must not lay forty canvases out for a panel that shows
 * eight, so a thumbnail renders once it has been near the viewport and stays.
 * Memoised on what can change ITS pixels - the slide object (copy-on-write, so
 * an edit elsewhere keeps its identity), the deck's theme / chrome / template
 * and the count (the slide number) - so a keystroke in one slide's title
 * re-renders one thumbnail, not the strip.
 */
import { memo, useEffect, useMemo, useRef, useState } from "react";

import { cn } from "@/lib/utils";
import type { Presentation } from "@/types";

import type { DeckDraft } from "./presentation-editor";
import { SlidePreview } from "./SlidePreview";
import "./speaker.css";

/**
 * The design the DRAFT resolves to - the palette per scheme, the template's
 * footer pattern and name - as the panel reads them off the template it is
 * trying; the stored deck's own values would draw a filmstrip in the
 * previous template's colours while the stage already wears the new one.
 */
export type ThumbnailDesign = Partial<
  Pick<
    Presentation,
    "theme_config" | "theme_configs" | "template_chrome" | "template_design" | "template_name" | "template_container_id"
  >
>;

function ThumbnailInner({
  presentation,
  draft,
  design,
  index,
  className,
  onSelect,
}: {
  presentation: Presentation;
  draft: DeckDraft;
  /** A MEMOISED object (the memo compares it): the draft's resolved design. */
  design?: ThumbnailDesign;
  index: number;
  className?: string;
  /** A STABLE callback (the memo compares it): the row's index is passed back. */
  onSelect?: (index: number) => void;
}) {
  const frameRef = useRef<HTMLDivElement | null>(null);
  const [near, setNear] = useState(false);
  useEffect(() => {
    const el = frameRef.current;
    if (!el) return;
    if (typeof IntersectionObserver === "undefined") {
      setNear(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setNear(true);
          observer.disconnect();
        }
      },
      { rootMargin: "240px" },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // The deck as the draft would save it: the preview reads the theme, the
  // chrome and the slide count off it (the footer, the number, the palette).
  const shown = useMemo<Presentation>(
    () => ({
      ...presentation,
      ...(design ?? {}),
      spec: {
        ...presentation.spec,
        title: draft.title || presentation.spec.title,
        theme: draft.theme,
        chrome: draft.chrome,
        slides: draft.slides,
      },
    }),
    [presentation, design, draft.title, draft.theme, draft.chrome, draft.slides],
  );
  const slide = draft.slides[index];
  const aspect = presentation.spec.aspect_ratio;
  const ratio = aspect === "4:3" ? "4 / 3" : aspect === "16:9" ? "16 / 9" : aspect === "A4" ? "210 / 297" : "8.5 / 11";

  return (
    <div
      ref={frameRef}
      className={cn("relative overflow-hidden rounded-[4px] bg-elevation-default", className)}
      style={{ aspectRatio: ratio }}
      onClick={onSelect ? () => onSelect(index) : undefined}
      aria-hidden="true"
    >
      {near && slide ? (
        // The preview must never take the click meant for the row (a chart
        // legend inside it is interactive): pointer events are the frame's.
        <div className="pointer-events-none absolute inset-0">
          <SlidePreview presentation={shown} slide={slide} index={index} fit="width" />
        </div>
      ) : null}
    </div>
  );
}

export const SlideThumbnail = memo(
  ThumbnailInner,
  (previous, next) =>
    previous.index === next.index &&
    previous.className === next.className &&
    previous.onSelect === next.onSelect &&
    previous.presentation === next.presentation &&
    previous.design === next.design &&
    previous.draft.slides[previous.index] === next.draft.slides[next.index] &&
    previous.draft.slides.length === next.draft.slides.length &&
    previous.draft.theme === next.draft.theme &&
    previous.draft.chrome === next.draft.chrome &&
    previous.draft.title === next.draft.title &&
    previous.draft.template_container_id === next.draft.template_container_id,
);
