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
 * "The deck has finished arriving".
 *
 * After React has committed a deck's tree, three things still land
 * asynchronously: web fonts, `<img>` bytes and the lazy chunks that flag
 * themselves with `[data-deck-pending]` (icon catalog, Mermaid diagrams,
 * images measuring their ratio). Anything that captures the deck — the
 * headless browser behind the PDF / PPTX export (`PresentationStaticRender`)
 * and the viewer's browser-print path — must wait for all of them, or it
 * captures a half-loaded slide. One definition of "settled", so the two
 * capture paths cannot drift.
 *
 * No wait is forever. A figure that waits on something with no end of
 * its own gives up after `FIGURE_WAIT_MS` of visible time (`figure-wait.ts`):
 * it drops `data-deck-pending`, stays as it stands and carries
 * `data-deck-unfinished`, as does a figure whose lazy drawing could not load.
 * The browser print adds a last resort of its own, `PRINT_SETTLE_MS`, and
 * tells the reader when what it printed was not finished. The export page does
 * neither: its exporter waits under a server-side timeout (`READY_TIMEOUT_MS`),
 * no client bound may make it capture a figure half drawn, and a figure that
 * could not be drawn fails the render (`deckError`) instead of being exported.
 */
import { settlesWithin } from "./visible-time";

/**
 * The longest a figure waits on something that reports nothing - a lazy
 * chunk, a Mermaid render, the icon catalog, a picture's bytes - before it
 * gives up and stands as it is, in visible time (`visible-time.ts`).
 */
export const FIGURE_WAIT_MS = 15_000;

/**
 * The longest the browser print waits for the whole deck, in visible time:
 * twice a figure's own bound, so it is the last resort behind them - whatever
 * still waits then (a font, a picture still on its way) prints as it stands.
 */
export const PRINT_SETTLE_MS = 2 * FIGURE_WAIT_MS;

/** Resolves once every `<img>` under `root` has loaded or failed. */
function imagesSettled(root: ParentNode): Promise<void> {
  const pending = Array.from(root.querySelectorAll("img")).filter((img) => !img.complete);
  if (pending.length === 0) return Promise.resolve();
  return Promise.all(
    pending.map(
      (img) =>
        new Promise<void>((resolve) => {
          const done = () => resolve();
          img.addEventListener("load", done, { once: true });
          img.addEventListener("error", done, { once: true });
        }),
    ),
  ).then(() => undefined);
}

/** Resolves once no `[data-deck-pending]` element is left under `root`. */
function pendingCleared(root: Element): Promise<void> {
  return new Promise((resolve) => {
    const check = () => {
      if (root.querySelectorAll("[data-deck-pending]").length === 0) {
        observer.disconnect();
        resolve();
      }
    };
    const observer = new MutationObserver(check);
    observer.observe(root, { childList: true, subtree: true, attributes: true });
    check();
  });
}

/** What `deckSettled()` found. */
interface DeckSettled {
  /**
   * Whether every figure finished: `false` when the bound ran out first, or
   * when a figure gave up waiting or could not be drawn
   * (`[data-deck-unfinished]`) - the deck then shows it as it stands. With no
   * bound (the export page) nothing gives up, so `false` means a figure could
   * not be drawn.
   */
  complete: boolean;
}

/**
 * Fonts, then pending chunks, then images — twice, then four frames.
 *
 * `bound` is the browser print's last resort (`PRINT_SETTLE_MS`): past it, in
 * visible time, whatever is still pending is left as it stands. The export
 * page passes none.
 */
export async function deckSettled(root: Element, { bound }: { bound?: number } = {}): Promise<DeckSettled> {
  const arrived = deckArrived(root);
  if (bound === undefined) await arrived;
  else if (!(await settlesWithin(arrived, bound))) return { complete: false };
  return { complete: root.querySelector("[data-deck-unfinished]") === null };
}

async function deckArrived(root: Element): Promise<void> {
  const fontsReady =
    typeof document.fonts?.ready?.then === "function" ? document.fonts.ready : Promise.resolve();
  await fontsReady;
  await pendingCleared(root);
  await imagesSettled(root);
  // Icons and diagrams can insert images of their own once they resolve.
  await pendingCleared(root);
  await imagesSettled(root);
  // Four frames: one for the last layout, two for the measured passes that
  // layout schedules through resize observers - the shrink-to-fit pass
  // (`fit.ts`) and the bounded spread (`spread.ts`), whose cap on a
  // `fill` grid block lands first and is only then read by the `distribute`
  // run around it, one observer round later - and one for paint.
  await new Promise<void>((resolve) => {
    window.requestAnimationFrame(() =>
      window.requestAnimationFrame(() =>
        window.requestAnimationFrame(() => window.requestAnimationFrame(() => resolve())),
      ),
    );
  });
}
