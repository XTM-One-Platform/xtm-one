// The runs Activity tabs on the shared DataTable (#9378 slice 4a): the URL order, old links included, and the Type column.

import { describe, expect, it } from "vitest";

import { runSortFromParams, runTypeLabel } from "./run-activity";

describe("runSortFromParams", () => {
  it("defaults to the newest first", () => {
    expect(runSortFromParams(null, null)).toEqual({ key: "started", direction: "desc" });
  });

  it("reads an Activity tab link written before the table", () => {
    expect(runSortFromParams("oldest", null)).toEqual({ key: "started", direction: "asc" });
    expect(runSortFromParams("newest", null)).toEqual({ key: "started", direction: "desc" });
    expect(runSortFromParams("status", null)).toEqual({ key: "status", direction: "asc" });
  });

  it("reads the table's own keys and drops an unknown column", () => {
    expect(runSortFromParams("duration", "desc")).toEqual({ key: "duration", direction: "desc" });
    expect(runSortFromParams("agent", "asc")).toEqual({ key: "agent", direction: "asc" });
    expect(runSortFromParams("nope", "asc")).toEqual({ key: "started", direction: "asc" });
  });
});

describe("runTypeLabel", () => {
  const run = (trigger_context: Record<string, unknown> | null, parent_run_id: string | null = null) => ({ trigger_context, parent_run_id });

  it("names an A2A partner's run and a delegation first", () => {
    expect(runTypeLabel(run({ source: "a2a", type: "webhook" }))).toBe("A2A");
    expect(runTypeLabel(run({ type: "manual" }, "parent"))).toBe("Delegation");
  });

  it("names the trigger type", () => {
    expect(runTypeLabel(run({ type: "schedule" }))).toBe("Scheduled");
    expect(runTypeLabel(run({ type: "integration_event" }))).toBe("Integration event");
    expect(runTypeLabel(run({ type: "scheduled_work_item" }))).toBe("Work item");
  });

  it("falls back to Manual without a context, Other for an unknown type", () => {
    expect(runTypeLabel(run(null))).toBe("Manual");
    expect(runTypeLabel(run({ type: "organization_digest" }))).toBe("Other");
  });
});
