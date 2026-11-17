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
 * Mermaid diagram block — loaded lazily, rendered in strict mode.
 *
 * `mermaid` is ~2 MB, so it is only imported when a deck actually contains a
 * `diagram` block (this module is `React.lazy`-loaded from `media.tsx`). The
 * source was validated server-side (no `%%{init}%%` directives, no `click`
 * handlers, no script tags — `check_diagram_source`); this layer adds the
 * runtime half: `securityLevel: "strict"` (labels are text, no HTML, no click),
 * SVG-only labels so the output has no `<foreignObject>`, and the deck theme
 * derived from the deck's own CSS variables (`diagram-theme.ts`) so a
 * diagram inherits the brand palette, fonts and geometry.
 *
 * The rendered SVG is inserted with `dangerouslySetInnerHTML` — the ONLY place
 * in the deck renderer that does so (decks carry no `rich_text`). It is
 * Mermaid's own output (not agent text), produced from a source that passed
 * both checks, and additionally passed through DOMPurify's SVG profile.
 */
import DOMPurify from "dompurify";
import { type CSSProperties, useEffect, useId, useRef, useState } from "react";

import type { DeckDiagramBlock } from "@/types";

import { useGiveUp } from "../figure-wait";
import { InlineMarkup } from "../inline-markup";
import { DiagramError, DiagramPlaceholder } from "./diagram-states";
import { type DeckDiagramGeometry, MERMAID_CONFIG, deckDiagramTheme, fitDeckGeometry } from "./diagram-theme";

type MermaidModule = typeof import("mermaid").default;

let mermaidPromise: Promise<MermaidModule> | null = null;

function loadMermaid(): Promise<MermaidModule> {
  mermaidPromise ??= import("mermaid").then((mod) => {
    const mermaid = mod.default;
    mermaid.initialize(MERMAID_CONFIG);
    return mermaid;
  });
  return mermaidPromise;
}

const SVG_SANITIZE = {
  USE_PROFILES: { svg: true, svgFilters: true },
  ADD_TAGS: ["style"],
  FORBID_TAGS: ["script", "foreignObject", "iframe", "object", "embed"],
  FORBID_ATTR: ["onload", "onclick", "onerror", "onmouseover", "href", "xlink:href"],
};

type FittedSvg = {
  svg: string;
  /** viewBox width / height, or null when the drawing has no viewBox. */
  ratio: number | null;
  /** viewBox width in Mermaid's own pixels — the drawing's natural size. */
  naturalWidth: number | null;
};

/**
 * Sanitizes Mermaid's output, draws the deck's geometry on it and frees it
 * from its natural pixel size.
 *
 * Mermaid emits `width="100%" style="max-width: <natural>px"` (`useMaxWidth`),
 * so a drawing scales DOWN into a narrow column but never UP: a seven-node
 * flowchart is ~1100px wide on the 1920px canvas and prints as a thumbnail.
 * Dropping that cap lets `presentation.css` fit the drawing into the block's
 * size bucket from its viewBox ratio instead (`.deck-diagram[data-sized]`),
 * with a bounded upscale so a three-node sketch does not become a billboard.
 * The same idea as `MeasuredImage` for raster/SVG assets.
 */
function sanitizeAndFit(raw: string, geometry: DeckDiagramGeometry): FittedSvg {
  const body = DOMPurify.sanitize(raw, { ...SVG_SANITIZE, RETURN_DOM: true });
  const root = body instanceof Element ? body.querySelector("svg") : null;
  if (!root) return { svg: "", ratio: null, naturalWidth: null };
  fitDeckGeometry(root, geometry);
  const viewBox = (root.getAttribute("viewBox") ?? "").trim().split(/[\s,]+/).map(Number);
  const [, , vbWidth, vbHeight] = viewBox;
  const sized = viewBox.length === 4 && vbWidth > 0 && vbHeight > 0;
  if (sized) {
    // Only a drawing with a viewBox keeps its proportions once the explicit
    // size is gone; one without is left exactly as Mermaid sized it.
    root.removeAttribute("width");
    root.removeAttribute("height");
    root.style.removeProperty("max-width");
  }
  return {
    svg: root.outerHTML,
    ratio: sized ? vbWidth / vbHeight : null,
    naturalWidth: sized ? vbWidth : null,
  };
}

type RenderState =
  | { status: "pending" }
  | ({ status: "ready" } & FittedSvg)
  /** `unloaded`: Mermaid itself could not load - the figure is unfinished, not the source wrong. */
  | { status: "error"; message: string; unloaded: boolean };

export default function Diagram({
  block,
  editAttrs,
}: {
  block: DeckDiagramBlock;
  /** The block's `data-edit-block` in edit mode, read by the lazy host. */
  editAttrs?: Record<string, string | undefined>;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const reactId = useId();
  const [state, setState] = useState<RenderState>({ status: "pending" });
  // Bumped when the theme this diagram baked into its SVG is no longer the
  // deck's — see the observer below.
  const [paletteVersion, setPaletteVersion] = useState(0);
  const paletteRef = useRef("");

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    let cancelled = false;
    // Mermaid ids must be valid CSS identifiers; React's useId contains colons.
    const renderId = `deck-diagram-${reactId.replace(/[^a-zA-Z0-9_-]/g, "")}`;
    // Read the theme now, at commit, so the observer compares against what
    // THIS render is about to bake in — not against the previous render's.
    const theme = deckDiagramTheme(host);
    paletteRef.current = JSON.stringify(theme);
    // A re-render (new source, new palette) keeps the previous drawing on
    // screen until the new one is ready rather than blinking through the
    // placeholder; the first render still marks itself pending, which is what
    // the export capture waits on.
    setState((previous) => (previous.status === "ready" ? previous : { status: "pending" }));
    let loaded = false;
    loadMermaid()
      .then(async (mermaid) => {
        loaded = true;
        // `initialize` REPLACES the site configuration: the whole of it, every render.
        mermaid.initialize({ ...MERMAID_CONFIG, themeVariables: theme.themeVariables, themeCSS: theme.themeCSS });
        const { svg } = await mermaid.render(renderId, block.source);
        if (cancelled) return;
        setState({ status: "ready", ...sanitizeAndFit(svg, theme.geometry) });
      })
      .catch((error: unknown) => {
        // A failed render leaves a stray element behind in <body>.
        document.getElementById(`d${renderId}`)?.remove();
        if (cancelled) return;
        setState({
          status: "error",
          message: error instanceof Error ? error.message : String(error),
          unloaded: !loaded,
        });
      });
    return () => {
      cancelled = true;
    };
  }, [block.source, reactId, paletteVersion]);

  // The theme is not a prop: it is read off the deck root's CSS variables at
  // render time and baked into the SVG, so a theme switch on the root (the
  // editor previewing `light` over a dark deck) would leave a diagram
  // in the old colours. Watch the root's theme attribute and inline palette,
  // and re-render only when the variables this diagram consumed actually
  // changed — the same root also carries a page document's zoom variables,
  // which must not cost a Mermaid re-render per scroll-wheel notch.
  useEffect(() => {
    const root = hostRef.current?.closest<HTMLElement>(".deck-root");
    if (!root || typeof MutationObserver === "undefined") return;
    const observer = new MutationObserver(() => {
      const host = hostRef.current;
      if (!host) return;
      if (JSON.stringify(deckDiagramTheme(host)) !== paletteRef.current) {
        setPaletteVersion((version) => version + 1);
      }
    });
    observer.observe(root, { attributes: true, attributeFilter: ["data-deck-theme", "style"] });
    // The slide root: a slide may switch to a scheme of its own, which
    // lands on the section / static canvas, not on the deck root.
    const slideRoot = hostRef.current?.closest<HTMLElement>("[data-scheme]");
    if (slideRoot && slideRoot !== root) {
      observer.observe(slideRoot, { attributes: true, attributeFilter: ["data-scheme", "style"] });
    }
    return () => observer.disconnect();
  }, []);

  // Mermaid's load and render report nothing while they run: one that has not
  // answered within `FIGURE_WAIT_MS` leaves the placeholder standing and lets
  // go of the ready signal. A late answer still draws. Mermaid that
  // could not load is unfinished, like this module's own chunk (`media.tsx`);
  // a source Mermaid refuses is the deck's content, drawn as its error.
  const pending = state.status === "pending";
  const gaveUp = useGiveUp(hostRef, pending ? `${paletteVersion}:${block.source}` : null);
  const unfinished = (pending && gaveUp) || (state.status === "error" && state.unloaded);

  const sized = state.status === "ready" && state.ratio !== null && state.naturalWidth !== null;
  // The one inline style besides the theme variables: the drawing's proportions
  // are known only once Mermaid has laid it out, so they cannot be a class
  // (same exception as `MeasuredImage`).
  const style = sized
    ? ({
        "--deck-diagram-ratio": String(state.ratio),
        "--deck-diagram-natural-w": `${state.naturalWidth}px`,
      } as CSSProperties)
    : undefined;

  return (
    <figure
      ref={hostRef}
      className="deck-diagram"
      data-size={block.size ?? "md"}
      data-fill={block.fill ? "true" : undefined}
      data-state={state.status}
      data-sized={sized ? "true" : undefined}
      // The export capture waits for every `[data-deck-pending]` to clear.
      data-deck-pending={pending && !gaveUp ? "true" : undefined}
      data-deck-unfinished={unfinished ? "true" : undefined}
      style={style}
      {...editAttrs}
    >
      {state.status === "ready" ? (
        <div className="deck-diagram-svg" dangerouslySetInnerHTML={{ __html: state.svg }} />
      ) : state.status === "error" ? (
        <DiagramError message={state.message} source={block.source} />
      ) : (
        <DiagramPlaceholder />
      )}
      {block.caption ? (
        <figcaption>
          <InlineMarkup value={block.caption} field="caption" />
        </figcaption>
      ) : null}
    </figure>
  );
}
