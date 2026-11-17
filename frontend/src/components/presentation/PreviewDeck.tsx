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
 * A throwaway presentation inside a dialog: the template preview and
 * the generator's candidate preview.
 *
 * Same surface as a real presentation of the same `kind`, so a preview is
 * operated exactly like what it previews, with no title, no sharing and
 * nothing persisted:
 *
 *   - a deck (16:9 / 4:3) gets the stage — compact toolbar with navigation, the
 *     slide counter, fullscreen and the presenter menu. reveal's own on-slide
 *     arrows are off everywhere (`PresentationDeck`), so a bare deck would have
 *     no way to turn the page at all;
 *   - a page (A4 / letter) gets the document reader — the pages stacked as
 *     sheets on a scrolling surface with fit-to-width zoom, because a template
 *     built for briefs previews as the document its decks will be, not as a
 *     slide show of portrait slides.
 *
 * Either owns the keys from the moment it appears (`autoEngage` /
 * `keyScope="document"`): inside a modal dialog nothing else competes for the
 * arrows, and asking for a click first would make the preview feel dead on
 * open.
 *
 * Code-split like the chat card: the Templates page must not pay for reveal
 * until someone actually opens a preview.
 */
import { Suspense, lazy, type ReactNode } from "react";

import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { Presentation } from "@/types";

import { type DeckController, useDeckController } from "./deck-controller";
import { deckAspectRatio } from "./geometry";
import { usePageController } from "./page-controller";
import { Spinner } from "@filigran/design-system";

const DeckStage = lazy(() => import("./DeckStage").then((mod) => ({ default: mod.DeckStage })));
const PageDocument = lazy(() =>
  import("./PageDocument").then((mod) => ({ default: mod.PageDocument })),
);

/** A portrait preview is height-capped so the dialog stays on screen. */
const PAGE_SURFACE_CLASS = "max-h-[70vh] min-h-[320px]";

export function PreviewDeck({
  presentation,
  controller,
  leading,
  className,
}: {
  presentation: Presentation;
  /** Pass the host dialog's own controller when it needs the deck's state
   *  (see `previewEscapeGuard` in `deck-keys.ts`); otherwise the preview keeps
   *  one for itself. A page has no overview and no fullscreen, so the guard
   *  never holds the dialog open for it. */
  controller?: DeckController;
  /** Left region of the toolbar (a caption such as "8 layouts previewed"). */
  leading?: ReactNode;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  const own = useDeckController();
  const active = controller ?? own;
  const pageController = usePageController();
  const aspect = deckAspectRatio(presentation.spec.aspect_ratio);
  const frame = cn("overflow-hidden rounded-sm border border-elevation-subtle bg-elevation-default", className);

  if (presentation.kind === "page") {
    return (
      <div className={frame}>
        <Suspense
          fallback={
            <div className={cn("flex w-full items-center justify-center", PAGE_SURFACE_CLASS)}>
              <Spinner size="md" tone="inherit" className="text-accent" label={t_i18n("Loading...")} />
            </div>
          }
        >
          <PageDocument
            presentation={presentation}
            controller={pageController}
            compact
            keyScope="document"
            leading={leading}
            surfaceClassName={PAGE_SURFACE_CLASS}
          />
        </Suspense>
      </div>
    );
  }

  // Match the deck's own geometry so the preview is not letterboxed inside a
  // box of the wrong shape.
  return (
    <div className={frame}>
      <Suspense
        fallback={
          <div className="deck-aspect-box flex w-full items-center justify-center" data-aspect={aspect}>
            <Spinner size="md" tone="inherit" className="text-accent" label={t_i18n("Loading...")} />
          </div>
        }
      >
        <DeckStage
          presentation={presentation}
          variant="embedded"
          controller={active}
          compact
          autoEngage
          leading={leading}
          surfaceClassName="deck-aspect-box w-full"
        />
      </Suspense>
    </div>
  );
}
