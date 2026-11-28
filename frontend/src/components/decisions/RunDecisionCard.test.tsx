// The one decision card on a run (#842, S3): a tool approval decides every
// pending call with Approve / Approve always / Reject, a consent offers no
// "always", and a run question's APPROVE <code> is one explicit option.

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, within } from "@testing-library/react";

import { DecisionCard, effectText, orderedOptions } from "@/components/decisions/DecisionCard";
import {
  approvalDecisionView,
  approvalDecisions,
  approvalOptions,
  CONSENT_OPTION,
  questionDecisionView,
  RunDecisionCard,
} from "@/components/decisions/RunDecisionCard";
import { keepsWaitingOption, keptAnswer, workItemDecisionView } from "@/components/work-items/WorkItemDecisionCard";
import { renderWithProviders } from "@/testing/providers";
import type { ActionProposal, WorkItem, WorkItemAskOption } from "@/types";

const t = (s: string, v?: Record<string, unknown>) =>
  v ? s.replace(/\{(\w+)\}/g, (_, k: string) => String(v[k] ?? `{${k}}`)) : s;

function proposal(id: string, extra: Partial<ActionProposal> = {}): ActionProposal {
  return {
    tool_call_id: id,
    tool_name: "send_email",
    tool_description: "Sends an email",
    arguments: { to: "a@example.com" },
    input_schema: { properties: { to: { description: "recipient" } } },
    source: "builtin",
    ...extra,
  };
}

afterEach(cleanup);

describe("decision view helpers", () => {
  it("drops a leading Recommended the agent wrote and puts the recommendation first", () => {
    expect(effectText("Recommended: ship it")).toBe("ship it");
    expect(effectText("(recommended) ship it")).toBe("ship it");
    const opts = [{ key: "A", label: "a" }, { key: "B", label: "b" }];
    expect(orderedOptions(opts, "B").map((o) => o.key)).toEqual(["B", "A"]);
  });

  it("offers Approve, Approve always and Reject for a tool approval", () => {
    const keys = approvalOptions([proposal("c1")], t).map((o) => o.key);
    expect(keys).toEqual(["approve", "approve_always", "reject"]);
  });

  it("offers no Approve always on a consent, and its Approve asks for a confirm", () => {
    const options = approvalOptions([proposal("c1", { requires_consent: true })], t);
    expect(options.map((o) => o.key)).toEqual(["approve", "reject"]);
    expect(options[0].confirm).toBeTruthy();
    expect(options[0].warning).toBe(true);
  });

  it("sends one decision per pending call, the reason on a reject only", () => {
    const ps = [proposal("c1"), proposal("c2")];
    expect(approvalDecisions(ps, { option: "approve_always" })).toEqual([
      { tool_call_id: "c1", decision: "approve_always" },
      { tool_call_id: "c2", decision: "approve_always" },
    ]);
    expect(approvalDecisions(ps, { option: "reject", text: "wrong tenant" })).toEqual([
      { tool_call_id: "c1", decision: "reject", rejection_reason: "wrong tenant" },
      { tool_call_id: "c2", decision: "reject", rejection_reason: "wrong tenant" },
    ]);
    expect(approvalDecisions(ps, { option: "approve", text: "ignored" })).toEqual([
      { tool_call_id: "c1", decision: "approve" },
      { tool_call_id: "c2", decision: "approve" },
    ]);
    expect(approvalDecisions(ps, { text: "free" })).toBeNull();
  });

  it("keeps the tool and its arguments out of the question, under Technical details", () => {
    const view = approvalDecisionView([proposal("c1")], t);
    expect(view.question).not.toContain("send_email");
    // The tool's progress label is a line of its own, never inside the question (UX r1 B2).
    expect(view.question).toBe("Approve this action?");
    expect(view.context?.[0]).toBeTruthy();
    expect(approvalDecisionView([proposal("c1"), proposal("c2")], t).question).toContain("Approve these # actions?");
    expect(view.freeText).toBe("none");
    expect(view.technical).toBeTruthy();
  });

  it("turns APPROVE <code> into one consent option, and none without a code", () => {
    const view = questionDecisionView("Merge them? Reply APPROVE M-1A2B3C4D to go on.", t);
    expect(view.options.map((o) => o.key)).toEqual([CONSENT_OPTION]);
    expect(view.options[0].confirm).toBeTruthy();
    expect(questionDecisionView("Which feed should I use?", t).options).toHaveLength(0);
  });

  it("maps a work item ask without changing what it shows", () => {
    const option: WorkItemAskOption = { key: "A", label: "Ship", effect: "Recommended: ship now" };
    const item = {
      id: "w1",
      title: "T",
      ask: { question: "Ship it?", options: [option], recommendation: "A", context: "line 1\nline 2" },
      blocked_reason: "WAITS FOR: x",
    } as unknown as WorkItem;
    const view = workItemDecisionView(item, t);
    expect(view.question).toBe("Ship it?");
    expect(view.recommendation).toBe("A");
    expect(view.context).toEqual(["line 1", "line 2"]);
    expect(view.technical).toBeTruthy();
  });

  it("folds the whole context under Full context when the card's cap cut it, and offers none otherwise (#2800)", () => {
    const cut = {
      id: "w4",
      title: "T",
      ask: { question: "Close it?", options: [], context: "Host 1 runs nginx \u2026", context_full: "Host 1 runs nginx 1.24.\nHost 2 runs nginx 1.22." },
    } as unknown as WorkItem;
    const view = workItemDecisionView(cut, t);
    expect(view.fullContext).toBe("Host 1 runs nginx 1.24.\nHost 2 runs nginx 1.22.");
    renderWithProviders(<DecisionCard view={view} canAnswer onAnswer={vi.fn()} testId="fc" />);
    const full = screen.getByTestId("fc-full-context");
    expect(within(full).getByText("Full context")).toBeTruthy();
    expect(full.textContent).toContain("Host 2 runs nginx 1.22.");
    expect(screen.getByTestId("fc-context").textContent).toContain("Host 1 runs nginx");
    // Opened, the fold holds the whole text alone: the capped copy is gone, nothing shows twice.
    (full as HTMLDetailsElement).open = true;
    fireEvent(full, new Event("toggle"));
    expect(screen.queryByTestId("fc-context")).toBeNull();
    expect(screen.getByTestId("fc-full-context").textContent).toContain("Host 1 runs nginx 1.24.");
    expect(screen.getByTestId("fc-full-context").textContent).toContain("Host 2 runs nginx 1.22.");
    // Closed again, the capped context comes back.
    (full as HTMLDetailsElement).open = false;
    fireEvent(full, new Event("toggle"));
    expect(screen.getByTestId("fc-context")).toBeTruthy();
    cleanup();
    const whole = { ...cut, ask: { question: "Close it?", options: [], context: "Short." } } as unknown as WorkItem;
    renderWithProviders(<DecisionCard view={workItemDecisionView(whole, t)} canAnswer onAnswer={vi.fn()} testId="fs" />);
    expect(screen.queryByTestId("fs-full-context")).toBeNull();
  });

  it("leads with Open the case on an item about a case, never when the card is shown inside that case (#2370)", () => {
    const item = {
      id: "w3",
      title: "T",
      correlation_key: "ctem:validation:c1",
      ctem_case: { id: "c1", title: "Case", state: "validating", path: "/ctem/cases/c1" },
      ask: { question: "Run it?", options: [] },
    } as unknown as WorkItem;
    expect(workItemDecisionView(item, t).lead).toBeTruthy();
    expect(workItemDecisionView(item, t, { inCase: true }).lead).toBeUndefined();
  });

  it("asks to confirm an option the agent marked confirm, and only that one (#2150)", () => {
    const item = {
      id: "w2",
      title: "T",
      ask: {
        question: "Close the case?",
        options: [
          { key: "keep", label: "Keep it open" },
          { key: "cancel", label: "Cancel the case", confirm: true },
        ],
        recommendation: "keep",
      },
    } as unknown as WorkItem;
    const [keep, cancel] = workItemDecisionView(item, t).options;
    expect(keep.confirm).toBeUndefined();
    expect(keep.warning).toBeUndefined();
    expect(cancel.confirm).toBeTruthy();
    expect(cancel.warning).toBe(true);
    expect(cancel.label).toBe("Cancel the case");
  });
});

describe("a keep-waiting answer on a work item (#2500)", () => {
  const ask = {
    question: "Ship it?",
    options: [
      { key: "ship", label: "Ship it" },
      { key: "later", label: "Later", keeps_waiting: true },
    ],
    recommendation: "ship",
  };
  const answered = (extra: Partial<WorkItem> = {}) =>
    ({
      id: "w9",
      title: "T",
      status: "blocked",
      needs_human: true,
      ask,
      payload: { answer: { option: "later", label: "Later", answered_by: "Ana", answered_at: "2026-11-26T09:00:00+00:00" } },
      ...extra,
    }) as unknown as WorkItem;

  it("reads the recorded answer of a keep-waiting option, never one of another option", () => {
    expect(keepsWaitingOption({ ask } as unknown as WorkItem, "LATER")?.key).toBe("later");
    expect(keepsWaitingOption({ ask } as unknown as WorkItem, "ship")).toBeNull();
    const kept = keptAnswer(answered(), t);
    expect(kept).toMatchObject({ label: "Later", by: "Ana", at: "2026-11-26T09:00:00+00:00" });
    expect(kept?.waiting).toContain("its agent asks again");
    const other = answered({ payload: { answer: { option: "ship", answered_at: "2026-11-26T09:00:00+00:00" } } });
    expect(keptAnswer(other, t)).toBeNull();
    expect(keptAnswer(answered({ needs_human: false }), t)).toBeNull();
  });

  it("says what the item still waits for: its other steps, else its agent", () => {
    expect(keptAnswer(answered({ blocked_on: ["a", "b"] }), t)?.waiting).toContain("other steps");
    const named = answered({ context: { owner_agent: { id: "g", name: "Desk", accessible: true } } } as unknown as Partial<WorkItem>);
    expect(keptAnswer(named, t)?.waiting).toContain("Desk asks again");
  });

  it("shows the answer, folds the options behind Change the answer, never Waiting for your decision", () => {
    const view = workItemDecisionView(answered(), t);
    renderWithProviders(<DecisionCard view={view} canAnswer onAnswer={vi.fn()} testId="wi" />);
    const card = screen.getByTestId("wi");
    expect(within(card).queryByText("Waiting for your decision")).toBeNull();
    expect(within(card).getByText("Answer recorded")).toBeTruthy();
    expect(within(card).getByTestId("wi-answered-line").textContent).toContain("Answered: Later by Ana");
    const change = within(card).getByTestId("wi-change");
    expect(change.hasAttribute("open")).toBe(false);
    expect(within(change).getAllByTestId("wi-option")).toHaveLength(2);
  });

  it("keeps the open card unchanged before any answer", () => {
    const view = workItemDecisionView(answered({ payload: {} }), t);
    expect(view.answered).toBeNull();
    renderWithProviders(<DecisionCard view={view} canAnswer onAnswer={vi.fn()} testId="wo" />);
    expect(screen.getByText("Waiting for your decision")).toBeTruthy();
    expect(screen.queryByTestId("wo-change")).toBeNull();
  });
});

describe("RunDecisionCard", () => {
  it("approves from the card: one tile per option, the call sent", () => {
    const onApprove = vi.fn();
    renderWithProviders(<RunDecisionCard runId="r1" proposals={[proposal("c1")]} canAnswer onApprove={onApprove} />);
    const card = screen.getByTestId("run-decision");
    const tiles = within(card).getAllByTestId("run-decision-option");
    expect(tiles).toHaveLength(3);
    fireEvent.click(tiles[0]);
    expect(onApprove).toHaveBeenCalledWith("r1", [{ tool_call_id: "c1", decision: "approve" }]);
  });

  it("asks a confirm before Approve always, then sends it", () => {
    const onApprove = vi.fn();
    renderWithProviders(<RunDecisionCard runId="r1" proposals={[proposal("c1")]} canAnswer onApprove={onApprove} />);
    const always = screen
      .getAllByTestId("run-decision-tile")
      .find((tile) => tile.dataset.option === "approve_always")!;
    fireEvent.click(within(always).getByTestId("run-decision-option"));
    expect(onApprove).not.toHaveBeenCalled();
    expect(screen.getByTestId("run-decision-confirm").dataset.option).toBe("approve_always");
    fireEvent.click(screen.getByTestId("run-decision-confirm-send"));
    expect(onApprove).toHaveBeenCalledWith("r1", [{ tool_call_id: "c1", decision: "approve_always" }]);
  });

  it("rejects with the reason typed in the confirm panel", () => {
    const onApprove = vi.fn();
    renderWithProviders(<RunDecisionCard runId="r1" proposals={[proposal("c1")]} canAnswer onApprove={onApprove} />);
    const reject = screen.getAllByTestId("run-decision-tile").find((tile) => tile.dataset.option === "reject")!;
    fireEvent.click(within(reject).getByTestId("run-decision-option"));
    const panel = screen.getByTestId("run-decision-confirm");
    fireEvent.change(within(panel).getByRole("textbox"), { target: { value: "use staging" } });
    fireEvent.click(screen.getByTestId("run-decision-confirm-send"));
    expect(onApprove).toHaveBeenCalledWith("r1", [
      { tool_call_id: "c1", decision: "reject", rejection_reason: "use staging" },
    ]);
  });

  it("sends exactly the APPROVE reply once the consent is confirmed", () => {
    const onRespond = vi.fn();
    renderWithProviders(
      <RunDecisionCard runId="r1" question="Reply APPROVE M-1A2B3C4D to merge." proposals={[]} canAnswer onRespond={onRespond} />,
    );
    fireEvent.click(screen.getByTestId("run-decision-option"));
    expect(onRespond).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("run-decision-confirm-send"));
    expect(onRespond).toHaveBeenCalledWith("r1", "APPROVE M-1A2B3C4D");
  });

  it("answers a question in free text, and says who may answer when the reader may not", () => {
    const onRespond = vi.fn();
    renderWithProviders(<RunDecisionCard runId="r1" question="Which feed?" proposals={[]} canAnswer onRespond={onRespond} />);
    fireEvent.change(screen.getByLabelText("Your answer"), { target: { value: "Feed A" } });
    fireEvent.click(screen.getByRole("button", { name: /Send answer/ }));
    expect(onRespond).toHaveBeenCalledWith("r1", "Feed A");
    cleanup();
    // The run question is Markdown, rendered (UX r1 B1); Discard sits in the
    // Send answer row, before it (UX r1 B3).
    renderWithProviders(
      <RunDecisionCard runId="r1" question={"Pick the **weekly digest** feed"} proposals={[]} canAnswer onRespond={vi.fn()} onDismiss={vi.fn()} />,
    );
    const question = screen.getByTestId("run-decision-question");
    expect(question.textContent).not.toContain("**");
    expect(question.querySelector("strong")?.textContent).toBe("weekly digest");
    const discard = screen.getByTestId("run-decision-discard");
    const send = screen.getByRole("button", { name: /Send answer/ });
    expect(discard.parentElement).toBe(send.parentElement);
    expect(discard.compareDocumentPosition(send) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    cleanup();
    renderWithProviders(<RunDecisionCard runId="r1" proposals={[proposal("c1")]} canAnswer={false} onApprove={vi.fn()} />);
    expect(screen.getByText("Waiting for approval from someone with access to this run.")).toBeTruthy();
    for (const tile of screen.getAllByTestId("run-decision-option")) expect((tile as HTMLButtonElement).disabled).toBe(true);
  });
});
