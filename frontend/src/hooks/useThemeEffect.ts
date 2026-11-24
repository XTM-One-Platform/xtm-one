// SPDX-License-Identifier: Apache-2.0

/**
 * Applies the user's theme preference to the document element.
 *
 * Sets `data-mode="light"` on `<html>` when the user selects light theme.
 * Caches the choice in localStorage so subsequent page loads apply the
 * theme before the /me response arrives (prevents dark→light flash).
 *
 * The same element also carries a `light` / `dark` CLASS, which is the only
 * theme switch the Filigran design system reacts to: its tokens are declared
 * under `.light {}` and `.dark {}` selectors, with no attribute-based variant
 * and no `prefers-color-scheme` fallback. Theme activation is explicitly a host
 * responsibility, so the attribute stays this app's source of truth — the
 * branding form, `useColorMode` and the `light:` / `dark:` Tailwind variants all
 * read it — and the class is mirrored from it here.
 *
 * Mirroring in one place matters because missing it fails quietly: the app turns
 * light while every design-system token stays dark, with no error anywhere. That
 * is exactly what the first attempt at this migration shipped.
 */

import { useEffect } from "react";
import { useMe } from "./api/useAuthApi";

const THEME_STORAGE_KEY = "copilot-theme";

/** `<meta name="theme-color">` per mode. The inline pre-paint script of
 * index.html repeats both values (it runs before any module loads, and writes
 * the light value only over the dark default); index-html-prepaint.test.ts
 * keeps them equal. */
export const THEME_COLOR_LIGHT = "#ececf2";
export const THEME_COLOR_DARK = "#0a0f1c";

/** Mirrors the mode onto the class the design system themes through. */
function applyDesignSystemThemeClass(mode: "light" | "dark") {
  const { classList } = document.documentElement;
  classList.toggle("light", mode === "light");
  classList.toggle("dark", mode === "dark");
}

export function applyTheme(theme: string | null | undefined) {
  const mode = theme === "light" ? "light" : "dark";

  if (mode === "light") {
    document.documentElement.dataset.mode = "light";
  } else {
    delete document.documentElement.dataset.mode;
  }
  applyDesignSystemThemeClass(mode);

  const isXtm = document.documentElement.dataset.theme === "xtm-one";
  const themeHex = isXtm ? "#5E1AD5" : mode === "light" ? THEME_COLOR_LIGHT : THEME_COLOR_DARK;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", themeHex);

  try {
    localStorage.setItem(THEME_STORAGE_KEY, mode);
  } catch { /* quota exceeded or private browsing */ }
}

/**
 * The design system draws one chip style since 1.13.0 (#1595): the former
 * "Pastel colours" preference is gone, so the class and the cache entry it
 * left on a browser are cleared once at load.
 */
function clearRetiredPastel() {
  document.documentElement.classList.remove("pastel");
  try {
    localStorage.removeItem("copilot-pastel");
  } catch { /* ignore */ }
}

clearRetiredPastel();

function readCachedTheme(): string | null {
  try {
    return localStorage.getItem(THEME_STORAGE_KEY);
  } catch {
    return null;
  }
}

/**
 * The theme to paint once `/me` answered.
 *
 * An explicit account preference (`light` / `dark`) wins. No preference
 * (`null`: a fresh account, the UX lab's seeded administrator) keeps the theme
 * this browser cached, so a reload paints what the user last chose instead of
 * switching a light page back to dark when `/me` arrives.
 */
export function resolveTheme(
  serverTheme: string | null | undefined,
  cachedTheme: string | null,
): "light" | "dark" {
  if (serverTheme === "light" || serverTheme === "dark") return serverTheme;
  return cachedTheme === "light" ? "light" : "dark";
}

/**
 * The theme this browser paints for an account preference, for screens that
 * show or edit the preference (Profile). With no preference this is the cached
 * theme, not dark, so the Theme row reads what the page shows and a save with
 * no change keeps it instead of switching the session to dark.
 */
export function effectiveTheme(serverTheme: string | null | undefined): "light" | "dark" {
  return resolveTheme(serverTheme, readCachedTheme());
}

/** Restore cached theme immediately (called once at module load). */
function restoreCachedTheme() {
  try {
    const cached = readCachedTheme();
    if (cached === "light") {
      document.documentElement.dataset.mode = "light";
      applyDesignSystemThemeClass("light");
    }
  } catch { /* ignore */ }
}

restoreCachedTheme();

/**
 * Keeps the class following the attribute, whoever writes it.
 *
 * `applyTheme` writes both, but the attribute is the documented source of
 * truth and nothing stops other code from setting it alone - main's deck
 * end-to-end tests flip `data-mode` directly to view a deck in the light app
 * mode, and with only `applyTheme` mirroring they got light Tailwind variants
 * over dark design-system tokens: the half-switched state this module exists
 * to prevent. Observing the attribute closes that for every writer at once.
 */
function followModeAttribute() {
  if (typeof MutationObserver === "undefined") return;
  const root = document.documentElement;
  const sync = () => applyDesignSystemThemeClass(root.dataset.mode === "light" ? "light" : "dark");
  sync();
  new MutationObserver(sync).observe(root, { attributes: true, attributeFilter: ["data-mode"] });
}

followModeAttribute();

export function useThemeEffect() {
  const { data: user } = useMe();
  const userTheme = user?.theme;

  useEffect(() => {
    if (userTheme === undefined) return;
    applyTheme(resolveTheme(userTheme, readCachedTheme()));
  }, [userTheme]);
}
