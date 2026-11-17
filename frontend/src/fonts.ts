// SPDX-License-Identifier: Apache-2.0

/**
 * Self-hosted brand fonts.
 *
 * Bundled from the fontsource packages so the product renders the same in an
 * air-gapped install, in the headless browser that exports a deck to PDF/PPTX
 * and on screen — a Google Fonts request is both an outbound dependency and a
 * source of non-deterministic rendering (fonts may or may not have arrived
 * when the export is captured).
 *
 * These are JS-side imports on purpose: routed through the Tailwind PostCSS
 * `@import` path the `url(./files/...)` references of each fontsource
 * stylesheet are inlined verbatim and never emitted, so every font 404s in a
 * production build. Imported here, Vite resolves them relative to each
 * package and fingerprints the `.woff2` subsets like any other asset.
 *
 * Each entry declares every unicode-range subset; the browser only fetches
 * the subsets a page actually uses. Elms Sans is the display face a
 * template may name (the THREAD conference deck); its files are fetched only
 * by a deck that uses it.
 */
import "@fontsource/elms-sans/400.css";
import "@fontsource/elms-sans/500.css";
import "@fontsource/elms-sans/600.css";
import "@fontsource/elms-sans/700.css";
import "@fontsource/geologica/400.css";
import "@fontsource/geologica/500.css";
import "@fontsource/geologica/600.css";
import "@fontsource/geologica/700.css";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/400-italic.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/ibm-plex-sans/700.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
