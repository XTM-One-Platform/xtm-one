import { describe, expect, it } from "vitest";

import { askAgentPrompt, offersAskTheAgent } from "@/components/work-items/WorkItemAskAgent";
import { CLOSE_EFFECTS, REQUEUE_EFFECT, STALE_REASON_LABELS, staleBadgeText, staleReasonText } from "@/lib/work-item-effects";

const identity = (text: string, values?: Record<string, string>) =>
  text.replace(/\{(\w+)\}/g, (_, key: string) => values?.[key] ?? `{${key}}`);

const blocked = { id: "item-1", title: "Waits on PR 12", needs_human: false, status: "blocked" as const };

describe("Ask the agent on every item (#9879)", () => {
  it("is offered on every unfinished status and never on a finished one", () => {
    for (const status of ["open", "claimed", "in_progress", "blocked"] as const) {
      expect(offersAskTheAgent({ status })).toBe(true);
    }
    expect(offersAskTheAgent({ status: "done" })).toBe(false);
    expect(offersAskTheAgent({ status: "cancelled" })).toBe(false);
  });

  it("It's solved names the item and leaves room for the person's words", () => {
    const prompt = askAgentPrompt(identity, blocked, "solved");
    expect(prompt).toContain("item-1");
    expect(prompt).toContain("Waits on PR 12");
    expect(prompt).toContain("[what changed]");
    expect(prompt).toContain("Verify it live");
  });

  it("words Explain and Re-check around the wait when no decision is asked", () => {
    expect(askAgentPrompt(identity, blocked, "explain")).toContain("what it waits for");
    expect(askAgentPrompt(identity, blocked, "recheck")).toContain("still pending");
  });

  it("keeps the decision wording on an item waiting for the reader", () => {
    const deciding = { ...blocked, needs_human: true };
    expect(askAgentPrompt(identity, deciding, "explain")).toContain("the decision, the options");
  });
});

describe("effects spelled out (#9879)", () => {
  it("says what each close and the requeue do to the owner agent", () => {
    expect(CLOSE_EFFECTS.done).toContain("does not run it again");
    expect(CLOSE_EFFECTS.done).toContain("It's solved");
    expect(CLOSE_EFFECTS.cancelled).toContain("no completion");
    expect(REQUEUE_EFFECT).toContain("runs it again");
  });

  it("names the server's stale reason, else the generic one", () => {
    expect(staleReasonText(identity, { stale_reason: "wait_never_releases" })).toBe(STALE_REASON_LABELS.wait_never_releases);
    expect(staleReasonText(identity, { stale_reason: null })).toBe("Needs attention.");
  });

  it("puts the stale cause on the badge, never on hover only", () => {
    expect(staleBadgeText(identity, { stale_reason: "past_due" })).toBe("Past due");
    expect(staleBadgeText(identity, { stale_reason: "wait_never_releases" })).toBe("Never released");
    expect(staleBadgeText(identity, { stale_reason: "ref_wait_quiet" })).toBe("Reference quiet");
    expect(staleBadgeText(identity, { stale_reason: "quiet" })).toBe("Quiet for a day");
    expect(staleBadgeText(identity, { stale_reason: null })).toBe("Stale");
  });
});
