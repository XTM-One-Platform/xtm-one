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
 * Static render page behind the PDF / PPTX export — `/presentations/:id/static`.
 * Not a user-facing page.
 *
 * The platform's headless Chromium (`backend/app/services/presentation_export.py`)
 * loads this route with a **render token** in the query string and nothing
 * else: no session, no cookies. Every slide is laid out here as one UNSCALED
 * canvas (1920x1080, or 1440x1080 for 4:3), one under the other, with no
 * reveal.js runtime, no transitions and no controls — the same components, the
 * same stylesheet and the same theme variables as the on-screen deck, so what
 * gets printed, screenshotted and measured is exactly what the viewer shows.
 *
 * Protocol with the exporter (all on `document.documentElement.dataset`):
 *   - `deckReady = "true"` once the spec is loaded AND the fonts are ready AND
 *     every image has finished loading (or failed) AND no `[data-deck-pending]`
 *     element is left (icon catalog chunk, Mermaid diagrams);
 *   - `deckError = <message>` when the deck cannot be rendered at all, or when
 *     a figure could not be drawn (`[data-deck-unfinished]`): the
 *     export fails at once instead of shipping the figure's error state.
 * The `.deck-root` carries `data-deck-revision`, the revision of the spec the
 * slides were rendered from: this page loads the deck's CURRENT row, so
 * the layout check compares it with the revision it was asked about rather
 * than reading a collaborator's newer deck against its own titles.
 * Each slide is `.deck-static-slide` — what `page.pdf()` paginates on, what
 * the slide screenshots target and what the DOM walk measures.
 *
 * Assets: the page cannot send an `Authorization` header from `<img src>`, so
 * `DeckAssetProvider` resolves every file id to the token-gated
 * `render-asset` route, which only serves files the deck actually references.
 *
 * The PDF a public link offers is rendered from the same page at
 * `/public/presentations/:token/static`: there the deck and its pictures are
 * read through the PUBLIC routes with the link's token - exactly what a
 * visitor of the link sees, no speaker notes and no account's visibility.
 */
import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";

import { DeckAssetProvider } from "@/components/presentation/asset-context";
import { deckFormat, pdfPageSize } from "@/components/presentation/geometry";
import { DeckPollsContext, type DeckPollsValue, specHasPoll } from "@/components/presentation/poll-context";
import { deckSettled } from "@/components/presentation/ready";
import { SlideLinkContext, slideAnchorId, useSlideLinksValue } from "@/components/presentation/slide-link-context";
import { StaticSlide } from "@/components/presentation/StaticSlide";
import { themeConfigFor, themeStyle } from "@/components/presentation/theme";
import { API_BASE } from "@/lib/api";
import { publicAssetUrl, publicDeckAsPresentation } from "@/lib/public-deck";
import type { DeckPollsResponse, Presentation, PresentationSlide, PublicPresentation } from "@/types";
import "@/components/presentation/presentation.css";

/** A slide link on the export page is an anchor to its slide's canvas: the PDF keeps it as an internal link. */
function slideAnchorHref(index: number): string {
  return `#${slideAnchorId(index)}`;
}

function StaticSlideLinks({ slides, children }: { slides: PresentationSlide[]; children: ReactNode }) {
  const value = useSlideLinksValue(slides, undefined, slideAnchorHref);
  return <SlideLinkContext.Provider value={value}>{children}</SlideLinkContext.Provider>;
}

const RENDER_TOKEN_PARAM = "render_token";
/** What the exporter reports when a figure's code could not load or its drawing failed (`[data-deck-unfinished]`). */
const UNDRAWN_FIGURE_ERROR = "A chart, diagram or icon of the deck could not be drawn: its code did not load, or its drawing failed.";

type LoadState =
  | { status: "loading" }
  | { status: "ready"; presentation: Presentation; polls: DeckPollsResponse | undefined }
  | { status: "error"; message: string };

export default function PresentationStaticRender() {
  const { id, token: publicToken } = useParams<{ id: string; token: string }>();
  const [searchParams] = useSearchParams();
  const token = searchParams.get(RENDER_TOKEN_PARAM) ?? "";
  const [state, setState] = useState<LoadState>({ status: "loading" });

  // Plain fetch on purpose: `api.ts` attaches the session JWT (there is none
  // here) and redirects to the login page on 401, which would hang the export.
  useEffect(() => {
    if (!publicToken && (!id || !token)) {
      setState({ status: "error", message: "Missing presentation id or render token." });
      return;
    }
    let cancelled = false;
    const url = publicToken
      ? `${API_BASE}/public/presentations/${encodeURIComponent(publicToken)}`
      : `${API_BASE}/presentations/${encodeURIComponent(id ?? "")}/render-spec?${RENDER_TOKEN_PARAM}=${encodeURIComponent(token)}`;
    fetch(url, { credentials: "omit" })
      .then(async (res) => {
        if (!res.ok) throw new Error(`The deck could not be loaded (HTTP ${res.status}).`);
        const body: unknown = await res.json();
        const presentation = publicToken ? publicDeckAsPresentation(body as PublicPresentation) : (body as Presentation);
        // Live polls (#10157): the export draws a snapshot of their link, QR and
        // results. Only for the token-gated export of a deck holding a poll; a
        // public link shows no results. A failed read draws the block without
        // results rather than failing the export.
        let polls: DeckPollsResponse | undefined;
        if (!publicToken && specHasPoll(presentation.spec.slides)) {
          const pollsUrl = `${API_BASE}/presentations/${encodeURIComponent(id ?? "")}/render-polls?${RENDER_TOKEN_PARAM}=${encodeURIComponent(token)}`;
          polls = await fetch(pollsUrl, { credentials: "omit" })
            .then((pollsRes) => (pollsRes.ok ? (pollsRes.json() as Promise<DeckPollsResponse>) : undefined))
            .catch(() => undefined);
        }
        return { presentation, polls };
      })
      .then(({ presentation, polls }) => {
        if (!cancelled) setState({ status: "ready", presentation, polls });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setState({
          status: "error",
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => {
      cancelled = true;
    };
  }, [id, publicToken, token]);

  // Signal failure to the exporter. Cleared on unmount so a hot-reload in dev
  // does not leave a stale flag behind.
  useEffect(() => {
    if (state.status !== "error") return;
    document.documentElement.dataset.deckError = state.message;
    return () => {
      delete document.documentElement.dataset.deckError;
    };
  }, [state]);

  const resolver = useMemo(
    () =>
      publicToken
        ? (fileId: string) => publicAssetUrl(API_BASE, publicToken, fileId)
        : id && token
          ? (fileId: string) =>
              `${API_BASE}/presentations/${encodeURIComponent(id)}/render-asset/${encodeURIComponent(fileId)}?${RENDER_TOKEN_PARAM}=${encodeURIComponent(token)}`
          : null,
    [id, publicToken, token],
  );

  // Ready signal: fonts, images and lazy chunks (icons, diagrams) — everything
  // that arrives asynchronously after React has committed the tree. No figure
  // gives up here, so an unfinished one is one that could not be
  // drawn: the render fails on it rather than exporting its error state.
  const signalReady = useCallback((root: HTMLDivElement | null) => {
    if (!root) return;
    let cancelled = false;
    let failed = false;
    void deckSettled(root).then(({ complete }) => {
      if (cancelled) return;
      if (complete) {
        document.documentElement.dataset.deckReady = "true";
      } else {
        failed = true;
        document.documentElement.dataset.deckError = UNDRAWN_FIGURE_ERROR;
      }
    });
    return () => {
      cancelled = true;
      if (failed) delete document.documentElement.dataset.deckError;
    };
  }, []);

  if (state.status !== "ready") {
    // Nothing to paint: the exporter reads `deckError` (set above) or times out.
    return null;
  }

  const { presentation, polls } = state;
  const pollsValue: DeckPollsValue | null = polls ? { presentationId: presentation.id, data: polls } : null;
  return (
    <DeckPollsContext.Provider value={pollsValue}>
      <DeckAssetProvider value={resolver}>
        {/*
          The PDF paper. `@page` cannot be scoped by a selector, depends on the
          deck's aspect ratio (runtime data) and must sort AFTER every bundled
          stylesheet — the report styles ship a global `@page { size: A4 }`, and
          Chromium keeps a document's `@page` orientation even when `page.pdf()`
          is handed an explicit landscape paper — so it is emitted here, in the
          body, rather than in `presentation.css`. Values come from a fixed map.
        */}
        <style>{`@page { size: ${pdfPageSize(presentation.spec.aspect_ratio)}; margin: 0; }`}</style>
        <div
          ref={signalReady}
          className="deck-root"
          data-deck-theme={presentation.spec.theme}
          data-deck-format={deckFormat(presentation.spec.aspect_ratio)}
          data-deck-static="true"
          data-deck-revision={presentation.revision}
          style={themeStyle(themeConfigFor(presentation, presentation.spec.theme))}
        >
          <StaticSlideLinks slides={presentation.spec.slides}>
            {presentation.spec.slides.map((slide, index) => (
              <StaticSlide key={slide.id ?? `slide-${index}`} slide={slide} index={index} presentation={presentation} />
            ))}
          </StaticSlideLinks>
        </div>
      </DeckAssetProvider>
    </DeckPollsContext.Provider>
  );
}
