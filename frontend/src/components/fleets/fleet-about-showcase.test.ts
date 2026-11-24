import { describe, expect, it } from "vitest";

import type { FleetLivePrerequisite, FleetMemberHealth } from "@/hooks/api/useFleetApi";
import { INTEGRATION_PROVIDER_NAMES } from "@/lib/constants";

import { versionNoteText } from "./fleet-documentation";
import {
  aboutStatus,
  automationCount,
  automationOn,
  checklistGroups,
  clampChips,
  INTEGRATION_CARD_PROVIDERS_MAX,
  INTEGRATION_CARD_CHIPS_MAX,
  integrationUses,
  lastRunAt,
  memberCards,
  moduleLabel,
  quotaBasisKind,
  quotaEstimate,
  quotaRange,
  quotaSubtitle,
  quotaValue,
  setupPrerequisites,
  shownAutomationCount,
  successRate,
  triggerKindLabel,
  triggerKinds,
} from "./fleet-about-showcase";
import { prerequisiteSummary } from "./fleet-prerequisites";

const member = (enabled: boolean, last_run_at: string | null): FleetMemberHealth =>
  ({ agent_id: "a", name: "A", status: "ok", enabled, last_run_at, last_run_status: null, runs_in_flight: 0, open_work_items: 0, stale_work_items: 0 }) as FleetMemberHealth;

const ran = { members: [member(true, "2026-11-01T00:00:00Z")] };
const never = { members: [member(true, null)] };

describe("moduleLabel", () => {
  // The page's own constant, never a map injected by the test (UX r1 B4 on #9988: "Github" slipped through one).
  const brands = INTEGRATION_PROVIDER_NAMES;

  it("reads the integration providers a corporate fleet listens to by their brand", () => {
    expect(moduleLabel("github", brands).text).toBe("GitHub");
    expect(moduleLabel("notion", brands).text).toBe("Notion");
    expect(moduleLabel("microsoft", brands).text).toBe("Microsoft");
  });

  it("reads a base module from the catalog and a provider by its brand, never the platform (#9979)", () => {
    expect(moduleLabel("opencti", brands)).toEqual({ text: "OpenCTI", translate: true });
    expect(moduleLabel("webhook", brands)).toEqual({ text: "Webhook", translate: true });
    expect(moduleLabel(undefined, brands)).toEqual({ text: "XTM One", translate: true });
    expect(moduleLabel("github", brands)).toEqual({ text: "GitHub", translate: false });
    expect(moduleLabel("made_up_tool", brands)).toEqual({ text: "Made up tool", translate: false });
  });
});

describe("automationCount", () => {
  it("counts the members' assignments, not the agents' enabled flag", () => {
    const assignments = [
      { agent_id: "a", enabled: false },
      { agent_id: "a", enabled: false },
      { agent_id: "a", enabled: true },
      { agent_id: "other", enabled: true },
    ];
    expect(automationCount(ran, assignments)).toEqual({ on: 1, total: 3 });
  });
  it("is unknown while either read is", () => {
    expect(automationCount(undefined, [])).toBeNull();
    expect(automationCount(ran, undefined)).toBeNull();
  });
});

describe("shownAutomationCount", () => {
  // One count source with the header pill and the CTEM loop setup (UX r1 B2 on #1092): "12 of 33" on the
  // About tile against "0 of 21" in the setup for the same fleet came from two populations.
  it("reads the health's fleet automations when the health carries them", () => {
    const assignments = [
      { agent_id: "a", enabled: true },
      { agent_id: "a", enabled: false },
    ];
    expect(shownAutomationCount({ ...ran, automations: { enabled: 0, total: 21 } }, assignments)).toEqual({ on: 0, total: 21 });
  });
  it("falls back to the members' assignments while the health has no count", () => {
    expect(shownAutomationCount(ran, [{ agent_id: "a", enabled: true }])).toEqual({ on: 1, total: 1 });
    expect(shownAutomationCount(undefined, [])).toBeNull();
  });
});

describe("aboutStatus", () => {
  it("is unknown without health or automations", () => {
    expect(aboutStatus(undefined, { on: 0, total: 0 })).toBeNull();
    expect(aboutStatus(ran, null)).toBeNull();
  });
  it("is Not set up with no automation, or none on and none ran", () => {
    expect(aboutStatus(ran, { on: 0, total: 0 })).toBe("not_set_up");
    expect(aboutStatus(never, { on: 0, total: 3 })).toBe("not_set_up");
  });
  it("is Paused when every automation is off after a run, the agents still enabled", () => {
    expect(aboutStatus(ran, { on: 0, total: 3 })).toBe("paused");
  });
  it("is Partially on when some automations are on", () => {
    expect(aboutStatus(ran, { on: 1, total: 3 })).toBe("partial");
    expect(aboutStatus(never, { on: 1, total: 3 })).toBe("partial");
  });
  it("is On when every automation is on", () => {
    expect(aboutStatus(ran, { on: 3, total: 3 })).toBe("on");
  });
});

describe("figures", () => {
  it("reads the latest run of any member", () => {
    expect(lastRunAt({ members: [member(true, "2026-11-01T00:00:00Z"), member(true, "2026-11-03T00:00:00Z")] })).toBe(
      "2026-11-03T00:00:00Z",
    );
    expect(lastRunAt({ members: [member(true, null)] })).toBeNull();
  });
  it("sums the quota lines into a range", () => {
    expect(quotaRange({ quota: [{ trigger: "a", runs_min: 2, runs_max: 5 }, { trigger: "b", runs_min: 1, runs_max: 3 }] })).toEqual({
      min: 3,
      max: 8,
      openEnded: false,
      kinds: [],
    });
    // #10108: one event-driven line makes the range a floor.
    expect(quotaRange({ quota: [{ trigger: "a", runs_min: 2, runs_max: 5 }, { trigger: "b", runs_min: 0, runs_max: 0, open_ended: true }] })).toEqual({
      min: 2,
      max: 5,
      openEnded: true,
      kinds: [],
    });
    expect(quotaRange(null)).toBeNull();
  });

  // UX r1 B1, B2, B3 on #10112: the wording follows the trigger kinds present, never one kind for all.
  it("collects the trigger kinds present, in a fixed order", () => {
    const range = quotaRange({
      quota: [
        { trigger: "a", runs_min: 0, runs_max: 0, open_ended: true, kinds: ["manual"] },
        { trigger: "b", runs_min: 30, runs_max: 30, kinds: ["schedule"] },
      ],
    });
    expect(range?.kinds).toEqual(["schedule", "manual"]);
  });

  it("words a hand-started row as started by hand and an event row as event-driven (B1)", () => {
    expect(quotaValue({ runs_min: 0, runs_max: 0, open_ended: true, kinds: ["manual"] })).toBe("manual");
    expect(quotaValue({ runs_min: 0, runs_max: 0, open_ended: true, kinds: ["event"] })).toBe("event");
    expect(quotaValue({ runs_min: 0, runs_max: 0, open_ended: true, kinds: ["manual", "event"] })).toBe("event");
    expect(quotaValue({ runs_min: 0, runs_max: 0, open_ended: true })).toBe("event");
    expect(quotaValue({ runs_min: 4, runs_max: 4, open_ended: true, kinds: ["event"] })).toBe("floor");
    expect(quotaValue({ runs_min: 5, runs_max: 5 })).toBe("exact");
    expect(quotaValue({ runs_min: 0, runs_max: 5 })).toBe("range");
  });

  it("never says '0 or more' on a fleet with no runs, and builds the subtitle from the kinds present (B2)", () => {
    expect(quotaValue({ runs_min: 0, runs_max: 0, open_ended: true, kinds: ["manual"] })).not.toBe("floor");
    expect(quotaSubtitle({ openEnded: true, kinds: ["manual"] })).toBe("manual");
    expect(quotaSubtitle({ openEnded: true, kinds: ["schedule", "manual"] })).toBe("schedule_manual");
    expect(quotaSubtitle({ openEnded: true, kinds: ["event"] })).toBe("event");
    expect(quotaSubtitle({ openEnded: true, kinds: ["event", "manual"] })).toBe("event_manual");
    expect(quotaSubtitle({ openEnded: true, kinds: ["schedule", "event"] })).toBe("schedule_event");
    expect(quotaSubtitle({ openEnded: true, kinds: [] })).toBe("legacy");
    expect(quotaSubtitle({ openEnded: false, kinds: ["schedule"] })).toBe("fixed");
  });

  it("renders the basis from its kind, the written text only for an AI estimate or an older version (B3)", () => {
    expect(quotaBasisKind({ runs_max: 1, kinds: ["event"], observed: true, basis: "Observed: 1 runs" })).toBe("observed");
    expect(quotaBasisKind({ runs_max: 30, kinds: ["schedule"], basis: "Estimate" })).toBe("schedule");
    expect(quotaBasisKind({ runs_max: 0, kinds: ["event"], basis: "Event-driven, no fixed estimate" })).toBe("none");
    expect(quotaBasisKind({ runs_max: 20, kinds: ["event"], basis: "Estimate: one per new issue" })).toBe("written");
    expect(quotaBasisKind({ runs_max: 9, kinds: ["schedule", "event"], basis: "x" })).toBe("grouped");
    expect(quotaBasisKind({ runs_max: 9, basis: "legacy text" })).toBe("written");
    expect(quotaBasisKind({ runs_max: 9 })).toBeNull();
  });
});

describe("version notes and computed blocks (UX r1 B3, B6 on #9988)", () => {
  const t = (text: string, values?: Record<string, string | number>) =>
    Object.entries(values ?? {}).reduce((acc, [k, v]) => acc.replace(`{${k}}`, String(v)), text);

  it("renders a note code in plain words, never an internal key", () => {
    expect(versionNoteText({ code: "part_derived", params: { block: "triggers" } }, t)).toBe(
      "Trigger cards come from the fleet's configuration: the AI wrote none",
    );
    expect(versionNoteText({ code: "block_lost", params: { block: "made_up" } }, t)).toBe("A block: missing from this version, the previous version had them");
    expect(versionNoteText({ code: "text", params: { text: "An older note" } }, t)).toBe("An older note");
    expect(versionNoteText({ code: "unknown_code" }, t)).toBe("");
  });

  it("lists the members that use something", () => {
    const s = {
      automations: [{ name: "Daily", enabled: true }],
      integrations: [{ member: "A", integrations: ["github"] }, { member: "B" }],
    };
    expect(integrationUses(s).map((u) => u.member)).toEqual(["A"]);
    expect(triggerKindLabel("Schedule", (x) => `t:${x}`)).toBe("t:Schedule");
    expect(triggerKindLabel("custom", (x) => `t:${x}`)).toBe("custom");
  });
});

describe("automationOn", () => {
  it("reads an automation stored without enabled (its default) as on, only an explicit false as off (UX r2 B1 on #9988)", () => {
    expect(automationOn({})).toBe(true);
    expect(automationOn({ enabled: true })).toBe(true);
    expect(automationOn({ enabled: false })).toBe(false);
  });
});

describe("clampChips", () => {
  it("keeps every card the same size: past the cap, the rest is counted for a +N chip (UX r2 B3 on #9988)", () => {
    const fifteen = Array.from({ length: 15 }, (_, i) => `p${i}`);
    const { shown, rest } = clampChips(fifteen);
    expect(shown.length + 1).toBe(INTEGRATION_CARD_CHIPS_MAX);
    expect(shown.length + rest).toBe(15);
    expect(clampChips(["a", "b"])).toEqual({ shown: ["a", "b"], rest: 0 });
    expect(clampChips(undefined)).toEqual({ shown: [], rest: 0 });
  });

  it("keeps the providers row to one line: three named providers and +N past the providers cap (UX r3 B1 on #9988)", () => {
    const fifteen = Array.from({ length: 15 }, (_, i) => `p${i}`);
    const { shown, rest } = clampChips(fifteen, INTEGRATION_CARD_PROVIDERS_MAX);
    expect(shown).toEqual(["p0", "p1", "p2"]);
    expect(rest).toBe(12);
    expect(shown.length + 1).toBeLessThanOrEqual(INTEGRATION_CARD_PROVIDERS_MAX);
    expect(clampChips(["a", "b", "c", "d"], INTEGRATION_CARD_PROVIDERS_MAX)).toEqual({ shown: ["a", "b", "c", "d"], rest: 0 });
  });
});

describe("the About KPI row and member cards (#10123)", () => {
  it("says about N when the estimate is one number, never an identical range", () => {
    expect(quotaEstimate({ min: 1238, max: 1238, openEnded: false })).toEqual({ kind: "about", value: 1238 });
    expect(quotaEstimate({ min: 10, max: 20, openEnded: false })).toEqual({ kind: "range", min: 10, max: 20 });
    expect(quotaEstimate({ min: 0, max: 40, openEnded: true })).toEqual({ kind: "floor", value: 40 });
    expect(quotaEstimate(null)).toBeNull();
  });

  it("names an open-ended estimate with no fixed run instead of 0 or more (UX r2 B2)", () => {
    expect(quotaEstimate({ min: 0, max: 0, openEnded: true, kinds: ["event"] })).toEqual({ kind: "named", state: "event" });
    expect(quotaEstimate({ min: 0, max: 0, openEnded: true, kinds: ["event", "manual"] })).toEqual({ kind: "named", state: "event" });
    expect(quotaEstimate({ min: 0, max: 0, openEnded: true, kinds: ["manual"] })).toEqual({ kind: "named", state: "manual" });
    expect(quotaEstimate({ min: 0, max: 0, openEnded: true })).toEqual({ kind: "named", state: "event" });
    expect(quotaEstimate({ min: 0, max: 30, openEnded: true, kinds: ["schedule", "event"] })).toEqual({ kind: "floor", value: 30 });
  });

  it("lists the unmet prerequisites first and folds the met ones (UX r2 B1)", () => {
    const states = [
      { label: "a", met: true },
      { label: "b", met: false },
      { label: "c", met: null },
      { label: "d", met: true },
      { label: "e", met: false },
    ];
    const { open, met } = checklistGroups(states);
    expect(open.map((p) => p.label)).toEqual(["b", "e", "c"]);
    expect(met.map((p) => p.label)).toEqual(["a", "d"]);
  });

  it("computes the success rate over finished runs only", () => {
    expect(successRate({ succeeded: 9, failed: 1 })).toBe(90);
    expect(successRate({ succeeded: 0, failed: 0 })).toBeNull();
    expect(successRate(undefined)).toBeNull();
  });

  it("puts the orchestrator first and counts each member's automations live", () => {
    const health = {
      members: [
        { ...member(true, null), agent_id: "b", name: "Beta" },
        { ...member(true, "2026-11-01T00:00:00Z"), agent_id: "o", name: "Orchestrator" },
        { ...member(true, null), agent_id: "a", name: "Alpha" },
      ],
    };
    const structured = { members: [{ name: "Alpha", role: "Reads the news" }], integrations: [{ member: "Alpha", integrations: ["github"] }] };
    const assignments = [
      { agent_id: "a", enabled: true },
      { agent_id: "a", enabled: false },
    ];
    const cards = memberCards(health, structured, assignments, "o");
    expect(cards.map((c) => c.name)).toEqual(["Orchestrator", "Alpha", "Beta"]);
    expect(cards[1]).toMatchObject({ roleLine: "Reads the news", providers: ["github"], automationsOn: 1, automationsTotal: 2 });
    expect(cards[2]).toMatchObject({ roleLine: "", providers: [], automationsOn: 0, automationsTotal: 0 });
  });

  it("lists each trigger kind once", () => {
    expect(triggerKinds({ automations: [{ name: "a", trigger: "Schedule" }, { name: "b", trigger: "Event" }, { name: "c", trigger: "Schedule" }] })).toEqual(["Event", "Schedule"]);
    expect(triggerKinds(null)).toEqual([]);
  });

  it("counts the setup row's prerequisites as the hero chip does: live lines first, documented states as fallback", () => {
    const live = (state: FleetLivePrerequisite["state"], check: string): FleetLivePrerequisite =>
      ({ check, kind: "integration", label: check, state, reason: "", member_ids: [], fix_route: "", can_fix: false });
    const lines = [
      ...Array.from({ length: 7 }, (_, i) => live("met", `m${i}`)),
      live("not_met", "n1"),
      live("not_met", "n2"),
      live("not_met", "n3"),
      live("to_confirm", "c1"),
    ];
    const documented = [{ met: true }, { met: true }, { met: false }, { met: null }];
    const chip = prerequisiteSummary(lines);
    expect(setupPrerequisites(lines, documented)).toEqual({ met: chip.met, total: chip.total });
    // A manual line to confirm counts (#10469): it is never a line left out of the figure.
    expect(setupPrerequisites(lines, documented)).toEqual({ met: 7, total: 11 });
    expect(setupPrerequisites([], documented)).toEqual({ met: 2, total: 4 });
    expect(setupPrerequisites(undefined, [])).toBeNull();
    expect(setupPrerequisites([live("to_confirm", "c")], documented)).toEqual({ met: 0, total: 1 });
  });
});
