import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { useState, type ReactNode } from "react";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient } from "@tanstack/react-query";

import { EvaluationAgentPickerCard } from "@/components/evaluation/evaluation-agent-picker-card";
import { EvaluationDatasetLinkPicker } from "@/components/evaluation/evaluation-dataset-link-picker";
import { EvaluationModelPickerCard } from "@/components/evaluation/evaluation-model-picker-card";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
}));

/**
 * The agent, model and dataset pickers of the evaluation screens: each
 * list opens above the page, never inside its card, where the next card
 * painted over it; a click or the keyboard adds what the list offers.
 */

/** The list is positioned with floating-ui, which happy-dom does not lay out. */
beforeAll(() => {
  if (!("ResizeObserver" in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = vi.fn();
  }
});

beforeEach(() => {
  vi.mocked(api.get).mockReset();
});

/** The picker's card, then the card the screens put below it. */
function Stacked({ children }: { children: ReactNode }) {
  return (
    <>
      <section aria-label="Picker card">{children}</section>
      <section aria-label="Next card">Agent Tags & Baseline</section>
    </>
  );
}

/** The open list is in the page, and not inside the picker's card. */
async function expectListAboveTheCards() {
  const list = await screen.findByRole("listbox");
  expect(within(screen.getByRole("region", { name: "Picker card" })).queryByRole("listbox")).toBeNull();
  expect(document.body).toContainElement(list);
}

/**
 * The app's query policy (`main.tsx`): a failed query throws to the page's
 * ErrorBoundary unless its hook opts out, which the error tests need to hold.
 */
const appQueryClient = () =>
  new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity, throwOnError: true } } });

const optionTexts = async () => (await screen.findAllByRole("option")).map((option) => option.textContent);

describe("EvaluationAgentPickerCard", () => {
  // The server's answer, in its order.
  const AGENTS = [
    { id: "a-1", name: "Triage", slug: "triage", icon: "Bot" },
    { id: "a-2", name: "Analyst", slug: "analyst", icon: "Bot" },
  ];

  function renderCard(onAdd = vi.fn()) {
    vi.mocked(api.get).mockResolvedValue(AGENTS);
    renderWithProviders(
      <Stacked>
        <EvaluationAgentPickerCard agents={[]} boundIds={[]} canManage editing onAdd={onAdd} onRemove={vi.fn()} />
      </Stacked>,
    );
    return { onAdd, field: screen.getByRole("combobox", { name: "Search agents to add..." }) };
  }

  /** The card as a screen uses it: what is added joins its agents. */
  function Adding() {
    const [agents, setAgents] = useState<{ id: string; name: string; slug: string | null }[]>([]);
    return (
      <Stacked>
        <EvaluationAgentPickerCard
          agents={agents}
          boundIds={agents.map((agent) => agent.id)}
          canManage
          editing
          onAdd={(agent) => setAgents((prev) => [...prev, agent])}
          onRemove={vi.fn()}
        />
      </Stacked>
    );
  }

  it("opens its list outside the card and adds the agent clicked", async () => {
    const user = userEvent.setup();
    const { onAdd, field } = renderCard();

    await user.click(field);
    await expectListAboveTheCards();
    await user.click(await screen.findByRole("option", { name: /Analyst/ }));

    expect(onAdd).toHaveBeenCalledWith(AGENTS[1]);
  });

  it("lists the server's answer in its order, and the keyboard picks: opened on the first row, the arrows move", async () => {
    const user = userEvent.setup();
    const { onAdd, field } = renderCard();

    field.focus();
    await user.keyboard("{ArrowDown}");
    expect(await optionTexts()).toEqual(["Triagetriage", "Analystanalyst"]);
    await user.keyboard("{ArrowDown}{Enter}");

    expect(onAdd).toHaveBeenCalledWith(AGENTS[1]);
  });

  it("offers no row while the server has not answered what is typed, so Enter adds nothing", async () => {
    const user = userEvent.setup();
    const { onAdd, field } = renderCard();

    await user.click(field);
    await screen.findByRole("option", { name: /Triage/ });
    await user.keyboard("ana");

    expect(screen.queryAllByRole("option")).toEqual([]);
    expect(screen.getByText("Searching…")).toBeInTheDocument();
    await user.keyboard("{Enter}");
    expect(onAdd).not.toHaveBeenCalled();
    // The server's answer comes back once the typing settles.
    expect(await screen.findByRole("option", { name: /Analyst/ })).toBeInTheDocument();
  });

  it("says it is searching until the server first answers, never that every agent is added", async () => {
    vi.mocked(api.get).mockReturnValue(new Promise(() => {}));
    const user = userEvent.setup();
    renderWithProviders(
      <Stacked>
        <EvaluationAgentPickerCard agents={[]} boundIds={[]} canManage editing onAdd={vi.fn()} onRemove={vi.fn()} />
      </Stacked>,
    );

    await user.click(screen.getByRole("combobox", { name: "Search agents to add..." }));

    expect(await screen.findByText("Searching…")).toBeInTheDocument();
    expect(screen.queryByText("All agents are already added.")).toBeNull();
  });

  it("says the list could not be loaded when the search fails, never that every agent is added", async () => {
    vi.mocked(api.get).mockRejectedValue(new Error("Service unavailable"));
    const user = userEvent.setup();
    const onAdd = vi.fn();
    renderWithProviders(
      <Stacked>
        <EvaluationAgentPickerCard agents={[]} boundIds={[]} canManage editing onAdd={onAdd} onRemove={vi.fn()} />
      </Stacked>,
      appQueryClient(),
    );

    await user.click(screen.getByRole("combobox", { name: "Search agents to add..." }));

    expect(await screen.findByText("The agents could not be loaded.")).toBeInTheDocument();
    expect(screen.queryByText("All agents are already added.")).toBeNull();
    expect(screen.queryAllByRole("option")).toEqual([]);
    await user.keyboard("{Enter}");
    expect(onAdd).not.toHaveBeenCalled();
  });

  it("drops the rows it had when a later search of the same list fails", async () => {
    vi.mocked(api.get).mockResolvedValue(AGENTS);
    const user = userEvent.setup();
    const client = appQueryClient();
    renderWithProviders(
      <Stacked>
        <EvaluationAgentPickerCard agents={[]} boundIds={[]} canManage editing onAdd={vi.fn()} onRemove={vi.fn()} />
      </Stacked>,
      client,
    );
    await user.click(screen.getByRole("combobox", { name: "Search agents to add..." }));
    await screen.findByRole("option", { name: /Analyst/ });

    // The cache still holds the previous answer when the refetch fails.
    vi.mocked(api.get).mockRejectedValue(new Error("Service unavailable"));
    await act(() => client.refetchQueries({ queryKey: ["evaluation-agents"] }));

    expect(await screen.findByText("The agents could not be loaded.")).toBeInTheDocument();
    expect(screen.queryAllByRole("option")).toEqual([]);
  });

  it("starts an empty search again after each addition", async () => {
    vi.mocked(api.get).mockResolvedValue(AGENTS);
    const user = userEvent.setup();
    renderWithProviders(<Adding />);
    const field = () => screen.getByRole("combobox", { name: "Search agents to add..." });

    await user.click(field());
    await user.keyboard("ana");
    await user.click(await screen.findByRole("option", { name: /Analyst/ }));

    expect(screen.getByRole("button", { name: "Remove agent Analyst" })).toBeInTheDocument();
    expect(field()).toHaveValue("");
    await user.click(field());
    await screen.findByRole("option", { name: /Triage/ });
    // The rows come from the cache of the first open; the request for the
    // emptied search is sent in a later effect (the query's options follow
    // the key in an effect): wait for it rather than read the last call now.
    await waitFor(() => expect(vi.mocked(api.get).mock.lastCall?.[0]).not.toContain("search="));
  });
});

describe("EvaluationModelPickerCard", () => {
  function renderCard(onAddModel = vi.fn()) {
    renderWithProviders(
      <Stacked>
        <EvaluationModelPickerCard
          editing
          useAgentDefaultModel
          onUseAgentDefaultModelChange={vi.fn()}
          canDisableDefaultModel={false}
          selectedModels={[]}
          availableModelsToAdd={[
            { model: "gpt-4o", provider: "openai" },
            { model: "claude-haiku", provider: "anthropic" },
          ]}
          onAddModel={onAddModel}
          onRemoveModel={vi.fn()}
          evaluationUseAgentDefaultModel
        />
      </Stacked>,
    );
    return { onAddModel, field: screen.getByRole("combobox", { name: "Search models to add..." }) };
  }

  it("opens its list outside the card and adds the bare id of the model clicked", async () => {
    const user = userEvent.setup();
    const { onAddModel, field } = renderCard();

    await user.click(field);
    await expectListAboveTheCards();
    await user.click(screen.getByRole("option", { name: "gpt-4o" }));

    expect(onAddModel).toHaveBeenCalledWith("gpt-4o");
  });

  it("finds a model by its provider and adds it with the keyboard", async () => {
    const user = userEvent.setup();
    const { onAddModel, field } = renderCard();

    await user.click(field);
    await user.keyboard("anthropic");
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual(["claude-haiku"]);
    await user.keyboard("{ArrowDown}{Enter}");

    expect(onAddModel).toHaveBeenCalledWith("claude-haiku");
  });
});

describe("EvaluationDatasetLinkPicker", () => {
  // The server's answer, in its order.
  const DATASETS = [
    { id: "d-1", name: "Phishing", entry_count: 3, is_read_only: false, needs_family: false },
    { id: "d-2", name: "Malware", entry_count: 1, is_read_only: true, needs_family: false },
    { id: "d-3", name: "Spam", entry_count: 2, is_read_only: false, needs_family: true },
  ];

  function renderPicker(onLink = vi.fn()) {
    vi.mocked(api.get).mockResolvedValue(DATASETS);
    renderWithProviders(
      <Stacked>
        <EvaluationDatasetLinkPicker family="CLASSIFICATION" linkedIds={[]} onLink={onLink} />
      </Stacked>,
    );
    return { onLink, field: screen.getByRole("combobox", { name: "Search datasets to link..." }) };
  }

  it("opens its list outside the card, says which dataset has no family, and links the one clicked", async () => {
    const user = userEvent.setup();
    const { onLink, field } = renderPicker();

    await user.click(field);
    await expectListAboveTheCards();
    expect(await screen.findByRole("option", { name: /Malware/ })).toHaveTextContent("Read-only · 1 entry");
    expect(screen.getByRole("option", { name: /Spam/ })).toHaveTextContent("Needs family · 2 entries");
    expect(screen.getByRole("option", { name: /Phishing/ })).not.toHaveTextContent("Needs family");
    await user.click(screen.getByRole("option", { name: /Phishing/ }));

    expect(onLink).toHaveBeenCalledWith(DATASETS[0]);
    expect(api.get).toHaveBeenCalledWith("/datasets?family=CLASSIFICATION");
  });

  it("lists the server's answer in its order, and the keyboard picks: opened on the first row, the arrows move", async () => {
    const user = userEvent.setup();
    const { onLink, field } = renderPicker();

    field.focus();
    await user.keyboard("{ArrowDown}");
    expect(await optionTexts()).toEqual(["Phishing3 entries", "MalwareRead-only · 1 entry", "SpamNeeds family · 2 entries"]);
    await user.keyboard("{ArrowDown}{Enter}");

    expect(onLink).toHaveBeenCalledWith(DATASETS[1]);
  });

  it("offers no row while the server has not answered what is typed, so Enter links nothing", async () => {
    const user = userEvent.setup();
    const { onLink, field } = renderPicker();

    await user.click(field);
    await screen.findByRole("option", { name: /Phishing/ });
    await user.keyboard("mal");

    expect(screen.queryAllByRole("option")).toEqual([]);
    expect(screen.getByText("Searching…")).toBeInTheDocument();
    await user.keyboard("{Enter}");
    expect(onLink).not.toHaveBeenCalled();
  });

  it("says it is searching until the server first answers, never that there is no dataset", async () => {
    vi.mocked(api.get).mockReturnValue(new Promise(() => {}));
    const user = userEvent.setup();
    renderWithProviders(
      <Stacked>
        <EvaluationDatasetLinkPicker family="CLASSIFICATION" linkedIds={[]} onLink={vi.fn()} />
      </Stacked>,
    );

    await user.click(screen.getByRole("combobox", { name: "Search datasets to link..." }));

    expect(await screen.findByText("Searching…")).toBeInTheDocument();
    expect(screen.queryByText("No datasets available. Create one above or link later.")).toBeNull();
  });

  it("says the list could not be loaded when the search fails, never that there is no dataset", async () => {
    vi.mocked(api.get).mockRejectedValue(new Error("Service unavailable"));
    const user = userEvent.setup();
    const onLink = vi.fn();
    renderWithProviders(
      <Stacked>
        <EvaluationDatasetLinkPicker family="CLASSIFICATION" linkedIds={[]} onLink={onLink} />
      </Stacked>,
      appQueryClient(),
    );

    await user.click(screen.getByRole("combobox", { name: "Search datasets to link..." }));

    expect(await screen.findByText("The datasets could not be loaded.")).toBeInTheDocument();
    expect(screen.queryByText("No datasets available. Create one above or link later.")).toBeNull();
    expect(screen.queryAllByRole("option")).toEqual([]);
    await user.keyboard("{Enter}");
    expect(onLink).not.toHaveBeenCalled();
  });

  it("drops the rows it had when a later search of the same list fails", async () => {
    vi.mocked(api.get).mockResolvedValue(DATASETS);
    const user = userEvent.setup();
    const client = appQueryClient();
    renderWithProviders(
      <Stacked>
        <EvaluationDatasetLinkPicker family="CLASSIFICATION" linkedIds={[]} onLink={vi.fn()} />
      </Stacked>,
      client,
    );
    await user.click(screen.getByRole("combobox", { name: "Search datasets to link..." }));
    await screen.findByRole("option", { name: /Phishing/ });

    // The cache still holds the previous answer when the refetch fails.
    vi.mocked(api.get).mockRejectedValue(new Error("Service unavailable"));
    await act(() => client.refetchQueries({ queryKey: ["datasets"] }));

    expect(await screen.findByText("The datasets could not be loaded.")).toBeInTheDocument();
    expect(screen.queryAllByRole("option")).toEqual([]);
  });

  it("starts an empty search again after each link", async () => {
    vi.mocked(api.get).mockResolvedValue(DATASETS);
    const user = userEvent.setup();
    function Linking() {
      const [linkedIds, setLinkedIds] = useState<string[]>([]);
      return (
        <Stacked>
          <EvaluationDatasetLinkPicker linkedIds={linkedIds} onLink={(dataset) => setLinkedIds((prev) => [...prev, dataset.id])} />
        </Stacked>
      );
    }
    renderWithProviders(<Linking />);
    const field = () => screen.getByRole("combobox", { name: "Search datasets to link..." });

    await user.click(field());
    await user.keyboard("mal");
    await user.click(await screen.findByRole("option", { name: /Malware/ }));

    expect(field()).toHaveValue("");
    await user.click(field());
    await screen.findByRole("option", { name: /Phishing/ });
    // Same as the agent picker above: the rows are the cache's, the request
    // for the emptied search follows in an effect.
    await waitFor(() => expect(vi.mocked(api.get).mock.lastCall?.[0]).toBe("/datasets"));
  });
});
