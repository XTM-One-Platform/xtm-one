import { describe, expect, it } from "vitest";

import {
  canMarkAllRead,
  decisionSource,
  hasUnreadApproval,
  isDecisionKind,
  isWaitingDecision,
  notificationSubtitle,
  readsOnOpen,
} from "./notification-kinds";

const t = (text: string, values?: Record<string, string | number>) =>
  `fr:${Object.entries(values ?? {}).reduce((out, [key, value]) => out.replace(`{${key}}`, String(value)), text)}`;

describe("notification kinds", () => {
  it("pins a decision while it waits for its answer (#991)", () => {
    expect(isWaitingDecision({ awaiting_answer: true })).toBe(true);
    expect(isWaitingDecision({ awaiting_answer: false })).toBe(false);
    expect(isWaitingDecision({})).toBe(false);
  });

  it("never reads a waiting decision on open, reads every other unread entry (#901, #991)", () => {
    expect(readsOnOpen({ unread: true, awaiting_answer: true })).toBe(false);
    expect(readsOnOpen({ unread: true })).toBe(true);
    expect(readsOnOpen({ unread: false })).toBe(false);
  });

  it("offers Mark all read only for an entry it can read", () => {
    expect(canMarkAllRead([{ unread: true, awaiting_answer: true }])).toBe(false);
    expect(canMarkAllRead([{ unread: true, awaiting_answer: true }, { unread: true }])).toBe(true);
    expect(canMarkAllRead([{ unread: false }])).toBe(false);
  });

  it("alerts the badge on any decision still waiting (#991)", () => {
    expect(hasUnreadApproval([{ unread: true, awaiting_answer: true }])).toBe(true);
    expect(hasUnreadApproval([{ unread: true }, { unread: false }])).toBe(false);
    expect(hasUnreadApproval([])).toBe(false);
  });

  it("reads one decision kind and its three aliases (#842, S4)", () => {
    expect(isDecisionKind("decision")).toBe(true);
    expect(isDecisionKind("hitl")).toBe(true);
    expect(isDecisionKind("flow_hitl")).toBe(true);
    expect(isDecisionKind("work_item_waiting")).toBe(true);
    expect(isDecisionKind("run")).toBe(false);
    expect(decisionSource({ kind: "decision", source: "flow_run" })).toBe("flow_run");
    expect(decisionSource({ kind: "hitl" })).toBe("run");
    expect(decisionSource({ kind: "flow_hitl" })).toBe("flow_run");
    expect(decisionSource({ kind: "work_item_waiting" })).toBe("work_item");
    expect(decisionSource({ kind: "reminder" })).toBeNull();
  });

  it("says the source and who asks under a decision, a name elsewhere", () => {
    expect(notificationSubtitle({ kind: "decision", source: "run", subtitle: "Remediation planner" }, t)).toBe("fr:fr:Run · Remediation planner");
    expect(notificationSubtitle({ kind: "decision", source: "work_item", subtitle: null }, t)).toBe("fr:Work item");
    expect(notificationSubtitle({ kind: "work_item_waiting", subtitle: "Waiting for your decision" }, t)).toBe("fr:Work item");
    expect(notificationSubtitle({ kind: "hitl", subtitle: "Remediation planner" }, t)).toBe("fr:fr:Run · Remediation planner");
    expect(notificationSubtitle({ kind: "reminder", subtitle: "Reminder" }, t)).toBe("Reminder");
  });
});
