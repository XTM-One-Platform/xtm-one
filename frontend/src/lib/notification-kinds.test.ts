import { describe, expect, it } from "vitest";

import { notificationSubtitle, UNREAD_UNTIL_ANSWERED } from "./notification-kinds";

const t = (text: string) => `fr:${text}`;

describe("notification kinds", () => {
  it("pins a waiting work item and a waiting run until answered", () => {
    expect(UNREAD_UNTIL_ANSWERED.has("work_item_waiting")).toBe(true);
    expect(UNREAD_UNTIL_ANSWERED.has("hitl")).toBe(true);
    expect(UNREAD_UNTIL_ANSWERED.has("reminder")).toBe(false);
  });

  it("translates a waiting work item's subtitle and keeps an agent's name", () => {
    expect(notificationSubtitle({ kind: "work_item_waiting", subtitle: "Work item" }, t)).toBe("fr:Work item");
    expect(notificationSubtitle({ kind: "hitl", subtitle: "Remediation planner" }, t)).toBe("Remediation planner");
  });
});
