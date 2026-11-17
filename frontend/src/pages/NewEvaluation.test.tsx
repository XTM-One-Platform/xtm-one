// The evaluation creation page: what is picked in its three pickers joins the page's
// own lists, and the overall metric selection and each agent's output
// extraction it sets are what a later edit of the Configuration tab would save.

import { MemoryRouter } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { FamilyInfo, MetricCatalogEntry } from "@/types";
import NewEvaluation from "./NewEvaluation";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/usePlatformConfig")>()),
  useAiEnabled: () => true,
}));

/** The lists are positioned with floating-ui, which happy-dom does not lay out. */
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

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);

const FAMILIES: FamilyInfo[] = [
  {
    id: "CLASSIFICATION",
    label: "Classification",
    description: "Labels",
    metric_keys: [],
    structured_output: true,
    extractor_output_kinds: ["label_set"],
  },
  {
    id: "STRUCTURED_JSON",
    label: "Structured JSON",
    description: "Objects",
    metric_keys: [],
    structured_output: true,
    extractor_output_kinds: ["array", "object"],
  },
];
const CATALOG: MetricCatalogEntry[] = [
  { key: "classification.micro_prf", label: "Micro", kind: "quality", direction: "higher_is_better", default_contributor: true },
  { key: "classification.macro_prf", label: "Macro", kind: "quality", direction: "higher_is_better", default_contributor: false },
  { key: "performance.latency.wall_clock", label: "Latency", kind: "performance", direction: "lower_is_better", default_contributor: true },
];
const STRUCTURED_CATALOG: MetricCatalogEntry[] = [
  { key: "structured.field_accuracy", label: "Fields", kind: "quality", direction: "higher_is_better", default_contributor: true },
  CATALOG[2],
];
const AGENTS = [
  { id: "agent-triage", name: "Triage", slug: "triage", icon: "bot", is_company_managed: false, enabled: true },
  { id: "agent-hunter", name: "Hunter", slug: "hunter", icon: "bot", is_company_managed: false, enabled: true },
];

beforeEach(() => {
  get.mockReset();
  post.mockReset();
  get.mockImplementation(async (path: string) => {
    if (path === "/evaluations/families") return FAMILIES;
    if (path.startsWith("/evaluations/metric-catalog?family=CLASSIFICATION")) return CATALOG;
    if (path.startsWith("/evaluations/metric-catalog?family=STRUCTURED_JSON")) return STRUCTURED_CATALOG;
    if (path.startsWith("/evaluations/agents/all")) {
      const excluded = new URLSearchParams(path.split("?")[1] ?? "").get("exclude_ids")?.split(",") ?? [];
      return AGENTS.filter((agent) => !excluded.includes(agent.id));
    }
    if (path.startsWith("/settings/available-models")) return [{ model: "gpt-4o", provider: "openai", provider_id: "p-1" }];
    if (path.startsWith("/datasets")) return [{ id: "d-1", name: "Phishing", entry_count: 3, is_read_only: false, needs_family: false }];
    return [];
  });
  post.mockResolvedValue({ id: "new-evaluation" });
});

function renderPage() {
  renderWithProviders(
    <MemoryRouter>
      <NewEvaluation />
    </MemoryRouter>,
  );
}

async function chooseFamily(user: ReturnType<typeof userEvent.setup>, current: string, label: string) {
  // The family field shows what is chosen, or its placeholder.
  const field = (await screen.findAllByRole("combobox")).find((element) => element.textContent === current);
  await user.click(field!);
  await user.click(await screen.findByRole("option", { name: label }));
}

async function pick(user: ReturnType<typeof userEvent.setup>, field: string, option: RegExp) {
  await user.click(screen.getByRole("combobox", { name: field }));
  await user.click(await screen.findByRole("option", { name: option }));
}

async function addAgent(user: ReturnType<typeof userEvent.setup>, name: string) {
  await pick(user, "Search agents to add...", new RegExp(name));
}

describe("NewEvaluation", () => {
  it("lists the agent, the model and the dataset picked in its three pickers", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <MemoryRouter>
        <NewEvaluation />
      </MemoryRouter>,
    );

    await pick(user, "Search agents to add...", /Triage/);
    await pick(user, "Search models to add...", /gpt-4o/);
    await pick(user, "Search datasets to link...", /Phishing/);

    expect(screen.getByRole("button", { name: "Remove agent Triage" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove model" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove dataset" })).toBeInTheDocument();
    expect(screen.getByText("Phishing")).toBeInTheDocument();
  });

  it("creates the evaluation with its metric selection and the extractors of the agents it keeps", async () => {
    // Radix keeps `pointer-events: none` on the body while a closing Select animates out.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderWithProviders(
      <MemoryRouter>
        <NewEvaluation />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Phishing triage" } });

    // The family first: both settings are checked against it.
    await user.click(screen.getByText("Select an output family"));
    await user.click(await screen.findByRole("option", { name: "Classification" }));

    await addAgent(user, "Triage");
    await addAgent(user, "Hunter");

    // An extractor per agent, seeded in the kind the family scores; then Hunter leaves.
    await user.click(await screen.findByRole("switch", { name: "Extract before scoring for Triage" }));
    await user.click(screen.getByRole("switch", { name: "Extract before scoring for Hunter" }));
    await user.click(screen.getByRole("button", { name: "Remove agent Hunter" }));

    // Overall Quality composed by hand, from this family's defaults; Performance inherits.
    await waitFor(() => expect(screen.getAllByRole("switch", { name: "Use defaults" })).toHaveLength(2));
    await user.click(screen.getAllByRole("switch", { name: "Use defaults" })[0]);

    await user.click(screen.getByRole("button", { name: "Create Evaluation" }));

    await waitFor(() => expect(post).toHaveBeenCalledWith("/evaluations", expect.anything()));
    const request = post.mock.calls.find(([path]) => path === "/evaluations")?.[1] as Record<string, unknown>;
    expect(request.agent_ids).toEqual(["agent-triage"]);
    expect(request.agent_extractors).toEqual({
      "agent-triage": { source: { kind: "agent_output" }, operations: [{ op: "parse_json" }], output_kind: "label_set" },
    });
    expect(request.overall_quality_metrics).toEqual(["classification.micro_prf"]);
    expect(request.overall_performance_metrics).toBeNull();
  });

  it("drops the metric selection and the extractors made for the previous family", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderPage();

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Phishing triage" } });
    await chooseFamily(user, "Select an output family", "Classification");
    await addAgent(user, "Triage");
    await user.click(await screen.findByRole("switch", { name: "Extract before scoring for Triage" }));
    await waitFor(() => expect(screen.getAllByRole("switch", { name: "Use defaults" })[0]).toBeEnabled());
    await user.click(screen.getAllByRole("switch", { name: "Use defaults" })[0]);

    // A label set and Classification's metrics: neither is Structured JSON's.
    await chooseFamily(user, "Classification", "Structured JSON");
    expect(await screen.findByRole("switch", { name: "Extract before scoring for Triage" })).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "Create Evaluation" }));

    await waitFor(() => expect(post).toHaveBeenCalledWith("/evaluations", expect.anything()));
    const request = post.mock.calls.find(([path]) => path === "/evaluations")?.[1] as Record<string, unknown>;
    expect(request.family).toBe("STRUCTURED_JSON");
    expect(request.agent_extractors).toBeUndefined();
    expect(request.overall_quality_metrics).toBeNull();
  });

  it("keeps the metrics on their defaults until the family's catalog has loaded", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    get.mockImplementation(async (path: string) => {
      if (path === "/evaluations/families") return FAMILIES;
      if (path.startsWith("/evaluations/metric-catalog")) return new Promise(() => {});
      return [];
    });
    renderPage();

    await chooseFamily(user, "Select an output family", "Classification");

    // Switching off now would seed the score from an empty catalog: composed from nothing.
    const switches = await screen.findAllByRole("switch", { name: "Use defaults" });
    expect(switches).toHaveLength(2);
    for (const toggle of switches) expect(toggle).toBeDisabled();
  });

  it("shows neither setting before a family is chosen", () => {
    renderWithProviders(
      <MemoryRouter>
        <NewEvaluation />
      </MemoryRouter>,
    );

    expect(screen.queryByText("Output extractors")).toBeNull();
    expect(screen.queryByText("Overall Score Composition")).toBeNull();
  });

  it.each([
    ["What each execution mode runs", "LLM Only sends only the agent's persona"],
    ["What each run mode does", "Parallel starts a launch's runs together"],
    ["What the execution timeout covers", "one launch of this evaluation may take, all its runs together"],
    ["What an output family decides", "The shape every entry's expected answer takes."],
    ["How the performance tolerance scores runs", "(of the gap between the best and worst runs when the best is 0 or less) scores 100"],
    ["How the sub-score weights count", "each of the LLM judge's sub-scores counts in an entry's Judge Quality"],
    ["Which datasets can be linked", "Only datasets of the chosen output family, or with none yet, are offered"],
  ])("explains %j to a keyboard user", async (icon, explanation) => {
    const user = userEvent.setup();
    renderPage();
    const target = screen.getByRole("img", { name: icon });

    // Reached with Tab alone, as a keyboard user reaches it.
    for (let presses = 0; presses < 60 && document.activeElement !== target; presses++) await user.tab();
    expect(document.activeElement).toBe(target);
    expect((await screen.findByRole("tooltip")).textContent).toContain(explanation);
  });

  it("keeps saying a linked dataset has no output family, and explains it to a keyboard user", async () => {
    const user = userEvent.setup();
    get.mockImplementation(async (path: string) =>
      path.startsWith("/datasets") ? [{ id: "d-2", name: "Unshaped", entry_count: 1, is_read_only: false, needs_family: true }] : [],
    );
    renderPage();
    await pick(user, "Search datasets to link...", /Unshaped/);

    const badge = screen.getByText("Needs family").closest('[tabindex="0"]')!;
    for (let presses = 0; presses < 80 && document.activeElement !== badge; presses++) await user.tab();
    expect(document.activeElement).toBe(badge);
    expect((await screen.findByRole("tooltip")).textContent).toContain("cannot run, replay or rescore until a family is set");
  });

  it("names its icon-only dataset buttons and says whether the entries are shown", async () => {
    const user = userEvent.setup();
    renderPage();
    await pick(user, "Search datasets to link...", /Phishing/);

    const toggle = screen.getByRole("button", { name: "Show entries" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(screen.getByRole("button", { name: "Hide entries" })).toHaveAttribute("aria-expanded", "true");
  });

  it("creates a dataset made here with the chosen output family, so the evaluation can run on it", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    post.mockImplementation(async (path: string, body: unknown) =>
      path === "/datasets"
        ? { id: "d-new", name: (body as { name: string }).name, entry_count: 0, is_read_only: false, needs_family: false }
        : { id: "new-evaluation" },
    );
    renderPage();

    await chooseFamily(user, "Select an output family", "Structured JSON");
    await user.click(screen.getByRole("button", { name: "New Dataset" }));
    await user.type(screen.getByRole("textbox", { name: "Name *" }), "Invoices");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(post).toHaveBeenCalledWith("/datasets", expect.objectContaining({ name: "Invoices", family: "STRUCTURED_JSON" })));
    expect(await screen.findByText("Invoices")).toBeInTheDocument();
    expect(screen.queryByText("Needs family")).toBeNull();
  });

  it("asks for an output family before it creates a dataset here", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderPage();

    await user.click(screen.getByRole("button", { name: "New Dataset" }));
    await user.type(screen.getByRole("textbox", { name: "Name *" }), "Invoices");
    expect(screen.getByText("Choose an output family in Scoring first: the new dataset is created with it.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();

    await chooseFamily(user, "Select an output family", "Classification");
    expect(screen.queryByText("Choose an output family in Scoring first: the new dataset is created with it.")).toBeNull();
    expect(screen.getByRole("button", { name: "Create" })).toBeEnabled();
    expect(post).not.toHaveBeenCalledWith("/datasets", expect.anything());
  });

  it("calls the dataset's judge text its default judge instructions, and says what replaces them", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "New Dataset" }));

    expect(screen.getByRole("textbox", { name: "Default Judge Instructions" })).toBeInTheDocument();
    const tip = screen.getByRole("img", { name: "When the dataset's judge instructions apply" });
    for (let presses = 0; presses < 80 && document.activeElement !== tip; presses++) await user.tab();
    expect((await screen.findByRole("tooltip")).textContent).toContain("they are never combined");
  });
});
