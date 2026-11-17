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

/** Which slides carry the deck chrome's footer bar. */
import type { PresentationSlide } from "@/types";

/** Layouts that carry the persistent footer bar (event label, footer text): the
 *  body slides, the `dashboard` included - it reserves the bar's room
 *  like every body slide (the slide number already sits there) and has a
 *  `hide_footer` of its own to opt out. */
const FOOTER_LAYOUTS: ReadonlySet<string> = new Set(["agenda", "content", "roadmap", "takeaways", "dashboard"]);

/** Opening, section and closing slides never show it; body slides can opt out.
 *  A template whose section slide is a body slide (`design.section: "outline"`)
 *  passes `sectionIsBody`: the section then carries the same footer. */
export function slideShowsFooter(slide: PresentationSlide, sectionIsBody = false): boolean {
  if (!FOOTER_LAYOUTS.has(slide.layout) && !(sectionIsBody && slide.layout === "section")) return false;
  return !("hide_footer" in slide && slide.hide_footer);
}
