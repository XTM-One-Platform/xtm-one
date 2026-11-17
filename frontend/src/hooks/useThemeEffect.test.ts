/**
 * First-paint theme resolution.
 *
 * The theme cached in this browser (`copilot-theme`) is applied at module load,
 * before `/me` answers. Once it answers, an explicit account preference wins;
 * no preference (`null`) must keep the cached theme instead of switching a
 * light page back to dark and overwriting the cache.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { applyTheme, effectiveTheme, resolveTheme } from "./useThemeEffect";

afterEach(() => {
  document.documentElement.removeAttribute("data-mode");
  document.documentElement.classList.remove("light");
  document.documentElement.classList.add("dark");
  localStorage.clear();
});

describe("resolveTheme", () => {
  it("an explicit account preference wins over the cache", () => {
    expect(resolveTheme("dark", "light")).toBe("dark");
    expect(resolveTheme("light", "dark")).toBe("light");
    expect(resolveTheme("light", null)).toBe("light");
  });

  it("no account preference keeps the cached theme", () => {
    expect(resolveTheme(null, "light")).toBe("light");
    expect(resolveTheme(undefined, "light")).toBe("light");
    expect(resolveTheme(null, "dark")).toBe("dark");
  });

  it("no preference and no cache is dark, as before", () => {
    expect(resolveTheme(null, null)).toBe("dark");
    expect(resolveTheme("sepia", null)).toBe("dark");
  });
});

describe("a reload with a cached light theme and no account preference", () => {
  it("stays light on every layer and keeps the cache", () => {
    localStorage.setItem("copilot-theme", "light");
    applyTheme(resolveTheme(null, localStorage.getItem("copilot-theme")));
    const root = document.documentElement;
    expect(root.dataset.mode).toBe("light");
    expect(root.classList.contains("light")).toBe(true);
    expect(root.classList.contains("dark")).toBe(false);
    expect(localStorage.getItem("copilot-theme")).toBe("light");
  });
});

// Profile showed "Dark" for a null preference on a light page, and a
// save with no change wrote theme=dark and switched the session.
describe("effectiveTheme (the Profile Theme row and its edit select)", () => {
  it("a null preference with a light cache reads light, so a save keeps light", () => {
    localStorage.setItem("copilot-theme", "light");
    expect(effectiveTheme(null)).toBe("light");
    expect(effectiveTheme(undefined)).toBe("light");
  });

  it("an explicit preference wins over the cache", () => {
    localStorage.setItem("copilot-theme", "light");
    expect(effectiveTheme("dark")).toBe("dark");
    localStorage.setItem("copilot-theme", "dark");
    expect(effectiveTheme("light")).toBe("light");
  });

  it("no preference and no cache is dark", () => {
    expect(effectiveTheme(null)).toBe("dark");
  });

  it("Profile reads and seeds the theme through effectiveTheme, never a dark default", () => {
    const source = readFileSync(resolve(__dirname, "../pages/Profile.tsx"), "utf8");
    expect(source).not.toMatch(/user\.theme\s*\?\?\s*"dark"/);
    expect(source).not.toMatch(/user\.theme\s*===\s*"light"/);
    expect(source).toContain("setTheme(effectiveTheme(user.theme))");
    expect(source).toContain("effectiveTheme(user.theme) === \"light\"");
  });
});
