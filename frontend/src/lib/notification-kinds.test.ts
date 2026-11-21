import { describe, expect, it } from "vitest";

import { hasUnreadApproval, notificationSubtitle, readsOnOpen, UNREAD_UNTIL_ANSWERED } from "./notification-kinds";

const t = (text: string) => `fr:${text}`;

describe("notification kinds", () => {
  it("pins a waiting work item and a waiting run until answered", () => {
    expect(UNREAD_UNTIL_ANSWERED.has("work_item_waiting")).toBe(true);
    expect(UNREAD_UNTIL_ANSWERED.has("hitl")).toBe(true);
    expect(UNREAD_UNTIL_ANSWERED.has("reminder")).toBe(false);
  });

  it("reads every unread entry on open, a pinned one included (#901)", () => {
    expect(readsOnOpen({ unread: true })).toBe(true);
    expect(readsOnOpen({ unread: false })).toBe(false);
  });

  it("alerts the badge on an unread approval only (#901)", () => {
    expect(hasUnreadApproval([{ kind: "hitl", unread: true }])).toBe(true);
    expect(hasUnreadApproval([{ kind: "hitl", unread: false }, { kind: "reminder", unread: true }])).toBe(false);
    expect(hasUnreadApproval([])).toBe(false);
  });

  it("translates a waiting work item's subtitle and keeps an agent's name", () => {
    expect(notificationSubtitle({ kind: "work_item_waiting", subtitle: "Work item" }, t)).toBe("fr:Work item");
    expect(notificationSubtitle({ kind: "hitl", subtitle: "Remediation planner" }, t)).toBe("Remediation planner");
  });
});
