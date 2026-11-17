// What a host sends through the panel's handle never goes missing: the
// composer's text for a send stays until the send lands, and a send that goes
// nowhere - at once, or once held behind a running turn - ends up in the
// composer, after what the user typed.

import { describe, expect, it } from "vitest";
import type { ChatSendOutcome } from "@filigran/chatbot";

import { createHostSender } from "@/components/chat-panel/host-send";

function fakePanel(outcome: ChatSendOutcome | Error, draft = "") {
  const panel = {
    draft,
    sent: [] as string[],
    send: async (text: string) => {
      panel.sent.push(text);
      if (outcome instanceof Error) throw outcome;
      return outcome;
    },
    getDraft: () => panel.draft,
    setDraft: (text: string) => {
      panel.draft = text;
    },
  };
  return panel;
}

describe("createHostSender", () => {
  it("clears the composer's text once its send lands, and only while the composer still holds it alone", async () => {
    for (const outcome of ["sent", "steered"] as const) {
      const panel = fakePanel(outcome, "the subject");
      await createHostSender(() => panel).send("the subject", undefined, { composer: "the subject" });
      expect(panel.draft).toBe("");
    }
    const edited = fakePanel("sent", "typed since\n\nthe subject");
    await createHostSender(() => edited).send("the subject", undefined, { composer: "the subject" });
    expect(edited.draft).toBe("typed since\n\nthe subject");
  });

  it("leaves the composer's text in place when its send goes nowhere", async () => {
    for (const outcome of ["failed", "dropped", new Error("network")] as const) {
      const panel = fakePanel(outcome, "the subject");
      await createHostSender(() => panel).send("the subject", undefined, { composer: "the subject" });
      expect(panel.draft).toBe("the subject");
    }
  });

  it("puts a host's text that went nowhere in the composer, after what was typed", async () => {
    const empty = fakePanel("failed");
    await createHostSender(() => empty).send("Continue where you left off.");
    expect(empty.draft).toBe("Continue where you left off.");

    const typed = fakePanel("dropped", "my own words");
    await createHostSender(() => typed).send("Continue where you left off.");
    expect(typed.draft).toBe("my own words\n\nContinue where you left off.");
  });

  it("keeps a held send's text until the panel says it left", async () => {
    const panel = fakePanel("held", "the subject");
    const sender = createHostSender(() => panel);
    expect(await sender.send("the subject", undefined, { composer: "the subject" })).toBe("held");
    expect(panel.draft).toBe("the subject");

    sender.onHeldSendSettled("the subject", "sent");
    expect(panel.draft).toBe("");
  });

  it("gives a held host send back to the composer when Stop returns it, or it fails or is dropped", async () => {
    for (const outcome of ["returned", "failed", "dropped"] as const) {
      const panel = fakePanel("held");
      const sender = createHostSender(() => panel);
      await sender.send("Run the report", undefined, { restore: "Run the report" });
      sender.onHeldSendSettled("Run the report", outcome);
      expect(panel.draft).toBe("Run the report");
      // Settled once: a later held message says nothing of it.
      panel.draft = "";
      sender.onHeldSendSettled("something else", "failed");
      expect(panel.draft).toBe("");
    }
  });

  it("does not put back a held send cancelled with its conversation", async () => {
    const panel = fakePanel("held");
    const sender = createHostSender(() => panel);
    await sender.send("Run the report");
    sender.onHeldSendSettled("Run the report", "cancelled");
    expect(panel.draft).toBe("");
  });
});
