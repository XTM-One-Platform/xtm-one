// "Waiting for you" (#842, S5): every open decision of the reader in one list,
// whatever its source, answered from the side panel through the one answer path.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { useQuery } from "@tanstack/react-query";

import { sourceText, waitingDecisionView, WaitingForYou } from "@/components/decisions/WaitingForYou";
import type { WaitingDecision } from "@/hooks/api/useDecisionApi";
import { MemoryRouter } from "react-router-dom";
import { renderWithProviders as renderBare } from "@/testing/providers";

const renderWithProviders = (ui: React.ReactElement) => renderBare(<MemoryRouter>{ui}</MemoryRouter>);

const get = vi.hoisted(() => vi.fn());
const post = vi.hoisted(() => vi.fn(async () => ({})));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, get, post } };
});

const ai = vi.hoisted(() => ({ off: false }));
vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => ai.off,
  useAiDisabledReason: () => (ai.off ? "AI is disabled on this platform." : null),
}));

const t = (s: string) => s;

function row(kind: WaitingDecision["kind"], id: string, extra: Partial<WaitingDecision> = {}): WaitingDecision {
  const source_type = kind === "flow_ask" ? "flow_run" : kind === "work_item" ? "work_item" : "run";
  return {
    id,
    source_type,
    source_id: `${id}-source`,
    kind,
    question: `Question of ${id}`,
    ask: {
      question: `Question of ${id}`,
      options: [
        { key: "A", label: `Go ${id}`, effect: "It goes" },
        { key: "B", label: `Stop ${id}` },
      ],
      recommendation: "A",
    },
    origin: `Origin ${id}`,
    agent: `Agent ${id}`,
    asked_at: "2026-11-22T03:00:00Z",
    deadline: null,
    link: `/somewhere/${id}`,
    ...extra,
  };
}

const FOUR = [row("hitl", "run1"), row("tool_approval", "appr1"), row("flow_ask", "flow1"), row("work_item", "item1")];

beforeEach(() => {
  get.mockReset();
  post.mockClear();
  ai.off = false;
});
afterEach(cleanup);

describe("waiting decision helpers", () => {
  it("maps a row to the shared card: the ask, the deadline, who asks", () => {
    const view = waitingDecisionView(row("work_item", "w", { deadline: "2026-11-23T10:00:00Z" }));
    expect(view.question).toBe("Question of w");
    expect(view.options.map((o) => o.key)).toEqual(["A", "B"]);
    expect(view.recommendation).toBe("A");
    expect(view.deadline).toBe("2026-11-23T10:00:00Z");
    expect(view.asker).toBe("Agent w");
  });

  it("renders a run's question as Markdown, the others as text", () => {
    expect(waitingDecisionView(row("hitl", "r")).questionMarkdown).toBe(true);
    expect(waitingDecisionView(row("work_item", "w")).questionMarkdown).toBe(false);
  });

  it("names the source kind then its name in the From column", () => {
    expect(sourceText(row("flow_ask", "f"), t)).toBe("Flow: Origin f");
    expect(sourceText(row("tool_approval", "a", { origin: null }), t)).toBe("Tool approval");
  });
});

describe("WaitingForYou", () => {
  it("lists the four sources in one view", async () => {
    get.mockResolvedValue({ enabled: true, reason: null, items: FOUR });
    renderWithProviders(<WaitingForYou />);
    const rows = await screen.findAllByTestId("waiting-for-you-row");
    expect(rows).toHaveLength(4);
    expect(get).toHaveBeenCalledWith("/decisions/waiting");
    for (const text of ["Run: Origin run1", "Tool approval: Origin appr1", "Flow: Origin flow1", "Work item: Origin item1"]) {
      expect(screen.getByText(text)).toBeTruthy();
    }
  });

  it("filters the list by kind from the shared toolbar", async () => {
    get.mockResolvedValue({ enabled: true, reason: null, items: FOUR });
    renderWithProviders(<WaitingForYou />);
    expect(await screen.findAllByTestId("waiting-for-you-row")).toHaveLength(4);
    // The shared toolbar: a search box and the Kind pill (UX r1 B1 on #1081).
    expect(screen.getByRole("searchbox")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Kind/ })).toBeTruthy();
  });

  it("opens the card in the centred dialog and answers through the one answer path", async () => {
    get.mockResolvedValue({ enabled: true, reason: null, items: FOUR });
    renderWithProviders(<WaitingForYou />);
    const rows = await screen.findAllByTestId("waiting-for-you-row");
    fireEvent.click(within(rows[2]).getByText("Question of flow1"));
    const panel = await screen.findByTestId("waiting-for-you-panel");
    // The shared dialog, titled by the source (no slide-over, UX r1 B3 on #1081).
    expect(screen.getByRole("dialog", { name: "Flow: Origin flow1" })).toBeTruthy();
    expect(within(panel).getByTestId("waiting-decision-source-link").getAttribute("href")).toBe("/somewhere/flow1");
    fireEvent.click(within(panel).getByText("Go flow1"));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/decisions/flow1/answer", { option: "A", text: undefined, surface: "inbox" }),
    );
    await waitFor(() => expect(screen.queryByTestId("waiting-for-you-panel")).toBeNull());
  });

  it("keeps the next row's dialog open when the previous answer settles late", async () => {
    // The answer settles once every invalidated list is read again: the inbox first (the row
    // leaves, the reader opens the next one), then a slower work-items list. Its late close
    // must not shut the dialog the reader opened meanwhile (flaky E2E waiting-for-you-inbox:138).
    let items = FOUR;
    get.mockImplementation(async () => ({ enabled: true, reason: null, items }));
    let releaseWorkItems: (value: unknown) => void = () => undefined;
    let workItemsReads = 0;
    function WorkItemsList() {
      useQuery({
        queryKey: ["work-items"],
        queryFn: () => {
          workItemsReads += 1;
          return workItemsReads === 1 ? Promise.resolve([]) : new Promise((resolve) => (releaseWorkItems = resolve));
        },
      });
      return null;
    }
    renderWithProviders(
      <>
        <WorkItemsList />
        <WaitingForYou />
      </>,
    );
    const rows = await screen.findAllByTestId("waiting-for-you-row");
    await waitFor(() => expect(workItemsReads).toBe(1));
    fireEvent.click(within(rows[2]).getByText("Question of flow1"));
    const first = await screen.findByTestId("waiting-for-you-panel");
    items = FOUR.filter((r) => r.id !== "flow1");
    fireEvent.click(within(first).getByText("Go flow1"));
    // The inbox is read again: the answered row leaves and its dialog with it.
    await waitFor(() => expect(screen.getAllByTestId("waiting-for-you-row")).toHaveLength(3));
    await waitFor(() => expect(screen.queryByTestId("waiting-for-you-panel")).toBeNull());
    fireEvent.click(within(screen.getAllByTestId("waiting-for-you-row")[0]).getByText("Question of run1"));
    expect(await screen.findByTestId("waiting-for-you-panel")).toBeTruthy();
    // The slower list settles now: the first answer's close comes last.
    await waitFor(() => expect(workItemsReads).toBe(2));
    await act(async () => releaseWorkItems([]));
    expect(screen.getByRole("dialog", { name: "Run: Origin run1" })).toBeTruthy();
  });

  it("keeps the card read-only with the reason while AI is off", async () => {
    ai.off = true;
    get.mockResolvedValue({ enabled: true, reason: null, items: FOUR });
    renderWithProviders(<WaitingForYou />);
    const rows = await screen.findAllByTestId("waiting-for-you-row");
    fireEvent.click(within(rows[2]).getByText("Question of flow1"));
    const panel = await screen.findByTestId("waiting-for-you-panel");
    expect(within(panel).getByText("AI is disabled on this platform.")).toBeTruthy();
    const option = within(panel).getByText("Go flow1").closest("button");
    expect(option?.hasAttribute("disabled")).toBe(true);
    fireEvent.click(within(panel).getByText("Go flow1"));
    expect(post).not.toHaveBeenCalled();
  });

  it("draws nothing while the read switch is off", async () => {
    get.mockResolvedValue({ enabled: false, reason: "DECISIONS_READ_ENABLED is off", items: [] });
    const { container } = renderWithProviders(<WaitingForYou />);
    await waitFor(() => expect(get).toHaveBeenCalled());
    await waitFor(() => expect(container.querySelector('[data-testid="waiting-for-you"]')).toBeNull());
  });

  it("keeps a failed read on its surface with a retry", async () => {
    get.mockRejectedValue(new Error("500"));
    renderWithProviders(<WaitingForYou />);
    expect(await screen.findByText("Could not load what is waiting for you.", {}, { timeout: 4000 })).toBeTruthy();
  });
});
