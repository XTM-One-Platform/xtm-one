// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import type { CtemSignalResyncRoute } from "@/hooks/api/useCtemSignalsApi";
import { initialChoices, NOT_SELECTED, parseWindow, routeOutcome, sourcesToSend } from "./purge-resync-windows";

const ROUTES: CtemSignalResyncRoute[] = [
  { key: "vulnerability", group: "world", label: "Vulnerabilities", default: 7, windowed: true, selectable: true },
  { key: "intrusion_set", group: "world", label: "Intrusion sets", default: 30, windowed: true, selectable: true },
  { key: "aikido", group: "inside", label: "Aikido open issues", default: null, windowed: true, selectable: true },
  { key: "gitlab", group: "inside", label: "GitLab dependency findings", default: null, windowed: false, selectable: false, note: "no listing" },
  { key: "attack_surface", group: "pass", label: "Attack surface pass", default: null, windowed: false, selectable: true },
];

describe("purge resync choices (#2578, #2745)", () => {
  it("starts every source ticked on its default in days, an info row left out", () => {
    expect(initialChoices(ROUTES)).toEqual({
      vulnerability: { enabled: true, text: "7", unit: "days" },
      intrusion_set: { enabled: true, text: "30", unit: "days" },
      aikido: { enabled: true, text: "", unit: "days" },
      attack_surface: { enabled: true, text: "", unit: "days" },
    });
  });

  it("reads a whole number within the bound of its unit", () => {
    expect(parseWindow("3", { group: "world" }, 365)).toBe(3);
    expect(parseWindow(" 365 ", { group: "world" }, 365)).toBe(365);
    expect(parseWindow("0", { group: "world" }, 365)).toBe("invalid");
    expect(parseWindow("366", { group: "world" }, 365)).toBe("invalid");
    expect(parseWindow("366", { group: "world" }, 365, "hours")).toBe(366);
    expect(parseWindow("8761", { group: "world" }, 365, "hours")).toBe("invalid");
    expect(parseWindow("2.5", { group: "world" }, 365)).toBe("invalid");
    expect(parseWindow("-1", { group: "world" }, 365)).toBe("invalid");
  });

  it("takes an empty inside field as no age limit, never an empty world field", () => {
    expect(parseWindow("", { group: "inside" }, 365)).toBeNull();
    expect(parseWindow("", { group: "world" }, 365)).toBe("invalid");
  });

  it("sends the default dialog as every source ticked on its default", () => {
    expect(sourcesToSend(ROUTES, initialChoices(ROUTES), 365)).toEqual({
      vulnerability: { enabled: true, window: 7, unit: "days" },
      intrusion_set: { enabled: true, window: 30, unit: "days" },
      aikido: { enabled: true, window: null, unit: "days" },
      attack_surface: { enabled: true },
    });
  });

  it("sends an unticked source without a window, one hour in hours, nothing while a ticked field is invalid", () => {
    const choices = {
      ...initialChoices(ROUTES),
      vulnerability: { enabled: true, text: "1", unit: "hours" as const },
      intrusion_set: { enabled: false, text: "", unit: "days" as const },
      attack_surface: { enabled: false, text: "", unit: "days" as const },
    };
    expect(sourcesToSend(ROUTES, choices, 365)).toEqual({
      vulnerability: { enabled: true, window: 1, unit: "hours" },
      intrusion_set: { enabled: false },
      aikido: { enabled: true, window: null, unit: "days" },
      attack_surface: { enabled: false },
    });
    expect(sourcesToSend(ROUTES, { ...choices, vulnerability: { enabled: true, text: "", unit: "days" } }, 365)).toBeNull();
  });

  it("reads a route's outcome: skipped, error, or done with its counts", () => {
    expect(routeOutcome({ window_days: null, skipped: "no Aikido integration" })).toEqual({
      kind: "skipped",
      reason: "no Aikido integration",
      unselected: false,
    });
    expect(routeOutcome({ window_days: null, skipped: NOT_SELECTED })).toMatchObject({ kind: "skipped", unselected: true });
    expect(routeOutcome({ window_days: 7, read: 2, recorded: 1, errors: ["OpenCTI: timeout"] })).toEqual({
      kind: "error",
      reason: "OpenCTI: timeout",
      read: 2,
      recorded: 1,
    });
    expect(routeOutcome({ window_days: 7, read: 5000, recorded: 40, capped: true })).toEqual({
      kind: "done",
      read: 5000,
      recorded: 40,
      capped: true,
      note: undefined,
    });
  });
});
