// SPDX-License-Identifier: Apache-2.0

/**
 * Lands the page on one card (a `?card=` link, a missing field): the card is scrolled to the top of
 * the view, below the sticky bars (its `scroll-margin-top`), and kept there while the page settles.
 *
 * A single `scrollIntoView` is not enough (UX round 1 B1 on xtm-one #108): the panel is still
 * growing when the card mounts (late queries, cards unfolding), so the scroll is clamped to the
 * short page and the card of a tab's end stays at the bottom of the view. The scroll is applied again
 * whenever the page resizes, until the reader scrolls, types or clicks, or `CARD_LANDING_SETTLE_MS`
 * has passed. When the page cannot scroll that far, the browser stops at its end, where the last
 * card is the last thing on the page and reads whole.
 */

/** How long a landing keeps its card in place while the page settles. */
const CARD_LANDING_SETTLE_MS = 1500;

/** What the reader does that ends a landing: the page is theirs again. */
const READER_EVENTS = ["wheel", "touchstart", "keydown", "pointerdown"] as const;

/** Scrolls `element` into place now and again on every resize of the page; returns the function that ends it. */
export function landOnCard(element: HTMLElement, settleMs: number = CARD_LANDING_SETTLE_MS): () => void {
  const place = () => {
    if (element.isConnected) element.scrollIntoView?.({ block: "start" });
  };
  place();
  let ended = false;
  // A scroll another effect makes in the same frame (a tab reveal, a restored position) is undone on the next one.
  const frame = requestAnimationFrame(() => {
    if (!ended) place();
  });
  if (typeof ResizeObserver === "undefined") return () => cancelAnimationFrame(frame);
  const doc = element.ownerDocument;
  const observer = new ResizeObserver(() => {
    if (!ended) place();
  });
  observer.observe(doc.documentElement);
  observer.observe(doc.body);
  const end = () => {
    if (ended) return;
    ended = true;
    observer.disconnect();
    cancelAnimationFrame(frame);
    window.clearTimeout(timer);
    for (const name of READER_EVENTS) window.removeEventListener(name, end, true);
  };
  for (const name of READER_EVENTS) window.addEventListener(name, end, { capture: true, passive: true });
  const timer = window.setTimeout(end, settleMs);
  return end;
}
