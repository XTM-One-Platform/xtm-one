// The dataset page explains its concepts in place: the header's badges, the
// entry editor's fields, the settings and the versions, each reached with the keyboard
// as well as the mouse, and each worded as the backend behaves.

import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { DatasetDetail as DatasetDetailData, FamilyInfo } from "@/types";
import DatasetDetail from "./DatasetDetail";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

// The page's tabs register palette commands; the palette is not under test here.
vi.mock("@/lib/commands/CommandsProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/commands/CommandsProvider")>()),
  useCommandSections: () => {},
}));

let aiEnabled = true;
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/usePlatformConfig")>()),
  useAiEnabled: () => aiEnabled,
}));

/** The family picker is positioned with floating-ui, which happy-dom does not lay out. */
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

const FAMILIES: FamilyInfo[] = [
  {
    id: "CLASSIFICATION",
    label: "Classification",
    description: "Labels compared with a reference label set.",
    metric_keys: [],
    structured_output: true,
    extractor_output_kinds: [],
  },
  {
    id: "STRUCTURED_JSON",
    label: "JSON",
    description: "JSON objects compared field by field.",
    metric_keys: [],
    structured_output: true,
    extractor_output_kinds: [],
  },
];

function dataset(overrides: Partial<DatasetDetailData> = {}): DatasetDetailData {
  return {
    id: "ds-1",
    user_id: "u-1",
    name: "Phishing",
    description: null,
    evaluation_instructions: null,
    output_schema: null,
    family: "CLASSIFICATION",
    label_vocabulary: null,
    needs_family: false,
    content_hash: null,
    current_version: 3,
    is_company_managed: false,
    is_read_only: false,
    shared_group_ids: null,
    evaluation_count: 2,
    entry_count: 1,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    entries: [
      {
        id: "e-1",
        dataset_id: "ds-1",
        position: 0,
        description: null,
        input_prompt: "Is this mail phishing?",
        expected_answer: "phishing",
        evaluation_prompt: "Accept any casing.",
        file_ids: null,
        output_schema: null,
        files: null,
        created_at: "2026-10-01T00:00:00Z",
      },
    ],
    ...overrides,
  } as DatasetDetailData;
}

let current: DatasetDetailData;

beforeEach(() => {
  aiEnabled = true;
  current = dataset();
  get.mockReset();
  get.mockImplementation(async (path: string) => {
    if (path === "/auth/me") return { id: "u-1", role: "admin" };
    if (path === "/evaluations/families") return FAMILIES;
    if (path === "/datasets/ds-1") return current;
    if (path === "/datasets/ds-1/versions") return [];
    return [];
  });
});

function renderPage(tab = "") {
  renderWithProviders(
    <MemoryRouter initialEntries={[`/evaluations/datasets/ds-1${tab ? `/${tab}` : ""}`]}>
      <Routes>
        <Route path="/evaluations/datasets/:id/:tab?" element={<DatasetDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

/** Tab alone until *target* has the focus, as a keyboard user reaches it. */
async function tabTo(user: ReturnType<typeof userEvent.setup>, target: Element) {
  for (let presses = 0; presses < 80 && document.activeElement !== target; presses++) await user.tab();
  expect(document.activeElement).toBe(target);
}

/** The focusable trigger an `InfoLabel` puts around a term. */
function termTrigger(term: string | Element): Element {
  const element = typeof term === "string" ? screen.getAllByText(term)[0] : term;
  return element.closest('[tabindex="0"]')!;
}

describe("DatasetDetail explains its concepts", () => {
  it.each([
    ["Read-only", "cannot be deleted, until read-only is turned off"],
    ["v3", "a run pins the version current at its launch"],
    ["2 evaluations", "The number of evaluations linked to this dataset."],
    ["Classification", "Labels compared with a reference label set."],
  ])("explains the header's %j to a keyboard user", async (term, explanation) => {
    current = dataset({ is_read_only: true });
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("heading", { name: "Phishing" });
    if (term === "Classification") await screen.findAllByText("Classification");

    await tabTo(user, termTrigger(term));
    expect((await screen.findByRole("tooltip")).textContent).toContain(explanation);
  });

  it("explains a missing family to a keyboard user", async () => {
    current = dataset({ family: null, needs_family: true });
    const user = userEvent.setup();
    renderPage();

    await tabTo(user, termTrigger(await screen.findByText("Needs family")));
    expect((await screen.findByRole("tooltip")).textContent).toContain(
      "cannot run, replay or rescore until a family is set",
    );
  });

  it("calls an entry's own instructions judge instructions, which replace the evaluation's too", async () => {
    const user = userEvent.setup();
    renderPage();

    await tabTo(user, termTrigger(await screen.findByText("Judge Instructions")));
    expect((await screen.findByRole("tooltip")).textContent).toContain(
      "They replace the evaluation's and the dataset's judge instructions",
    );
  });

  it("marks the expected answer required when an existing entry is edited", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit entry" }));
    // The asterisk is the label's own text, on an existing entry as on a new one.
    expect(screen.getByLabelText("Expected Answer").previousElementSibling?.textContent).toContain("*");

    await tabTo(user, screen.getByRole("img", { name: "What the expected answer is" }));
    expect((await screen.findByRole("tooltip")).textContent).toContain("The answer the agent should give.");
  });

  it("names the dataset-level text Default Judge Instructions and says what replaces it", async () => {
    const user = userEvent.setup();
    renderPage("settings");

    expect(await screen.findByLabelText("Default Judge Instructions")).toBeInTheDocument();
    await tabTo(user, screen.getByRole("img", { name: "What the default judge instructions do" }));
    expect((await screen.findByRole("tooltip")).textContent).toContain(
      "An evaluation's own judge instructions replace them",
    );
  });

  it("says read-only also blocks deletion", async () => {
    renderPage("settings");

    expect(await screen.findByText(/the dataset cannot be deleted, until read-only is turned off/)).toBeInTheDocument();
  });

  it("explains the picked output family next to its picker", async () => {
    const user = userEvent.setup();
    renderPage("settings");

    await screen.findByRole("combobox", { name: "Output Family" });
    await tabTo(user, screen.getByRole("img", { name: "What this output family compares" }));
    expect((await screen.findByRole("tooltip")).textContent).toContain("Labels compared with a reference label set.");
  });

  it("says a JSON dataset without a schema gets no quality metric", async () => {
    current = dataset({ family: "STRUCTURED_JSON" });
    renderPage("settings");

    expect(
      await screen.findByText(/without a JSON Schema, here or on the entry, a JSON entry gets no quality metric/),
    ).toBeInTheDocument();
  });

  it("says when a version is saved", async () => {
    renderPage("versions");

    expect(
      await screen.findByText(
        "A version is saved each time its entries, files, JSON schema or label vocabulary change, and when an evaluation first runs on this dataset.",
      ),
    ).toBeInTheDocument();
  });

  it("explains a Delete disabled by AI being off", async () => {
    aiEnabled = false;
    const user = userEvent.setup();
    renderPage();

    const del = await screen.findByRole("button", { name: "Delete" });
    expect(del).toBeDisabled();
    await tabTo(user, del.parentElement!);
    expect((await screen.findByRole("tooltip")).textContent).toContain("AI is disabled on this platform.");
  });
});
