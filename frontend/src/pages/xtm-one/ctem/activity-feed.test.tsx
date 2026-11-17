// D3-4 of the CTEM open-source program (#9567): the deterministic activity feed
// of the Live activity card, read from the posture and the registrations with
// no model.

import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { fireEvent, screen, within } from "@testing-library/react";

import type { CtemPosture, CtemStage } from "@/types";
import { renderWithProviders } from "@/testing/providers";
import { deterministicActivity, eventStatus, feedCapacity, type FeedRegistration } from "./activity-feed";
import { CTEM_WIDGETS } from "./dashboard-layout";
import { ActivityFeed } from "./ActivityFeed";

type FeedPosture = Pick<CtemPosture, "stages" | "products" | "covered_stages" | "total_stages" | "generated_at">;

function validation(buckets: Record<string, Record<string, number>>): CtemStage {
  return {
    key: "validation", label: "Validation", score: null, prev_score: null, band: "unknown", covered: true,
    confidence: "high", primary_products: ["openaev"], secondary_products: [], missing_products: [],
    products_present: ["openaev"], methodology: "", facts: [], viz: { expectation_buckets: buckets },
  };
}

function posture(over: Partial<FeedPosture> = {}): FeedPosture {
  return {
    stages: [validation({ PREVENTION: { SUCCESS: 6, FAILED: 2 }, DETECTION: { SUCCESS: 2, PARTIAL: 2 } })],
    products: {
      opencti: { present: true, last_sync: "2026-11-07T08:00:00Z" },
      openaev: { present: true, last_sync: "2026-11-07T07:00:00Z" },
      opencrq: { present: false, coming_soon: true },
    },
    covered_stages: 4,
    total_stages: 5,
    generated_at: "2026-11-07T08:10:00Z",
    ...over,
  };
}

function reg(over: Partial<FeedRegistration> = {}): FeedRegistration {
  return {
    id: "r1", platform_identifier: "opencti", platform_title: "OpenCTI prod",
    registered_at: "2026-11-01T10:00:00Z", last_heartbeat_at: "2026-11-07T08:09:00Z", healthy: true, ...over,
  };
}

describe("deterministicActivity", () => {
  it("answers nothing without a posture or a registration", () => {
    expect(deterministicActivity(null, null)).toEqual([]);
    expect(deterministicActivity(undefined, [])).toEqual([]);
  });

  it("lists syncs, validation and registrations newest first", () => {
    const events = deterministicActivity(posture(), [reg()]);
    expect(events.map((e) => e.kind)).toEqual([
      "posture_synced", "product_synced", "product_synced", "validation", "platform_registered",
    ]);
    expect(events[0]).toMatchObject({ covered: 4, total: 5 });
    expect(events[1]).toMatchObject({ product: "opencti" });
  });

  it("counts validation results over every expectation type, partial as not met", () => {
    const v = deterministicActivity(posture(), []).find((e) => e.kind === "validation");
    expect(v).toMatchObject({ met: 8, total: 12, at: "2026-11-07T07:00:00Z" });
    // #9594 A3: some met is partial (amber), not a failure.
    expect(eventStatus(v!)).toBe("partial");
  });

  it("reads a validation with none met as failed and all met as completed", () => {
    const none = deterministicActivity(posture({ stages: [validation({ PREVENTION: { FAILED: 3 } })] }), []).find((e) => e.kind === "validation");
    expect(eventStatus(none!)).toBe("failed");
    const all = deterministicActivity(posture({ stages: [validation({ PREVENTION: { SUCCESS: 3 } })] }), []).find((e) => e.kind === "validation");
    expect(eventStatus(all!)).toBe("completed");
  });

  it("leaves validation out when OpenAEV is not connected or has no result", () => {
    const off = posture({ products: { openaev: { present: false, last_sync: "2026-11-07T07:00:00Z" } } });
    expect(deterministicActivity(off, []).some((e) => e.kind === "validation")).toBe(false);
    const empty = posture({ stages: [validation({})] });
    expect(deterministicActivity(empty, []).some((e) => e.kind === "validation")).toBe(false);
  });

  it("never invents a time: no sync time, no row", () => {
    const never = posture({ generated_at: undefined, products: { opencti: { present: true, last_sync: null } } });
    expect(deterministicActivity(never, [])).toEqual([]);
  });

  it("adds a failed row for a platform that stopped sending heartbeats", () => {
    const events = deterministicActivity(null, [reg({ healthy: false, last_heartbeat_at: "2026-11-06T12:00:00Z" })]);
    expect(events.map((e) => e.kind)).toEqual(["platform_unhealthy", "platform_registered"]);
    expect(eventStatus(events[0])).toBe("failed");
    expect(eventStatus(events[1])).toBe("completed");
  });

  it("keeps at most the limit", () => {
    expect(deterministicActivity(posture(), [reg()], 2)).toHaveLength(2);
  });

  // #9627: the timeline `[service.lab-ctem-activity]` seeds (lab_asgi and
  // seed_ctem_activity.py read the same file) puts every kind of event in the
  // newest 6, and the four rows of the default grid card are not all
  // registrations stamped at the lab start.
  it("lists every kind in the first 6 over the lab's seeded timeline", () => {
    const timeline = JSON.parse(readFileSync(
      path.resolve(__dirname, "../../../../../.xtm-one/lab/ctem_activity_timeline.json"), "utf8",
    )) as Record<"product_sync" | "heartbeat" | "registered", Record<string, number>>;
    const now = Date.parse("2026-11-07T10:00:00Z");
    const ago = (minutes: number) => new Date(now - minutes * 60_000).toISOString();
    const products = Object.fromEntries(Object.entries(timeline.product_sync)
      .map(([p, m]) => [p, { present: true, last_sync: ago(m) }]));
    const registrations = Object.entries(timeline.registered).map(([p, m]) => reg({
      id: p, platform_identifier: p, platform_title: p, registered_at: ago(m),
      last_heartbeat_at: ago(timeline.heartbeat[p] ?? 1), healthy: !(p in timeline.heartbeat),
    }));
    const events = deterministicActivity(posture({ products, generated_at: ago(0) }), registrations);
    expect(new Set(events.map((e) => e.kind))).toEqual(new Set([
      "posture_synced", "product_synced", "validation", "platform_unhealthy", "platform_registered",
    ]));
    const grid = events.slice(0, feedCapacity({ cols: CTEM_WIDGETS.live_activity.w, rows: CTEM_WIDGETS.live_activity.h }, events.length));
    expect(grid.map((e) => e.kind)).toEqual(["posture_synced", "product_synced", "product_synced", "validation"]);
  });
});

describe("feedCapacity", () => {
  // #9568 UX round 1 B1: at the default size the card holds four whole rows
  // above the lock footer; the fifth would sit under an inner scroll.
  it("draws only the rows the card holds whole", () => {
    const { h, w } = CTEM_WIDGETS.live_activity;
    expect(feedCapacity({ cols: w, rows: h }, 6)).toBe(4);
    expect(feedCapacity({ cols: w, rows: CTEM_WIDGETS.live_activity.minH ?? 6 }, 6)).toBe(1);
    expect(feedCapacity({ cols: w, rows: 40 }, 6)).toBe(6);
  });

  it("draws every row in the stacked layout and none without an event", () => {
    expect(feedCapacity(null, 6)).toBe(6);
    expect(feedCapacity({ cols: 5, rows: 12 }, 0)).toBe(0);
  });
});

describe("Live activity rows", () => {
  // #9568 UX round 1 B2: one row layout in the card, shared by the feed and
  // the greyed demo rows.
  it("renders the feed and the demo rows through ActivityRow", () => {
    const read = (p: string) => readFileSync(new URL(p, import.meta.url), "utf8");
    const feed = read("./ActivityFeed.tsx");
    const center = read("../CtemCommandCenter.tsx");
    expect(feed).toMatch(/<ActivityRow\b/);
    const demo = center.slice(center.indexOf("function DemoActivityRow"));
    expect(demo.slice(0, demo.indexOf("\n}\n"))).toMatch(/<ActivityRow\b/);
    expect(feed).not.toMatch(/h-7 w-7 rounded-sm/);
  });
});

describe("ActivityFeed", () => {
  it("writes one sentence per event", () => {
    renderWithProviders(<ActivityFeed events={deterministicActivity(posture(), [reg({ healthy: false })])} />);
    expect(screen.getByRole("list", { name: "Recent platform activity" })).toBeInTheDocument();
    expect(screen.getByText("Posture synced")).toBeInTheDocument();
    expect(screen.getByText("4 of 5 stages covered")).toBeInTheDocument();
    expect(screen.getByText("Data synced from OpenCTI")).toBeInTheDocument();
    expect(screen.getByText("Validation results")).toBeInTheDocument();
    expect(screen.getByText("8 of 12 expectations met in simulations")).toBeInTheDocument();
    expect(screen.getByText("OpenCTI prod registered")).toBeInTheDocument();
    expect(screen.getByText("OpenCTI prod stopped responding")).toBeInTheDocument();
  });

  // #9594 A2: the colour of the dot is also said in text.
  it("names the status of every row for assistive technology", () => {
    renderWithProviders(<ActivityFeed events={deterministicActivity(posture(), [reg({ healthy: false })])} />);
    const validationRow = screen.getByText("Validation results").closest("li")!;
    expect(within(validationRow).getByText("Partial")).toBeInTheDocument();
    expect(validationRow.querySelector("[data-status]")?.getAttribute("data-status")).toBe("partial");
    const downRow = screen.getByText("OpenCTI prod stopped responding").closest("li")!;
    expect(within(downRow).getByText("Failed")).toBeInTheDocument();
    expect(downRow.querySelector("[data-status]")?.getAttribute("data-status")).toBe("failed");
  });

  // #9594 A1: relative time in the row, the absolute time in a tooltip.
  it("shows the absolute time of a row in a tooltip", async () => {
    renderWithProviders(<ActivityFeed events={deterministicActivity(posture(), [])} />);
    const row = screen.getByText("Validation results").closest("li")!;
    const time = within(row).getByTestId("activity-row-time");
    expect(time.textContent).toMatch(/ago|now|yesterday/);
    fireEvent.focus(time);
    expect((await screen.findByRole("tooltip")).textContent).toMatch(/Nov/);
  });
});
