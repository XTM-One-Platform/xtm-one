// The waiting game's canvas paints a resolved colour, not a CSS variable, so
// it has to read the accent again whenever the platform mode or the colour
// mode changes: XTM One mode is purple, Copilot mode is blue.

import { afterEach, describe, expect, it } from "vitest";

import { followAccent, resolveAccent, resolveArcadeTheme } from "@/components/arcade/theme";
import { hexToHsl } from "@/lib/color-utils";

const COPILOT = "#0fbcff";
const XTM_ONE = "#a47af0";

function modes(): HTMLStyleElement {
  const style = document.createElement("style");
  style.textContent = `:root{--color-accent:${COPILOT}} [data-theme="xtm-one"]{--color-accent:${XTM_ONE}}`;
  document.head.appendChild(style);
  return style;
}

function mountCanvas(): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  document.body.appendChild(canvas);
  return canvas;
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

/** The token as the arcade palette writes it (`hsl(h s% l%)`), derived, never a literal. */
function asArcadeHsl(hex: string): string {
  const { h, s, l } = hexToHsl(hex) ?? { h: NaN, s: NaN, l: NaN };
  return `hsl(${Math.round(h)} ${Math.round(s)}% ${Math.round(l)}%)`;
}

describe("the waiting game's accent", () => {
  afterEach(() => {
    document.head.querySelectorAll("style").forEach((style) => style.remove());
    document.body.innerHTML = "";
    delete document.documentElement.dataset.theme;
  });

  it("is the Copilot accent in Copilot mode", () => {
    modes();
    expect(resolveAccent(mountCanvas())).toBe(COPILOT);
  });

  it("is the XTM One accent in XTM One mode", () => {
    modes();
    document.documentElement.dataset.theme = "xtm-one";
    expect(resolveAccent(mountCanvas())).toBe(XTM_ONE);
  });

  it("follows a switch to XTM One mode made after the game started, and back", async () => {
    modes();
    const seen: string[] = [];
    const stop = followAccent(mountCanvas(), (accent) => seen.push(accent));
    expect(seen[seen.length - 1]).toBe(COPILOT);

    document.documentElement.dataset.theme = "xtm-one";
    await flush();
    expect(seen[seen.length - 1], "the canvas kept the accent it read at mount").toBe(XTM_ONE);

    delete document.documentElement.dataset.theme;
    await flush();
    expect(seen[seen.length - 1]).toBe(COPILOT);
    stop();
  });

  it("paints the full-screen arcade in the XTM One accent in XTM One mode, not the Copilot blue", () => {
    modes();
    document.documentElement.dataset.theme = "xtm-one";
    const accent = resolveArcadeTheme(mountCanvas()).accent;
    expect(accent).toBe(asArcadeHsl(XTM_ONE));
    expect(accent).not.toBe(asArcadeHsl(COPILOT));
  });

  it("paints the full-screen arcade in the Copilot accent in Copilot mode", () => {
    modes();
    expect(resolveArcadeTheme(mountCanvas()).accent).toBe(asArcadeHsl(COPILOT));
  });

  it("stops following once the game is destroyed", async () => {
    modes();
    const seen: string[] = [];
    const stop = followAccent(mountCanvas(), (accent) => seen.push(accent));
    stop();
    document.documentElement.dataset.theme = "xtm-one";
    await flush();
    expect(seen).toEqual([COPILOT]);
  });
});
