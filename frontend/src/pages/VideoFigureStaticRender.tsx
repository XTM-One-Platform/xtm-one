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
 * The page a video's figure scene is drawn from - `/videos/figures/static`.
 * Not a user-facing page.
 *
 * The render's headless browser (`backend/app/services/video_studio/figures.py`)
 * loads it and answers its two requests itself, never the API: `__figure__/
 * spec.json` - a one-slide deck holding the figure's block, on the video
 * template's palette - and `__figure__/asset/<file id>` for the pictures the
 * block draws. The slide is the deck's own `StaticSlide`, so a figure looks
 * exactly like the block on a slide; everything but the block is hidden
 * (`.deck-figure-render` in `presentation.css`).
 *
 * Protocol with the render (on `document.documentElement.dataset`, like the
 * deck's static page): `figureReady = "true"` once the deck has settled and
 * the build is installed as `window.__xtmFigure` (`figure-build.ts`), or
 * `figureError = <message>`.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { DeckAssetProvider } from "@/components/presentation/asset-context";
import { installFigure, type FigureBuild } from "@/components/presentation/figure-build";
import { deckFormat } from "@/components/presentation/geometry";
import { deckSettled } from "@/components/presentation/ready";
import { StaticSlide } from "@/components/presentation/StaticSlide";
import { themeConfigFor, themeStyle } from "@/components/presentation/theme";
import type { Presentation } from "@/types";
import "@/components/presentation/presentation.css";

interface FigureDocument {
  presentation: Presentation;
  /** The block's type, or `roadmap` for a roadmap slide's phases: which parts build in. */
  kind: string;
}

type LoadState = { status: "loading" } | { status: "ready"; figure: FigureDocument } | { status: "error"; message: string };

/** Where the figure's block sits on its slide. */
function figureRoot(canvas: Element, kind: string): Element | null {
  return kind === "roadmap" ? canvas.querySelector(".deck-roadmap") : canvas.querySelector(".deck-blocks > *");
}

const figureUrl = (path: string) => new URL(`__figure__/${path}`, window.location.href).toString();

export default function VideoFigureStaticRender() {
  const [state, setState] = useState<LoadState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    fetch(figureUrl("spec.json"), { credentials: "omit" })
      .then(async (res) => {
        if (!res.ok) throw new Error(`The figure could not be loaded (HTTP ${res.status}).`);
        return (await res.json()) as FigureDocument;
      })
      .then((figure) => {
        if (!cancelled) setState({ status: "ready", figure });
      })
      .catch((error: unknown) => {
        if (!cancelled) setState({ status: "error", message: error instanceof Error ? error.message : String(error) });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (state.status !== "error") return;
    document.documentElement.dataset.figureError = state.message;
    return () => {
      delete document.documentElement.dataset.figureError;
    };
  }, [state]);

  const resolver = useMemo(() => (fileId: string) => figureUrl(`asset/${encodeURIComponent(fileId)}`), []);
  const figure = state.status === "ready" ? state.figure : null;

  const signalReady = useCallback(
    (root: HTMLDivElement | null) => {
      if (!root || !figure) return;
      let cancelled = false;
      void deckSettled(root).then(({ complete }) => {
        if (cancelled) return;
        const canvas = root.querySelector<HTMLElement>(".deck-static-slide");
        const target = canvas ? figureRoot(canvas, figure.kind) : null;
        if (!complete || !canvas || !target) {
          document.documentElement.dataset.figureError = complete
            ? "The figure's block did not render."
            : "The figure could not be drawn: its code did not load, or its drawing failed.";
          return;
        }
        target.setAttribute("data-figure-root", "");
        (window as Window & { __xtmFigure?: FigureBuild }).__xtmFigure = installFigure(target, canvas, figure.kind);
        document.documentElement.dataset.figureReady = "true";
      });
      return () => {
        cancelled = true;
      };
    },
    [figure],
  );

  if (!figure) return null;
  const { presentation } = figure;
  return (
    <DeckAssetProvider value={resolver}>
      <div
        ref={signalReady}
        className="deck-root deck-figure-render"
        data-deck-theme={presentation.spec.theme}
        data-deck-format={deckFormat(presentation.spec.aspect_ratio)}
        data-deck-static="true"
        style={themeStyle(themeConfigFor(presentation, presentation.spec.theme))}
      >
        <StaticSlide slide={presentation.spec.slides[0]} index={0} presentation={presentation} />
      </div>
    </DeckAssetProvider>
  );
}
