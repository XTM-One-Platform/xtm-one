import { describe, expect, it } from "vitest";

import { askPending, explainPrompt, offersAskTheAgent } from "@/components/work-items/WorkItemAskAgent";
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

  it("words Explain around the wait, naming the item", () => {
    const prompt = explainPrompt(identity, blocked);
    expect(prompt).toContain("item-1");
    expect(prompt).toContain("what it waits for");
  });

  it("keeps the decision wording on an item waiting for the reader", () => {
    const deciding = { ...blocked, needs_human: true };
    expect(explainPrompt(identity, deciding)).toContain("the decision, the options");
  });
});

describe("a send waits for its agent once (#774)", () => {
  const at = "2026-11-20T22:00:00Z";
  const now = Date.parse(at) + 60_000;
  it("is pending while nothing moved and under ten minutes passed", () => {
    expect(askPending({ payload: { _ask: { at } }, last_activity_at: at }, now)).toBe(true);
  });
  it("is over once the item moved, after ten minutes, or with no send", () => {
    expect(askPending({ payload: { _ask: { at } }, last_activity_at: "2026-11-20T22:00:30Z" }, now)).toBe(false);
    expect(askPending({ payload: { _ask: { at } }, last_activity_at: at }, Date.parse(at) + 11 * 60_000)).toBe(false);
    expect(askPending({ payload: {}, last_activity_at: at }, now)).toBe(false);
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
