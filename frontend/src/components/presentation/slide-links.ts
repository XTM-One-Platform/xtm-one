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
 * Slide links: `/presentations/<deck id>#/<slide ref>`.
 *
 * The deck tools name every slide they wrote by this link (`slide_link()` in
 * `services/builtin_tools/presentation.py`), so the agent can put it in its
 * reply — "change applied, [slide 4](/presentations/<id>#/<slide id>)" — and
 * the user lands on the change instead of hunting for it. The fragment is the
 * slide's STABLE id (the one `edit_presentation` addresses slides by), so the
 * link keeps pointing at the same slide after a reorder; reveal's own 0-based
 * `#/<n>` form, which the viewer writes in its address bar, stays valid.
 *
 * Wherever the deck is already on screen — the studio stage, the inline card in
 * a transcript — the link is followed IN PLACE through
 * `PresentationCardContext.navigateToSlide`. Anywhere else it is an ordinary
 * link into the viewer, whose hash parser reads the same fragment.
 *
 * Plain module (no React) so the chat Markdown renderer can import it without
 * dragging the deck runtime into the eagerly loaded chat chunk.
 */
// Relative on purpose: `yarn test:unit` runs this module under Node with no
// path alias, like every other pure presentation module with a `.test.ts`.
import { BASE_PATH, stripBasePathFrom } from "../../lib/base-path.ts";
import type { PresentationSpec } from "../../types/index.ts";

const UUID_PATTERN = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";

/** The viewer route, and only it: `/presentations/<id>/speaker` is not a stage. */
const VIEWER_PATH_RE = new RegExp(`^/presentations/(${UUID_PATTERN})/?$`, "i");

/**
 * `#/<ref>` optionally followed by reveal's vertical index (`#/3/1` -> `3`);
 * a bare `#/` is reveal's first slide. The ref grammar mirrors
 * `SLIDE_ID_PATTERN` in `presentation_spec.py`.
 */
const SLIDE_FRAGMENT_RE = /^#\/([A-Za-z0-9_-]{1,64})?(?:\/|$)/;

interface PresentationLink {
  /** Canonical lowercase deck id. */
  presentationId: string;
  /** The slide the link points at, `null` for a link to the deck itself. */
  slideRef: string | null;
}

/**
 * Read a slide ref out of a URL fragment: `#/<ref>` -> `<ref>`, `#/` -> `"0"`,
 * anything else -> `null`.
 */
export function parseSlideFragment(hash: string): string | null {
  const match = SLIDE_FRAGMENT_RE.exec(hash);
  if (!match) return null;
  return match[1] ?? "0";
}

/**
 * Recognise a link into the viewer — relative (`/presentations/<id>#/<ref>`,
 * as the tools write it) or absolute on `origin` (a model that prepended the
 * host, a link copied from the address bar - which carries the URL path prefix
 * the platform is served under). A link to another host, another route
 * or a sub-page of the viewer is not a slide link and returns `null`.
 *
 * Two readings of the pathname, in this order: the ROUTE form first (the
 * pathname as written, which is what the tools write and the viewer routes
 * under `basename`), then the pathname with `basePath` removed (the browser
 * form, `/xtm-one/presentations/<id>`). The order matters when the prefix is
 * itself `/presentations` (accepted once `BASE_PATH_MODE` is declared):
 * stripping first would read the tool's `/presentations/<id>` as
 * `/<id>` and lose in-place navigation, while the copied browser URL
 * `/presentations/presentations/<id>` still resolves through the second
 * reading. Neither reading guesses with the non-idempotent `withBasePath`.
 */
export function parsePresentationLinkFrom(
  basePath: string,
  origin: string,
  href: string | null | undefined,
): PresentationLink | null {
  if (!href) return null;
  let url: URL;
  try {
    url = new URL(href, origin);
  } catch {
    return null;
  }
  if (url.origin !== origin) return null;
  const path =
    VIEWER_PATH_RE.exec(url.pathname) ?? VIEWER_PATH_RE.exec(stripBasePathFrom(basePath, url.pathname));
  if (!path) return null;
  return { presentationId: path[1].toLowerCase(), slideRef: parseSlideFragment(url.hash) };
}

/** {@link parsePresentationLinkFrom} against the page's own prefix and origin. */
export function parsePresentationLink(href: string | null | undefined): PresentationLink | null {
  return parsePresentationLinkFrom(BASE_PATH, window.location.origin, href);
}

/**
 * The 0-based index a slide ref names in `spec`: the slide carrying that STABLE
 * id first, then — for an all-digit ref — reveal's 0-based index. Ids are tried
 * first because a slide id may itself be all digits. A numeric ref past the end
 * lands on the last slide, as reveal clamps it; `null` when nothing matches.
 */
export function resolveSlideRef(
  spec: Pick<PresentationSpec, "slides"> | null | undefined,
  ref: string,
): number | null {
  const slides = spec?.slides ?? [];
  const byId = slides.findIndex((slide) => slide.id === ref);
  if (byId !== -1) return byId;
  if (!/^\d+$/.test(ref)) return null;
  const index = Number.parseInt(ref, 10);
  if (slides.length === 0) return index;
  return Math.min(index, slides.length - 1);
}
