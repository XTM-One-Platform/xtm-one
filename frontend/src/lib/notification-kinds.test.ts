import { describe, expect, it } from "vitest";

import { canMarkAllRead, hasUnreadApproval, isWaitingDecision, notificationSubtitle, readsOnOpen } from "./notification-kinds";

const t = (text: string) => `fr:${text}`;

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

  it("translates a waiting work item's subtitle and keeps an agent's name", () => {
    expect(notificationSubtitle({ kind: "work_item_waiting", subtitle: "Work item" }, t)).toBe("fr:Work item");
    expect(notificationSubtitle({ kind: "hitl", subtitle: "Remediation planner" }, t)).toBe("Remediation planner");
  });
});
