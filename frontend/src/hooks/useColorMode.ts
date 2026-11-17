// SPDX-License-Identifier: Apache-2.0

/**
 * Reactive accessor for the active color mode ("light" | "dark").
 *
 * The app toggles light mode by setting `data-mode="light"` on the root
 * `<html>` element (see `useThemeEffect`). Most UI themes via CSS-variable
 * tokens that flip automatically, but SVG presentation attributes
 * (`fill`, `stroke`) can't reference Tailwind tokens — so canvas/graph
 * components read the mode through this hook and pick theme-aware colors.
 *
 * Subscribes to `<html>` attribute mutations so the value updates live when
 * the user switches themes without a reload.
 */
import { useSyncExternalStore } from "react";

type ColorMode = "light" | "dark";

function getSnapshot(): ColorMode {
  return document.documentElement.dataset.mode === "light" ? "light" : "dark";
}

function subscribe(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-mode"],
  });
  return () => observer.disconnect();
}

export function useColorMode(): ColorMode {
  return useSyncExternalStore(subscribe, getSnapshot, () => "dark");
}

interface ChartTheme {
  /** Gridline / axis-line stroke (baked opacity — no separate strokeOpacity). */
  gridStroke: string;
  /** Axis tick-label fill. */
  tickFill: string;
}

/**
 * Theme-aware colors for hand-rolled SVG charts. Recharts and raw SVG
 * `stroke`/`fill` are presentation attributes that can't resolve CSS `var()`
 * tokens, so hardcoded white rgba() rendered invisible on the white light-mode
 * card. Charts read these concrete per-mode values instead.
 */
export function useChartTheme(): ChartTheme {
  const isDark = useColorMode() === "dark";
  return {
    gridStroke: isDark ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.08)",
    tickFill: isDark ? "rgba(255,255,255,0.3)" : "rgba(0,0,0,0.45)",
  };
}
