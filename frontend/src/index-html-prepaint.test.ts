/**
 * Theme pre-paint in index.html - `yarn test`.
 *
 * `index.html` ships `class="dark layer-0"` on `<html>`. Its inline script
 * reads the theme `useThemeEffect` cached (`copilot-theme`) and switches the
 * document to light before the bundle loads, so a reload in light theme has no
 * dark first frame. The script runs here as the browser runs it, before any
 * module of the app is imported.
 */
import { afterEach, beforeEach, expect, test } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { THEME_COLOR_DARK, THEME_COLOR_LIGHT, applyTheme } from "./hooks/useThemeEffect";

// vitest runs from frontend/ (CI and `yarn test`), as chat-unavailable-copy.test.ts.
const INDEX_HTML = readFileSync(join(process.cwd(), "index.html"), "utf8");

/** The text of every classic inline script of index.html (no src, no type). */
function inlineScripts(html: string): string[] {
  return [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((match) => match[1]);
}

/** The `content` of `<meta name="theme-color">` as index.html ships it. */
const SHIPPED_THEME_COLOR = /<meta name="theme-color" content="([^"]*)"/.exec(INDEX_HTML)?.[1];

function themeColorMeta(): HTMLMetaElement {
  let meta = document.head.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (!meta) {
    meta = document.createElement("meta");
    meta.name = "theme-color";
    document.head.appendChild(meta);
  }
  return meta;
}

/**
 * Resets `<html>` and the theme-color meta to what index.html ships (or to the
 * `served` value the backend branding writes into it), then runs its inline scripts.
 */
function loadIndexHtml(served: string = SHIPPED_THEME_COLOR ?? "") {
  const root = document.documentElement;
  root.className = "dark layer-0";
  delete root.dataset.mode;
  delete root.dataset.theme;
  themeColorMeta().setAttribute("content", served);
  for (const script of inlineScripts(INDEX_HTML)) new Function(script)();
}

beforeEach(() => localStorage.clear());
afterEach(() => localStorage.clear());

test("index.html carries exactly one inline pre-paint script and keeps the dark default", () => {
  expect(INDEX_HTML).toContain('<html lang="en" class="dark layer-0">');
  const scripts = inlineScripts(INDEX_HTML);
  expect(scripts).toHaveLength(1);
  expect(scripts[0]).toContain('localStorage.getItem("copilot-theme")');
});

test("the pre-paint script runs before the module bundle", () => {
  expect(INDEX_HTML.indexOf("<script>")).toBeLessThan(INDEX_HTML.indexOf('<script type="module"'));
});

test("a cached light theme paints light before React mounts", () => {
  localStorage.setItem("copilot-theme", "light");
  loadIndexHtml();
  const root = document.documentElement;
  expect(root.dataset.mode).toBe("light");
  expect(root.classList.contains("light")).toBe(true);
  expect(root.classList.contains("dark")).toBe(false);
  expect(root.classList.contains("layer-0")).toBe(true);
});

test("index.html ships the dark theme-color and its guard reads the same value", () => {
  expect(SHIPPED_THEME_COLOR).toBe(THEME_COLOR_DARK);
  const script = inlineScripts(INDEX_HTML)[0];
  expect(script).toContain(`meta.getAttribute("content") === "${THEME_COLOR_DARK}"`);
  expect(script).toContain(`meta.setAttribute("content", "${THEME_COLOR_LIGHT}")`);
});

test("a cached light theme sets the theme-color meta to the light value applyTheme writes", () => {
  localStorage.setItem("copilot-theme", "light");
  loadIndexHtml();
  expect(themeColorMeta().getAttribute("content")).toBe(THEME_COLOR_LIGHT);
  applyTheme("light");
  expect(themeColorMeta().getAttribute("content")).toBe(THEME_COLOR_LIGHT);
});

test.each(["#5E1AD5", "#123456"])(
  "a served branded theme-color %s is kept with a cached light theme",
  (served) => {
    localStorage.setItem("copilot-theme", "light");
    loadIndexHtml(served);
    expect(document.documentElement.classList.contains("light")).toBe(true);
    expect(themeColorMeta().getAttribute("content")).toBe(served);
  },
);

test.each([null, "dark", "anything"])("a cached theme of %s keeps the dark default", (cached) => {
  if (cached !== null) localStorage.setItem("copilot-theme", cached);
  loadIndexHtml();
  const root = document.documentElement;
  expect(root.dataset.mode).toBeUndefined();
  expect(root.classList.contains("dark")).toBe(true);
  expect(root.classList.contains("light")).toBe(false);
  expect(themeColorMeta().getAttribute("content")).toBe(SHIPPED_THEME_COLOR);
});

test("an unreadable localStorage leaves the dark default and throws nothing", () => {
  const original = Object.getOwnPropertyDescriptor(window, "localStorage");
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    get() {
      throw new Error("SecurityError");
    },
  });
  try {
    expect(() => loadIndexHtml()).not.toThrow();
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  } finally {
    if (original) Object.defineProperty(window, "localStorage", original);
  }
});
