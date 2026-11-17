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
 * Slide layout dispatcher for agent-generated decks.
 *
 * One `case` per entry in the backend's `SLIDE_LAYOUTS` catalog
 * (`backend/app/services/presentation_spec.py`). The agent picks a layout and
 * fills its fixed prop set — it never controls the markup — so a deck can be
 * rendered from untrusted-ish structured data without giving away the page.
 * The backend test suite greps this switch for every catalog id; keep each
 * `case "<layout>":` literal on its own line.
 *
 * The v1 layouts (`two_columns`, `comparison`, the v1 `dashboard`, `generic`)
 * A stored slide wearing one is upgraded to a current
 * layout when the row is read, so no slide reaching this switch carries them,
 * and nothing here builds HTML from spec text. The `dashboard` case below is
 * the v2 layout (KPI tiles over a panel grid) - a v1 slide of that
 * name has neither `tiles` nor `panels` and is told apart by shape upstream.
 *
 * Styling lives in `presentation.css`; classes are plain CSS (not Tailwind
 * utilities) because the deck is a self-contained visual surface with its own
 * `--deck-*` token scale, sized in absolute pixels on the 1920x1080 canvas.
 */
import type { ReactNode } from "react";

import type { PresentationSlide } from "@/types";

import { SlideImage } from "../blocks/shared";
import { heroHostsBackgroundImage, slideOverlay } from "../hero";
import { useSlideScheme } from "../theme";
import { AgendaLayout, ContentLayout, RoadmapLayout, TakeawaysLayout } from "./body";
import { DashboardLayout } from "./dashboard";
import { SlideHeader } from "./frame";
import { ClosingLayout, SectionLayout, TitleLayout } from "./opening";

/**
 * A slide whose layout this bundle does not know (a deck stored by a newer
 * backend): its title, and nothing invented under it. Inside a live
 * presentation that is a far better failure mode than a blank deck or a
 * throw, and it never guesses at fields it cannot type.
 */
function UnknownLayout({ slide }: { slide: { title?: unknown } }) {
  const title = typeof slide.title === "string" ? slide.title : undefined;
  return <SlideHeader title={title} />;
}

/**
 * The scrim a slide draws between its background image and its content
 * (`overlay`): a full-canvas layer in the negative z-order of the
 * frame's stacking context, painted in the slide's scheme by the stylesheet.
 * Nothing without an image, nothing when the `split` hero hosts the image in
 * its own panel (which scrims it itself), nothing for `none` - so a slide that
 * never set the field renders the DOM it always did.
 */
function SlideOverlay({ slide }: { slide: PresentationSlide }): ReactNode {
  const overlay = slideOverlay(slide);
  if (overlay === "none" || !slide.background_image_file_id || heroHostsBackgroundImage(slide)) return null;
  return <div className="deck-slide-overlay" data-overlay={overlay} aria-hidden="true" />;
}

/** The body layouts the watermark is drawn on: the covers and the section divider have backdrops of their own. */
const WATERMARK_LAYOUTS: ReadonlySet<string> = new Set(["agenda", "content", "roadmap", "takeaways", "dashboard"]);

/**
 * The template's mark "en filigrane" on a body slide (the template chrome's
 * `watermark`, `chromeWatermark()` in `chrome-pattern.ts`): the mark cropped
 * into the top-right corner at a whisper of opacity, over a soft bloom of the
 * brand gradient from the bottom corners - the cover's watermark and glow,
 * quietened for a slide that has work to show. Read off the slide's own
 * palette (`mark_file_id`), so a light slide inks it and a template with no
 * mark draws nothing.
 *
 * A backdrop like the cover's: the lowest layer of the frame's
 * stacking context, `aria-hidden` (out of the layout audit), and
 * `data-export-backdrop` on the element painting the bloom, so the PPTX
 * captures it as ONE picture with the slide's content hidden and keeps every
 * block editable above it.
 */
function SlideWatermark({ slide }: { slide: PresentationSlide }): ReactNode {
  const resolution = useSlideScheme();
  const fileId = resolution?.tokens.mark_file_id;
  if (!fileId || !WATERMARK_LAYOUTS.has(slide.layout)) return null;
  return (
    <div className="deck-slide-watermark" data-export-backdrop="true" aria-hidden="true">
      <SlideImage fileId={fileId} alt="" className="deck-slide-watermark-mark" fit="contain" />
    </div>
  );
}

/** Render one slide's body by layout; `watermark` is the deck's `chromeWatermark()`. */
export function SlideBody({ slide, watermark = false }: { slide: PresentationSlide; watermark?: boolean }): ReactNode {
  return (
    <>
      {watermark ? <SlideWatermark slide={slide} /> : null}
      <SlideOverlay slide={slide} />
      <SlideLayout slide={slide} />
    </>
  );
}

function SlideLayout({ slide }: { slide: PresentationSlide }): ReactNode {
  switch (slide.layout) {
    case "title":
      return <TitleLayout slide={slide} />;
    case "section":
      return <SectionLayout slide={slide} />;
    case "agenda":
      return <AgendaLayout slide={slide} />;
    case "content":
      return <ContentLayout slide={slide} />;
    case "roadmap":
      return <RoadmapLayout slide={slide} />;
    case "takeaways":
      return <TakeawaysLayout slide={slide} />;
    case "dashboard":
      return <DashboardLayout slide={slide} />;
    case "closing":
      return <ClosingLayout slide={slide} />;
    default:
      return <UnknownLayout slide={slide as { title?: unknown }} />;
  }
}
