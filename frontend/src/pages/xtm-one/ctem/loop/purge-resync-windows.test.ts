// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import type { CtemSignalResyncRoute } from "@/hooks/api/useCtemSignalsApi";
import { initialWindowText, parseWindow, routeOutcome, windowsToSend } from "./purge-resync-windows";

const ROUTES: CtemSignalResyncRoute[] = [
  { key: "vulnerability", group: "world", label: "Vulnerabilities", default: 7, windowed: true },
  { key: "intrusion_set", group: "world", label: "Intrusion sets", default: 30, windowed: true },
  { key: "aikido", group: "inside", label: "Aikido open issues", default: null, windowed: true },
  { key: "attack_surface", group: "pass", label: "Attack surface pass", default: null, windowed: false },
];

describe("purge resync windows (#2578)", () => {
  it("starts every windowed route on its default, an unlimited one empty, a pass left out", () => {
    expect(initialWindowText(ROUTES)).toEqual({ vulnerability: "7", intrusion_set: "30", aikido: "" });
  });

  it("reads a whole number of days within the bound", () => {
    expect(parseWindow("3", { group: "world" }, 365)).toBe(3);
    expect(parseWindow(" 365 ", { group: "world" }, 365)).toBe(365);
    expect(parseWindow("0", { group: "world" }, 365)).toBe("invalid");
    expect(parseWindow("366", { group: "world" }, 365)).toBe("invalid");
    expect(parseWindow("2.5", { group: "world" }, 365)).toBe("invalid");
    expect(parseWindow("-1", { group: "world" }, 365)).toBe("invalid");
  });

  it("takes an empty inside field as no age limit, never an empty world field", () => {
    expect(parseWindow("", { group: "inside" }, 365)).toBeNull();
    expect(parseWindow("", { group: "world" }, 365)).toBe("invalid");
  });

  it("sends every windowed route, or nothing while one field is invalid", () => {
    expect(windowsToSend(ROUTES, { vulnerability: "3", intrusion_set: "30", aikido: "" }, 365)).toEqual({
      vulnerability: 3,
      intrusion_set: 30,
      aikido: null,
    });
    expect(windowsToSend(ROUTES, { vulnerability: "", intrusion_set: "30", aikido: "" }, 365)).toBeNull();
  });

  it("reads a route's outcome: skipped, error, or done with its counts", () => {
    expect(routeOutcome({ window_days: null, skipped: "no Aikido integration" })).toEqual({ kind: "skipped", reason: "no Aikido integration" });
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
