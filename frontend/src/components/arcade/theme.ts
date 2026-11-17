/**
 * The waiting arcade's colours, read from the design tokens.
 *
 * The canvas cannot use Tailwind classes, so the palette is resolved from the
 * CSS variables of the active theme (`--color-accent`, `--success`...), light
 * and dark alike; the few art colours with no token (violet, gold, ember, deep
 * space) are derived from the tokens by hue rotation.
 *
 * The accent is the product accent `--color-accent`: XTM One purple in XTM One
 * mode, Copilot blue in Copilot mode, the operator's colour when branded. It
 * used to read `--cyan`, which no stylesheet defines any more, so the arcade
 * always fell back to the Copilot blue, even in XTM One mode.
 */
import { hexToHsl } from "@/lib/color-utils";
import type { ArcadeTheme } from "./engine/types";

interface Hsl {
  h: number;
  s: number;
  l: number;
}

function parseTriple(value: string, fallback: Hsl): Hsl {
  const match = value.trim().match(/^(-?[\d.]+)(?:deg)?\s+([\d.]+)%\s+([\d.]+)%/);
  if (!match) return fallback;
  return { h: parseFloat(match[1]), s: parseFloat(match[2]), l: parseFloat(match[3]) };
}

/**
 * A resolved colour as HSL: a hex (`#a47af0`, the design system's tokens), an
 * `hsl(h s% l%)` (the branding block) or a bare `h s% l%` triple.
 */
function parseColour(value: string, fallback: Hsl): Hsl {
  const trimmed = value.trim();
  if (trimmed.startsWith("#")) return hexToHsl(trimmed) ?? fallback;
  const inner = trimmed.match(/^hsla?\((.*)\)$/);
  return parseTriple(inner ? inner[1].replace(/,/g, " ").replace(/\s+/g, " ") : trimmed, fallback);
}

/**
 * The design system's accent (`--color-accent`) as the canvas sees it, read on
 * the canvas itself so a scoped theme (the AI surfaces) wins. A canvas cannot
 * read a CSS variable, so the resolved value is passed to fillStyle; with no
 * token in scope, the canvas's own text colour (never a hard-coded colour).
 */
export function resolveAccent(canvas: HTMLCanvasElement): string {
  const style = getComputedStyle(canvas);
  return style.getPropertyValue("--color-accent").trim() || style.color;
}

/**
 * Calls `onAccent` with the canvas's accent now and again whenever it may have
 * changed: the platform mode (`data-theme`, XTM One purple or Copilot blue) and
 * the colour mode (`data-mode`, `class`) on `<html>`, and the administrator's
 * branding `<style>` in `<head>`. The canvas paints a resolved value, so a
 * value read once at mount kept the Copilot blue whenever the waiting panel or
 * the arcade mounted before the platform config set XTM One mode (a turn
 * resumed on page load), and the light set's accent after a switch to dark.
 * Returns the stop.
 */
export function followAccent(canvas: HTMLCanvasElement, onAccent: (accent: string) => void): () => void {
  onAccent(resolveAccent(canvas));
  if (typeof MutationObserver === "undefined") return () => {};
  const observer = new MutationObserver(() => onAccent(resolveAccent(canvas)));
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "data-mode", "class", "style"] });
  observer.observe(document.head, { childList: true, subtree: true, characterData: true });
  return () => observer.disconnect();
}

function css({ h, s, l }: Hsl): string {
  return `hsl(${Math.round(h)} ${Math.round(s)}% ${Math.round(l)}%)`;
}

function rotate(colour: Hsl, degrees: number, overrides: Partial<Hsl> = {}): Hsl {
  return { ...colour, h: (colour.h + degrees + 360) % 360, ...overrides };
}

/**
 * The arcade's palette as `scope` sees it (the overlay's canvas, so a scoped
 * theme wins), else as the document root sees it.
 */
export function resolveArcadeTheme(scope: Element = document.documentElement): ArcadeTheme {
  const root = document.documentElement;
  const styles = getComputedStyle(root);
  const read = (name: string, fallback: Hsl) => parseTriple(styles.getPropertyValue(name), fallback);
  const isLight = root.dataset.mode === "light";
  const accent = parseColour(getComputedStyle(scope).getPropertyValue("--color-accent"), { h: 197, s: 100, l: 53 });
  const foreground = read("--foreground", { h: 0, s: 0, l: 100 });
  const background = read("--background", { h: 220, s: 56, l: 6 });
  const muted = read("--muted-foreground", { h: 230, s: 4, l: 70 });
  const success = read("--success", { h: 160, s: 84, l: 39 });
  const warning = read("--warning", { h: 38, s: 92, l: 50 });
  const danger = read("--destructive", { h: 0, s: 84, l: 60 });
  const lift = isLight ? 0 : 8;
  return {
    isLight,
    accent: css(accent),
    foreground: css(foreground),
    background: css(background),
    muted: css(muted),
    success: css({ ...success, l: success.l + lift }),
    warning: css(warning),
    danger: css(danger),
    violet: css(rotate(accent, 75, { s: 90, l: isLight ? 48 : 68 })),
    gold: css(rotate(warning, 8, { s: 100, l: isLight ? 45 : 58 })),
    ember: css(rotate(warning, -14, { s: 100, l: isLight ? 48 : 56 })),
    // Deep space shows through the holes, whatever the theme.
    space: isLight ? css({ h: background.h, s: 45, l: 8 }) : css({ ...background, l: Math.max(2, background.l - 3) }),
  };
}
